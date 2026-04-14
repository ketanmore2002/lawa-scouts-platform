import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Report, ReportComment, Scout, User, WorkspaceMember
from app.schemas import CommentCreate, CommentUpdate
from app.services.auth import get_current_user
from app.services.notification_service import create_notification, process_mentions, log_activity
from app.services import ws_hub

router = APIRouter(prefix="/api", tags=["comments"])


async def _check_report_access(report: Report, user: User, db: AsyncSession):
    """Raise 404 if user has no access to the report's scout."""
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    scout = await db.get(Scout, report.scout_id)
    if not scout:
        raise HTTPException(status_code=404, detail="Report not found")
    if scout.user_id == user.id:
        return scout
    if scout.workspace_id:
        result = await db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == scout.workspace_id,
                WorkspaceMember.user_id == user.id,
            )
        )
        if result.scalar_one_or_none():
            return scout
    raise HTTPException(status_code=404, detail="Report not found")


@router.get("/reports/{report_id}/comments")
async def list_comments(
    report_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    report = await db.get(Report, report_id)
    await _check_report_access(report, user, db)

    result = await db.execute(
        select(ReportComment)
        .where(ReportComment.report_id == report_id)
        .order_by(ReportComment.created_at.asc())
    )
    comments = result.scalars().all()

    # Batch-fetch users
    user_ids = list({c.user_id for c in comments})
    if user_ids:
        users_result = await db.execute(select(User).where(User.id.in_(user_ids)))
        users_map = {u.id: u for u in users_result.scalars().all()}
    else:
        users_map = {}

    return [
        {
            "id": c.id,
            "report_id": c.report_id,
            "user_id": c.user_id,
            "user_name": users_map.get(c.user_id) and users_map[c.user_id].name,
            "user_email": users_map.get(c.user_id) and users_map[c.user_id].email or "",
            "parent_id": c.parent_id,
            "content": c.content,
            "created_at": c.created_at,
            "updated_at": c.updated_at,
        }
        for c in comments
    ]


@router.post("/reports/{report_id}/comments")
async def create_comment(
    report_id: uuid.UUID,
    data: CommentCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    report = await db.get(Report, report_id)
    scout = await _check_report_access(report, user, db)

    # Validate parent_id if provided
    if data.parent_id:
        parent = await db.get(ReportComment, data.parent_id)
        if not parent or parent.report_id != report_id:
            raise HTTPException(status_code=400, detail="Invalid parent comment")

    comment = ReportComment(
        report_id=report_id,
        user_id=user.id,
        parent_id=data.parent_id,
        content=data.content,
    )
    db.add(comment)

    # Notify parent comment author on reply
    if data.parent_id:
        parent = await db.get(ReportComment, data.parent_id)
        if parent and parent.user_id != user.id:
            await create_notification(
                db, parent.user_id, "comment_reply",
                f"{user.name or user.email} replied to your comment",
                body=data.content[:200],
                link=f"/scouts/{scout.id}",
            )

    # Process @mentions
    await process_mentions(
        db, data.content, user,
        link=f"/scouts/{scout.id}",
        context=f"commented on report '{report.title}'",
    )

    # Log activity if workspace scout
    if scout.workspace_id:
        await log_activity(
            db, scout.workspace_id, user.id, "comment_added",
            f"commented on report '{report.title}'",
            entity_type="comment", entity_id=comment.id,
        )

    await db.commit()
    await db.refresh(comment)

    comment_data = {
        "id": str(comment.id),
        "report_id": str(comment.report_id),
        "user_id": str(comment.user_id),
        "user_name": user.name,
        "user_email": user.email,
        "parent_id": str(comment.parent_id) if comment.parent_id else None,
        "content": comment.content,
        "created_at": comment.created_at.isoformat() if comment.created_at else None,
        "updated_at": comment.updated_at.isoformat() if comment.updated_at else None,
    }

    # Broadcast new comment to all viewers of this report via WebSocket
    await ws_hub.broadcast_to_report(report_id, {
        "type": "new_comment",
        "data": comment_data,
    }, exclude_user=user.id)

    return comment_data


@router.put("/comments/{comment_id}")
async def update_comment(
    comment_id: uuid.UUID,
    data: CommentUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    comment = await db.get(ReportComment, comment_id)
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    if comment.user_id != user.id:
        raise HTTPException(status_code=403, detail="Can only edit your own comments")

    comment.content = data.content
    comment.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(comment)

    return {
        "id": comment.id,
        "report_id": comment.report_id,
        "user_id": comment.user_id,
        "user_name": user.name,
        "user_email": user.email,
        "parent_id": comment.parent_id,
        "content": comment.content,
        "created_at": comment.created_at,
        "updated_at": comment.updated_at,
    }


@router.delete("/comments/{comment_id}")
async def delete_comment(
    comment_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    comment = await db.get(ReportComment, comment_id)
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    if comment.user_id != user.id:
        raise HTTPException(status_code=403, detail="Can only delete your own comments")
    report_id = comment.report_id
    comment_id_str = str(comment.id)
    await db.delete(comment)
    await db.commit()

    # Broadcast deletion so other viewers drop it from their UI in realtime.
    await ws_hub.broadcast_to_report(report_id, {
        "type": "comment_deleted",
        "data": {"id": comment_id_str, "report_id": str(report_id)},
    }, exclude_user=user.id)
    return {"detail": "Comment deleted"}
