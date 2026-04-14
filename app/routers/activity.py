import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import ActivityEvent, User, WorkspaceMember
from app.services.auth import get_current_user

router = APIRouter(prefix="/api/workspaces", tags=["activity"])


@router.get("/{workspace_id}/activity")
async def get_activity(
    workspace_id: uuid.UUID,
    page: int = Query(1, ge=1),
    per_page: int = Query(30, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Verify membership
    result = await db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user.id,
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Workspace not found")

    # Total count
    count_result = await db.execute(
        select(func.count(ActivityEvent.id)).where(
            ActivityEvent.workspace_id == workspace_id
        )
    )
    total = count_result.scalar()

    # Paginated events with user info
    result = await db.execute(
        select(ActivityEvent, User)
        .join(User, User.id == ActivityEvent.user_id)
        .where(ActivityEvent.workspace_id == workspace_id)
        .order_by(ActivityEvent.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )

    events = [
        {
            "id": event.id,
            "workspace_id": event.workspace_id,
            "user_id": event.user_id,
            "user_name": u.name,
            "user_email": u.email,
            "event_type": event.event_type,
            "entity_type": event.entity_type,
            "description": event.description,
            "created_at": event.created_at,
        }
        for event, u in result.all()
    ]

    return {
        "events": events,
        "total": total,
        "page": page,
        "has_more": (page * per_page) < total,
    }
