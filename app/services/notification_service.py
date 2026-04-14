"""
Notification & activity service — centralised helpers for creating notifications,
detecting @mentions, and logging workspace activity events.
Pushes real-time events via WebSocket when possible.
"""

import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notification, ActivityEvent, WorkspaceMember, User
from app.services import ws_hub

MENTION_PATTERN = re.compile(r"@([\w.+-]+@[\w.-]+\.\w+)")


async def create_notification(
    db: AsyncSession,
    user_id: uuid.UUID,
    type: str,
    title: str,
    body: str | None = None,
    link: str | None = None,
    metadata: dict | None = None,
) -> Notification:
    """Add a notification to the session. Caller must commit.
    Also pushes a real-time WebSocket event to the user."""
    notif = Notification(
        user_id=user_id,
        type=type,
        title=title,
        body=body,
        link=link,
        metadata_json=metadata,
    )
    db.add(notif)
    # Push via WebSocket (fire and forget — won't fail if user offline)
    try:
        await ws_hub.send_to_user(user_id, {
            "type": "notification",
            "data": {
                "type": type,
                "title": title,
                "body": body,
                "link": link,
            }
        })
    except Exception:
        pass
    return notif


async def notify_workspace_members(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    exclude_user_id: uuid.UUID,
    type: str,
    title: str,
    body: str | None = None,
    link: str | None = None,
    metadata: dict | None = None,
):
    """Send a notification to every workspace member except the actor."""
    result = await db.execute(
        select(WorkspaceMember.user_id).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id != exclude_user_id,
        )
    )
    for (uid,) in result.all():
        await create_notification(db, uid, type, title, body, link, metadata)


async def process_mentions(
    db: AsyncSession,
    content: str,
    author: User,
    link: str | None = None,
    context: str = "",
):
    """Extract @email mentions from text and create notification for each."""
    emails = MENTION_PATTERN.findall(content)
    if not emails:
        return
    result = await db.execute(select(User).where(User.email.in_(emails)))
    mentioned_users = result.scalars().all()
    for u in mentioned_users:
        if u.id == author.id:
            continue
        await create_notification(
            db,
            u.id,
            "mention",
            f"{author.name or author.email} mentioned you",
            body=context,
            link=link,
        )


async def log_activity(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    event_type: str,
    description: str,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    metadata: dict | None = None,
):
    """Create an activity event for a workspace timeline. Caller must commit."""
    event = ActivityEvent(
        workspace_id=workspace_id,
        user_id=user_id,
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        description=description,
        metadata_json=metadata,
    )
    db.add(event)
    return event
