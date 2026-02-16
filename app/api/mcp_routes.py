from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
import time
from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from app.api.deps import get_current_user
from app.api.deps_mcp import get_current_mcp_user
from app.core.errors import AppError, AuthForbiddenError
from app.models.schemas import (
    CreateMcpIndexJobRequest,
    McpAuthInfoResponse,
    McpIndexJobItem,
    McpResetTokenResponse,
    McpSettingsItem,
    UpdateMcpSettingsRequest,
)
from app.services.auth_service import AuthUser, auth_service
from app.services.mcp_audit_service import McpAuditRecord, mcp_audit_service
from app.services.mcp_index_job_service import mcp_index_job_service
from app.services.mcp_settings_service import mcp_settings_service
from app.services.mcp_token_service import mcp_token_service
from app.services.mcp_vector_service import mcp_vector_service
from app.services.workspace_service import workspace_service

router = APIRouter()


class McpToolCallRequest(BaseModel):
    tool: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)


class McpToolCallResponse(BaseModel):
    mode: str
    workspace: str | None = None
    data: dict[str, Any] | list[dict[str, Any]] | str | list[str] | None = None


class McpBoundRpcRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: int | str | None = None
    method: str
    params: dict[str, Any] = Field(default_factory=dict)


@router.get("/api/mcp/auth/info", response_model=McpAuthInfoResponse)
def get_mcp_auth_info(
    request: Request,
    current_user: AuthUser = Depends(get_current_user),
) -> McpAuthInfoResponse:
    info = mcp_token_service.get_info(current_user.id)
    mcp_url, template = mcp_settings_service.build_mcp_url(_request_base_url(request))
    return McpAuthInfoResponse(
        mcp_url=mcp_url,
        workspace_mcp_url_template=template,
        has_token=info.has_token,
        token_hint=info.token_hint,
        updated_at=info.updated_at,
    )


@router.post("/api/mcp/auth/reset-token", response_model=McpResetTokenResponse)
def reset_mcp_token(
    request: Request,
    current_user: AuthUser = Depends(get_current_user),
) -> McpResetTokenResponse:
    result = mcp_token_service.reset_token(current_user.id)
    mcp_url, template = mcp_settings_service.build_mcp_url(_request_base_url(request))
    return McpResetTokenResponse(
        token=result.token,
        token_hint=result.token_hint,
        mcp_url=mcp_url,
        workspace_mcp_url_template=template,
        updated_at=result.updated_at,
    )


@router.get("/api/admin/mcp/settings", response_model=McpSettingsItem)
def get_mcp_settings(
    current_user: AuthUser = Depends(get_current_user),
) -> McpSettingsItem:
    view = mcp_settings_service.get_settings_view(
        is_superadmin=current_user.role == "superadmin"
    )
    return McpSettingsItem(
        mcp_enabled=view.mcp_enabled,
        mcp_base_path=view.mcp_base_path,
        mcp_public_base_url=view.mcp_public_base_url,
        kb_enable_vector=view.kb_enable_vector,
        kb_chroma_dir=view.kb_chroma_dir,
        kb_vector_topk_default=view.kb_vector_topk_default,
        kb_file_max_bytes=view.kb_file_max_bytes,
        kb_read_max_lines=view.kb_read_max_lines,
        embedding_backend=view.embedding_backend,
        embedding_base_url=view.embedding_base_url,
        embedding_model=view.embedding_model,
        embedding_batch_size=view.embedding_batch_size,
        has_embedding_api_key=view.has_embedding_api_key,
        editable_fields=view.editable_fields,
        updated_at=view.updated_at,
    )


@router.put("/api/admin/mcp/settings", response_model=McpSettingsItem)
def update_mcp_settings(
    body: UpdateMcpSettingsRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> McpSettingsItem:
    updated = mcp_settings_service.update_settings(
        is_superadmin=current_user.role == "superadmin",
        mcp_enabled=body.mcp_enabled,
        mcp_base_path=body.mcp_base_path,
        mcp_public_base_url=body.mcp_public_base_url,
        kb_enable_vector=body.kb_enable_vector,
        kb_chroma_dir=body.kb_chroma_dir,
        kb_vector_topk_default=body.kb_vector_topk_default,
        kb_file_max_bytes=body.kb_file_max_bytes,
        kb_read_max_lines=body.kb_read_max_lines,
        embedding_backend=body.embedding_backend,
        embedding_base_url=body.embedding_base_url,
        embedding_model=body.embedding_model,
        embedding_batch_size=body.embedding_batch_size,
        embedding_api_key=body.embedding_api_key,
        clear_embedding_api_key=body.clear_embedding_api_key,
    )
    return McpSettingsItem(
        mcp_enabled=updated.mcp_enabled,
        mcp_base_path=updated.mcp_base_path,
        mcp_public_base_url=updated.mcp_public_base_url,
        kb_enable_vector=updated.kb_enable_vector,
        kb_chroma_dir=updated.kb_chroma_dir,
        kb_vector_topk_default=updated.kb_vector_topk_default,
        kb_file_max_bytes=updated.kb_file_max_bytes,
        kb_read_max_lines=updated.kb_read_max_lines,
        embedding_backend=updated.embedding_backend,
        embedding_base_url=updated.embedding_base_url,
        embedding_model=updated.embedding_model,
        embedding_batch_size=updated.embedding_batch_size,
        has_embedding_api_key=updated.has_embedding_api_key,
        editable_fields=updated.editable_fields,
        updated_at=updated.updated_at,
    )


@router.post("/api/admin/mcp/index-jobs", response_model=McpIndexJobItem)
def create_mcp_index_job(
    body: CreateMcpIndexJobRequest,
    current_user: AuthUser = Depends(get_current_user),
) -> McpIndexJobItem:
    if current_user.role != "superadmin":
        raise AuthForbiddenError()
    workspace_service.get_workspace_path(body.workspace)
    item = mcp_index_job_service.create_job(
        user_id=current_user.id,
        workspace=body.workspace,
        mode=body.mode,
    )
    return _to_job_item(item)


@router.get("/api/admin/mcp/index-jobs/{job_id}", response_model=McpIndexJobItem)
def get_mcp_index_job(
    job_id: str,
    current_user: AuthUser = Depends(get_current_user),
) -> McpIndexJobItem:
    item = mcp_index_job_service.get_job(
        job_id=job_id,
        requester_id=current_user.id,
        requester_is_superadmin=current_user.role == "superadmin",
    )
    return _to_job_item(item)


@router.post("/api/mcp/tool-call", response_model=McpToolCallResponse)
def mcp_tool_call_general(
    body: McpToolCallRequest,
    current_user: AuthUser = Depends(get_current_mcp_user),
) -> McpToolCallResponse:
    _ensure_mcp_enabled()
    data = execute_mcp_tool(
        current_user=current_user,
        tool=body.tool,
        arguments=body.arguments,
    )
    return McpToolCallResponse(mode="general", data=data)


@router.post("/api/mcp/{workspace}/tool-call", response_model=McpToolCallResponse)
def mcp_tool_call_bound(
    workspace: str,
    body: McpToolCallRequest,
    current_user: AuthUser = Depends(get_current_mcp_user),
) -> McpToolCallResponse:
    _ensure_mcp_enabled()
    arguments = dict(body.arguments)
    incoming_workspace = arguments.get("workspace")
    if incoming_workspace is not None and str(incoming_workspace) != workspace:
        raise AppError(
            code="MCP_WORKSPACE_MISMATCH",
            message="workspace argument mismatches path workspace",
            details={"workspace": workspace, "argument_workspace": incoming_workspace},
            status_code=400,
        )
    arguments["workspace"] = workspace
    data = execute_mcp_tool(
        current_user=current_user,
        tool=body.tool,
        arguments=arguments,
    )
    return McpToolCallResponse(mode="bound", workspace=workspace, data=data)


@router.post("/mcp/{workspace}")
def mcp_bound_rpc(
    workspace: str,
    body: McpBoundRpcRequest,
    current_user: AuthUser = Depends(get_current_mcp_user),
) -> dict[str, Any]:
    _ensure_mcp_enabled()
    _assert_workspace_access(current_user, workspace)
    if body.jsonrpc != "2.0":
        return {
            "jsonrpc": "2.0",
            "id": body.id,
            "error": {"code": -32600, "message": "invalid jsonrpc version"},
        }

    if body.method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": body.id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": "jework-mcp-bound",
                    "version": "0.2.2",
                },
            },
        }

    if body.method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": body.id,
            "result": {
                "tools": _bound_tools_schema(workspace),
            },
        }

    if body.method == "tools/call":
        name = _normalize_optional(body.params.get("name"))
        if not name:
            return {
                "jsonrpc": "2.0",
                "id": body.id,
                "error": {"code": -32602, "message": "tool name is required"},
            }

        arguments = body.params.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        incoming_workspace = arguments.get("workspace")
        if incoming_workspace is not None and str(incoming_workspace) != workspace:
            return {
                "jsonrpc": "2.0",
                "id": body.id,
                "error": {
                    "code": -32602,
                    "message": "workspace argument mismatches bound workspace",
                },
            }
        arguments["workspace"] = workspace
        try:
            result = execute_mcp_tool(
                current_user=current_user,
                tool=name,
                arguments=arguments,
            )
        except AppError as exc:
            return {
                "jsonrpc": "2.0",
                "id": body.id,
                "result": {
                    "isError": True,
                    "content": [
                        {
                            "type": "text",
                            "text": f"{exc.code}: {exc.message}",
                        }
                    ],
                },
            }

        return {
            "jsonrpc": "2.0",
            "id": body.id,
            "result": {
                "isError": False,
                "content": [{"type": "text", "text": str(result)}],
                "structuredContent": result,
            },
        }

    return {
        "jsonrpc": "2.0",
        "id": body.id,
        "error": {
            "code": -32601,
            "message": f"unsupported method: {body.method}",
        },
    }


def execute_mcp_tool(
    *,
    current_user: AuthUser,
    tool: str,
    arguments: dict[str, Any],
) -> Any:
    started = time.perf_counter()
    workspace_for_audit: str | None = None
    path_or_query: str | None = None
    status = "ok"
    try:
        if tool == "list_workspaces":
            workspace = _normalize_optional(arguments.get("workspace"))
            if workspace:
                _assert_workspace_access(current_user, workspace)
                detail_items = workspace_service.list_workspaces(
                    {workspace},
                )
                item = detail_items[0] if detail_items else None
                return {
                    "mode": "workspace_bound",
                    "message": "当前 MCP 为 workspace 绑定模式，仅返回当前 workspace",
                    "items": [
                        {
                            "name": workspace,
                            "note": item.note if item else None,
                        }
                    ],
                }

            if current_user.role == "superadmin":
                detail_items = workspace_service.list_workspaces()
            else:
                accessible = auth_service.get_accessible_workspaces(current_user)
                detail_items = workspace_service.list_workspaces(set(accessible))
            return {
                "mode": "general",
                "items": [
                    {"name": item.name, "note": item.note}
                    for item in detail_items
                ],
            }

        if tool == "list_files":
            workspace = _required_workspace(current_user, arguments)
            workspace_for_audit = workspace
            path = _normalize_optional(arguments.get("path")) or "."
            depth = _int_value(arguments.get("depth"), default=2, min_value=0, max_value=8)
            include_hidden = _bool_value(arguments.get("include_hidden"), default=False)
            path_or_query = path
            return _list_files(workspace, path=path, depth=depth, include_hidden=include_hidden)

        if tool == "read_file":
            workspace = _required_workspace(current_user, arguments)
            workspace_for_audit = workspace
            path = _normalize_optional(arguments.get("path"))
            if not path:
                raise AppError(
                    code="MCP_TOOL_INVALID_ARGUMENT",
                    message="path is required",
                    status_code=400,
                )
            start_line = _int_value(arguments.get("start_line"), default=1, min_value=1, max_value=2_000_000)
            end_line = _int_value(arguments.get("end_line"), default=300, min_value=start_line, max_value=2_000_000)
            path_or_query = path
            return _read_file(workspace, path=path, start_line=start_line, end_line=end_line)

        if tool == "grep_files":
            workspace = _required_workspace(current_user, arguments)
            workspace_for_audit = workspace
            pattern = _normalize_optional(arguments.get("pattern"))
            if not pattern:
                raise AppError(
                    code="MCP_TOOL_INVALID_ARGUMENT",
                    message="pattern is required",
                    status_code=400,
                )
            glob_pattern = _normalize_optional(arguments.get("glob")) or "**/*"
            top_k = _int_value(arguments.get("top_k"), default=20, min_value=1, max_value=200)
            path_or_query = pattern
            return _grep_files(workspace, pattern=pattern, glob_pattern=glob_pattern, top_k=top_k)

        if tool in {"semantic_search", "hybrid_search"}:
            workspace = _required_workspace(current_user, arguments)
            workspace_for_audit = workspace
            query = _normalize_optional(arguments.get("query"))
            if not query:
                raise AppError(
                    code="MCP_TOOL_INVALID_ARGUMENT",
                    message="query is required",
                    status_code=400,
                )
            top_k = _int_value(arguments.get("top_k"), default=8, min_value=1, max_value=50)
            path_or_query = query
            if tool == "semantic_search":
                return {
                    "workspace": workspace,
                    "hits": mcp_vector_service.semantic_search(
                        workspace=workspace,
                        query=query,
                        top_k=top_k,
                    ),
                }
            return {
                "workspace": workspace,
                "hits": mcp_vector_service.hybrid_search(
                    workspace=workspace,
                    query=query,
                    top_k=top_k,
                ),
            }

        raise AppError(
            code="MCP_TOOL_NOT_SUPPORTED",
            message="tool is not supported",
            details={"tool": tool},
            status_code=400,
        )
    except Exception:
        status = "failed"
        raise
    finally:
        elapsed = int((time.perf_counter() - started) * 1000)
        mcp_audit_service.append(
            McpAuditRecord(
                user_id=current_user.id,
                tool_name=tool,
                workspace=workspace_for_audit,
                path_or_query=path_or_query,
                elapsed_ms=elapsed,
                status=status,
            )
        )


def _required_workspace(current_user: AuthUser, arguments: dict[str, Any]) -> str:
    workspace = _normalize_optional(arguments.get("workspace"))
    if not workspace:
        raise AppError(
            code="MCP_TOOL_INVALID_ARGUMENT",
            message="workspace is required",
            status_code=400,
        )
    _assert_workspace_access(current_user, workspace)
    return workspace


def _assert_workspace_access(current_user: AuthUser, workspace: str) -> None:
    if not auth_service.can_access_workspace(current_user, workspace):
        raise AuthForbiddenError()
    workspace_service.get_workspace_path(workspace)


def _user_workspaces(current_user: AuthUser) -> list[str]:
    if current_user.role == "superadmin":
        return [item.name for item in workspace_service.list_workspaces()]
    return auth_service.get_accessible_workspaces(current_user)


def _list_files(workspace: str, *, path: str, depth: int, include_hidden: bool) -> dict[str, Any]:
    root = workspace_service.get_workspace_path(workspace)
    target = (root / path).resolve()
    if root not in target.parents and target != root:
        raise AppError(
            code="PATH_OUT_OF_WORKSPACE",
            message="path escapes workspace root",
            details={"workspace": workspace, "path": path},
            status_code=400,
        )
    if not target.exists():
        raise AppError(
            code="FILE_NOT_FOUND",
            message="path not found",
            details={"workspace": workspace, "path": path},
            status_code=404,
        )

    base_depth = len(target.parts)
    items: list[dict[str, Any]] = []
    iterator = [target] if target.is_file() else list(target.rglob("*"))
    for item in iterator:
        try:
            relative = item.relative_to(root)
        except ValueError:
            continue
        if not include_hidden and any(part.startswith(".") for part in relative.parts):
            continue
        current_depth = len(item.parts) - base_depth
        if current_depth > depth:
            continue
        if item.is_dir():
            items.append(
                {
                    "path": str(relative),
                    "type": "dir",
                    "size": None,
                    "mtime": datetime.fromtimestamp(item.stat().st_mtime, tz=timezone.utc).isoformat(),
                }
            )
        elif item.is_file():
            items.append(
                {
                    "path": str(relative),
                    "type": "file",
                    "size": int(item.stat().st_size),
                    "mtime": datetime.fromtimestamp(item.stat().st_mtime, tz=timezone.utc).isoformat(),
                }
            )
    items.sort(key=lambda row: row["path"])
    return {"workspace": workspace, "path": path, "items": items}


def _read_file(workspace: str, *, path: str, start_line: int, end_line: int) -> dict[str, Any]:
    settings_value = mcp_settings_service.get_settings()
    root = workspace_service.get_workspace_path(workspace)
    target = (root / path).resolve()
    if root not in target.parents and target != root:
        raise AppError(
            code="PATH_OUT_OF_WORKSPACE",
            message="path escapes workspace root",
            details={"workspace": workspace, "path": path},
            status_code=400,
        )
    if not target.exists() or not target.is_file():
        raise AppError(
            code="FILE_NOT_FOUND",
            message="file not found",
            details={"workspace": workspace, "path": path},
            status_code=404,
        )

    if target.stat().st_size > settings_value.kb_file_max_bytes:
        raise AppError(
            code="FILE_TOO_LARGE",
            message="file exceeds max bytes limit",
            details={
                "path": path,
                "size": target.stat().st_size,
                "limit": settings_value.kb_file_max_bytes,
            },
            status_code=400,
        )

    text = _read_text_with_fallback(target)
    lines = text.splitlines()
    start_idx = max(0, start_line - 1)
    end_idx = min(len(lines), end_line)
    snippet = lines[start_idx:end_idx]

    max_lines = settings_value.kb_read_max_lines
    truncated = False
    if len(snippet) > max_lines:
        snippet = snippet[:max_lines]
        truncated = True

    content = "\n".join(snippet)
    return {
        "workspace": workspace,
        "path": path,
        "start_line": start_line,
        "end_line": start_line + len(snippet) - 1 if snippet else start_line,
        "truncated": truncated,
        "encoding": "utf-8",
        "content": content,
    }


def _grep_files(workspace: str, *, pattern: str, glob_pattern: str, top_k: int) -> dict[str, Any]:
    root = workspace_service.get_workspace_path(workspace)
    regex = re.compile(pattern)
    matches: list[dict[str, Any]] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if "/.git/" in path.as_posix():
            continue
        try:
            relative = str(path.relative_to(root))
        except ValueError:
            continue
        if not _glob_match(relative, glob_pattern):
            continue
        text = _safe_read_text(path)
        if text is None:
            continue
        for idx, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                matches.append({"path": relative, "line": idx, "text": line[:1000]})
                if len(matches) >= top_k:
                    return {"workspace": workspace, "matches": matches}
    return {"workspace": workspace, "matches": matches}


def _semantic_fallback(workspace: str, *, query: str, top_k: int) -> dict[str, Any]:
    tokens = [item for item in re.split(r"\s+", query) if item]
    if not tokens:
        return {"workspace": workspace, "hits": []}

    root = workspace_service.get_workspace_path(workspace)
    hits: list[dict[str, Any]] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if "/.git/" in path.as_posix():
            continue
        text = _safe_read_text(path)
        if text is None:
            continue
        lines = text.splitlines()
        for idx, line in enumerate(lines, start=1):
            lowered = line.lower()
            score = sum(1 for token in tokens if token.lower() in lowered)
            if score <= 0:
                continue
            hits.append(
                {
                    "chunk_id": f"{path}:{idx}",
                    "path": str(path.relative_to(root)),
                    "start_line": idx,
                    "end_line": idx,
                    "score": score,
                    "snippet": line[:800],
                    "source": "keyword-fallback",
                }
            )
    hits.sort(key=lambda row: (-int(row["score"]), str(row["path"])))
    return {"workspace": workspace, "hits": hits[:top_k]}


def _bound_tools_schema(workspace: str) -> list[dict[str, Any]]:
    return [
        {
            "name": "list_workspaces",
            "description": "仅返回当前绑定 workspace",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "list_files",
            "description": "列出工作空间目录中的文件",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "depth": {"type": "integer"},
                    "include_hidden": {"type": "boolean"},
                },
            },
        },
        {
            "name": "read_file",
            "description": "按行读取文件内容",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
        {
            "name": "grep_files",
            "description": "关键词/正则查找文件片段",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "glob": {"type": "string"},
                    "top_k": {"type": "integer"},
                },
                "required": ["pattern"],
            },
        },
        {
            "name": "semantic_search",
            "description": "向量语义检索（代码与文档）",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "hybrid_search",
            "description": "向量召回 + 关键词重排",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "top_k": {"type": "integer"},
                },
                "required": ["query"],
            },
        },
    ]




def _glob_match(path: str, pattern: str) -> bool:
    if pattern in {"*", "**", "**/*"}:
        return True
    regex = re.escape(pattern)
    regex = regex.replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
    return re.fullmatch(regex, path) is not None


def _safe_read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        try:
            return path.read_text(encoding="utf-8-sig")
        except Exception:
            return None


def _read_text_with_fallback(path: Path) -> str:
    text = _safe_read_text(path)
    if text is None:
        raise AppError(
            code="FILE_ENCODING_UNSUPPORTED",
            message="file is not utf-8 text",
            details={"path": str(path)},
            status_code=400,
        )
    return text


def _normalize_optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _int_value(value: Any, *, default: int, min_value: int, max_value: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except Exception:
        return default
    return max(min_value, min(parsed, max_value))


def _bool_value(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return text in {"1", "true", "yes", "on"}


def _request_base_url(request: Request) -> str:
    return f"{request.url.scheme}://{request.url.netloc}".rstrip("/")


def _ensure_mcp_enabled() -> None:
    cfg = mcp_settings_service.get_settings()
    if not cfg.mcp_enabled:
        raise AppError(
            code="MCP_DISABLED",
            message="MCP is disabled",
            status_code=400,
        )


def _to_job_item(item) -> McpIndexJobItem:
    return McpIndexJobItem(
        job_id=item.job_id,
        workspace=item.workspace,
        mode=item.mode,
        status=item.status,
        percent=item.percent,
        total_files=item.total_files,
        total_chunks=item.total_chunks,
        processed_chunks=item.processed_chunks,
        failed_chunks=item.failed_chunks,
        elapsed_ms=item.elapsed_ms,
        error_message=item.error_message,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )
