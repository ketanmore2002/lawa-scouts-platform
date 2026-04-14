import ipaddress
import logging
from contextlib import asynccontextmanager

import httpx
import asyncio

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.database import init_db, async_session
from app.models import AccessLog
from app.routers import auth, pages, scouts, reports, admin, workspaces, highlights, notifications, comments, reactions, collections, activity, realtime
from app.services.scheduler import start_scheduler, stop_scheduler
from app.services.ws_hub import start_redis_listener, stop_redis_listener

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ── IP geolocation cache ──
_geo_cache: dict[str, str | None] = {}


async def _geolocate_ip(ip: str) -> str | None:
    """Resolve an IP to a 2-letter country code via ip-api.com (free, no key).
    Results are cached in-memory to avoid repeated lookups."""
    if ip in _geo_cache:
        return _geo_cache[ip]
    try:
        addr = ipaddress.ip_address(ip)
        if addr.is_private or addr.is_loopback:
            _geo_cache[ip] = None
            return None
    except ValueError:
        _geo_cache[ip] = None
        return None
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"http://ip-api.com/json/{ip}?fields=countryCode")
            if resp.status_code == 200:
                code = resp.json().get("countryCode")
                _geo_cache[ip] = code
                return code
    except Exception:
        pass
    _geo_cache[ip] = None
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logging.info("Database initialized")
    await start_redis_listener()
    start_scheduler()
    yield
    stop_scheduler()
    await stop_redis_listener()


app = FastAPI(title="LAWA Scouts", lifespan=lifespan)


@app.get("/healthz")
async def healthz():
    """Liveness probe for DO load balancer / uptime monitors."""
    return {"status": "ok"}

# ── CORS (allow all origins in dev, restrict in production) ──
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files with caching
class CachedStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

app.mount("/static", CachedStaticFiles(directory="app/static"), name="static")

# ── Access logging middleware ──
@app.middleware("http")
async def log_access(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/static") or path.startswith("/api/") or path.startswith("/ws"):
        return response
    # Non-blocking access log — don't slow down page responses
    asyncio.create_task(_log_access(request, path))
    return response


async def _log_access(request: Request, path: str):
    """Write access log entry in background."""
    try:
        import uuid as uuid_mod
        user_id = None
        token = request.cookies.get("access_token")
        if token:
            try:
                from app.services.auth import decode_access_token
                payload = decode_access_token(token)
                uid = payload.get("sub")
                if uid:
                    user_id = uuid_mod.UUID(uid)
            except Exception:
                pass
        ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        if not ip:
            ip = request.client.host if request.client else "unknown"
        country_code = (
            request.headers.get("CF-IPCountry")
            or request.headers.get("X-Country-Code")
            or await _geolocate_ip(ip)
        )
        async with async_session() as session:
            log = AccessLog(
                user_id=user_id,
                ip_address=ip[:45],
                country_code=country_code[:2] if country_code else None,
                path=path[:500],
            )
            session.add(log)
            await session.commit()
    except Exception:
        pass

# Include routers — auth first, then API, then page routes
app.include_router(auth.router)
app.include_router(scouts.router)
app.include_router(reports.router)
app.include_router(admin.router)
app.include_router(workspaces.router)
app.include_router(highlights.router)
app.include_router(notifications.router)
app.include_router(comments.router)
app.include_router(reactions.router)
app.include_router(collections.router)
app.include_router(activity.router)
app.include_router(realtime.router)
app.include_router(pages.router)
