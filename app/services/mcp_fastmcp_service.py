from __future__ import annotations

from contextvars import ContextVar
import logging
from typing import Annotated, Any

from fastapi.responses import JSONResponse
from pydantic import Field
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


# 工具参数中的 workspace 统一说明，避免多处重复且保证描述一致。
_WORKSPACE_PARAM_DESCRIPTION = (
    "目标工作空间名称。"
    "在 /mcp（多工作空间入口）下通常必填；"
    "在 /mcp/{workspace}（绑定入口）下可省略，若传入必须与绑定工作空间一致。"
)

# 服务级说明：告诉客户端本服务的能力与调用约束。
_SERVER_INSTRUCTIONS = """
你当前连接的是 Jework 提供的代码知识库 MCP 服务。
该服务面向已接入 Jework 的工作空间代码库（包括代码与文档），用于连接已有的代码库内容。
服务用途是读取与检索工作空间内容，用于理解实现逻辑、配置与文档；不提供写入或代码修改能力。

使用规则：
1. 先调用 list_workspaces 获取可访问工作空间及备注（name + note）。
2. 当前入口可能是多工作空间(/mcp)或绑定工作空间(/mcp/{workspace})。
3. 在多工作空间入口，调用 list_files/read_file/grep_files/semantic_search/hybrid_search 时应显式传 workspace。
4. 在绑定入口，工具默认作用于绑定工作空间；workspace 参数可省略，若传入必须一致。
5. semantic_search 若向量不可用，建议回退 grep_files + read_file。

你可以完成的典型任务：
- 快速了解某个模块/目录做什么（list_files + read_file）
- 根据关键词定位实现位置（grep_files）
- 根据自然语言问题做跨文件语义召回（semantic_search / hybrid_search）
- 追溯文档描述对应的代码实现与配置来源（grep_files + read_file + semantic_search）
""".strip()


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

    mcp = FastMCP(
        name="jework-mcp",
        instructions=_SERVER_INSTRUCTIONS,
    )

    @mcp.tool(
        name="list_workspaces",
        description=(
            "列出当前可访问的工作空间（name + note）。"
            "在多工作空间模式下返回全部可访问 workspace；"
            "在绑定工作空间模式下仅返回当前绑定 workspace，并附带绑定模式提示。"
        ),
    )
    def list_workspaces(workspace: str | None = None) -> dict[str, Any]:
        arguments: dict[str, Any] = {}
        if workspace:
            arguments["workspace"] = workspace
        return _safe_tool_call("list_workspaces", arguments)

    @mcp.tool(
        name="list_files",
        description=(
            "列出目标工作空间下某个目录的文件树。"
            "适合先做目录探测，再决定 read_file/grep_files 的目标路径。"
        ),
    )
    def list_files(
        workspace: Annotated[
            str | None,
            Field(
                description=_WORKSPACE_PARAM_DESCRIPTION,
            ),
        ] = None,
        path: Annotated[
            str,
            Field(
                description="要遍历的相对路径，默认根目录“.”。",
            ),
        ] = ".",
        depth: Annotated[
            int,
            Field(
                description="目录遍历深度，建议 0-8，默认 2。",
            ),
        ] = 2,
        include_hidden: Annotated[
            bool,
            Field(
                description="是否包含隐藏文件/目录（以 . 开头）。",
            ),
        ] = False,
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
        description=(
            "按行读取文件片段，用于查看实现细节、配置内容和文档原文。"
            "返回内容包含行号范围，便于后续精确引用。"
        ),
    )
    def read_file(
        path: Annotated[
            str,
            Field(
                description="要读取的文件相对路径（必填）。",
            ),
        ],
        workspace: Annotated[
            str | None,
            Field(
                description=_WORKSPACE_PARAM_DESCRIPTION,
            ),
        ] = None,
        start_line: Annotated[
            int,
            Field(description="起始行号（从 1 开始）。"),
        ] = 1,
        end_line: Annotated[
            int,
            Field(description="结束行号（包含该行）。"),
        ] = 300,
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
        description=(
            "基于关键词或正则在文件中查找片段。"
            "适合做快速定位，再配合 read_file 读取上下文。"
        ),
    )
    def grep_files(
        pattern: Annotated[
            str,
            Field(
                description="关键词或正则表达式（必填）。",
            ),
        ],
        workspace: Annotated[
            str | None,
            Field(
                description=_WORKSPACE_PARAM_DESCRIPTION,
            ),
        ] = None,
        glob: Annotated[
            str,
            Field(
                description=(
                    "文件过滤模式（glob），默认 '**/*'。"
                ),
            ),
        ] = "**/*",
        top_k: Annotated[
            int,
            Field(description="最多返回匹配条数，默认 20。"),
        ] = 20,
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
        description=(
            "向量语义检索（代码与文档）。"
            "当问题是自然语言描述时优先使用，适合跨文件语义召回。"
        ),
    )
    def semantic_search(
        query: Annotated[
            str,
            Field(description="语义检索查询文本（必填）。"),
        ],
        workspace: Annotated[
            str | None,
            Field(
                description=_WORKSPACE_PARAM_DESCRIPTION,
            ),
        ] = None,
        top_k: Annotated[
            int,
            Field(description="返回结果数量，默认 8。"),
        ] = 8,
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
        description=(
            "混合检索：向量召回 + 关键词重排。"
            "当语义检索结果不稳定时，可用该工具提升精确命中率。"
        ),
    )
    def hybrid_search(
        query: Annotated[
            str,
            Field(description="混合检索查询文本（必填）。"),
        ],
        workspace: Annotated[
            str | None,
            Field(
                description=_WORKSPACE_PARAM_DESCRIPTION,
            ),
        ] = None,
        top_k: Annotated[
            int,
            Field(description="返回结果数量，默认 8。"),
        ] = 8,
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
