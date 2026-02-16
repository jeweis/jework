from __future__ import annotations

from contextvars import ContextVar
import logging
from typing import Any

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.api.mcp_routes import execute_mcp_tool
from app.core.errors import AppError, AuthRequiredError
from app.services.auth_service import AuthUser, auth_service
from app.services.mcp_settings_service import mcp_settings_service
from app.services.mcp_token_service import mcp_token_service
from app.services.mcp_prompt_templates import (
    ANALYZE_CODEBASE_QUESTION,
    TRACE_DOC_TO_CODE,
    render_prompt_text,
)

logger = logging.getLogger(__name__)

_CURRENT_MCP_USER: ContextVar[AuthUser | None] = ContextVar(
    "current_mcp_user",
    default=None,
)
_CURRENT_BOUND_WORKSPACE: ContextVar[str | None] = ContextVar(
    "current_bound_workspace",
    default=None,
)


class _McpAuthMiddleware(BaseHTTPMiddleware):
    """为 FastMCP 请求注入 Jework MCP 鉴权上下文。"""

    async def dispatch(self, request: Request, call_next):
        # 支持 CORS 预检请求。
        if request.method.upper() == "OPTIONS":
            return await call_next(request)

        try:
            if not mcp_settings_service.get_settings().mcp_enabled:
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": {
                            "code": "MCP_DISABLED",
                            "message": "MCP is disabled",
                            "details": None,
                        }
                    },
                )

            authorization = request.headers.get("Authorization")
            if not authorization or not authorization.startswith("Bearer "):
                raise AuthRequiredError()
            raw_token = authorization[len("Bearer ") :].strip()
            if not raw_token:
                raise AuthRequiredError()
            user_id = mcp_token_service.verify_token(raw_token)
            current_user = auth_service.get_user_by_id(user_id)
        except AppError as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        "details": exc.details,
                    }
                },
            )

        token = _CURRENT_MCP_USER.set(current_user)
        bound_workspace = request.scope.get("state", {}).get(
            "mcp_bound_workspace",
        )
        token_workspace = _CURRENT_BOUND_WORKSPACE.set(bound_workspace)
        try:
            return await call_next(request)
        finally:
            _CURRENT_BOUND_WORKSPACE.reset(token_workspace)
            _CURRENT_MCP_USER.reset(token)


def _require_current_user() -> AuthUser:
    current_user = _CURRENT_MCP_USER.get()
    if current_user is None:
        raise AuthRequiredError()
    return current_user


def _safe_tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    current_user = _require_current_user()
    bound_workspace = _CURRENT_BOUND_WORKSPACE.get()
    data = execute_mcp_tool(
        current_user=current_user,
        tool=name,
        arguments=arguments,
        bound_workspace=bound_workspace,
    )
    return data if isinstance(data, dict) else {"value": data}


def build_fastmcp_asgi_app():
    """构建并返回 FastMCP ASGI 应用。

    当运行环境尚未安装 fastmcp 依赖时返回 None，避免服务启动崩溃。
    """

    try:
        from fastmcp import FastMCP
    except Exception as exc:  # pragma: no cover
        logger.warning("fastmcp is unavailable, fallback disabled: %s", exc)
        return None

    mcp = FastMCP(name="jework-mcp")

    @mcp.tool(
        name="list_workspaces",
        description="列出当前 token 可访问的工作空间",
    )
    def list_workspaces(workspace: str | None = None) -> dict[str, Any]:
        arguments: dict[str, Any] = {}
        if workspace:
            arguments["workspace"] = workspace
        return _safe_tool_call("list_workspaces", arguments)

    @mcp.tool(
        name="list_files",
        description="列出工作空间目录中的文件",
    )
    def list_files(
        workspace: str | None = None,
        path: str = ".",
        depth: int = 2,
        include_hidden: bool = False,
    ) -> dict[str, Any]:
        return _safe_tool_call(
            "list_files",
            {
                "workspace": workspace,
                "path": path,
                "depth": depth,
                "include_hidden": include_hidden,
            },
        )

    @mcp.tool(
        name="read_file",
        description="按行读取文件内容",
    )
    def read_file(
        path: str,
        workspace: str | None = None,
        start_line: int = 1,
        end_line: int = 300,
    ) -> dict[str, Any]:
        return _safe_tool_call(
            "read_file",
            {
                "workspace": workspace,
                "path": path,
                "start_line": start_line,
                "end_line": end_line,
            },
        )

    @mcp.tool(
        name="grep_files",
        description="关键词/正则查找文件片段",
    )
    def grep_files(
        pattern: str,
        workspace: str | None = None,
        glob: str = "**/*",
        top_k: int = 20,
    ) -> dict[str, Any]:
        return _safe_tool_call(
            "grep_files",
            {
                "workspace": workspace,
                "pattern": pattern,
                "glob": glob,
                "top_k": top_k,
            },
        )

    @mcp.tool(
        name="semantic_search",
        description="向量语义检索（代码与文档）",
    )
    def semantic_search(
        query: str,
        workspace: str | None = None,
        top_k: int = 8,
    ) -> dict[str, Any]:
        return _safe_tool_call(
            "semantic_search",
            {
                "workspace": workspace,
                "query": query,
                "top_k": top_k,
            },
        )

    @mcp.tool(
        name="hybrid_search",
        description="向量召回 + 关键词重排",
    )
    def hybrid_search(
        query: str,
        workspace: str | None = None,
        top_k: int = 8,
    ) -> dict[str, Any]:
        return _safe_tool_call(
            "hybrid_search",
            {
                "workspace": workspace,
                "query": query,
                "top_k": top_k,
            },
        )

    @mcp.prompt(
        name=ANALYZE_CODEBASE_QUESTION.name,
        description=ANALYZE_CODEBASE_QUESTION.description,
    )
    def analyze_codebase_question(
        question: str,
        workspace: str | None = None,
        scope_hint: str = "",
        top_k: int = 8,
    ) -> str:
        return render_prompt_text(
            name=ANALYZE_CODEBASE_QUESTION.name,
            arguments={
                "workspace": workspace,
                "question": question,
                "scope_hint": scope_hint,
                "top_k": top_k,
            },
        )

    @mcp.prompt(
        name=TRACE_DOC_TO_CODE.name,
        description=TRACE_DOC_TO_CODE.description,
    )
    def trace_doc_to_code(
        doc_query: str,
        workspace: str | None = None,
        doc_path_hint: str = "",
        top_k: int = 10,
    ) -> str:
        return render_prompt_text(
            name=TRACE_DOC_TO_CODE.name,
            arguments={
                "workspace": workspace,
                "doc_query": doc_query,
                "doc_path_hint": doc_path_hint,
                "top_k": top_k,
            },
        )

    # FastMCP 默认实现 streamable HTTP，使用 path="/" 便于挂载到 /mcp。
    if hasattr(mcp, "http_app"):
        runtime_app = mcp.http_app(path="/", stateless_http=True)
    elif hasattr(mcp, "streamable_http_app"):
        runtime_app = mcp.streamable_http_app(path="/")
    else:  # pragma: no cover
        logger.error("fastmcp api mismatch: no http_app/streamable_http_app")
        return None

    # 关键：直接返回 FastMCP 生成的 StarletteWithLifespan 实例。
    # 如果外面再包一层普通 Starlette，会丢失其 lifespan，
    # 导致 StreamableHTTPSessionManager 的 task group 未初始化。
    runtime_app.add_middleware(_McpAuthMiddleware)
    return runtime_app
