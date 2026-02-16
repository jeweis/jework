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
    display_name: str | None = None
    role: str
    created_at: str
    accessible_workspaces: list[str] = Field(default_factory=list)


class LoginResponse(BaseModel):
    token: str
    user: UserResponse


class FeishuStatusResponse(BaseModel):
    enabled: bool
    app_id: str | None = None


class FeishuLoginRequest(BaseModel):
    code: str = Field(min_length=1, max_length=2048)


class FeishuSettingsItem(BaseModel):
    enabled: bool
    app_id: str | None = None
    has_app_secret: bool = False
    base_url: str = "https://open.feishu.cn"
    default_workspace_names: list[str] = Field(default_factory=list)


class UpdateFeishuSettingsRequest(BaseModel):
    enabled: bool | None = None
    app_id: str | None = Field(default=None, max_length=128)
    app_secret: str | None = Field(default=None, max_length=512)
    base_url: str | None = Field(default=None, max_length=256)
    default_workspace_names: list[str] | None = None


class McpAuthInfoResponse(BaseModel):
    mcp_url: str
    workspace_mcp_url_template: str
    has_token: bool
    token_hint: str | None = None
    updated_at: str | None = None


class McpResetTokenResponse(BaseModel):
    token: str
    token_hint: str
    mcp_url: str
    workspace_mcp_url_template: str
    updated_at: str


class McpSettingsItem(BaseModel):
    mcp_enabled: bool
    mcp_base_path: str
    mcp_public_base_url: str | None = None
    kb_enable_vector: bool
    kb_chroma_dir: str
    kb_vector_topk_default: int
    kb_file_max_bytes: int
    kb_read_max_lines: int
    embedding_backend: str
    embedding_base_url: str | None = None
    embedding_model: str | None = None
    embedding_batch_size: int
    has_embedding_api_key: bool
    editable_fields: list[str] = Field(default_factory=list)
    updated_at: str | None = None


class UpdateMcpSettingsRequest(BaseModel):
    mcp_enabled: bool | None = None
    mcp_base_path: str | None = Field(default=None, max_length=128)
    mcp_public_base_url: str | None = Field(default=None, max_length=512)
    kb_enable_vector: bool | None = None
    kb_chroma_dir: str | None = Field(default=None, max_length=512)
    kb_vector_topk_default: int | None = None
    kb_file_max_bytes: int | None = None
    kb_read_max_lines: int | None = None
    embedding_backend: str | None = Field(default=None, max_length=64)
    embedding_base_url: str | None = Field(default=None, max_length=512)
    embedding_model: str | None = Field(default=None, max_length=256)
    embedding_batch_size: int | None = None
    embedding_api_key: str | None = Field(default=None, max_length=1024)
    clear_embedding_api_key: bool | None = None


class CreateMcpIndexJobRequest(BaseModel):
    workspace: str
    mode: str = Field(default="incremental")


class McpIndexJobItem(BaseModel):
    job_id: str
    workspace: str
    mode: str
    status: str
    percent: int
    total_files: int
    total_chunks: int
    processed_chunks: int
    failed_chunks: int
    elapsed_ms: int
    error_message: str | None = None
    created_at: str
    updated_at: str


class McpIndexJobListResponse(BaseModel):
    items: list[McpIndexJobItem]
    total: int
    page: int
    size: int


class McpIndexFailureItem(BaseModel):
    job_id: str
    workspace: str
    path: str
    reason: str
    retry_count: int
    created_at: str


class McpIndexFailureListResponse(BaseModel):
    items: list[McpIndexFailureItem]
    total: int
    page: int
    size: int


class RetryFailedJobsRequest(BaseModel):
    workspace: str | None = None


class RetryFailedJobsResponse(BaseModel):
    items: list[McpIndexJobItem]


class RetryJobFailurePathsRequest(BaseModel):
    paths: list[str] = Field(default_factory=list)


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
