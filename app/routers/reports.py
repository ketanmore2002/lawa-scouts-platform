import asyncio
import json
import logging
import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Report, Scout, User, WorkspaceMember
from app.schemas import ReportResponse, FollowUpRequest
from app.services.auth import get_current_user
from app.services import presence


async def _check_scout_access(scout: Scout, user: User, db: AsyncSession):
    """Raise 404 if user has no access to the scout (personal or workspace)."""
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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["reports"])


@router.get("/scouts/{scout_id}/reports", response_model=list[ReportResponse])
async def list_reports(
    scout_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    scout = await db.get(Scout, scout_id)
    await _check_scout_access(scout, user, db)

    result = await db.execute(
        select(Report).where(Report.scout_id == scout_id).order_by(Report.created_at.desc())
    )
    return result.scalars().all()


@router.get("/reports/{report_id}", response_model=ReportResponse)
async def get_report(
    report_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    report = await db.get(Report, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    scout = await db.get(Scout, report.scout_id)
    await _check_scout_access(scout, user, db)
    return report


# ── Share ──

@router.post("/reports/{report_id}/share")
async def share_report(
    report_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Generate or return a share token for a report."""
    report = await db.get(Report, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    scout = await db.get(Scout, report.scout_id)
    await _check_scout_access(scout, user, db)

    if not report.share_token:
        report.share_token = secrets.token_urlsafe(16)
        await db.commit()
        await db.refresh(report)

    return {"share_token": report.share_token}


@router.delete("/reports/{report_id}/share")
async def unshare_report(
    report_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Revoke a shared link."""
    report = await db.get(Report, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    scout = await db.get(Scout, report.scout_id)
    await _check_scout_access(scout, user, db)

    report.share_token = None
    await db.commit()
    return {"detail": "Share link revoked"}


@router.get("/shared/{share_token}")
async def get_shared_report(
    share_token: str,
    db: AsyncSession = Depends(get_db),
):
    """Public endpoint - no auth required."""
    result = await db.execute(
        select(Report).where(Report.share_token == share_token)
    )
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Shared report not found")
    scout = await db.get(Scout, report.scout_id)
    return {
        "id": str(report.id),
        "title": report.title,
        "summary": report.summary,
        "findings": report.findings,
        "created_at": report.created_at.isoformat() if report.created_at else None,
        "scout_topic": scout.topic if scout else "",
        "scout_name": scout.name if scout else "",
    }


# ── Follow-ups ──

@router.post("/reports/follow-up")
async def follow_up(
    data: FollowUpRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Ask a natural language follow-up question about a report."""
    report = await db.get(Report, data.report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    scout = await db.get(Scout, report.scout_id)
    await _check_scout_access(scout, user, db)

    from app.config import get_settings
    settings = get_settings()
    if not settings.openai_api_key:
        raise HTTPException(status_code=500, detail="AI service not configured")

    # Build context from report
    findings = report.findings or {}
    sd = findings.get("structured_data", {})
    rows = sd.get("rows", [])
    columns = sd.get("columns", [])
    insights = sd.get("insights", [])
    analysis = sd.get("analysis", "")

    context_parts = [
        f"Report Title: {report.title}",
        f"Scout Topic: {scout.topic}",
        f"Summary: {report.summary}",
    ]
    if analysis:
        context_parts.append(f"Analysis: {analysis}")
    if insights:
        context_parts.append("Key Insights:\n" + "\n".join(f"- {i}" for i in insights))
    if columns and rows:
        col_names = [c.get("label", c.get("key", "")) for c in columns]
        context_parts.append(f"Data columns: {', '.join(col_names)}")
        context_parts.append(f"Total rows: {len(rows)}")
        sample_rows = rows[:20]
        col_keys = [c.get("key", "") for c in columns]
        table_lines = []
        for row in sample_rows:
            vals = [str(row.get(k, "")) for k in col_keys]
            table_lines.append(" | ".join(vals))
        context_parts.append("Sample data:\n" + "\n".join(table_lines))

    context = "\n\n".join(context_parts)

    try:
        from openai import OpenAI
        client = OpenAI(api_key=settings.openai_api_key)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a helpful research assistant. The user has a report from an AI web scout. "
                        "Answer their follow-up question based on the report data provided. "
                        "Be concise, specific, and reference data from the report. "
                        "Use markdown formatting (bold, bullets, etc.) for clarity. "
                        "If the question can't be answered from the data, say so clearly."
                    ),
                },
                {"role": "user", "content": f"Report context:\n{context}\n\nUser question: {data.question}"},
            ],
            max_tokens=1000,
            temperature=0.3,
        )
        answer = response.choices[0].message.content
    except Exception as e:
        logger.error(f"Follow-up LLM call failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate answer")

    return {"answer": answer}


# ── Presence SSE ──

@router.get("/reports/{report_id}/presence")
async def report_presence_stream(
    report_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Server-Sent Events stream for real-time presence on a report."""
    report = await db.get(Report, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")
    scout = await db.get(Scout, report.scout_id)
    await _check_scout_access(scout, user, db)

    rid = str(report_id)
    uid = str(user.id)
    user_info = {"user_id": uid, "name": user.name or user.email, "email": user.email}

    async def event_stream():
        presence.add_viewer(rid, uid, user_info)
        q = presence.subscribe(rid)
        try:
            # Send initial viewer list
            viewers = presence.get_viewers(rid)
            yield f"data: {json.dumps(viewers)}\n\n"
            while True:
                try:
                    update = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {json.dumps(update)}\n\n"
                except asyncio.TimeoutError:
                    yield f": heartbeat\n\n"
        finally:
            presence.remove_viewer(rid, uid)
            presence.unsubscribe(rid, q)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
