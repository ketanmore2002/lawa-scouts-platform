import uuid
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User, Scout, Report, AccessLog
from app.services.auth import get_admin_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/stats")
async def admin_stats(
    user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    total_users = await db.scalar(select(func.count()).select_from(User))
    total_scouts = await db.scalar(select(func.count()).select_from(Scout))
    total_reports = await db.scalar(select(func.count()).select_from(Report))
    active_scouts = await db.scalar(
        select(func.count()).select_from(Scout).where(Scout.status == "active")
    )
    return {
        "total_users": total_users or 0,
        "total_scouts": total_scouts or 0,
        "total_reports": total_reports or 0,
        "active_scouts": active_scouts or 0,
    }


@router.get("/users")
async def admin_users(
    user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    # Get all users
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    users = result.scalars().all()

    # Bulk load scout counts
    sc_result = await db.execute(
        select(Scout.user_id, func.count(Scout.id).label("scout_count"))
        .group_by(Scout.user_id)
    )
    scout_counts = {row.user_id: row.scout_count for row in sc_result}

    # Bulk load report counts via scouts
    rc_result = await db.execute(
        select(Scout.user_id, func.count(Report.id).label("report_count"))
        .join(Report, Report.scout_id == Scout.id)
        .group_by(Scout.user_id)
    )
    report_counts = {row.user_id: row.report_count for row in rc_result}

    return [
        {
            "id": str(u.id),
            "email": u.email,
            "name": u.name,
            "is_admin": u.is_admin,
            "scout_count": scout_counts.get(u.id, 0),
            "report_count": report_counts.get(u.id, 0),
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        }
        for u in users
    ]


@router.delete("/users/{user_id}")
async def admin_delete_user(
    user_id: uuid.UUID,
    user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    if user_id == user.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    await db.delete(target)
    await db.commit()
    return {"detail": "User deleted"}


@router.get("/scouts")
async def admin_scouts(
    user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Scout).order_by(Scout.created_at.desc()))
    scouts = result.scalars().all()

    # Bulk load report counts
    rc_result = await db.execute(
        select(Report.scout_id, func.count(Report.id).label("cnt"))
        .group_by(Report.scout_id)
    )
    report_counts = {row.scout_id: row.cnt for row in rc_result}

    # Bulk load user info
    user_ids = list(set(s.user_id for s in scouts))
    if user_ids:
        users_result = await db.execute(select(User).where(User.id.in_(user_ids)))
        users_map = {u.id: u for u in users_result.scalars().all()}
    else:
        users_map = {}

    return [
        {
            "id": str(s.id),
            "name": s.name,
            "topic": s.topic,
            "status": s.status,
            "schedule_minutes": s.schedule_minutes,
            "last_run_at": s.last_run_at.isoformat() if s.last_run_at else None,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "report_count": report_counts.get(s.id, 0),
            "owner_email": users_map[s.user_id].email if s.user_id in users_map else "unknown",
            "owner_name": users_map[s.user_id].name if s.user_id in users_map else None,
        }
        for s in scouts
    ]


@router.get("/recent-reports")
async def admin_recent_reports(
    user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Report).order_by(Report.created_at.desc()).limit(20)
    )
    reports = result.scalars().all()

    # Bulk load scout and user info
    scout_ids = list(set(r.scout_id for r in reports))
    if scout_ids:
        scouts_result = await db.execute(select(Scout).where(Scout.id.in_(scout_ids)))
        scouts_map = {s.id: s for s in scouts_result.scalars().all()}
    else:
        scouts_map = {}

    user_ids = list(set(s.user_id for s in scouts_map.values()))
    if user_ids:
        users_result = await db.execute(select(User).where(User.id.in_(user_ids)))
        users_map = {u.id: u for u in users_result.scalars().all()}
    else:
        users_map = {}

    return [
        {
            "id": str(r.id),
            "title": r.title,
            "summary": r.summary,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "scout_name": scouts_map[r.scout_id].name if r.scout_id in scouts_map else "Unknown",
            "owner_email": users_map[scouts_map[r.scout_id].user_id].email
            if r.scout_id in scouts_map and scouts_map[r.scout_id].user_id in users_map
            else "unknown",
        }
        for r in reports
    ]


@router.get("/charts")
async def admin_charts(
    user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    thirty_days_ago = datetime.utcnow() - timedelta(days=30)

    # User growth per day
    user_result = await db.execute(
        select(
            func.date(User.created_at).label("date"),
            func.count(User.id).label("count"),
        )
        .where(User.created_at >= thirty_days_ago)
        .group_by(func.date(User.created_at))
        .order_by(func.date(User.created_at))
    )
    user_growth = [{"date": str(row.date), "count": row.count} for row in user_result]

    # Scout creation trend
    scout_result = await db.execute(
        select(
            func.date(Scout.created_at).label("date"),
            func.count(Scout.id).label("count"),
        )
        .where(Scout.created_at >= thirty_days_ago)
        .group_by(func.date(Scout.created_at))
        .order_by(func.date(Scout.created_at))
    )
    scout_creation = [{"date": str(row.date), "count": row.count} for row in scout_result]

    # Reports per day
    report_result = await db.execute(
        select(
            func.date(Report.created_at).label("date"),
            func.count(Report.id).label("count"),
        )
        .where(Report.created_at >= thirty_days_ago)
        .group_by(func.date(Report.created_at))
        .order_by(func.date(Report.created_at))
    )
    reports_per_day = [{"date": str(row.date), "count": row.count} for row in report_result]

    return {
        "user_growth": user_growth,
        "scout_creation": scout_creation,
        "reports_per_day": reports_per_day,
    }


@router.get("/geo")
async def admin_geo(
    user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(
            AccessLog.country_code,
            func.count(AccessLog.id).label("count"),
        )
        .where(AccessLog.country_code.isnot(None))
        .group_by(AccessLog.country_code)
        .order_by(func.count(AccessLog.id).desc())
    )
    return {
        "countries": [
            {"country_code": row.country_code, "count": row.count}
            for row in result
        ]
    }
