import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import User
from app.config import get_settings
from app.services.auth import decode_access_token

router = APIRouter(tags=["pages"])
templates = Jinja2Templates(directory="app/templates")


def _spa_context(request: Request, extra: dict | None = None) -> dict:
    """Build extra template context with SPA partial support.
    If X-Requested-With: SPA header is present, use base_partial.html."""
    ctx = {}
    if extra:
        ctx.update(extra)
    if request.headers.get("X-Requested-With") == "SPA":
        ctx["base_template"] = "base_partial.html"
    return ctx


async def _is_authenticated(request: Request, db: AsyncSession) -> bool:
    """Check if the request has a valid session cookie AND user exists in DB."""
    token = request.cookies.get("access_token")
    if not token:
        return False
    try:
        payload = decode_access_token(token)
        user_id = payload.get("sub")
        if not user_id:
            return False
        user = await db.get(User, uuid.UUID(user_id))
        return user is not None
    except Exception:
        return False


@router.get("/login")
async def login_page(request: Request, db: AsyncSession = Depends(get_db)):
    if await _is_authenticated(request, db):
        return RedirectResponse("/", status_code=302)
    # Clear any stale cookie
    response = templates.TemplateResponse(request, "login.html", _spa_context(request))
    if request.cookies.get("access_token"):
        response.delete_cookie("access_token")
    return response


@router.get("/")
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    if not await _is_authenticated(request, db):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "dashboard.html", _spa_context(request))


@router.get("/scouts/new")
async def create_scout_page(request: Request, db: AsyncSession = Depends(get_db)):
    if not await _is_authenticated(request, db):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "scout_create.html", _spa_context(request))


@router.get("/scouts/templates")
async def templates_page(request: Request, db: AsyncSession = Depends(get_db)):
    if not await _is_authenticated(request, db):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "scout_templates.html", _spa_context(request))


@router.get("/scouts/{scout_id}")
async def scout_detail_page(request: Request, scout_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    if not await _is_authenticated(request, db):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "scout_detail.html", _spa_context(request, {"scout_id": str(scout_id)}))


@router.get("/scouts/{scout_id}/reports/{report_id}")
async def report_detail_page(request: Request, scout_id: uuid.UUID, report_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    if not await _is_authenticated(request, db):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "report_detail.html", _spa_context(request, {
        "scout_id": str(scout_id),
        "report_id": str(report_id),
    }))


@router.get("/workspaces/{workspace_id}")
async def workspace_page(request: Request, workspace_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    if not await _is_authenticated(request, db):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "workspace.html", _spa_context(request, {"workspace_id": str(workspace_id)}))


@router.get("/admin")
async def admin_page(request: Request, db: AsyncSession = Depends(get_db)):
    if not await _is_authenticated(request, db):
        return RedirectResponse("/login", status_code=302)
    # Check admin status
    token = request.cookies.get("access_token")
    try:
        payload = decode_access_token(token)
        user_id = payload.get("sub")
        user = await db.get(User, uuid.UUID(user_id))
        settings = get_settings()
        admin_emails = [e.strip().lower() for e in settings.admin_emails.split(",") if e.strip()]
        if not (user.is_admin or user.email.lower() in admin_emails):
            return RedirectResponse("/", status_code=302)
    except Exception:
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request, "admin_dashboard.html", _spa_context(request))


@router.get("/collections")
async def collections_page(request: Request, db: AsyncSession = Depends(get_db)):
    if not await _is_authenticated(request, db):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "collections.html", _spa_context(request))


@router.get("/collections/{collection_id}")
async def collection_detail_page(request: Request, collection_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    if not await _is_authenticated(request, db):
        return RedirectResponse("/login", status_code=302)
    return templates.TemplateResponse(request, "collection_detail.html", _spa_context(request, {"collection_id": str(collection_id)}))


@router.get("/invitations/{token}")
async def invitation_page(request: Request, token: str, db: AsyncSession = Depends(get_db)):
    """Invitation accept/decline page."""
    authenticated = await _is_authenticated(request, db)
    return templates.TemplateResponse(request, "invitation.html", _spa_context(request, {
        "token": token,
        "authenticated": authenticated,
    }))


@router.get("/workspaces/join/{token}")
async def join_workspace_page(request: Request, token: str, db: AsyncSession = Depends(get_db)):
    if not await _is_authenticated(request, db):
        return RedirectResponse(f"/login?next=/workspaces/join/{token}", status_code=302)
    return templates.TemplateResponse(request, "join_workspace.html", _spa_context(request, {"token": token}))


@router.get("/shared/{share_token}")
async def shared_report_page(request: Request, share_token: str):
    """Public page - no auth required."""
    return templates.TemplateResponse(request, "shared_report.html", _spa_context(request, {
        "share_token": share_token,
    }))
