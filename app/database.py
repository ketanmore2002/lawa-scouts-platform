import ssl

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings

settings = get_settings()

# ── Engine configuration ──
_engine_kwargs: dict = {"echo": False}

if settings.database_url.startswith("postgresql"):
    # SSL required for Digital Ocean managed PostgreSQL
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    _engine_kwargs.update(
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        pool_recycle=300,
        connect_args={"ssl": ssl_ctx},
    )

engine = create_async_engine(settings.database_url, **_engine_kwargs)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with async_session() as session:
        yield session


async def init_db():
    """Initialize the database schema.

    Production (PostgreSQL): schema is managed by Alembic — run
    `alembic upgrade head` as a release step. We do NOT call create_all so
    that schema drift is caught in version control.

    Development (SQLite): create_all + ad-hoc column adds, so devs can run
    the app without learning Alembic.
    """
    if settings.database_url.startswith("postgresql"):
        # No-op: Alembic owns the schema.
        return
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate_add_columns)


def _migrate_add_columns(conn):
    """Add new columns to existing tables (safe for both SQLite and PostgreSQL)."""
    import sqlalchemy as sa
    insp = sa.inspect(conn)
    # Add is_admin to users
    if "users" in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns("users")]
        if "is_admin" not in cols:
            conn.execute(sa.text("ALTER TABLE users ADD COLUMN is_admin BOOLEAN DEFAULT FALSE"))
    # Add source list and workspace columns to scouts
    if "scouts" in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns("scouts")]
        if "include_sources" not in cols:
            conn.execute(sa.text("ALTER TABLE scouts ADD COLUMN include_sources TEXT"))
        if "exclude_sources" not in cols:
            conn.execute(sa.text("ALTER TABLE scouts ADD COLUMN exclude_sources TEXT"))
        if "workspace_id" not in cols:
            dialect = conn.dialect.name
            if dialect == "postgresql":
                conn.execute(sa.text("ALTER TABLE scouts ADD COLUMN workspace_id UUID"))
            else:
                conn.execute(sa.text("ALTER TABLE scouts ADD COLUMN workspace_id CHAR(32)"))
        else:
            # Fix: migrate CHAR(32) → UUID if on PostgreSQL and column was created with wrong type
            dialect = conn.dialect.name
            if dialect == "postgresql":
                col_info = next((c for c in insp.get_columns("scouts") if c["name"] == "workspace_id"), None)
                if col_info and "CHAR" in str(col_info["type"]).upper():
                    conn.execute(sa.text("ALTER TABLE scouts ALTER COLUMN workspace_id TYPE UUID USING workspace_id::uuid"))
    # Add invite link columns to workspaces
    if "workspaces" in insp.get_table_names():
        cols = [c["name"] for c in insp.get_columns("workspaces")]
        if "invite_token" not in cols:
            conn.execute(sa.text("ALTER TABLE workspaces ADD COLUMN invite_token VARCHAR(64)"))
        if "invite_token_enabled" not in cols:
            conn.execute(sa.text("ALTER TABLE workspaces ADD COLUMN invite_token_enabled BOOLEAN DEFAULT FALSE"))
