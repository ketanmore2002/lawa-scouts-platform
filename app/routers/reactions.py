import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Report, ReportReaction, Scout, User, WorkspaceMember
from app.schemas import ReactionToggle, ALLOWED_EMOJIS
from app.services.auth import get_current_user
from app.services import ws_hub

router = APIRouter(prefix="/api", tags=["reactions"])


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


@router.post("/reports/{report_id}/reactions")
async def toggle_reaction(
    report_id: uuid.UUID,
    data: ReactionToggle,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if data.emoji not in ALLOWED_EMOJIS:
        raise HTTPException(status_code=400, detail=f"Invalid emoji. Allowed: {', '.join(sorted(ALLOWED_EMOJIS))}")

    report = await db.get(Report, report_id)
    await _check_report_access(report, user, db)

    # Check existing
    result = await db.execute(
        select(ReportReaction).where(
            ReportReaction.report_id == report_id,
            ReportReaction.user_id == user.id,
            ReportReaction.emoji == data.emoji,
        )
    )
    existing = result.scalar_one_or_none()

    if existing:
        await db.delete(existing)
        await db.commit()
        result_data = {"action": "removed", "emoji": data.emoji}
    else:
        reaction = ReportReaction(
            report_id=report_id,
            user_id=user.id,
            emoji=data.emoji,
        )
        db.add(reaction)
        await db.commit()
        result_data = {"action": "added", "emoji": data.emoji}

    # Broadcast reaction change to all viewers of this report
    await ws_hub.broadcast_to_report(report_id, {
        "type": "reaction_update",
        "data": {
            "report_id": str(report_id),
            "emoji": data.emoji,
            "action": result_data["action"],
            "user_id": str(user.id),
        },
    }, exclude_user=user.id)

    return result_data


@router.get("/reports/{report_id}/reactions")
async def list_reactions(
    report_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    report = await db.get(Report, report_id)
    await _check_report_access(report, user, db)

    # Get all reactions grouped by emoji
    result = await db.execute(
        select(ReportReaction.emoji, func.count(ReportReaction.id).label("count"))
        .where(ReportReaction.report_id == report_id)
        .group_by(ReportReaction.emoji)
    )
    grouped = {row.emoji: row.count for row in result.all()}

    # Check which emojis the current user has reacted with
    user_result = await db.execute(
        select(ReportReaction.emoji).where(
            ReportReaction.report_id == report_id,
            ReportReaction.user_id == user.id,
        )
    )
    user_emojis = {row[0] for row in user_result.all()}

    return [
        {"emoji": emoji, "count": count, "user_reacted": emoji in user_emojis}
        for emoji, count in grouped.items()
    ]
