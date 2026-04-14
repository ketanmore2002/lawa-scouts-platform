import asyncio
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Workspace, WorkspaceMember, WorkspaceInvitation, WorkspaceInviteLink, Scout, User
from app.schemas import (
    WorkspaceCreate,
    WorkspaceUpdate,
    WorkspaceResponse,
    WorkspaceMemberAdd,
    WorkspaceMemberUpdate,
    WorkspaceMemberResponse,
)
from app.services.auth import get_current_user
from app.services.notification_service import create_notification, log_activity

logger = logging.getLogger(__name__)

VALID_ROLES = ("editor", "commenter", "viewer")

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])


async def _get_workspace_or_404(workspace_id: uuid.UUID, user: User, db: AsyncSession):
    """Get workspace if user is a member, else 404."""
    workspace = await db.get(Workspace, workspace_id)
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")
    result = await db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user.id,
        )
    )
    member = result.scalar_one_or_none()
    if not member:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspace, member


# ── Workspace CRUD ──

@router.post("", response_model=WorkspaceResponse)
async def create_workspace(
    data: WorkspaceCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    workspace = Workspace(name=data.name, owner_id=user.id)
    db.add(workspace)
    await db.flush()

    member = WorkspaceMember(
        workspace_id=workspace.id,
        user_id=user.id,
        role="owner",
    )
    db.add(member)
    await db.commit()
    await db.refresh(workspace)

    return {
        **{c.key: getattr(workspace, c.key) for c in workspace.__table__.columns},
        "member_count": 1,
    }


@router.get("", response_model=list[WorkspaceResponse])
async def list_workspaces(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(
            Workspace,
            func.count(WorkspaceMember.id).label("member_count"),
        )
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(
            Workspace.id.in_(
                select(WorkspaceMember.workspace_id).where(WorkspaceMember.user_id == user.id)
            )
        )
        .group_by(Workspace.id)
        .order_by(Workspace.created_at.desc())
    )
    rows = result.all()
    return [
        {
            **{c.key: getattr(ws, c.key) for c in ws.__table__.columns},
            "member_count": count,
        }
        for ws, count in rows
    ]


@router.get("/{workspace_id}", response_model=WorkspaceResponse)
async def get_workspace(
    workspace_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    workspace, _ = await _get_workspace_or_404(workspace_id, user, db)
    count_result = await db.execute(
        select(func.count(WorkspaceMember.id)).where(
            WorkspaceMember.workspace_id == workspace_id
        )
    )
    return {
        **{c.key: getattr(workspace, c.key) for c in workspace.__table__.columns},
        "member_count": count_result.scalar(),
    }


@router.put("/{workspace_id}", response_model=WorkspaceResponse)
async def update_workspace(
    workspace_id: uuid.UUID,
    data: WorkspaceUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    workspace, member = await _get_workspace_or_404(workspace_id, user, db)
    if member.role != "owner":
        raise HTTPException(status_code=403, detail="Only the owner can rename a workspace")

    if data.name is not None:
        workspace.name = data.name
    await db.commit()
    await db.refresh(workspace)

    count_result = await db.execute(
        select(func.count(WorkspaceMember.id)).where(
            WorkspaceMember.workspace_id == workspace_id
        )
    )
    return {
        **{c.key: getattr(workspace, c.key) for c in workspace.__table__.columns},
        "member_count": count_result.scalar(),
    }


@router.delete("/{workspace_id}")
async def delete_workspace(
    workspace_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    workspace, member = await _get_workspace_or_404(workspace_id, user, db)
    if member.role != "owner":
        raise HTTPException(status_code=403, detail="Only the owner can delete a workspace")

    # Delete all scouts belonging to this workspace (reports cascade via ORM relationship)
    result = await db.execute(
        select(Scout).where(Scout.workspace_id == workspace_id)
    )
    for scout in result.scalars().all():
        await db.delete(scout)

    await db.delete(workspace)
    await db.commit()
    return {"detail": "Workspace deleted"}


# ── Members ──

@router.get("/{workspace_id}/members", response_model=list[WorkspaceMemberResponse])
async def list_members(
    workspace_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_workspace_or_404(workspace_id, user, db)
    result = await db.execute(
        select(WorkspaceMember, User)
        .join(User, User.id == WorkspaceMember.user_id)
        .where(WorkspaceMember.workspace_id == workspace_id)
        .order_by(WorkspaceMember.created_at)
    )
    rows = result.all()
    return [
        {
            "id": m.id,
            "user_id": m.user_id,
            "email": u.email,
            "name": u.name,
            "role": m.role,
            "created_at": m.created_at,
        }
        for m, u in rows
    ]


@router.post("/{workspace_id}/members")
async def invite_member(
    workspace_id: uuid.UUID,
    data: WorkspaceMemberAdd,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Send an email invitation to join the workspace."""
    workspace, caller = await _get_workspace_or_404(workspace_id, user, db)
    if caller.role != "owner":
        raise HTTPException(status_code=403, detail="Only the owner can invite members")

    # Check if already a member
    target_result = await db.execute(select(User).where(User.email == data.email))
    target_user = target_result.scalar_one_or_none()
    if target_user:
        existing = await db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == target_user.id,
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="User is already a member")

    # Check pending invitation
    existing_invite = await db.execute(
        select(WorkspaceInvitation).where(
            WorkspaceInvitation.workspace_id == workspace_id,
            WorkspaceInvitation.email == data.email,
            WorkspaceInvitation.status == "pending",
        )
    )
    if existing_invite.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Invitation already pending for this email")

    role = data.role if data.role in VALID_ROLES else "viewer"
    token = secrets.token_urlsafe(32)
    invitation = WorkspaceInvitation(
        workspace_id=workspace_id,
        email=data.email,
        role=role,
        token=token,
        invited_by=user.id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    db.add(invitation)

    # Create in-app notification if user exists
    if target_user:
        await create_notification(
            db, target_user.id, "workspace_invite",
            f"Invited to {workspace.name}",
            body=f"{user.name or user.email} invited you as {role}",
            link=f"/invitations/{token}",
            metadata={"workspace_id": str(workspace_id), "role": role},
        )

    await db.commit()

    # Send invitation email in background
    asyncio.get_event_loop().run_in_executor(
        None, _send_invitation_email_sync,
        data.email, workspace.name, user.name or user.email, role, token,
    )

    return {
        "detail": "Invitation sent",
        "invitation_id": str(invitation.id),
        "email": data.email,
        "role": role,
        "status": "pending",
    }


def _send_invitation_email_sync(email: str, workspace_name: str, inviter_name: str, role: str, token: str):
    """Send invitation email synchronously (runs in thread pool)."""
    try:
        from app.services.email_service import send_invitation_email
        send_invitation_email(email, workspace_name, inviter_name, role, token)
    except Exception as e:
        logger.error(f"Failed to send invitation email to {email}: {e}")


@router.put("/{workspace_id}/members/{member_id}", response_model=WorkspaceMemberResponse)
async def update_member(
    workspace_id: uuid.UUID,
    member_id: uuid.UUID,
    data: WorkspaceMemberUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _, caller = await _get_workspace_or_404(workspace_id, user, db)
    if caller.role != "owner":
        raise HTTPException(status_code=403, detail="Only the owner can change roles")

    target = await db.get(WorkspaceMember, member_id)
    if not target or target.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Member not found")
    if target.role == "owner":
        raise HTTPException(status_code=400, detail="Cannot change the owner's role")

    target.role = data.role if data.role in VALID_ROLES else "viewer"
    await db.commit()
    await db.refresh(target)
    target_user = await db.get(User, target.user_id)
    return {
        "id": target.id,
        "user_id": target.user_id,
        "email": target_user.email,
        "name": target_user.name,
        "role": target.role,
        "created_at": target.created_at,
    }


@router.post("/{workspace_id}/leave")
async def leave_workspace(
    workspace_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Allow a non-owner member to leave a workspace."""
    _, caller = await _get_workspace_or_404(workspace_id, user, db)
    if caller.role == "owner":
        raise HTTPException(
            status_code=400,
            detail="Owners cannot leave a workspace. Transfer ownership or delete it instead.",
        )

    await db.delete(caller)
    await log_activity(
        db, workspace_id, user.id, "member_left",
        f"{user.name or user.email} left the workspace",
        entity_type="member",
    )
    await db.commit()
    return {"detail": "You have left the workspace"}


@router.delete("/{workspace_id}/members/{member_id}")
async def remove_member(
    workspace_id: uuid.UUID,
    member_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    workspace, caller = await _get_workspace_or_404(workspace_id, user, db)
    if caller.role != "owner":
        raise HTTPException(status_code=403, detail="Only the owner can remove members")

    target = await db.get(WorkspaceMember, member_id)
    if not target or target.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Member not found")
    if target.role == "owner":
        raise HTTPException(status_code=400, detail="Cannot remove the workspace owner")

    target_user = await db.get(User, target.user_id)
    await db.delete(target)

    # Log activity
    await log_activity(
        db, workspace_id, user.id, "member_removed",
        f"{target_user.name or target_user.email} was removed",
        entity_type="member",
    )

    await db.commit()
    return {"detail": "Member removed"}


# ── Invitations ──

@router.get("/{workspace_id}/invitations")
async def list_invitations(
    workspace_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _, caller = await _get_workspace_or_404(workspace_id, user, db)
    if caller.role != "owner":
        raise HTTPException(status_code=403, detail="Only the owner can view invitations")

    result = await db.execute(
        select(WorkspaceInvitation)
        .where(
            WorkspaceInvitation.workspace_id == workspace_id,
            WorkspaceInvitation.status == "pending",
        )
        .order_by(WorkspaceInvitation.created_at.desc())
    )
    return [
        {
            "id": inv.id,
            "workspace_id": inv.workspace_id,
            "email": inv.email,
            "role": inv.role,
            "status": inv.status,
            "created_at": inv.created_at,
            "expires_at": inv.expires_at,
        }
        for inv in result.scalars().all()
    ]


@router.delete("/{workspace_id}/invitations/{invitation_id}")
async def cancel_invitation(
    workspace_id: uuid.UUID,
    invitation_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _, caller = await _get_workspace_or_404(workspace_id, user, db)
    if caller.role != "owner":
        raise HTTPException(status_code=403, detail="Only the owner can cancel invitations")

    inv = await db.get(WorkspaceInvitation, invitation_id)
    if not inv or inv.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Invitation not found")

    inv.status = "declined"
    await db.commit()
    return {"detail": "Invitation cancelled"}


@router.get("/invitations/{token}/accept")
async def accept_invitation(
    token: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Accept a workspace invitation. Requires authenticated user."""
    result = await db.execute(
        select(WorkspaceInvitation).where(
            WorkspaceInvitation.token == token,
            WorkspaceInvitation.status == "pending",
        )
    )
    inv = result.scalar_one_or_none()
    if not inv:
        raise HTTPException(status_code=404, detail="Invitation not found or already used")

    if inv.expires_at < datetime.now(timezone.utc):
        inv.status = "expired"
        await db.commit()
        raise HTTPException(status_code=410, detail="Invitation has expired")

    # Check not already a member
    existing = await db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == inv.workspace_id,
            WorkspaceMember.user_id == user.id,
        )
    )
    if existing.scalar_one_or_none():
        inv.status = "accepted"
        await db.commit()
        return {"detail": "Already a member", "workspace_id": str(inv.workspace_id)}

    # Create membership
    member = WorkspaceMember(
        workspace_id=inv.workspace_id,
        user_id=user.id,
        role=inv.role,
    )
    db.add(member)
    inv.status = "accepted"

    # Log activity
    await log_activity(
        db, inv.workspace_id, user.id, "member_joined",
        f"{user.name or user.email} joined the workspace",
        entity_type="member",
    )

    await db.commit()
    return {"detail": "Invitation accepted", "workspace_id": str(inv.workspace_id)}


@router.get("/invitations/{token}/decline")
async def decline_invitation(
    token: str,
    db: AsyncSession = Depends(get_db),
):
    """Decline a workspace invitation. No auth required."""
    result = await db.execute(
        select(WorkspaceInvitation).where(
            WorkspaceInvitation.token == token,
            WorkspaceInvitation.status == "pending",
        )
    )
    inv = result.scalar_one_or_none()
    if not inv:
        raise HTTPException(status_code=404, detail="Invitation not found or already used")

    inv.status = "declined"
    await db.commit()
    return {"detail": "Invitation declined"}


# ── Invite Links ──

def _serialize_invite_link(link: WorkspaceInviteLink) -> dict:
    return {
        "id": str(link.id),
        "workspace_id": str(link.workspace_id),
        "token": link.token,
        "label": link.label,
        "role": link.role,
        "enabled": link.enabled,
        "created_by": str(link.created_by),
        "created_at": link.created_at.isoformat() if link.created_at else None,
    }


@router.get("/{workspace_id}/invite-links")
async def list_invite_links(
    workspace_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _, member = await _get_workspace_or_404(workspace_id, user, db)
    if member.role != "owner":
        raise HTTPException(status_code=403, detail="Only the owner can manage invite links")

    result = await db.execute(
        select(WorkspaceInviteLink)
        .where(WorkspaceInviteLink.workspace_id == workspace_id)
        .order_by(WorkspaceInviteLink.created_at.desc())
    )
    return [_serialize_invite_link(l) for l in result.scalars().all()]


@router.post("/{workspace_id}/invite-links")
async def create_invite_link(
    workspace_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _, member = await _get_workspace_or_404(workspace_id, user, db)
    if member.role != "owner":
        raise HTTPException(status_code=403, detail="Only the owner can manage invite links")

    link = WorkspaceInviteLink(
        workspace_id=workspace_id,
        token=secrets.token_urlsafe(32),
        created_by=user.id,
        enabled=True,
        role="viewer",
    )
    db.add(link)
    await db.commit()
    await db.refresh(link)
    return _serialize_invite_link(link)


@router.patch("/{workspace_id}/invite-links/{link_id}")
async def toggle_invite_link(
    workspace_id: uuid.UUID,
    link_id: uuid.UUID,
    data: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _, member = await _get_workspace_or_404(workspace_id, user, db)
    if member.role != "owner":
        raise HTTPException(status_code=403, detail="Only the owner can manage invite links")

    link = await db.get(WorkspaceInviteLink, link_id)
    if not link or link.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Invite link not found")

    if "enabled" in data:
        link.enabled = bool(data["enabled"])
    if "label" in data:
        link.label = (data["label"] or None)
    await db.commit()
    await db.refresh(link)
    return _serialize_invite_link(link)


@router.delete("/{workspace_id}/invite-links/{link_id}")
async def delete_invite_link(
    workspace_id: uuid.UUID,
    link_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _, member = await _get_workspace_or_404(workspace_id, user, db)
    if member.role != "owner":
        raise HTTPException(status_code=403, detail="Only the owner can manage invite links")

    link = await db.get(WorkspaceInviteLink, link_id)
    if not link or link.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Invite link not found")
    await db.delete(link)
    await db.commit()
    return {"detail": "Invite link deleted"}


@router.post("/join/{token}")
async def join_via_link(
    token: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Join a workspace via invite link. Requires auth."""
    # Prefer the new multi-link table; fall back to the legacy single-token field.
    link_result = await db.execute(
        select(WorkspaceInviteLink).where(
            WorkspaceInviteLink.token == token,
            WorkspaceInviteLink.enabled == True,  # noqa: E712
        )
    )
    link = link_result.scalar_one_or_none()
    workspace = None
    join_role = "viewer"
    if link:
        workspace = await db.get(Workspace, link.workspace_id)
        join_role = link.role or "viewer"
    else:
        legacy_result = await db.execute(
            select(Workspace).where(
                Workspace.invite_token == token,
                Workspace.invite_token_enabled == True,  # noqa: E712
            )
        )
        workspace = legacy_result.scalar_one_or_none()
    if not workspace:
        raise HTTPException(status_code=404, detail="Invalid or expired invite link")

    # Check if already a member
    existing = await db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace.id,
            WorkspaceMember.user_id == user.id,
        )
    )
    if existing.scalar_one_or_none():
        return {"detail": "Already a member", "workspace_id": str(workspace.id), "workspace_name": workspace.name}

    member = WorkspaceMember(
        workspace_id=workspace.id,
        user_id=user.id,
        role=join_role,
    )
    db.add(member)

    await log_activity(
        db, workspace.id, user.id, "member_joined",
        f"{user.name or user.email} joined via invite link",
        entity_type="member",
    )

    await db.commit()
    return {"detail": "Joined workspace", "workspace_id": str(workspace.id), "workspace_name": workspace.name}
