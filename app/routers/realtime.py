"""
WebSocket endpoint — single persistent connection per browser tab.
Handles: notifications, comments, reactions, presence.
"""

import uuid
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.database import async_session
from app.models import User
from app.services.auth import decode_access_token
from app.services import ws_hub

logger = logging.getLogger(__name__)
router = APIRouter()


async def _authenticate_ws(websocket: WebSocket) -> uuid.UUID | None:
    """Extract user_id from the access_token cookie."""
    token = websocket.cookies.get("access_token")
    if not token:
        return None
    try:
        payload = decode_access_token(token)
        user_id = payload.get("sub")
        if not user_id:
            return None
        uid = uuid.UUID(user_id)
        # Verify user exists
        async with async_session() as db:
            user = await db.get(User, uid)
            if not user:
                return None
        return uid
    except Exception:
        return None


# Small in-process cache — user name/email rarely change within a session.
_user_cache: dict[uuid.UUID, dict] = {}


async def _get_viewer_details(viewer_ids: set[uuid.UUID]) -> list[dict]:
    """Fetch name/email for a set of user IDs (cached)."""
    if not viewer_ids:
        return []
    missing = [uid for uid in viewer_ids if uid not in _user_cache]
    if missing:
        async with async_session() as db:
            result = await db.execute(select(User).where(User.id.in_(missing)))
            for u in result.scalars().all():
                _user_cache[u.id] = {
                    "user_id": str(u.id),
                    "name": u.name or "",
                    "email": u.email or "",
                }
    return [_user_cache[uid] for uid in viewer_ids if uid in _user_cache]


async def _broadcast_presence(report_id: uuid.UUID, report_id_str: str):
    """Build and broadcast presence data for a report."""
    viewers = ws_hub.get_report_viewers(report_id)
    viewer_details = await _get_viewer_details(viewers)
    await ws_hub.broadcast_to_report(report_id, {
        "type": "presence",
        "data": {
            "report_id": report_id_str,
            "viewers": viewer_details,
        }
    })


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    user_id = await _authenticate_ws(websocket)
    if not user_id:
        await websocket.close(code=4001, reason="Unauthorized")
        return

    await websocket.accept()
    await ws_hub.connect(user_id, websocket)

    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")

            if action == "ping":
                await websocket.send_json({"type": "pong"})

            elif action == "subscribe_report":
                report_id = data.get("report_id")
                if report_id:
                    rid = uuid.UUID(report_id)
                    ws_hub.subscribe_report(user_id, rid)
                    await _broadcast_presence(rid, report_id)

            elif action == "unsubscribe_report":
                report_id = data.get("report_id")
                if report_id:
                    rid = uuid.UUID(report_id)
                    ws_hub.unsubscribe_report(user_id, rid)
                    await _broadcast_presence(rid, report_id)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"WS error for user {user_id}: {e}")
    finally:
        affected_reports = await ws_hub.disconnect(user_id, websocket)
        # Broadcast updated presence for any reports the user was viewing
        for rid in affected_reports:
            try:
                await _broadcast_presence(rid, str(rid))
            except Exception:
                pass
