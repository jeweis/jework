from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.errors import AppError


@dataclass(frozen=True)
class PromptArg:
    name: str
    description: str
    required: bool


@dataclass(frozen=True)
class PromptTemplate:
    name: str
    description: str
    arguments: tuple[PromptArg, ...]


ANALYZE_CODEBASE_QUESTION = PromptTemplate(
    name="analyze_codebase_question",
    description=(
        "针对代码库问题进行结构化分析，输出结论和证据，"
        "并在向量检索异常时自动降级到文本检索流程。"
    ),
    arguments=(
        PromptArg("workspace", "目标工作空间名称", True),
        PromptArg("question", "用户提出的问题", True),
        PromptArg("scope_hint", "可选范围提示（目录/模块名）", False),
        PromptArg("top_k", "候选召回数量，默认 8", False),
    ),
)

TRACE_DOC_TO_CODE = PromptTemplate(
    name="trace_doc_to_code",
    description=(
        "把文档需求映射到实际代码实现，输出文档点到代码点"
        "的可追溯映射。"
    ),
    arguments=(
        PromptArg("workspace", "目标工作空间名称", True),
        PromptArg("doc_query", "文档查询语句或需求片段", True),
        PromptArg("doc_path_hint", "可选文档路径提示", False),
        PromptArg("top_k", "候选召回数量，默认 10", False),
    ),
)

PROMPT_TEMPLATES: dict[str, PromptTemplate] = {
    ANALYZE_CODEBASE_QUESTION.name: ANALYZE_CODEBASE_QUESTION,
    TRACE_DOC_TO_CODE.name: TRACE_DOC_TO_CODE,
}


def search_fallback_policy_text() -> str:
    """统一检索降级策略文案。

    该文案会同时用于 FastMCP prompt 和手写 JSON-RPC prompt，
    保证两个入口在“向量失败后的降级策略”上保持一致。
    """

    return (
        "检索降级策略（必须遵守）:\n"
        "1) 优先使用向量检索（hybrid_search 或 semantic_search）。\n"
        "2) 若向量检索失败/索引未就绪/结果明显不足，最多重试 1 次。\n"
        "3) 重试后仍失败，立即降级到文本检索链路："
        "grep_files -> list_files -> read_file。\n"
        "4) 输出时必须标注检索模式：vector 或 fallback_text，"
        "并给出降级原因。\n"
    )


def prompt_list_for_rpc(*, bound_workspace: str | None = None) -> list[dict[str, Any]]:
    """构造 MCP prompts/list 返回结构。

    bound_workspace 不为空时，表示当前入口已经绑定工作空间；
    为避免重复输入，list 展示时会隐藏 workspace 参数。
    """

    result: list[dict[str, Any]] = []
    for template in PROMPT_TEMPLATES.values():
        args = []
        for arg in template.arguments:
            if bound_workspace and arg.name == "workspace":
                continue
            args.append(
                {
                    "name": arg.name,
                    "description": arg.description,
                    "required": arg.required,
                }
            )
        result.append(
            {
                "name": template.name,
                "description": template.description,
                "arguments": args,
            }
        )
    return result


def render_prompt_text(
    *,
    name: str,
    arguments: dict[str, Any] | None,
    bound_workspace: str | None = None,
) -> str:
    """渲染 prompt 文本内容（供 prompts/get 使用）。"""

    params = dict(arguments or {})

    if bound_workspace:
        params["workspace"] = bound_workspace

    if name == ANALYZE_CODEBASE_QUESTION.name:
        workspace = _required_text(params, "workspace")
        question = _required_text(params, "question")
        scope_hint = _optional_text(params, "scope_hint")
        top_k = _optional_int(params, "top_k", default=8)
        return (
            "你是 Jework 代码库分析助手。\n"
            f"目标工作空间: {workspace}\n"
            f"用户问题: {question}\n"
            f"范围提示: {scope_hint or '(未提供，先全局检索后再收敛)'}\n"
            f"候选召回数量: {top_k}\n\n"
            "请严格按以下步骤调用工具并输出结果:\n"
            "1) 先执行 hybrid_search(workspace, question, top_k)；\n"
            "2) 基于命中结果抽取关键文件路径，使用 read_file "
            "读取关键片段；\n"
            "3) 如需补充符号、关键字、调用点，执行 grep_files；\n"
            "4) 输出必须包含：\n"
            "   - 结论\n"
            "   - 关键证据（文件路径 + 行号范围）\n"
            "   - 相关调用链/模块关系\n"
            "   - 待确认项（如果有）\n\n"
            f"{search_fallback_policy_text()}"
            "禁止臆测代码实现，所有结论必须可回溯到工具输出证据。"
        )

    if name == TRACE_DOC_TO_CODE.name:
        workspace = _required_text(params, "workspace")
        doc_query = _required_text(params, "doc_query")
        doc_path_hint = _optional_text(params, "doc_path_hint")
        top_k = _optional_int(params, "top_k", default=10)
        return (
            "你是 Jework 文档到代码追踪助手。\n"
            f"目标工作空间: {workspace}\n"
            f"文档查询: {doc_query}\n"
            f"文档路径提示: {doc_path_hint or '(未提供)'}\n"
            f"候选召回数量: {top_k}\n\n"
            "请严格按以下流程调用工具:\n"
            "1) 先 semantic_search(workspace, doc_query, top_k)，"
            "同时关注文档片段与代码片段；\n"
            "2) 对候选代码文件使用 read_file 验证是否真的对应"
            "文档语义；\n"
            "3) 必要时结合 list_files/grep_files 补齐上下游实现"
            "（路由、service、model 等）；\n"
            "4) 输出必须包含：\n"
            "   - 文档要点\n"
            "   - 对应实现（文件路径 + 函数/类 + 行号范围）\n"
            "   - 调用链简述\n"
            "   - 差异与缺口（文档有但代码无/代码有但文档无）\n\n"
            f"{search_fallback_policy_text()}"
            "结论必须基于已读取内容，不允许仅凭文件名推断。"
        )

    raise AppError(
        code="MCP_PROMPT_NOT_SUPPORTED",
        message="prompt is not supported",
        details={"name": name},
        status_code=400,
    )


def _required_text(arguments: dict[str, Any], key: str) -> str:
    value = _optional_text(arguments, key)
    if not value:
        raise AppError(
            code="MCP_PROMPT_INVALID_ARGUMENT",
            message=f"{key} is required",
            details={"argument": key},
            status_code=400,
        )
    return value


def _optional_text(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    return str(value).strip() if value is not None else ""


def _optional_int(arguments: dict[str, Any], key: str, *, default: int) -> int:
    value = arguments.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except Exception:
        return default
