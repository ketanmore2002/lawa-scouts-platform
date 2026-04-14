import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Report, Scout, User, WorkspaceMember, ReportHighlight
from app.schemas import HighlightCreate
from app.services.auth import get_current_user
from app.services.notification_service import (
    process_mentions,
    log_activity,
)
from app.services import ws_hub

router = APIRouter(prefix="/api", tags=["highlights"])


async def _check_report_access(report: Report, user: User, db: AsyncSession):
    """Raise 404 if user has no access to the report's scout."""
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    scout = await db.get(Scout, report.scout_id)
    if not scout:
        raise HTTPException(status_code=404, detail="Report not found")
    if scout.user_id == user.id:
        return
    if scout.workspace_id:
        result = await db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == scout.workspace_id,
                WorkspaceMember.user_id == user.id,
            )
        )
        if result.scalar_one_or_none():
            return
    raise HTTPException(status_code=404, detail="Report not found")


@router.get("/reports/{report_id}/highlights")
async def list_highlights(
    report_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    report = await db.get(Report, report_id)
    await _check_report_access(report, user, db)

    result = await db.execute(
        select(ReportHighlight)
        .where(ReportHighlight.report_id == report_id)
        .order_by(ReportHighlight.created_at.asc())
    )
    highlights = result.scalars().all()

    # Collect unique user IDs and batch-fetch
    user_ids = list({hl.user_id for hl in highlights})
    users_result = await db.execute(select(User).where(User.id.in_(user_ids)))
    users_map = {u.id: u for u in users_result.scalars().all()}

    return [
        {
            "id": hl.id,
            "report_id": hl.report_id,
            "user_id": hl.user_id,
            "user_name": users_map.get(hl.user_id, None) and users_map[hl.user_id].name,
            "user_email": users_map.get(hl.user_id, None) and users_map[hl.user_id].email or "",
            "selected_text": hl.selected_text,
            "caption": hl.caption,
            "color": hl.color,
            "created_at": hl.created_at,
        }
        for hl in highlights
    ]


@router.post("/reports/{report_id}/highlights")
async def create_highlight(
    report_id: uuid.UUID,
    data: HighlightCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    report = await db.get(Report, report_id)
    await _check_report_access(report, user, db)

    highlight = ReportHighlight(
        report_id=report_id,
        user_id=user.id,
        selected_text=data.selected_text,
        caption=data.caption,
        color=data.color,
    )
    db.add(highlight)

    # Activity logging for workspace reports (no notifications for highlights)
    scout = await db.get(Scout, report.scout_id)
    if scout and scout.workspace_id:
        report_link = f"/scouts/{scout.id}"
        await log_activity(
            db, scout.workspace_id, user.id, "highlight_added",
            f"{user.name or user.email} highlighted text in a report",
            entity_type="report", entity_id=report_id,
        )
        # Process @mentions in caption
        if data.caption:
            await process_mentions(db, data.caption, user, link=report_link, context=data.caption)

    await db.commit()
    await db.refresh(highlight)

    hl_data = {
        "id": str(highlight.id),
        "report_id": str(highlight.report_id),
        "user_id": str(highlight.user_id),
        "user_name": user.name,
        "user_email": user.email,
        "selected_text": highlight.selected_text,
        "caption": highlight.caption,
        "color": highlight.color,
        "created_at": highlight.created_at.isoformat() if highlight.created_at else None,
    }

    # Broadcast new highlight to all viewers of this report
    await ws_hub.broadcast_to_report(report_id, {
        "type": "new_highlight",
        "data": hl_data,
    }, exclude_user=user.id)

    return hl_data


@router.delete("/highlights/{highlight_id}")
async def delete_highlight(
    highlight_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    highlight = await db.get(ReportHighlight, highlight_id)
    if not highlight:
        raise HTTPException(status_code=404, detail="Highlight not found")
    if highlight.user_id != user.id:
        raise HTTPException(status_code=403, detail="Can only delete your own highlights")
    report_id = highlight.report_id
    await db.delete(highlight)
    await db.commit()

    # Broadcast highlight deletion to all viewers of this report
    await ws_hub.broadcast_to_report(report_id, {
        "type": "highlight_deleted",
        "data": {"id": str(highlight_id), "report_id": str(report_id)},
    }, exclude_user=user.id)

    return {"detail": "Highlight deleted"}
