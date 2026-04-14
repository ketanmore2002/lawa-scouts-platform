import uuid
from datetime import datetime

from pydantic import BaseModel


# ── Auth ──

class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    name: str | None
    is_admin: bool = False
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Scouts ──

class ScoutCreate(BaseModel):
    name: str | None = None
    topic: str
    description: str | None = None
    keywords: str | None = None
    include_sources: str | None = None
    exclude_sources: str | None = None
    schedule_minutes: int = 60
    email_report: bool = False
    workspace_id: uuid.UUID | None = None


class ScoutUpdate(BaseModel):
    name: str | None = None
    topic: str | None = None
    description: str | None = None
    keywords: str | None = None
    include_sources: str | None = None
    exclude_sources: str | None = None
    schedule_minutes: int | None = None
    status: str | None = None
    email_report: bool | None = None


class ScoutResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    workspace_id: uuid.UUID | None = None
    name: str
    topic: str
    description: str | None
    keywords: str | None
    include_sources: str | None = None
    exclude_sources: str | None = None
    schedule_minutes: int
    status: str
    email_report: bool
    last_run_at: datetime | None
    next_run_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Reports ──

class ReportResponse(BaseModel):
    id: uuid.UUID
    scout_id: uuid.UUID
    title: str
    summary: str
    findings: dict | None
    raw_response: str | None
    share_token: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class FollowUpRequest(BaseModel):
    question: str
    report_id: uuid.UUID


# ── Highlights ──

class HighlightCreate(BaseModel):
    selected_text: str
    caption: str | None = None
    color: str = "yellow"


class HighlightResponse(BaseModel):
    id: uuid.UUID
    report_id: uuid.UUID
    user_id: uuid.UUID
    user_name: str | None = None
    user_email: str
    selected_text: str
    caption: str | None
    color: str
    created_at: datetime


# ── Workspaces ──

class WorkspaceCreate(BaseModel):
    name: str


class WorkspaceUpdate(BaseModel):
    name: str | None = None


class WorkspaceMemberAdd(BaseModel):
    email: str
    role: str = "viewer"


class WorkspaceMemberUpdate(BaseModel):
    role: str


class WorkspaceMemberResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    email: str
    name: str | None
    role: str
    created_at: datetime

    model_config = {"from_attributes": True}


class WorkspaceResponse(BaseModel):
    id: uuid.UUID
    name: str
    owner_id: uuid.UUID
    member_count: int = 0
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Notifications ──

class NotificationResponse(BaseModel):
    id: uuid.UUID
    type: str
    title: str
    body: str | None
    link: str | None
    is_read: bool
    metadata_json: dict | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Comments ──

class CommentCreate(BaseModel):
    content: str
    parent_id: uuid.UUID | None = None


class CommentUpdate(BaseModel):
    content: str


class CommentResponse(BaseModel):
    id: uuid.UUID
    report_id: uuid.UUID
    user_id: uuid.UUID
    user_name: str | None = None
    user_email: str
    parent_id: uuid.UUID | None
    content: str
    created_at: datetime
    updated_at: datetime | None

    model_config = {"from_attributes": True}


# ── Reactions ──

ALLOWED_EMOJIS = {"fire", "thumbs_up", "brain", "eyes", "rocket", "hundred"}


class ReactionToggle(BaseModel):
    emoji: str


class ReactionSummary(BaseModel):
    emoji: str
    count: int
    user_reacted: bool


# ── Collections ──

class CollectionCreate(BaseModel):
    name: str
    description: str | None = None
    workspace_id: uuid.UUID | None = None


class CollectionUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class CollectionItemCreate(BaseModel):
    report_id: uuid.UUID
    note: str | None = None


class CollectionResponse(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID | None
    user_id: uuid.UUID
    name: str
    description: str | None
    item_count: int = 0
    created_at: datetime

    model_config = {"from_attributes": True}


class CollectionItemResponse(BaseModel):
    id: uuid.UUID
    collection_id: uuid.UUID
    report_id: uuid.UUID
    report_title: str | None = None
    report_summary: str | None = None
    note: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Workspace Invitations ──

class InvitationResponse(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    email: str
    role: str
    status: str
    created_at: datetime
    expires_at: datetime

    model_config = {"from_attributes": True}


# ── Activity ──

class ActivityEventResponse(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    user_id: uuid.UUID
    user_name: str | None = None
    user_email: str | None = None
    event_type: str
    entity_type: str | None
    description: str
    created_at: datetime

    model_config = {"from_attributes": True}
