from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # ── Core ──
    openai_api_key: str = ""
    database_url: str = "sqlite+aiosqlite:///./scouts.db"
    serpapi_key: str = ""
    e2b_api_key: str = ""

    # ── Auth ──
    secret_key: str = "change-me-in-production"
    lawa_api_url: str = "https://lawa.app"
    access_token_expire_minutes: int = 60 * 24  # 24 hours
    admin_emails: str = ""  # comma-separated list of admin emails

    # ── Email ──
    email_from: str = "contact@lawa.app"
    email_host_password: str = ""
    base_url: str = "https://lawa.app"

    # ── Database (production) ──
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # ── Redis (pub/sub for multi-worker WebSockets + scheduler leader lock) ──
    # Optional: leave blank for single-worker dev; required for multi-worker / multi-instance.
    redis_url: str = ""

    # ── Scheduler ──
    # Set to true on exactly ONE process (e.g. dedicated worker) when running
    # multiple instances. If REDIS_URL is set, leader election is automatic and
    # this flag is ignored.
    enable_scheduler: bool = True

    class Config:
        env_file = ".env"


@lru_cache
def get_settings() -> Settings:
    return Settings()
