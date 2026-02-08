from datetime import datetime

from pydantic import BaseModel, Field


class WorkspaceItem(BaseModel):
    name: str
    path: str
    note: str | None = None
    git_url: str | None = None
    git_username: str | None = None
    has_git_pat: bool = False
    last_pull_at: str | None = None
    last_pull_status: str | None = None


class WorkspaceListResponse(BaseModel):
    items: list[WorkspaceItem]


class CreateWorkspaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    git_url: str | None = None
    git_username: str | None = None
    git_pat: str | None = None


class UpdateWorkspaceCredentialRequest(BaseModel):
    git_username: str | None = None
    git_pat: str | None = None


class WorkspaceCredentialItem(BaseModel):
    workspace: str
    git_url: str | None = None
    git_username: str | None = None
    has_git_pat: bool = False


class UpdateWorkspaceNoteRequest(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class WorkspaceNoteItem(BaseModel):
    workspace: str
    note: str | None = None
    updated_at: str | None = None


class WorkspacePullResponse(BaseModel):
    workspace: str
    before_commit: str | None
    after_commit: str | None
    changed: bool
    summary: str
    pulled_at: str


class WorkspaceDeleteResponse(BaseModel):
    workspace: str
    removed_sessions: int
    deleted_at: str


class CreateSessionRequest(BaseModel):
    workspace: str


class CreateSessionResponse(BaseModel):
    session_id: str
    workspace: str
    created_at: datetime


class SessionMessageItem(BaseModel):
    role: str
    text: str
    created_at: datetime


class SessionDetailResponse(BaseModel):
    session_id: str
    workspace: str
    created_at: datetime
    messages: list[SessionMessageItem]


class SessionSummaryItem(BaseModel):
    session_id: str
    workspace: str
    created_at: datetime
    updated_at: datetime
    message_count: int
    last_message_preview: str


class SessionListResponse(BaseModel):
    items: list[SessionSummaryItem]


class SendMessageRequest(BaseModel):
    message: str


class StreamChunk(BaseModel):
    type: str
    data: str


class ErrorResponse(BaseModel):
    error: dict


class BootstrapStatusResponse(BaseModel):
    requires_setup: bool


class BootstrapRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=6, max_length=128)


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=6, max_length=128)


class UserResponse(BaseModel):
    id: int
    username: str
    role: str
    created_at: str
    accessible_workspaces: list[str] = Field(default_factory=list)


class LoginResponse(BaseModel):
    token: str
    user: UserResponse


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=6, max_length=128)
    workspace_names: list[str] = Field(default_factory=list)


class UserListResponse(BaseModel):
    items: list[UserResponse]


class UpdateUserWorkspaceAccessRequest(BaseModel):
    workspace_names: list[str] = Field(default_factory=list)


class LlmConfigBase(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    base_url: str | None = None
    auth_token: str | None = None
    model: str | None = None
    default_sonnet_model: str | None = None
    default_haiku_model: str | None = None
    default_opus_model: str | None = None


class CreateLlmConfigRequest(LlmConfigBase):
    pass


class UpdateLlmConfigRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    base_url: str | None = None
    auth_token: str | None = None
    model: str | None = None
    default_sonnet_model: str | None = None
    default_haiku_model: str | None = None
    default_opus_model: str | None = None


class LlmConfigItem(BaseModel):
    id: int
    name: str
    base_url: str | None
    has_auth_token: bool
    model: str | None
    default_sonnet_model: str | None
    default_haiku_model: str | None
    default_opus_model: str | None
    is_active: bool
    created_at: str
    updated_at: str


class LlmConfigListResponse(BaseModel):
    items: list[LlmConfigItem]


class MarkdownNodeItem(BaseModel):
    type: str
    name: str
    path: str
    size: int | None = None
    mtime: str | None = None
    children: list["MarkdownNodeItem"] | None = None


class MarkdownIndexResponse(BaseModel):
    workspace: str
    generated_at: str
    items: list[MarkdownNodeItem]


class MarkdownContentResponse(BaseModel):
    workspace: str
    path: str
    name: str
    size: int
    mtime: str
    content: str


MarkdownNodeItem.model_rebuild()
