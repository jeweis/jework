import asyncio
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.api.deps import get_current_user
from app.core.errors import AppError, AuthForbiddenError
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
    CreateUserRequest,
    CreateWorkspaceRequest,
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
    UpdateWorkspaceNoteRequest,
    UserListResponse,
    UserResponse,
    WorkspaceCredentialItem,
    WorkspaceDeleteResponse,
    WorkspaceItem,
    WorkspaceListResponse,
    WorkspaceNoteItem,
    WorkspacePullResponse,
)
from app.services.agent_service import stream_agent_response
from app.services.auth_service import AuthUser, auth_service
from app.services.feishu_auth_service import feishu_auth_service
from app.services.feishu_settings_service import feishu_settings_service
from app.services.llm_config_service import llm_config_service
from app.services.markdown_service import markdown_service
from app.services.mcp_index_job_service import mcp_index_job_service
from app.services.mcp_settings_service import mcp_settings_service
from app.services.session_run_service import TERMINAL_RUN_STATUS, session_run_service
from app.services.session_service import session_service
from app.services.workspace_credential_service import workspace_credential_service
from app.services.workspace_git_service import workspace_git_service
from app.services.workspace_note_service import workspace_note_service
from app.services.workspace_service import workspace_service

router = APIRouter()
logger = logging.getLogger(__name__)
_run_tasks: dict[str, asyncio.Task[None]] = {}


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
    user = auth_service.create_user(
        current_user=current_user,
        username=body.username,
        password=body.password,
        workspace_names=body.workspace_names,
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
    for workspace in normalized:
        workspace_service.get_workspace_path(workspace)
    accessible = auth_service.set_user_workspace_access(
        current_user=current_user,
        user_id=user_id,
        workspace_names=normalized,
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
    if current_user.role != "superadmin":
        raise AuthForbiddenError()

    return workspace_service.create_workspace(
        workspace=body.name,
        git_url=body.git_url,
        git_username=body.git_username,
        git_pat=body.git_pat,
        user_id=current_user.id,
    )


@router.delete("/workspaces/{workspace}", response_model=WorkspaceDeleteResponse)
def delete_workspace(
    workspace: str,
    current_user: AuthUser = Depends(get_current_user),
) -> WorkspaceDeleteResponse:
    if current_user.role != "superadmin":
        raise AuthForbiddenError()

    result = workspace_service.delete_workspace(workspace)
    removed_sessions = session_service.delete_workspace_sessions(workspace)
    workspace_credential_service.delete_workspace_credential(workspace)
    workspace_git_service.delete_sync_meta(workspace)
    workspace_note_service.delete_note(workspace)
    auth_service.remove_workspace_access_for_all_users(workspace)

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
    if current_user.role != "superadmin":
        raise AuthForbiddenError()
    workspace_service.get_workspace_path(workspace)
    item = workspace_credential_service.get_workspace_credential(workspace)
    if item is None:
        return WorkspaceCredentialItem(workspace=workspace)
    return WorkspaceCredentialItem(
        workspace=item.workspace,
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
    if current_user.role != "superadmin":
        raise AuthForbiddenError()
    workspace_service.get_workspace_path(workspace)
    updated_at = datetime.now(timezone.utc).isoformat()
    item = workspace_note_service.upsert_note(
        workspace=workspace,
        note=body.note,
        updated_at=updated_at,
    )
    return WorkspaceNoteItem(
        workspace=item.workspace,
        note=item.note,
        updated_at=item.updated_at,
    )


@router.put("/workspaces/{workspace}/credential", response_model=WorkspaceCredentialItem)
def update_workspace_credential(
    workspace: str,
    body: UpdateWorkspaceCredentialRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> WorkspaceCredentialItem:
    if current_user.role != "superadmin":
        raise AuthForbiddenError()
    workspace_service.get_workspace_path(workspace)
    workspace_credential_service.upsert_workspace_credential(
        workspace=workspace,
        user_id=current_user.id,
        git_username=body.git_username,
        git_pat=body.git_pat,
    )
    item = workspace_credential_service.get_workspace_credential(workspace)
    if item is None:
        return WorkspaceCredentialItem(workspace=workspace)
    return WorkspaceCredentialItem(
        workspace=item.workspace,
        git_url=item.git_url,
        git_username=item.git_username,
        has_git_pat=item.has_git_pat,
    )


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
    workspace_path: str,
    claude_session_id: str | None,
    runtime_env: dict[str, str],
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
            session_run_service.append_event(
                session_id=session_id,
                run_id=run_id,
                user_id=user_id,
                event_type=event_type,
                data=normalized_data,
                title=normalized_title,
                message=normalized_message,
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
    # 每次发送前按 workspace 名实时解析路径，兼容历史会话里持久化路径失效。
    workspace_path = _require_workspace_access(session.workspace, current_user)
    session_service.set_workspace_path(
        session_id=session_id,
        user_id=current_user.id,
        workspace_path=workspace_path,
    )
    runtime_env = llm_config_service.get_active_env()
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
            workspace_path=str(workspace_path),
            claude_session_id=session.claude_session_id,
            runtime_env=runtime_env,
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


def _require_workspace_access(workspace: str, current_user: AuthUser):
    if not auth_service.can_access_workspace(current_user, workspace):
        raise AuthForbiddenError()
    return workspace_service.get_workspace_path(workspace)
