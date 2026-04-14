"""
WebSocket connection hub — manages active connections and broadcasts events.

Single-process mode (no REDIS_URL): events are dispatched in-memory only.
Multi-process mode (REDIS_URL set): events are also published to Redis so
peer workers/instances deliver them to their locally-connected sockets.

Each worker subscribes to the channel exactly once via `start_redis_listener`,
which is invoked from FastAPI's lifespan.
"""

import asyncio
import json
import logging
import uuid
from collections import defaultdict
from typing import Optional

from fastapi import WebSocket

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# user_id → set of WebSocket connections (LOCAL to this process)
_connections: dict[uuid.UUID, set[WebSocket]] = defaultdict(set)

# report_id → set of user_ids subscribed via THIS process
_report_subscribers: dict[uuid.UUID, set[uuid.UUID]] = defaultdict(set)

# ── Redis pub/sub plumbing ──
_CHANNEL = "lawa:ws"
_redis = None  # type: ignore  # lazy: aioredis client
_listener_task: Optional[asyncio.Task] = None
_PROCESS_ID = str(uuid.uuid4())  # tags messages so we don't re-deliver our own


def _uuid_default(o):
    if isinstance(o, uuid.UUID):
        return str(o)
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


async def _get_redis():
    """Lazily create the Redis client (None if REDIS_URL is unset)."""
    global _redis
    if _redis is not None or not settings.redis_url:
        return _redis
    try:
        import redis.asyncio as redis_asyncio
        _redis = redis_asyncio.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
        )
        await _redis.ping()
        logger.info("Redis pub/sub connected")
    except Exception as e:
        logger.warning(f"Redis unavailable, falling back to in-process WS hub: {e}")
        _redis = None
    return _redis


async def _publish(kind: str, payload: dict):
    """Publish an event so other workers/instances can deliver locally."""
    r = await _get_redis()
    if r is None:
        return
    envelope = {"src": _PROCESS_ID, "kind": kind, "payload": payload}
    try:
        await r.publish(_CHANNEL, json.dumps(envelope, default=_uuid_default))
    except Exception as e:
        logger.warning(f"Redis publish failed: {e}")


async def _handle_remote(envelope: dict):
    """Apply an event that originated on a different process."""
    if envelope.get("src") == _PROCESS_ID:
        return  # echo
    kind = envelope.get("kind")
    p = envelope.get("payload") or {}
    if kind == "send_to_user":
        await _local_send_to_user(uuid.UUID(p["user_id"]), p["message"])
    elif kind == "broadcast_to_report":
        exclude = uuid.UUID(p["exclude_user"]) if p.get("exclude_user") else None
        await _local_broadcast_to_report(uuid.UUID(p["report_id"]), p["message"], exclude)
    elif kind == "broadcast_to_users":
        exclude = uuid.UUID(p["exclude_user"]) if p.get("exclude_user") else None
        uids = [uuid.UUID(u) for u in p["user_ids"]]
        await _local_broadcast_to_users(uids, p["message"], exclude)


async def start_redis_listener():
    """Start the background pub/sub listener (idempotent)."""
    global _listener_task
    if _listener_task and not _listener_task.done():
        return
    r = await _get_redis()
    if r is None:
        return

    async def _run():
        try:
            pubsub = r.pubsub()
            await pubsub.subscribe(_CHANNEL)
            async for msg in pubsub.listen():
                if msg.get("type") != "message":
                    continue
                try:
                    envelope = json.loads(msg["data"])
                    await _handle_remote(envelope)
                except Exception as e:
                    logger.warning(f"Bad WS pubsub message: {e}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Redis listener crashed: {e}")

    _listener_task = asyncio.create_task(_run())
    logger.info("WS Redis listener started")


async def stop_redis_listener():
    global _listener_task
    if _listener_task:
        _listener_task.cancel()
        try:
            await _listener_task
        except Exception:
            pass
        _listener_task = None
    if _redis is not None:
        try:
            await _redis.close()
        except Exception:
            pass


# ── Connection registry ──

async def connect(user_id: uuid.UUID, ws: WebSocket):
    _connections[user_id].add(ws)
    logger.info(f"WS connected: user={user_id}, total={sum(len(v) for v in _connections.values())}")


async def disconnect(user_id: uuid.UUID, ws: WebSocket):
    _connections[user_id].discard(ws)
    affected_reports: list[uuid.UUID] = []
    if not _connections[user_id]:
        del _connections[user_id]
        for report_id in list(_report_subscribers.keys()):
            if user_id in _report_subscribers[report_id]:
                _report_subscribers[report_id].discard(user_id)
                affected_reports.append(report_id)
            if not _report_subscribers[report_id]:
                del _report_subscribers[report_id]
    logger.info(f"WS disconnected: user={user_id}")
    return affected_reports


# ── Local-only delivery helpers (called by both the public API and the remote handler) ──

async def _local_send_to_user(user_id: uuid.UUID, message: dict):
    conns = _connections.get(user_id, set())
    if not conns:
        return
    dead = []
    for ws in conns:
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        conns.discard(ws)


async def _local_broadcast_to_report(report_id: uuid.UUID, message: dict, exclude_user: uuid.UUID | None):
    user_ids = _report_subscribers.get(report_id, set())
    tasks = [
        _local_send_to_user(uid, message)
        for uid in user_ids
        if uid != exclude_user and uid in _connections
    ]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _local_broadcast_to_users(user_ids: list[uuid.UUID], message: dict, exclude_user: uuid.UUID | None):
    tasks = [
        _local_send_to_user(uid, message)
        for uid in user_ids
        if uid != exclude_user and uid in _connections
    ]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# ── Public API ──

async def send_to_user(user_id: uuid.UUID, message: dict):
    """Deliver to a user wherever they are connected (this process or peers)."""
    await _local_send_to_user(user_id, message)
    await _publish("send_to_user", {"user_id": str(user_id), "message": message})


async def broadcast_to_report(report_id: uuid.UUID, message: dict, exclude_user: uuid.UUID | None = None):
    await _local_broadcast_to_report(report_id, message, exclude_user)
    await _publish("broadcast_to_report", {
        "report_id": str(report_id),
        "message": message,
        "exclude_user": str(exclude_user) if exclude_user else None,
    })


async def broadcast_to_workspace_members(member_user_ids: list[uuid.UUID], message: dict, exclude_user: uuid.UUID | None = None):
    await _local_broadcast_to_users(member_user_ids, message, exclude_user)
    await _publish("broadcast_to_users", {
        "user_ids": [str(u) for u in member_user_ids],
        "message": message,
        "exclude_user": str(exclude_user) if exclude_user else None,
    })


def subscribe_report(user_id: uuid.UUID, report_id: uuid.UUID):
    _report_subscribers[report_id].add(user_id)


def unsubscribe_report(user_id: uuid.UUID, report_id: uuid.UUID):
    _report_subscribers[report_id].discard(user_id)
    if not _report_subscribers[report_id]:
        del _report_subscribers[report_id]


def get_report_viewers(report_id: uuid.UUID) -> set[uuid.UUID]:
    """LOCAL viewers only. For globally-accurate presence, store presence in Redis."""
    return _report_subscribers.get(report_id, set())


def is_user_online(user_id: uuid.UUID) -> bool:
    return user_id in _connections and len(_connections[user_id]) > 0
