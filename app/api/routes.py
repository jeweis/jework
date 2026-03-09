import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from app.api.deps import get_current_user
from app.core.errors import AppError, AuthForbiddenError, WorkspaceNotFoundError
from app.models.schemas import (
    AdminResetUserPasswordRequest,
    BootstrapRequest,
    BootstrapStatusResponse,
    SetLocalPasswordRequest,
    FeishuLoginRequest,
    FeishuSettingsItem,
    FeishuStatusResponse,
    CreateLlmConfigRequest,
    CreateSessionRequest,
    CreateSessionResponse,
    DeleteSessionResponse,
    CreateUserRequest,
    CreateWorkspaceRequest,
    FileContentResponse,
    FileIndexResponse,
    FileNodeItem,
    LlmConfigItem,
    LlmConfigListResponse,
    MarkdownContentResponse,
    MarkdownIndexResponse,
    MarkdownNodeItem,
    LoginRequest,
    LoginResponse,
    SendMessageRequest,
    SessionRunResponse,
    SessionRunItem,
    SessionDetailResponse,
    SessionListResponse,
    SessionMessageItem,
    SessionSummaryItem,
    UpdateLlmConfigRequest,
    UpdateUserWorkspaceAccessRequest,
    UpdateFeishuSettingsRequest,
    UpdateWorkspaceCredentialRequest,
    UpdateWorkspaceAgentProfileRequest,
    UpdateWorkspaceNoteRequest,
    UserListResponse,
    UserResponse,
    WorkspaceCredentialItem,
    WorkspaceDeleteResponse,
    WorkspaceItem,
    WorkspaceAgentProfileItem,
    WorkspaceListResponse,
    WorkspaceNoteItem,
    WorkspacePullResponse,
    WorkspaceSkillItem,
)
from app.services.agent_service import (
    DEFAULT_PERSONAL_WRITE_TOOLS,
    DEFAULT_READ_ONLY_TOOLS,
    stream_agent_response,
)
from app.services.auth_service import AuthUser, auth_service
from app.services.feishu_auth_service import feishu_auth_service
from app.services.feishu_settings_service import feishu_settings_service
from app.services.llm_config_service import llm_config_service
from app.services.markdown_service import markdown_service
from app.services.file_preview_service import file_preview_service
from app.services.mcp_index_job_service import mcp_index_job_service
from app.services.mcp_settings_service import mcp_settings_service
from app.services.personal_agent_service import personal_agent_service
from app.services.session_run_service import TERMINAL_RUN_STATUS, session_run_service
from app.services.session_service import session_service
from app.services.workspace_credential_service import workspace_credential_service
from app.services.workspace_git_service import workspace_git_service
from app.services.workspace_note_service import workspace_note_service
from app.services.workspace_agent_profile_service import workspace_agent_profile_service
from app.services.workspace_service import workspace_service

router = APIRouter()
logger = logging.getLogger(__name__)
_run_tasks: dict[str, asyncio.Task[None]] = {}
_WORKSPACE_WRITE_TOOLS = {"write", "edit", "multiedit", "notebookedit"}


@router.get("/auth/bootstrap-status", response_model=BootstrapStatusResponse)
def bootstrap_status() -> BootstrapStatusResponse:
    return BootstrapStatusResponse(requires_setup=auth_service.requires_bootstrap())


@router.post("/auth/bootstrap", response_model=UserResponse)
def bootstrap(body: BootstrapRequest) -> UserResponse:
    user = auth_service.bootstrap_superadmin(body.username, body.password)
    return UserResponse(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        created_at=user.created_at,
        has_local_password=user.has_local_password,
        accessible_workspaces=[],
    )


@router.post("/auth/login", response_model=LoginResponse)
def login(body: LoginRequest) -> LoginResponse:
    token, user = auth_service.login(body.username, body.password)
    accessible = auth_service.get_accessible_workspaces(user)
    return LoginResponse(
        token=token,
        user=UserResponse(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            role=user.role,
            created_at=user.created_at,
            has_local_password=user.has_local_password,
            accessible_workspaces=accessible,
        ),
    )


@router.get("/auth/feishu/status", response_model=FeishuStatusResponse)
def feishu_status() -> FeishuStatusResponse:
    status = feishu_settings_service.get_public_status()
    return FeishuStatusResponse(enabled=status.enabled, app_id=status.app_id)


@router.post("/auth/feishu/login", response_model=LoginResponse)
def feishu_login(body: FeishuLoginRequest) -> LoginResponse:
    config = feishu_settings_service.assert_login_enabled()
    user_access_token = feishu_auth_service.exchange_code_v2(
        base_url=config.base_url,
        app_id=config.app_id or "",
        app_secret=config.app_secret or "",
        code=body.code,
    )
    user_info = feishu_auth_service.get_user_info(
        base_url=config.base_url,
        user_access_token=user_access_token,
    )
    token, user, _ = auth_service.login_by_feishu(
        union_id=user_info.union_id,
        open_id=user_info.open_id,
        name=user_info.name,
        avatar_url=user_info.avatar_url,
        default_workspace_names=config.default_workspace_names,
    )
    accessible = auth_service.get_accessible_workspaces(user)
    return LoginResponse(
        token=token,
        user=UserResponse(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            role=user.role,
            created_at=user.created_at,
            has_local_password=user.has_local_password,
            accessible_workspaces=accessible,
        ),
    )


@router.get("/auth/me", response_model=UserResponse)
def get_me(current_user: AuthUser = Depends(get_current_user)) -> UserResponse:
    accessible = auth_service.get_accessible_workspaces(current_user)
    return UserResponse(
        id=current_user.id,
        username=current_user.username,
        display_name=current_user.display_name,
        role=current_user.role,
        created_at=current_user.created_at,
        has_local_password=current_user.has_local_password,
        accessible_workspaces=accessible,
    )


@router.post("/auth/password/set")
def set_local_password(
    body: SetLocalPasswordRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> dict[str, str]:
    auth_service.set_local_password(
        current_user=current_user,
        new_password=body.password,
    )
    return {"status": "ok"}


@router.get("/users", response_model=UserListResponse)
def list_users(current_user: AuthUser = Depends(get_current_user)) -> UserListResponse:
    users = auth_service.list_users(current_user)
    return UserListResponse(
        items=[
            UserResponse(
                id=user.id,
                username=user.username,
                display_name=user.display_name,
                role=user.role,
                created_at=user.created_at,
                has_local_password=user.has_local_password,
                accessible_workspaces=user.accessible_workspaces or [],
            )
            for user in users
        ]
    )


@router.get("/admin/feishu/settings", response_model=FeishuSettingsItem)
def get_feishu_settings(
    current_user: AuthUser = Depends(get_current_user),
) -> FeishuSettingsItem:
    if current_user.role != "superadmin":
        raise AuthForbiddenError()
    item = feishu_settings_service.get_settings_view()
    return FeishuSettingsItem(
        enabled=item.enabled,
        app_id=item.app_id,
        has_app_secret=item.has_app_secret,
        base_url=item.base_url,
        default_workspace_names=item.default_workspace_names,
    )


@router.put("/admin/feishu/settings", response_model=FeishuSettingsItem)
def update_feishu_settings(
    body: UpdateFeishuSettingsRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> FeishuSettingsItem:
    if current_user.role != "superadmin":
        raise AuthForbiddenError()
    item = feishu_settings_service.update_settings(
        enabled=body.enabled,
        app_id=body.app_id,
        app_secret=body.app_secret,
        base_url=body.base_url,
        default_workspace_names=body.default_workspace_names,
    )
    return FeishuSettingsItem(
        enabled=item.enabled,
        app_id=item.app_id,
        has_app_secret=item.has_app_secret,
        base_url=item.base_url,
        default_workspace_names=item.default_workspace_names,
    )


@router.post("/users", response_model=UserResponse)
def create_user(
    body: CreateUserRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> UserResponse:
    normalized = sorted(set(body.workspace_names))
    normalized_ids: list[str] = []
    for workspace in normalized:
        meta = workspace_service.get_workspace_meta(workspace)
        if meta.mode != "team":
            raise AppError(
                code="WORKSPACE_ASSIGNMENT_FORBIDDEN",
                message="Only team workspaces can be assigned in user ACL",
                details={"workspace": workspace, "mode": meta.mode},
                status_code=400,
            )
        normalized_ids.append(meta.workspace_id)
    user = auth_service.create_user(
        current_user=current_user,
        username=body.username,
        password=body.password,
        workspace_names=sorted(set(normalized_ids)),
    )
    return UserResponse(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        created_at=user.created_at,
        has_local_password=user.has_local_password,
        accessible_workspaces=user.accessible_workspaces or [],
    )


@router.put("/users/{user_id}/workspaces", response_model=UserResponse)
def update_user_workspaces(
    user_id: int,
    body: UpdateUserWorkspaceAccessRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> UserResponse:
    normalized = sorted(set(body.workspace_names))
    normalized_ids: list[str] = []
    for workspace in normalized:
        meta = workspace_service.get_workspace_meta(workspace)
        if meta.mode != "team":
            raise AppError(
                code="WORKSPACE_ASSIGNMENT_FORBIDDEN",
                message="Only team workspaces can be assigned in user ACL",
                details={"workspace": workspace, "mode": meta.mode},
                status_code=400,
            )
        normalized_ids.append(meta.workspace_id)
    accessible = auth_service.set_user_workspace_access(
        current_user=current_user,
        user_id=user_id,
        workspace_names=sorted(set(normalized_ids)),
    )
    users = auth_service.list_users(current_user)
    target = next((item for item in users if item.id == user_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    return UserResponse(
        id=target.id,
        username=target.username,
        display_name=target.display_name,
        role=target.role,
        created_at=target.created_at,
        has_local_password=target.has_local_password,
        accessible_workspaces=[] if target.role == "superadmin" else accessible,
    )


@router.post("/admin/users/{user_id}/password/reset")
def admin_reset_user_password(
    user_id: int,
    body: AdminResetUserPasswordRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> dict[str, str]:
    auth_service.admin_reset_user_password(
        current_user=current_user,
        user_id=user_id,
        new_password=body.password,
    )
    return {"status": "ok"}


@router.get("/llm-configs", response_model=LlmConfigListResponse)
def list_llm_configs(
    current_user: AuthUser = Depends(get_current_user),
) -> LlmConfigListResponse:
    items = llm_config_service.list_configs(current_user)
    return LlmConfigListResponse(
        items=[
            LlmConfigItem(
                id=item.id,
                name=item.name,
                base_url=item.base_url,
                has_auth_token=bool(item.auth_token),
                model=item.model,
                default_sonnet_model=item.default_sonnet_model,
                default_haiku_model=item.default_haiku_model,
                default_opus_model=item.default_opus_model,
                is_active=item.is_active,
                created_at=item.created_at,
                updated_at=item.updated_at,
            )
            for item in items
        ]
    )


@router.post("/llm-configs", response_model=LlmConfigItem)
def create_llm_config(
    body: CreateLlmConfigRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> LlmConfigItem:
    item = llm_config_service.create_config(
        current_user,
        name=body.name,
        base_url=body.base_url,
        auth_token=body.auth_token,
        model=body.model,
        default_sonnet_model=body.default_sonnet_model,
        default_haiku_model=body.default_haiku_model,
        default_opus_model=body.default_opus_model,
    )
    return LlmConfigItem(
        id=item.id,
        name=item.name,
        base_url=item.base_url,
        has_auth_token=bool(item.auth_token),
        model=item.model,
        default_sonnet_model=item.default_sonnet_model,
        default_haiku_model=item.default_haiku_model,
        default_opus_model=item.default_opus_model,
        is_active=item.is_active,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


@router.put("/llm-configs/{config_id}", response_model=LlmConfigItem)
def update_llm_config(
    config_id: int,
    body: UpdateLlmConfigRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> LlmConfigItem:
    item = llm_config_service.update_config(
        current_user,
        config_id,
        name=body.name,
        base_url=body.base_url,
        auth_token=body.auth_token,
        model=body.model,
        default_sonnet_model=body.default_sonnet_model,
        default_haiku_model=body.default_haiku_model,
        default_opus_model=body.default_opus_model,
    )
    return LlmConfigItem(
        id=item.id,
        name=item.name,
        base_url=item.base_url,
        has_auth_token=bool(item.auth_token),
        model=item.model,
        default_sonnet_model=item.default_sonnet_model,
        default_haiku_model=item.default_haiku_model,
        default_opus_model=item.default_opus_model,
        is_active=item.is_active,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


@router.post("/llm-configs/{config_id}/activate", response_model=LlmConfigItem)
def activate_llm_config(
    config_id: int,
    current_user: AuthUser = Depends(get_current_user),
) -> LlmConfigItem:
    item = llm_config_service.activate_config(current_user, config_id)
    return LlmConfigItem(
        id=item.id,
        name=item.name,
        base_url=item.base_url,
        has_auth_token=bool(item.auth_token),
        model=item.model,
        default_sonnet_model=item.default_sonnet_model,
        default_haiku_model=item.default_haiku_model,
        default_opus_model=item.default_opus_model,
        is_active=item.is_active,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


@router.delete("/llm-configs/{config_id}")
def delete_llm_config(
    config_id: int,
    current_user: AuthUser = Depends(get_current_user),
) -> dict[str, str]:
    llm_config_service.delete_config(current_user, config_id)
    return {"status": "ok"}


@router.get("/workspaces", response_model=WorkspaceListResponse)
def list_workspaces(
    current_user: AuthUser = Depends(get_current_user),
) -> WorkspaceListResponse:
    accessible = auth_service.get_accessible_workspaces(current_user)
    allowed = None if current_user.role == "superadmin" else set(accessible)
    return WorkspaceListResponse(items=workspace_service.list_workspaces(allowed))


@router.post("/workspaces", response_model=WorkspaceItem)
def create_workspace(
    body: CreateWorkspaceRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> WorkspaceItem:
    mode = body.mode.strip().lower()
    if mode == "team":
        if current_user.role != "superadmin":
            raise AuthForbiddenError()
        owner_user_id: int | None = None
    elif mode == "personal":
        owner_user_id = current_user.id
    else:
        raise AppError(
            code="WORKSPACE_MODE_INVALID",
            message="workspace mode must be team or personal",
            details={"mode": body.mode},
            status_code=400,
        )

    return workspace_service.create_workspace(
        workspace=body.name,
        mode=mode,
        git_url=body.git_url,
        git_username=body.git_username,
        git_pat=body.git_pat,
        creator_user_id=current_user.id,
        owner_user_id=owner_user_id,
    )


@router.delete("/workspaces/{workspace}", response_model=WorkspaceDeleteResponse)
def delete_workspace(
    workspace: str,
    current_user: AuthUser = Depends(get_current_user),
) -> WorkspaceDeleteResponse:
    meta = workspace_service.get_workspace_meta(workspace)
    if meta.mode == "team" and current_user.role != "superadmin":
        raise AuthForbiddenError()
    if meta.mode == "personal":
        if current_user.role != "superadmin" and meta.owner_user_id != current_user.id:
            raise AuthForbiddenError()

    result = workspace_service.delete_workspace(workspace)
    removed_sessions = (
        session_service.delete_workspace_sessions(meta.workspace_id)
        + session_service.delete_workspace_sessions(meta.workspace_name)
    )
    workspace_credential_service.delete_workspace_credential(meta.workspace_id)
    workspace_credential_service.delete_workspace_credential(meta.workspace_name)
    workspace_git_service.delete_sync_meta(meta.workspace_id)
    workspace_git_service.delete_sync_meta(meta.workspace_name)
    workspace_note_service.delete_note(meta.workspace_id)
    workspace_note_service.delete_note(meta.workspace_name)
    # 同时按 workspace_id 与 workspace_name 清理 ACL，避免注册表删除后遗留脏授权。
    # 否则会出现 can_access_workspace=True 但 resolve_workspace_reference=not found 的异常状态。
    auth_service.remove_workspace_access_for_all_users(meta.workspace_id)
    auth_service.remove_workspace_access_for_all_users(meta.workspace_name)

    return WorkspaceDeleteResponse(
        workspace=workspace,
        removed_sessions=removed_sessions,
        deleted_at=result.deleted_at,
    )


@router.get("/workspaces/{workspace}/credential", response_model=WorkspaceCredentialItem)
def get_workspace_credential(
    workspace: str,
    current_user: AuthUser = Depends(get_current_user),
) -> WorkspaceCredentialItem:
    meta = workspace_service.get_workspace_meta(workspace)
    if meta.mode == "team" and current_user.role != "superadmin":
        raise AuthForbiddenError()
    if meta.mode == "personal":
        if current_user.role != "superadmin" and meta.owner_user_id != current_user.id:
            raise AuthForbiddenError()
    workspace_service.get_workspace_path(meta.workspace_id)
    item = workspace_credential_service.get_workspace_credential(meta.workspace_id)
    if item is None:
        # 兼容历史 name 键。
        item = workspace_credential_service.get_workspace_credential(meta.workspace_name)
    if item is None:
        return WorkspaceCredentialItem(workspace=meta.workspace_name)
    return WorkspaceCredentialItem(
        workspace=meta.workspace_name,
        git_url=item.git_url,
        git_username=item.git_username,
        has_git_pat=item.has_git_pat,
    )


@router.put("/workspaces/{workspace}/note", response_model=WorkspaceNoteItem)
def update_workspace_note(
    workspace: str,
    body: UpdateWorkspaceNoteRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> WorkspaceNoteItem:
    meta = workspace_service.get_workspace_meta(workspace)
    if meta.mode == "team" and current_user.role != "superadmin":
        raise AuthForbiddenError()
    if meta.mode == "personal":
        if current_user.role != "superadmin" and meta.owner_user_id != current_user.id:
            raise AuthForbiddenError()
    workspace_service.get_workspace_path(meta.workspace_id)
    updated_at = datetime.now(timezone.utc).isoformat()
    item = workspace_note_service.upsert_note(
        workspace=meta.workspace_id,
        note=body.note,
        updated_at=updated_at,
    )
    return WorkspaceNoteItem(
        workspace=meta.workspace_name,
        note=item.note,
        updated_at=item.updated_at,
    )


@router.put("/workspaces/{workspace}/credential", response_model=WorkspaceCredentialItem)
def update_workspace_credential(
    workspace: str,
    body: UpdateWorkspaceCredentialRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> WorkspaceCredentialItem:
    meta = workspace_service.get_workspace_meta(workspace)
    if meta.mode == "team" and current_user.role != "superadmin":
        raise AuthForbiddenError()
    if meta.mode == "personal":
        if current_user.role != "superadmin" and meta.owner_user_id != current_user.id:
            raise AuthForbiddenError()
    workspace_service.get_workspace_path(meta.workspace_id)
    workspace_credential_service.upsert_workspace_credential(
        workspace=meta.workspace_id,
        user_id=current_user.id,
        git_username=body.git_username,
        git_pat=body.git_pat,
    )
    item = workspace_credential_service.get_workspace_credential(meta.workspace_id)
    if item is None:
        return WorkspaceCredentialItem(workspace=meta.workspace_name)
    return WorkspaceCredentialItem(
        workspace=meta.workspace_name,
        git_url=item.git_url,
        git_username=item.git_username,
        has_git_pat=item.has_git_pat,
    )


@router.get(
    "/workspaces/{workspace}/agent-profile",
    response_model=WorkspaceAgentProfileItem,
)
def get_workspace_agent_profile(
    workspace: str,
    current_user: AuthUser = Depends(get_current_user),
) -> WorkspaceAgentProfileItem:
    meta, workspace_path = _require_personal_workspace_manage_access(
        workspace,
        current_user,
    )
    profile = workspace_agent_profile_service.get_profile(meta.workspace_id)
    skills = workspace_agent_profile_service.list_skills(workspace_path)
    return _to_workspace_agent_profile_item(meta, profile, skills)


@router.put(
    "/workspaces/{workspace}/agent-profile",
    response_model=WorkspaceAgentProfileItem,
)
def update_workspace_agent_profile(
    workspace: str,
    body: UpdateWorkspaceAgentProfileRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> WorkspaceAgentProfileItem:
    meta, workspace_path = _require_personal_workspace_manage_access(
        workspace,
        current_user,
    )
    profile = workspace_agent_profile_service.upsert_profile(
        workspace_id=meta.workspace_id,
        mcp_servers=[item.model_dump() for item in body.mcp_servers],
        extra_allowed_tools=body.extra_allowed_tools,
        updated_by=current_user.id,
    )
    skills = workspace_agent_profile_service.list_skills(workspace_path)
    return _to_workspace_agent_profile_item(meta, profile, skills)


@router.post(
    "/workspaces/{workspace}/skills/upload",
    response_model=WorkspaceAgentProfileItem,
)
async def upload_workspace_skill(
    workspace: str,
    file: UploadFile = File(...),
    current_user: AuthUser = Depends(get_current_user),
) -> WorkspaceAgentProfileItem:
    meta, workspace_path = _require_personal_workspace_manage_access(
        workspace,
        current_user,
    )
    raw = await file.read()
    workspace_agent_profile_service.upload_skill(
        workspace_path=workspace_path,
        filename=file.filename or "",
        content=raw,
    )
    profile = workspace_agent_profile_service.get_profile(meta.workspace_id)
    skills = workspace_agent_profile_service.list_skills(workspace_path)
    return _to_workspace_agent_profile_item(meta, profile, skills)


@router.delete(
    "/workspaces/{workspace}/skills/{skill_name}",
    response_model=WorkspaceAgentProfileItem,
)
def delete_workspace_skill(
    workspace: str,
    skill_name: str,
    current_user: AuthUser = Depends(get_current_user),
) -> WorkspaceAgentProfileItem:
    meta, workspace_path = _require_personal_workspace_manage_access(
        workspace,
        current_user,
    )
    workspace_agent_profile_service.delete_skill(
        workspace_path=workspace_path,
        skill_name=skill_name,
    )
    profile = workspace_agent_profile_service.get_profile(meta.workspace_id)
    skills = workspace_agent_profile_service.list_skills(workspace_path)
    return _to_workspace_agent_profile_item(meta, profile, skills)


@router.get("/workspaces/{workspace}/markdown-index", response_model=MarkdownIndexResponse)
def get_workspace_markdown_index(
    workspace: str,
    current_user: AuthUser = Depends(get_current_user),
) -> MarkdownIndexResponse:
    workspace_path = _require_workspace_access(workspace, current_user)
    nodes = markdown_service.build_index(workspace=workspace, workspace_path=workspace_path)
    return MarkdownIndexResponse(
        workspace=workspace,
        generated_at=datetime.now(timezone.utc).isoformat(),
        items=[_to_markdown_node_item(node) for node in nodes],
    )


@router.get("/workspaces/{workspace}/file-index", response_model=FileIndexResponse)
def get_workspace_file_index(
    workspace: str,
    current_user: AuthUser = Depends(get_current_user),
) -> FileIndexResponse:
    """
    个人工作空间文件索引（新文件预览功能使用）。
    """
    _, workspace_path = _require_personal_workspace_manage_access(workspace, current_user)
    nodes = file_preview_service.build_index(workspace_path=workspace_path)
    return FileIndexResponse(
        workspace=workspace,
        generated_at=datetime.now(timezone.utc).isoformat(),
        items=[_to_file_node_item(node) for node in nodes],
    )


@router.get("/workspaces/{workspace}/file-content", response_model=FileContentResponse)
def get_workspace_file_content(
    workspace: str,
    path: str,
    current_user: AuthUser = Depends(get_current_user),
) -> FileContentResponse:
    """
    个人工作空间文件内容读取，支持 markdown/code/text/binary 识别。
    """
    _, workspace_path = _require_personal_workspace_manage_access(workspace, current_user)
    settings_value = mcp_settings_service.get_settings()
    content = file_preview_service.read_file_content(
        workspace=workspace,
        workspace_path=workspace_path,
        relative_path=path,
        max_bytes=settings_value.kb_file_max_bytes,
        max_lines=settings_value.kb_read_max_lines,
    )
    return FileContentResponse(
        workspace=content.workspace,
        path=content.path,
        name=content.name,
        size=content.size,
        mtime=content.mtime,
        content_type=content.content_type,
        content=content.content,
        is_binary=content.is_binary,
        truncated=content.truncated,
    )


@router.get("/workspaces/{workspace}/file-download")
def download_workspace_file(
    workspace: str,
    path: str,
    current_user: AuthUser = Depends(get_current_user),
):
    """
    下载个人工作空间中的任意文件（原始字节，不受预览截断限制）。
    """
    _, workspace_path = _require_personal_workspace_manage_access(workspace, current_user)
    target = file_preview_service.resolve_existing_file(
        workspace_path=workspace_path,
        relative_path=path,
    )
    return FileResponse(
        path=str(target),
        filename=target.name,
        media_type="application/octet-stream",
    )


@router.get(
    "/workspaces/{workspace}/markdown-content",
    response_model=MarkdownContentResponse,
)
def get_workspace_markdown_content(
    workspace: str,
    path: str,
    current_user: AuthUser = Depends(get_current_user),
) -> MarkdownContentResponse:
    workspace_path = _require_workspace_access(workspace, current_user)
    content = markdown_service.read_markdown_content(
        workspace=workspace,
        workspace_path=workspace_path,
        relative_path=path,
    )
    return MarkdownContentResponse(
        workspace=content.workspace,
        path=content.path,
        name=content.name,
        size=content.size,
        mtime=content.mtime,
        content=content.content,
    )


@router.post("/workspaces/{workspace}/git/pull", response_model=WorkspacePullResponse)
def pull_workspace_git(
    workspace: str,
    current_user: AuthUser = Depends(get_current_user),
) -> WorkspacePullResponse:
    _require_workspace_access(workspace, current_user)
    result = workspace_service.pull_workspace(workspace)
    # Pull 成功后触发增量索引，让向量库尽快与最新代码对齐。
    try:
        if result.changed and mcp_settings_service.get_settings().mcp_enabled:
            mcp_index_job_service.create_job(
                user_id=current_user.id,
                workspace=workspace,
                mode="incremental",
            )
    except Exception as exc:
        logger.warning(
            "auto incremental index schedule failed workspace=%s reason=%s",
            workspace,
            exc,
        )
    return WorkspacePullResponse(
        workspace=result.workspace,
        before_commit=result.before_commit,
        after_commit=result.after_commit,
        changed=result.changed,
        summary=result.summary,
        pulled_at=result.pulled_at,
    )


@router.post("/sessions", response_model=CreateSessionResponse)
def create_session(
    body: CreateSessionRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> CreateSessionResponse:
    workspace_path = _require_workspace_access(body.workspace, current_user)
    session = session_service.create_session(
        user_id=current_user.id,
        workspace=body.workspace,
        workspace_path=workspace_path,
    )
    return CreateSessionResponse(
        session_id=session.session_id,
        workspace=session.workspace,
        scope=session.scope,
        created_at=session.created_at,
    )


@router.post("/personal-agent/sessions", response_model=CreateSessionResponse)
def create_personal_agent_session(
    current_user: AuthUser = Depends(get_current_user),
) -> CreateSessionResponse:
    bootstrap = personal_agent_service.ensure_main_agent_workspace(
        user_id=current_user.id,
        username=current_user.username,
    )
    session = session_service.create_personal_agent_session(
        user_id=current_user.id,
        workspace_path=bootstrap.main_agent_root,
    )
    return CreateSessionResponse(
        session_id=session.session_id,
        workspace=session.workspace,
        scope=session.scope,
        created_at=session.created_at,
    )


@router.get("/workspaces/{workspace}/sessions", response_model=SessionListResponse)
def list_workspace_sessions(
    workspace: str,
    current_user: AuthUser = Depends(get_current_user),
) -> SessionListResponse:
    _require_workspace_access(workspace, current_user)
    sessions = session_service.list_workspace_sessions(
        user_id=current_user.id,
        workspace=workspace,
    )
    return SessionListResponse(
        items=[
            SessionSummaryItem(
                session_id=session.session_id,
                workspace=session.workspace,
                scope=session.scope,
                created_at=session.created_at,
                updated_at=session.updated_at,
                message_count=len(session.messages),
                last_message_preview=session.messages[-1].text[:120]
                if session.messages
                else "",
            )
            for session in sessions
        ]
    )


@router.get("/personal-agent/sessions", response_model=SessionListResponse)
def list_personal_agent_sessions(
    current_user: AuthUser = Depends(get_current_user),
) -> SessionListResponse:
    sessions = session_service.list_personal_agent_sessions(user_id=current_user.id)
    return SessionListResponse(
        items=[
            SessionSummaryItem(
                session_id=session.session_id,
                workspace=session.workspace,
                scope=session.scope,
                created_at=session.created_at,
                updated_at=session.updated_at,
                message_count=len(session.messages),
                last_message_preview=session.messages[-1].text[:120]
                if session.messages
                else "",
            )
            for session in sessions
        ]
    )


@router.get("/workspaces/{workspace}/sessions/latest", response_model=SessionSummaryItem)
def get_latest_workspace_session(
    workspace: str,
    current_user: AuthUser = Depends(get_current_user),
) -> SessionSummaryItem:
    _require_workspace_access(workspace, current_user)
    session = session_service.get_latest_workspace_session(
        user_id=current_user.id,
        workspace=workspace,
    )
    if session is None:
        raise HTTPException(status_code=404, detail="No session found for workspace")
    return SessionSummaryItem(
        session_id=session.session_id,
        workspace=session.workspace,
        scope=session.scope,
        created_at=session.created_at,
        updated_at=session.updated_at,
        message_count=len(session.messages),
        last_message_preview=session.messages[-1].text[:120] if session.messages else "",
    )


@router.get("/personal-agent/sessions/latest", response_model=SessionSummaryItem)
def get_latest_personal_agent_session(
    current_user: AuthUser = Depends(get_current_user),
) -> SessionSummaryItem:
    session = session_service.get_latest_personal_agent_session(user_id=current_user.id)
    if session is None:
        raise HTTPException(status_code=404, detail="No personal-agent session found")
    return SessionSummaryItem(
        session_id=session.session_id,
        workspace=session.workspace,
        scope=session.scope,
        created_at=session.created_at,
        updated_at=session.updated_at,
        message_count=len(session.messages),
        last_message_preview=session.messages[-1].text[:120] if session.messages else "",
    )


@router.get("/sessions/{session_id}", response_model=SessionDetailResponse)
def get_session_detail(
    session_id: str,
    current_user: AuthUser = Depends(get_current_user),
) -> SessionDetailResponse:
    session = session_service.get_session(session_id, user_id=current_user.id)
    messages = session_service.list_messages(session_id, user_id=current_user.id)
    return SessionDetailResponse(
        session_id=session.session_id,
        workspace=session.workspace,
        scope=session.scope,
        created_at=session.created_at,
        messages=[
            SessionMessageItem(
                role=message.role,
                text=message.text,
                created_at=message.created_at,
            )
            for message in messages
        ],
    )


@router.delete("/sessions/{session_id}", response_model=DeleteSessionResponse)
def delete_session(
    session_id: str,
    current_user: AuthUser = Depends(get_current_user),
) -> DeleteSessionResponse:
    # 正在执行中的会话不允许删除，避免后台任务继续写入被删除的会话。
    if session_run_service.has_running_run(
        session_id=session_id,
        user_id=current_user.id,
    ):
        raise AppError(
            code="SESSION_RUN_IN_PROGRESS",
            message="Session run still in progress",
            details={"session_id": session_id},
            status_code=409,
        )

    session = session_service.delete_session(session_id=session_id, user_id=current_user.id)
    removed_runs = session_run_service.delete_session_runs(
        session_id=session_id,
        user_id=current_user.id,
    )
    return DeleteSessionResponse(
        session_id=session.session_id,
        workspace=session.workspace,
        scope=session.scope,
        deleted_at=datetime.now(timezone.utc).isoformat(),
        removed_runs=removed_runs,
    )


def _to_session_run_item(run) -> SessionRunItem:
    return SessionRunItem(
        run_id=run.run_id,
        session_id=run.session_id,
        status=run.status,
        last_seq=run.last_seq,
        created_at=run.created_at,
        updated_at=run.updated_at,
        error_message=run.error_message,
    )


def _register_background_task(run_id: str, task: asyncio.Task[None]) -> None:
    """
    跟踪后台任务引用，避免任务对象被提前释放。
    """
    _run_tasks[run_id] = task

    def _cleanup(_: asyncio.Task[None]) -> None:
        _run_tasks.pop(run_id, None)

    task.add_done_callback(_cleanup)


async def _execute_session_run(
    *,
    session_id: str,
    run_id: str,
    user_id: int,
    prompt: str,
    workspace: str,
    workspace_path: str,
    workspace_mode: str,
    claude_session_id: str | None,
    runtime_env: dict[str, str],
    mcp_servers: dict[str, dict[str, object]],
    allowed_tools: list[str],
) -> None:
    """
    在后台执行一次 run，并持续把中间输出写入事件日志。
    """
    session_run_service.set_run_status(
        session_id=session_id,
        run_id=run_id,
        user_id=user_id,
        status="running",
    )

    chunks: list[str] = []
    has_workspace_change = False
    try:
        async for event in stream_agent_response(
            prompt,
            workspace_path,
            env=runtime_env,
            resume_session_id=claude_session_id,
            on_claude_session_id=lambda claude_sid: session_service.set_claude_session_id(
                session_id=session_id,
                user_id=user_id,
                claude_session_id=claude_sid,
            ),
            allowed_tools_override=allowed_tools,
            mcp_servers=mcp_servers,
            enable_workspace_write_guard=(workspace_mode == "personal"),
        ):
            event_type = str(event.get("type", "")).strip()
            if not event_type:
                continue
            data = event.get("data", "")
            title = event.get("title")
            message = event.get("message")
            normalized_data = data if isinstance(data, str) else str(data)
            normalized_title = title if isinstance(title, str) else None
            normalized_message = message if isinstance(message, str) else None
            if event_type == "chunk" and normalized_data:
                chunks.append(normalized_data)
            if event_type == "tool":
                tool_name = _extract_tool_name_from_title(normalized_title)
                if tool_name is not None and tool_name.lower() in _WORKSPACE_WRITE_TOOLS:
                    has_workspace_change = True
            session_run_service.append_event(
                session_id=session_id,
                run_id=run_id,
                user_id=user_id,
                event_type=event_type,
                data=normalized_data,
                title=normalized_title,
                message=normalized_message,
            )
        if has_workspace_change:
            session_run_service.append_event(
                session_id=session_id,
                run_id=run_id,
                user_id=user_id,
                event_type="workspace_dirty",
                data=workspace,
                message="workspace changed by agent tools",
            )
        session_run_service.append_event(
            session_id=session_id,
            run_id=run_id,
            user_id=user_id,
            event_type="done",
            message="completed",
        )
        session_run_service.set_run_status(
            session_id=session_id,
            run_id=run_id,
            user_id=user_id,
            status="done",
        )
    except Exception as exc:
        reason = str(exc)
        if isinstance(exc, AppError) and isinstance(exc.details, dict):
            detail_reason = exc.details.get("reason")
            if isinstance(detail_reason, str) and detail_reason.strip():
                reason = detail_reason.strip()
        logger.exception(
            "Session run failed: session_id=%s run_id=%s user_id=%s",
            session_id,
            run_id,
            user_id,
        )
        session_run_service.append_event(
            session_id=session_id,
            run_id=run_id,
            user_id=user_id,
            event_type="error",
            message=reason,
        )
        session_run_service.set_run_status(
            session_id=session_id,
            run_id=run_id,
            user_id=user_id,
            status="error",
            error_message=reason,
        )
    finally:
        answer = "".join(chunks)
        if answer:
            session_service.append_message(
                session_id=session_id,
                user_id=user_id,
                role="assistant",
                text=answer,
            )


@router.post("/sessions/{session_id}/messages", response_model=SessionRunResponse)
async def send_message(
    session_id: str,
    body: SendMessageRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> SessionRunResponse:
    session = session_service.get_session(session_id, user_id=current_user.id)
    runtime_env = llm_config_service.get_active_env()
    runtime_auth = (
        (runtime_env.get("ANTHROPIC_AUTH_TOKEN") or "").strip()
        or (runtime_env.get("ANTHROPIC_API_KEY") or "").strip()
    )
    if not runtime_auth:
        raise AppError(
            code="LLM_CONFIG_AUTH_REQUIRED",
            message=(
                "Active LLM config auth token is required. "
                "Please configure and activate ANTHROPIC_AUTH_TOKEN first."
            ),
            status_code=400,
        )
    if session.scope == "personal_agent":
        bootstrap = personal_agent_service.ensure_main_agent_workspace(
            user_id=current_user.id,
            username=current_user.username,
        )
        workspace_path = bootstrap.main_agent_root
        workspace_mode = "personal"
        workspace_key = session.workspace
        # personal-agent 改为纯项目级配置透传：
        # - MCP 由 workspace 根目录 .mcp.json 管理
        # - 这里不再代码注入 mcp_servers（避免覆盖项目配置）
        personal_mcp_server_names = ("jework", "personal_project")
        allowed_tools = _merge_allowed_tools(
            DEFAULT_PERSONAL_WRITE_TOOLS,
            [
                f"mcp__{server_name}__list_projects"
                for server_name in personal_mcp_server_names
            ]
            + [
                f"mcp__{server_name}__create_project"
                for server_name in personal_mcp_server_names
            ]
            + [
                f"mcp__{server_name}__get_current_workspace_path"
                for server_name in personal_mcp_server_names
            ],
        )
        workspace_mcp_servers: dict[str, dict[str, object]] = {}
    else:
        # 每次发送前按 workspace 名实时解析路径，兼容历史会话里持久化路径失效。
        workspace_path = _require_workspace_access(session.workspace, current_user)
        workspace_meta = workspace_service.get_workspace_meta(session.workspace)
        profile = workspace_agent_profile_service.get_profile(workspace_meta.workspace_id)
        workspace_mcp_servers = workspace_agent_profile_service.build_sdk_mcp_servers(
            profile.mcp_servers,
            runtime_env,
        )
        derived_mcp_tools = _derive_mcp_allowed_tools(profile.mcp_servers)
        if workspace_meta.mode == "personal":
            allowed_tools = _merge_allowed_tools(
                DEFAULT_PERSONAL_WRITE_TOOLS,
                [*profile.extra_allowed_tools, *derived_mcp_tools],
            )
        else:
            allowed_tools = list(DEFAULT_READ_ONLY_TOOLS)
        workspace_mode = workspace_meta.mode
        workspace_key = session.workspace

    session_service.set_workspace_path(
        session_id=session_id,
        user_id=current_user.id,
        workspace_path=workspace_path,
    )
    running = session_run_service.get_latest_running_run(
        session_id=session_id,
        user_id=current_user.id,
    )
    if running is not None:
        raise AppError(
            code="SESSION_RUN_IN_PROGRESS",
            message="Another run is still in progress for this session",
            details={"session_id": session_id, "run_id": running.run_id},
            status_code=409,
        )
    session_service.append_message(
        session_id,
        user_id=current_user.id,
        role="user",
        text=body.message,
    )

    run = session_run_service.create_run(
        session_id=session_id,
        user_id=current_user.id,
        prompt=body.message,
    )
    task = asyncio.create_task(
        _execute_session_run(
            session_id=session_id,
            run_id=run.run_id,
            user_id=current_user.id,
            prompt=body.message,
            workspace=workspace_key,
            workspace_path=str(workspace_path),
            workspace_mode=workspace_mode,
            claude_session_id=session.claude_session_id,
            runtime_env=runtime_env,
            mcp_servers=workspace_mcp_servers,
            allowed_tools=allowed_tools,
        ),
        name=f"session-run-{run.run_id}",
    )
    _register_background_task(run.run_id, task)
    return SessionRunResponse(item=_to_session_run_item(run))


@router.get(
    "/sessions/{session_id}/runs/running",
    response_model=SessionRunResponse | None,
)
def get_running_session_run(
    session_id: str,
    current_user: AuthUser = Depends(get_current_user),
) -> SessionRunResponse | None:
    session_service.get_session(session_id, user_id=current_user.id)
    run = session_run_service.get_latest_running_run(
        session_id=session_id,
        user_id=current_user.id,
    )
    if run is None:
        return None
    return SessionRunResponse(item=_to_session_run_item(run))


@router.get("/sessions/{session_id}/runs/{run_id}/stream")
async def stream_session_run(
    session_id: str,
    run_id: str,
    request: Request,
    after_seq: int = 0,
    current_user: AuthUser = Depends(get_current_user),
) -> StreamingResponse:
    session_service.get_session(session_id, user_id=current_user.id)
    run = session_run_service.get_run(
        session_id=session_id,
        run_id=run_id,
        user_id=current_user.id,
    )

    async def event_stream():
        # 先回放历史，确保刷新后可以无损恢复到最新可见状态。
        delivered_seq = after_seq
        history = session_run_service.list_events_after(
            session_id=session_id,
            run_id=run_id,
            user_id=current_user.id,
            after_seq=after_seq,
        )
        for item in history:
            delivered_seq = max(delivered_seq, item.seq)
            payload = {
                "seq": item.seq,
                "type": item.type,
                "created_at": item.created_at,
                "data": item.data,
                "title": item.title,
                "message": item.message,
            }
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        if run.status in TERMINAL_RUN_STATUS:
            return

        queue = session_run_service.subscribe(run_id)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                    if event.seq <= delivered_seq:
                        continue
                    delivered_seq = event.seq
                    payload = {
                        "seq": event.seq,
                        "type": event.type,
                        "created_at": event.created_at,
                        "data": event.data,
                        "title": event.title,
                        "message": event.message,
                    }
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    if event.type in {"done", "error"}:
                        break
                except asyncio.TimeoutError:
                    latest = session_run_service.get_run(
                        session_id=session_id,
                        run_id=run_id,
                        user_id=current_user.id,
                    )
                    if latest.status in TERMINAL_RUN_STATUS:
                        tail = session_run_service.list_events_after(
                            session_id=session_id,
                            run_id=run_id,
                            user_id=current_user.id,
                            after_seq=delivered_seq,
                        )
                        for item in tail:
                            delivered_seq = max(delivered_seq, item.seq)
                            payload = {
                                "seq": item.seq,
                                "type": item.type,
                                "created_at": item.created_at,
                                "data": item.data,
                                "title": item.title,
                                "message": item.message,
                            }
                            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                        break
        finally:
            session_run_service.unsubscribe(run_id, queue)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _to_markdown_node_item(node) -> MarkdownNodeItem:
    return MarkdownNodeItem(
        type=node.type,
        name=node.name,
        path=node.path,
        size=node.size,
        mtime=node.mtime,
        children=[_to_markdown_node_item(child) for child in node.children or []]
        if node.children is not None
        else None,
    )


def _to_file_node_item(node) -> FileNodeItem:
    return FileNodeItem(
        type=node.type,
        name=node.name,
        path=node.path,
        size=node.size,
        mtime=node.mtime,
        children=[_to_file_node_item(child) for child in node.children or []]
        if node.children is not None
        else None,
    )


def _extract_tool_name_from_title(title: str | None) -> str | None:
    if title is None:
        return None
    raw = title.strip()
    if not raw:
        return None
    # 标题格式通常为“调用工具: ToolName”，这里做容错解析。
    if ":" in raw:
        return raw.split(":")[-1].strip()
    if "：" in raw:
        return raw.split("：")[-1].strip()
    return raw


def _to_workspace_agent_profile_item(meta, profile, skills) -> WorkspaceAgentProfileItem:
    return WorkspaceAgentProfileItem(
        workspace_id=meta.workspace_id,
        workspace_name=meta.workspace_name,
        mcp_servers=[
            {
                "name": item.name,
                "type": item.type,
                "url": item.url,
                "command": item.command,
                "args": item.args,
                "headers": item.headers,
            }
            for item in profile.mcp_servers
        ],
        extra_allowed_tools=profile.extra_allowed_tools,
        skills=[
            WorkspaceSkillItem(
                name=item.name,
                relative_path=item.relative_path,
                description=item.description,
            )
            for item in skills
        ],
        updated_by=profile.updated_by,
        updated_at=profile.updated_at,
    )


def _merge_allowed_tools(
    base_tools: list[str],
    extra_tools: list[str],
) -> list[str]:
    """
    合并基础工具与扩展工具，保持顺序且自动去重。
    """
    merged: list[str] = []
    seen: set[str] = set()
    for value in [*base_tools, *extra_tools]:
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        merged.append(normalized)
    return merged


def _derive_mcp_allowed_tools(mcp_servers) -> list[str]:
    """
    根据 MCP 服务器名自动派生 Jework 常用工具白名单。

    这样用户只需配置服务器地址，不必手工填写 mcp__server__tool。
    """
    jework_tools = [
        "list_workspaces",
        "list_files",
        "read_file",
        "grep_files",
        "semantic_search",
        "hybrid_search",
    ]
    result: list[str] = []
    seen: set[str] = set()
    for server in mcp_servers:
        name = str(getattr(server, "name", "")).strip()
        if not name:
            continue
        for tool in jework_tools:
            item = f"mcp__{name}__{tool}"
            if item in seen:
                continue
            seen.add(item)
            result.append(item)
    return result


def _require_personal_workspace_manage_access(
    workspace: str,
    current_user: AuthUser,
):
    meta, workspace_path = _resolve_workspace_with_access(workspace, current_user)
    if meta.mode != "personal":
        raise AppError(
            code="WORKSPACE_AGENT_PROFILE_FORBIDDEN",
            message="Only personal workspace supports Agent profile",
            details={"workspace": workspace, "mode": meta.mode},
            status_code=400,
        )
    if current_user.role != "superadmin" and meta.owner_user_id != current_user.id:
        raise AuthForbiddenError()
    return meta, workspace_path


def _resolve_workspace_with_access(workspace: str, current_user: AuthUser):
    if not auth_service.can_access_workspace(current_user, workspace):
        raise AuthForbiddenError()
    allowed_ids = None
    if current_user.role != "superadmin":
        allowed_ids = set(auth_service.get_accessible_workspaces(current_user))
    try:
        meta = workspace_service.resolve_workspace_reference(
            workspace,
            allowed_workspace_ids=allowed_ids,
        )
    except WorkspaceNotFoundError:
        # 历史脏 ACL 兜底自愈：若 workspace 已不存在，移除当前用户残留授权，避免重复报错。
        if current_user.role != "superadmin":
            auth_service.remove_workspace_access_for_user(current_user.id, workspace)
        raise
    return meta, workspace_service.get_workspace_path(meta.workspace_id)


def _require_workspace_access(workspace: str, current_user: AuthUser):
    _, workspace_path = _resolve_workspace_with_access(workspace, current_user)
    return workspace_path
