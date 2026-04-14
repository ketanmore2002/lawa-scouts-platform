import asyncio
import json
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import func as sa_func

from app.database import get_db
from app.models import Scout, User, WorkspaceMember, Report
from app.schemas import ScoutCreate, ScoutUpdate, ScoutResponse
from app.services.auth import get_current_user
from app.services.scout_runner import run_scout

router = APIRouter(prefix="/api/scouts", tags=["scouts"])


async def _get_scout_or_404(scout, user: User, db: AsyncSession, require_edit: bool = False):
    """Ensure scout exists and user has access (personal or workspace)."""
    if not scout:
        raise HTTPException(status_code=404, detail="Scout not found")
    # Personal scout — owner always has full access
    if scout.user_id == user.id:
        return scout
    # Workspace scout — check membership
    if scout.workspace_id:
        result = await db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == scout.workspace_id,
                WorkspaceMember.user_id == user.id,
            )
        )
        member = result.scalar_one_or_none()
        if member:
            if require_edit and member.role in ("viewer", "commenter"):
                raise HTTPException(status_code=403, detail="Editor access required")
            return scout
    raise HTTPException(status_code=404, detail="Scout not found")


@router.post("", response_model=ScoutResponse)
async def create_scout(
    data: ScoutCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    name = data.name
    if not name:
        words = data.topic.split()[:6]
        name = " ".join(words)
        if len(data.topic.split()) > 6:
            name += "..."

    is_once = data.schedule_minutes == 0

    scout = Scout(
        user_id=user.id,
        workspace_id=data.workspace_id,
        name=name,
        topic=data.topic,
        description=data.description,
        keywords=data.keywords,
        include_sources=data.include_sources,
        exclude_sources=data.exclude_sources,
        schedule_minutes=0 if is_once else data.schedule_minutes,
        status="once" if is_once else "active",
        email_report=data.email_report,
        next_run_at=None if is_once else datetime.utcnow() + timedelta(minutes=data.schedule_minutes),
    )
    db.add(scout)
    await db.commit()
    await db.refresh(scout)
    return scout


@router.get("", response_model=list[ScoutResponse])
async def list_scouts(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Get workspace IDs the user belongs to
    ws_result = await db.execute(
        select(WorkspaceMember.workspace_id).where(WorkspaceMember.user_id == user.id)
    )
    workspace_ids = [row[0] for row in ws_result.all()]

    # Personal scouts + workspace scouts
    conditions = [Scout.user_id == user.id]
    if workspace_ids:
        conditions.append(Scout.workspace_id.in_(workspace_ids))

    result = await db.execute(
        select(Scout)
        .where(or_(*conditions))
        .order_by(Scout.created_at.desc())
    )
    return result.scalars().all()


@router.get("/summary")
async def scouts_summary(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Batched endpoint: returns all scouts with report_count and latest_report_date.
    Replaces the N+1 pattern on the dashboard."""
    # Get workspace IDs
    ws_result = await db.execute(
        select(WorkspaceMember.workspace_id).where(WorkspaceMember.user_id == user.id)
    )
    workspace_ids = [row[0] for row in ws_result.all()]

    # Personal + workspace scouts
    conditions = [Scout.user_id == user.id]
    if workspace_ids:
        conditions.append(Scout.workspace_id.in_(workspace_ids))

    scouts_result = await db.execute(
        select(Scout).where(or_(*conditions)).order_by(Scout.created_at.desc())
    )
    scouts = scouts_result.scalars().all()
    scout_ids = [s.id for s in scouts]

    # Batch: report counts per scout
    counts = {}
    latest_dates = {}
    latest_titles = {}
    if scout_ids:
        count_result = await db.execute(
            select(Report.scout_id, sa_func.count(Report.id), sa_func.max(Report.created_at))
            .where(Report.scout_id.in_(scout_ids))
            .group_by(Report.scout_id)
        )
        for row in count_result.all():
            counts[row[0]] = row[1]
            latest_dates[row[0]] = row[2]

        # Get latest report title for each scout
        for sid in scout_ids:
            if sid in latest_dates and latest_dates[sid]:
                title_result = await db.execute(
                    select(Report.title)
                    .where(Report.scout_id == sid)
                    .order_by(Report.created_at.desc())
                    .limit(1)
                )
                row = title_result.scalar_one_or_none()
                if row:
                    latest_titles[sid] = row

    return [
        {
            "id": s.id,
            "name": s.name,
            "topic": s.topic,
            "description": s.description,
            "keywords": s.keywords,
            "status": s.status,
            "schedule_minutes": s.schedule_minutes,
            "workspace_id": s.workspace_id,
            "user_id": s.user_id,
            "last_run_at": s.last_run_at,
            "next_run_at": s.next_run_at,
            "created_at": s.created_at,
            "report_count": counts.get(s.id, 0),
            "latest_report_date": latest_dates.get(s.id),
            "latest_report_title": latest_titles.get(s.id),
        }
        for s in scouts
    ]


@router.get("/{scout_id}", response_model=ScoutResponse)
async def get_scout(
    scout_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    scout = await db.get(Scout, scout_id)
    return await _get_scout_or_404(scout, user, db)


@router.put("/{scout_id}", response_model=ScoutResponse)
async def update_scout(
    scout_id: uuid.UUID,
    data: ScoutUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    scout = await db.get(Scout, scout_id)
    await _get_scout_or_404(scout, user, db, require_edit=True)

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(scout, key, value)
    if "schedule_minutes" in update_data:
        scout.next_run_at = datetime.utcnow() + timedelta(minutes=scout.schedule_minutes)
    await db.commit()
    await db.refresh(scout)
    return scout


@router.delete("/{scout_id}")
async def delete_scout(
    scout_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    scout = await db.get(Scout, scout_id)
    await _get_scout_or_404(scout, user, db, require_edit=True)
    await db.delete(scout)
    await db.commit()
    return {"detail": "Scout deleted"}


@router.post("/{scout_id}/run")
async def trigger_run(
    scout_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    scout = await db.get(Scout, scout_id)
    await _get_scout_or_404(scout, user, db, require_edit=True)
    report = await run_scout(scout, db)
    return {"detail": "Scout run completed", "report_id": str(report.id)}


@router.post("/{scout_id}/run-stream")
async def trigger_run_stream(
    scout_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Run a scout with Server-Sent Events for real-time progress."""
    scout = await db.get(Scout, scout_id)
    await _get_scout_or_404(scout, user, db, require_edit=True)

    async def event_stream():
        queue: asyncio.Queue = asyncio.Queue()

        async def on_progress(event: str, data: dict):
            await queue.put({"event": event, **data})

        task = asyncio.create_task(run_scout(scout, db, on_progress=on_progress))

        while not task.done():
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=2.0)
                yield f"data: {json.dumps(msg, default=str)}\n\n"
            except asyncio.TimeoutError:
                yield f"data: {json.dumps({'event': 'heartbeat'})}\n\n"

        while not queue.empty():
            msg = await queue.get()
            yield f"data: {json.dumps(msg, default=str)}\n\n"

        try:
            report = task.result()
            yield f"data: {json.dumps({'event': 'complete', 'message': 'Done!', 'report_id': str(report.id)})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'event': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
