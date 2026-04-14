"""
Background scheduler.

Multi-instance safety: when Postgres is in use, every tick acquires a
session-level advisory lock before doing work. Only one instance per cluster
can hold the lock, so the same scout never runs twice even if the scheduler
is started in many workers/instances. SQLite (dev) skips the lock.

You can also disable the scheduler entirely on a given process by setting
ENABLE_SCHEDULER=false in env (useful when you want a dedicated worker
process and bare API processes).
"""

import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, text, update

from app.config import get_settings
from app.database import async_session
from app.models import Scout, WorkspaceInvitation
from app.services.scout_runner import run_scout

logger = logging.getLogger(__name__)
settings = get_settings()
scheduler = AsyncIOScheduler()

# Arbitrary 64-bit int used to identify our advisory lock. Chosen at random.
_LEADER_LOCK_KEY = 7438291056123478

# True once we hold the lock in our session. We re-acquire each tick because
# we use pg_try_advisory_xact_lock (transaction-scoped) for resilience.
_IS_PG = settings.database_url.startswith("postgresql")


async def _try_acquire_leader(db) -> bool:
    """Try to grab the cluster-wide leader lock for the duration of this txn.
    Returns True if we got it (we should run the work), False otherwise.
    On SQLite, always returns True (single-process dev).
    """
    if not _IS_PG:
        return True
    result = await db.execute(text(f"SELECT pg_try_advisory_xact_lock({_LEADER_LOCK_KEY})"))
    return bool(result.scalar())


async def check_and_run_scouts():
    """Find scouts due to run and execute them. Leader-gated."""
    async with async_session() as db:
        # Begin a transaction so the advisory lock scope matches our work.
        async with db.begin():
            if not await _try_acquire_leader(db):
                logger.debug("Scheduler tick skipped — another instance is the leader")
                return

            now = datetime.utcnow()
            result = await db.execute(
                select(Scout).where(
                    Scout.status == "active",
                    Scout.next_run_at <= now,
                )
            )
            due_scouts = result.scalars().all()

        if due_scouts:
            logger.info(f"Found {len(due_scouts)} scout(s) due to run")

        for scout in due_scouts:
            try:
                # run_scout opens its own session as needed
                async with async_session() as work_db:
                    await run_scout(scout, work_db)
            except Exception as e:
                logger.error(f"Error running scout '{scout.name}' (id={scout.id}): {e}")


async def expire_old_invitations():
    """Mark pending invitations past their expiry date as expired. Leader-gated."""
    async with async_session() as db:
        async with db.begin():
            if not await _try_acquire_leader(db):
                return
            now = datetime.now(timezone.utc)
            result = await db.execute(
                update(WorkspaceInvitation)
                .where(
                    WorkspaceInvitation.status == "pending",
                    WorkspaceInvitation.expires_at <= now,
                )
                .values(status="expired")
            )
            if result.rowcount:
                logger.info(f"Expired {result.rowcount} old workspace invitation(s)")


def start_scheduler():
    if not settings.enable_scheduler:
        logger.info("Scheduler disabled on this process (ENABLE_SCHEDULER=false)")
        return
    scheduler.add_job(
        check_and_run_scouts,
        "interval",
        minutes=1,
        id="scout_checker",
        replace_existing=True,
    )
    scheduler.add_job(
        expire_old_invitations,
        "interval",
        hours=1,
        id="invitation_expiry",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(
        "Scheduler started — checking for due scouts every 60 seconds "
        f"(leader-gated: {_IS_PG})"
    )


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()
        logger.info("Scheduler stopped")
