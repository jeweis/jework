from collections.abc import AsyncGenerator
from pathlib import Path
import logging
import os
from typing import Any, Callable

from app.core.errors import AgentInvocationError

logger = logging.getLogger(__name__)
DEFAULT_AGENT_MAX_TURNS = 20
DEFAULT_READ_ONLY_TOOLS = ["Skill", "Read", "Glob", "Grep", "WebSearch", "WebFetch"]
DEFAULT_PERSONAL_WRITE_TOOLS = [
    "Skill",
    "Read",
    "Glob",
    "Grep",
    "Edit",
    "MultiEdit",
    "Write",
    "WebSearch",
    "WebFetch",
]
MAX_STDERR_LINES = 40
WRITE_LIKE_TOOL_NAMES = {"write", "edit", "multiedit", "notebookedit"}


def _resolve_agent_max_turns() -> int:
    raw = os.getenv("CLAUDE_AGENT_MAX_TURNS", str(DEFAULT_AGENT_MAX_TURNS))
    try:
        parsed = int(raw)
        if parsed < 1:
            return DEFAULT_AGENT_MAX_TURNS
        return parsed
    except ValueError:
        return DEFAULT_AGENT_MAX_TURNS


def _resolve_allowed_tools() -> list[str]:
    raw = os.getenv("CLAUDE_AGENT_ALLOWED_TOOLS", "")
    parsed = [item.strip() for item in raw.split(",") if item.strip()]
    if parsed:
        return parsed
    return DEFAULT_READ_ONLY_TOOLS


def _resolve_personal_allowed_tools() -> list[str]:
    raw = os.getenv("CLAUDE_AGENT_ALLOWED_PERSONAL_TOOLS", "")
    parsed = [item.strip() for item in raw.split(",") if item.strip()]
    if parsed:
        return parsed
    return DEFAULT_PERSONAL_WRITE_TOOLS


def _tool_use_name(block: Any) -> str | None:
    if block.__class__.__name__ == "ToolUseBlock":
        name = getattr(block, "name", None)
        if isinstance(name, str) and name:
            return name
    return None


def _extract_candidate_paths(payload: Any) -> list[str]:
    """
    从工具输入中提取潜在路径字段，供工作空间越界校验使用。

    说明：
    - Claude 不同工具参数命名并不完全一致，这里做“尽力提取”。
    - 命中字段后统一走 resolve + 前缀比较，避免字符串绕过。
    """
    candidates: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            normalized_key = str(key).lower()
            if normalized_key in {
                "path",
                "file_path",
                "filepath",
                "target_path",
                "targetfile",
                "target_file",
                "file",
                "filename",
                "source_path",
                "destination_path",
                "notebook_path",
                "new_path",
            }:
                if isinstance(value, str) and value.strip():
                    candidates.append(value.strip())
                continue
            if normalized_key in {"paths", "files"} and isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item.strip():
                        candidates.append(item.strip())
                continue
            candidates.extend(_extract_candidate_paths(value))
    elif isinstance(payload, list):
        for item in payload:
            candidates.extend(_extract_candidate_paths(item))
    return candidates


def _is_path_in_workspace(candidate: str, workspace_root: Path) -> bool:
    # 相对路径按 workspace 根解析；绝对路径直接校验前缀。
    raw = Path(candidate)
    resolved = raw.resolve() if raw.is_absolute() else (workspace_root / raw).resolve()
    return resolved == workspace_root or workspace_root in resolved.parents


async def _single_prompt_stream(prompt: str):
    yield {
        "type": "user",
        "message": {"role": "user", "content": prompt},
        "parent_tool_use_id": None,
        "session_id": "",
    }


def _has_runtime_env_auth(env: dict[str, str] | None) -> bool:
    candidates = (
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
    )
    runtime_env = env or {}
    return any(bool(runtime_env.get(key, "").strip()) for key in candidates)

def _validate_agent_runtime(cwd: str, env: dict[str, str] | None = None) -> dict[str, str]:
    """
    在调用 SDK 前做快速预检，尽早给出可读错误信息。
    """
    cwd_path = Path(cwd).resolve()
    if not cwd_path.exists() or not cwd_path.is_dir():
        raise AgentInvocationError(f"Agent working directory not found: {cwd_path}")

    # Jework 强制要求使用“激活的 LLM 配置”提供鉴权信息，不允许依赖 Claude CLI 本地登录态。
    has_env_auth = _has_runtime_env_auth(env)
    if not has_env_auth:
        raise AgentInvocationError(
            "Claude runtime auth is missing from active LLM config. "
            "Set ANTHROPIC_AUTH_TOKEN (or ANTHROPIC_API_KEY) in active LLM config."
        )

    return {
        "cwd": str(cwd_path),
        "has_auth": "env",
    }


def _format_agent_error(
    exc: Exception,
    stderr_lines: list[str],
    diagnostics: dict[str, str] | None = None,
) -> str:
    raw = str(exc).strip() or exc.__class__.__name__

    stderr_from_exc = getattr(exc, "stderr", None)
    if isinstance(stderr_from_exc, str) and stderr_from_exc.strip():
        return f"{raw}\nCLI stderr:\n{stderr_from_exc.strip()}"

    if stderr_lines:
        tail = "\n".join(stderr_lines[-12:])
        base = f"{raw}\nCLI stderr (tail):\n{tail}"
    else:
        base = raw

    if diagnostics:
        diagnostic_text = "\n".join([f"{key}={value}" for key, value in diagnostics.items()])
        return f"{base}\nRuntime diagnostics:\n{diagnostic_text}"

    return base


async def stream_agent_response(
    prompt: str,
    cwd: str,
    env: dict[str, str] | None = None,
    resume_session_id: str | None = None,
    on_claude_session_id: Callable[[str], None] | None = None,
    allowed_tools_override: list[str] | None = None,
    mcp_servers: dict[str, dict[str, object]] | None = None,
    enable_workspace_write_guard: bool = False,
) -> AsyncGenerator[dict[str, str], None]:
    runtime_diag = _validate_agent_runtime(cwd, env=env)

    try:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            PermissionResultAllow,
            PermissionResultDeny,
            ResultMessage,
            TextBlock,
            query,
        )
    except Exception as exc:
        raise AgentInvocationError(f"claude-agent-sdk import failed: {exc}") from exc

    stderr_lines: list[str] = []

    def _log_stderr(line: str) -> None:
        logger.error("[claude-cli] %s", line)
        stderr_lines.append(line)
        if len(stderr_lines) > MAX_STDERR_LINES:
            del stderr_lines[:-MAX_STDERR_LINES]

    resume_candidates: list[str | None] = [resume_session_id]
    if resume_session_id:
        # 历史会话 ID 失效时自动降级为新会话，避免用户手动删会话。
        resume_candidates.append(None)

    last_error: Exception | None = None
    for index, resume_candidate in enumerate(resume_candidates):
        has_resume = bool(resume_candidate)
        workspace_root = Path(cwd).resolve()

        async def _can_use_tool(
            tool_name: str,
            tool_input: dict[str, Any],
            _context: Any,
        ):
            # 个人可写模式：强制所有路径型参数留在 workspace_root 内。
            if not enable_workspace_write_guard:
                return PermissionResultAllow()
            normalized_tool_name = str(tool_name).strip().lower()
            candidates = _extract_candidate_paths(tool_input)
            # 写类工具必须显式提供路径参数；否则拒绝，避免“漏提取字段即放行”。
            if normalized_tool_name in WRITE_LIKE_TOOL_NAMES and not candidates:
                return PermissionResultDeny(
                    message=(
                        "Write-like tool requires explicit path arguments within workspace. "
                        f"tool={tool_name}"
                    ),
                    interrupt=False,
                )
            blocked = False
            for candidate in candidates:
                if not _is_path_in_workspace(candidate, workspace_root):
                    blocked = True
                    break
            if blocked:
                return PermissionResultDeny(
                    message=(
                        "Path is outside current workspace and was blocked by server guard. "
                        f"tool={tool_name}"
                    ),
                    interrupt=False,
                )
            return PermissionResultAllow()

        if allowed_tools_override is not None:
            resolved_tools = allowed_tools_override
        elif enable_workspace_write_guard:
            resolved_tools = _resolve_personal_allowed_tools()
        else:
            resolved_tools = _resolve_allowed_tools()
        options = ClaudeAgentOptions(
            cwd=cwd,
            allowed_tools=resolved_tools,
            # personal 工作台禁用 Bash，避免绕过目录边界写入 /mnt 等非工作区路径。
            disallowed_tools=["Bash"] if enable_workspace_write_guard else [],
            max_turns=_resolve_agent_max_turns(),
            env=env or {},
            mcp_servers=mcp_servers or {},
            stderr=_log_stderr,
            # 显式包含 local，确保项目下 .claude-local 配置也可被 Claude CLI 读取。
            setting_sources=["user", "project", "local"],
            resume=resume_candidate,
            continue_conversation=has_resume,
            include_partial_messages=True,
            permission_mode="acceptEdits" if enable_workspace_write_guard else None,
            can_use_tool=_can_use_tool if enable_workspace_write_guard else None,
        )
        try:
            logger.warning(
                "[claude-sdk] query start prompt=%r cwd=%s resume=%s allowed_tools=%s",
                prompt,
                cwd,
                resume_candidate,
                options.allowed_tools,
            )
            has_partial = False
            has_assistant_text = False
            tool_names: dict[int, str] = {}
            tool_inputs: dict[int, str] = {}

            query_prompt = _single_prompt_stream(prompt) if enable_workspace_write_guard else prompt
            async for message in query(prompt=query_prompt, options=options):
                logger.warning(
                    "[claude-sdk] message type=%s payload=%r",
                    type(message).__name__,
                    message,
                )
                message_subtype = getattr(message, "subtype", None)
                if type(message).__name__ == "SystemMessage" and message_subtype == "init":
                    init_data = getattr(message, "data", {}) or {}
                    if isinstance(init_data, dict):
                        init_summary = {
                            "keys": sorted(str(key) for key in init_data.keys()),
                            "slash_commands": init_data.get("slash_commands"),
                            "agents": init_data.get("agents"),
                            "output_style": init_data.get("output_style"),
                            "plugins": init_data.get("plugins"),
                        }
                        logger.warning(
                            "[claude-sdk] init summary=%s",
                            init_summary,
                        )

                # StreamEvent may not be exported from top-level package in some versions.
                # Detect partial stream events by structural typing.
                event_payload = getattr(message, "event", None)
                if isinstance(event_payload, dict):
                    event_type = event_payload.get("type")
                    logger.warning("[claude-sdk] stream event=%s", event_payload)
                    if event_type == "content_block_start":
                        block = event_payload.get("content_block", {})
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            index_id = event_payload.get("index")
                            name = block.get("name")
                            if isinstance(index_id, int):
                                tool_inputs[index_id] = ""
                                if isinstance(name, str) and name:
                                    tool_names[index_id] = name
                                    yield {
                                        "type": "tool",
                                        "title": f"调用工具: {name}",
                                        "data": "",
                                    }

                    if event_type == "content_block_delta":
                        delta = event_payload.get("delta", {})
                        delta_type = delta.get("type")
                        if delta_type == "text_delta":
                            text = delta.get("text")
                            if isinstance(text, str) and text:
                                has_partial = True
                                yield {"type": "chunk", "data": text}
                        elif delta_type == "thinking_delta":
                            thinking = delta.get("thinking")
                            if isinstance(thinking, str) and thinking:
                                yield {"type": "thinking", "data": thinking}
                        elif delta_type == "input_json_delta":
                            index_id = event_payload.get("index")
                            part = delta.get("partial_json")
                            if (
                                isinstance(index_id, int)
                                and isinstance(part, str)
                                and part
                            ):
                                current = tool_inputs.get(index_id, "")
                                current += part
                                tool_inputs[index_id] = current
                                name = tool_names.get(index_id, "Read")
                                yield {
                                    "type": "tool",
                                    "title": f"调用工具: {name}",
                                    "data": current,
                                }
                    continue

                if isinstance(message, AssistantMessage):
                    if has_partial:
                        continue
                    for block in message.content:
                        if isinstance(block, TextBlock) and block.text:
                            has_assistant_text = True
                            yield {"type": "chunk", "data": block.text}
                        else:
                            tool_name = _tool_use_name(block)
                            if tool_name:
                                yield {
                                    "type": "tool",
                                    "title": f"调用工具: {tool_name}",
                                    "data": str(getattr(block, "input", {})),
                                }
                    continue

                if isinstance(message, ResultMessage):
                    claude_session_id = getattr(message, "session_id", None)
                    if (
                        on_claude_session_id is not None
                        and isinstance(claude_session_id, str)
                        and claude_session_id
                    ):
                        on_claude_session_id(claude_session_id)
                    logger.warning(
                        "[claude-sdk] result subtype=%s is_error=%s result=%r usage=%r",
                        message.subtype,
                        message.is_error,
                        message.result,
                        message.usage,
                    )
                    if message.subtype == "error_max_turns":
                        raise AgentInvocationError(
                            "Claude agent reached max turns before producing final text. "
                            "Please increase CLAUDE_AGENT_MAX_TURNS."
                        )
                    if not has_partial and not has_assistant_text:
                        if isinstance(message.result, str) and message.result.strip():
                            yield {"type": "chunk", "data": message.result}

            logger.warning(
                "[claude-sdk] query finished cwd=%s resume=%s",
                cwd,
                resume_candidate,
            )
            return
        except Exception as exc:
            last_error = exc
            logger.exception(
                "[claude-sdk] query failed cwd=%s resume=%s",
                cwd,
                resume_candidate,
            )
            # 仅在首次使用 resume 失败时自动重试一次新会话。
            if has_resume and index == 0 and len(resume_candidates) > 1:
                logger.warning(
                    "[claude-sdk] resume session failed, fallback to new session. "
                    "old_session_id=%s",
                    resume_candidate,
                )
                continue
            raise AgentInvocationError(
                _format_agent_error(exc, stderr_lines, diagnostics=runtime_diag)
            ) from exc

    if last_error is not None:
        raise AgentInvocationError(
            _format_agent_error(last_error, stderr_lines, diagnostics=runtime_diag)
        ) from last_error
