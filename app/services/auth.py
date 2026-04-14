"""
Authentication service — verifies users against LAWA platform and manages JWT sessions.
"""

import uuid
import logging
from datetime import datetime, timedelta

import httpx
import jwt
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.models import User

logger = logging.getLogger(__name__)
settings = get_settings()

ALGORITHM = "HS256"


# ── LAWA verification ──

async def verify_lawa_user(username: str, password: str) -> dict:
    """Authenticate against LAWA platform's token endpoint.
    Returns LAWA tokens on success, raises HTTPException on failure."""
    url = f"{settings.lawa_api_url.rstrip('/')}/api/token/"
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.post(url, json={"username": username, "password": password})
        except httpx.RequestError as e:
            logger.error(f"LAWA API connection error: {e}")
            raise HTTPException(status_code=502, detail="Could not reach LAWA platform")

    if resp.status_code == 200:
        data = resp.json()
        return {
            "access": data.get("access"),
            "refresh": data.get("refresh"),
        }
    elif resp.status_code in (401, 400):
        raise HTTPException(status_code=401, detail="Invalid LAWA credentials")
    else:
        logger.error(f"LAWA API error: {resp.status_code} — {resp.text}")
        raise HTTPException(status_code=502, detail="LAWA platform error")


# ── JWT session tokens ──

def create_access_token(user_id: uuid.UUID, email: str) -> str:
    """Create a JWT for the scouts app session."""
    expire = datetime.utcnow() + timedelta(minutes=settings.access_token_expire_minutes)
    payload = {
        "sub": str(user_id),
        "email": email,
        "exp": expire,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Decode and validate a scouts app JWT."""
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Session expired — please log in again")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid session")


# ── User management ──

async def _check_lawa_admin(lawa_access_token: str) -> bool:
    """Check if the user is staff/superuser on the LAWA platform."""
    url = f"{settings.lawa_api_url.rstrip('/')}/api/user-detail/"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {lawa_access_token}"})
            if resp.status_code == 200:
                data = resp.json()
                return data.get("is_staff", False) or data.get("is_superuser", False)
    except Exception as e:
        logger.warning(f"Could not check LAWA admin status: {e}")
    return False


async def get_or_create_user(email: str, lawa_tokens: dict, db: AsyncSession) -> User:
    """Find existing user by email or create a new one."""
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    # Check admin status from LAWA and env var
    lawa_access = lawa_tokens.get("access", "")
    is_lawa_admin = await _check_lawa_admin(lawa_access) if lawa_access else False
    admin_emails = [e.strip().lower() for e in settings.admin_emails.split(",") if e.strip()]
    should_be_admin = is_lawa_admin or email.lower() in admin_emails

    if user:
        user.lawa_access_token = lawa_access
        user.lawa_refresh_token = lawa_tokens.get("refresh")
        user.last_login_at = datetime.utcnow()
        user.is_admin = should_be_admin
    else:
        user = User(
            email=email,
            name=email.split("@")[0],
            lawa_access_token=lawa_access,
            lawa_refresh_token=lawa_tokens.get("refresh"),
            last_login_at=datetime.utcnow(),
            is_admin=should_be_admin,
        )
        db.add(user)

    await db.commit()
    await db.refresh(user)
    return user


# ── Dependencies ──

async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    """FastAPI dependency — extracts user from cookie or Authorization header."""
    token = None

    # Try cookie first (web UI)
    token = request.cookies.get("access_token")

    # Fall back to Authorization header (API clients)
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]

    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    payload = decode_access_token(token)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid session")

    user = await db.get(User, uuid.UUID(user_id))
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    return user


async def get_admin_user(user: User = Depends(get_current_user)) -> User:
    """FastAPI dependency — ensures the current user is an admin."""
    if user.is_admin:
        return user
    admin_emails = [e.strip().lower() for e in settings.admin_emails.split(",") if e.strip()]
    if user.email.lower() in admin_emails:
        return user
    raise HTTPException(status_code=403, detail="Admin access required")
