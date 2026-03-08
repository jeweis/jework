from collections.abc import AsyncGenerator
from pathlib import Path
import logging
import os
import re
import shutil
import subprocess
from typing import Any, Callable

from app.core.errors import AgentInvocationError

logger = logging.getLogger(__name__)
DEFAULT_AGENT_MAX_TURNS = 20
DEFAULT_READ_ONLY_TOOLS = ["Skill", "Read", "Glob", "Grep", "WebSearch", "WebFetch"]
MAX_STDERR_LINES = 40
MIN_CLAUDE_CLI_VERSION = (2, 0, 0)


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


def _tool_use_name(block: Any) -> str | None:
    if block.__class__.__name__ == "ToolUseBlock":
        name = getattr(block, "name", None)
        if isinstance(name, str) and name:
            return name
    return None


def _resolve_cli_path() -> str:
    """
    解析 Claude CLI 路径。

    优先级：
    1) 环境变量显式指定（与 SDK option 语义保持一致）；
    2) PATH 中的 `claude`。
    """
    explicit = os.getenv("CLAUDE_CODE_CLI_PATH", "").strip()
    if explicit:
        return explicit
    return "claude"


def _parse_semver(version_text: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", version_text)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _has_anthropic_auth(env: dict[str, str] | None) -> bool:
    candidates = (
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
    )
    merged: dict[str, str] = {}
    merged.update(os.environ)
    if env:
        merged.update(env)
    return any(bool(merged.get(key, "").strip()) for key in candidates)


def _validate_agent_runtime(cwd: str, env: dict[str, str] | None = None) -> dict[str, str]:
    """
    在调用 SDK 前做快速预检，尽早给出可读错误信息。
    """
    cwd_path = Path(cwd).resolve()
    if not cwd_path.exists() or not cwd_path.is_dir():
        raise AgentInvocationError(f"Agent working directory not found: {cwd_path}")

    cli = _resolve_cli_path()
    if cli == "claude":
        detected = shutil.which("claude")
        if not detected:
            raise AgentInvocationError(
                "Claude CLI not found in PATH. "
                "Install it or set CLAUDE_CODE_CLI_PATH."
            )
    elif not Path(cli).exists():
        raise AgentInvocationError(f"CLAUDE_CODE_CLI_PATH not found: {cli}")

    try:
        version_process = subprocess.run(
            [cli, "-v"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:
        raise AgentInvocationError(f"Claude CLI preflight failed: {exc}") from exc

    version_stdout = (version_process.stdout or "").strip()
    version_stderr = (version_process.stderr or "").strip()
    version_text = version_stdout or version_stderr
    parsed = _parse_semver(version_text)
    if parsed is None:
        raise AgentInvocationError(
            "Unable to parse Claude CLI version. "
            f"raw_output={version_text or '<empty>'}"
        )
    if parsed < MIN_CLAUDE_CLI_VERSION:
        raise AgentInvocationError(
            "Claude CLI version is too old for claude-agent-sdk. "
            f"current={parsed[0]}.{parsed[1]}.{parsed[2]}, "
            f"required>={MIN_CLAUDE_CLI_VERSION[0]}.{MIN_CLAUDE_CLI_VERSION[1]}.{MIN_CLAUDE_CLI_VERSION[2]}"
        )

    if not _has_anthropic_auth(env):
        raise AgentInvocationError(
            "Claude runtime auth is missing. "
            "Set ANTHROPIC_AUTH_TOKEN (or ANTHROPIC_API_KEY) in active LLM config."
        )

    return {
        "cli_path": cli,
        "cli_version": f"{parsed[0]}.{parsed[1]}.{parsed[2]}",
        "cwd": str(cwd_path),
        "has_auth": "true",
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
) -> AsyncGenerator[dict[str, str], None]:
    runtime_diag = _validate_agent_runtime(cwd, env=env)

    try:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
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
        options = ClaudeAgentOptions(
            cwd=cwd,
            allowed_tools=_resolve_allowed_tools(),
            max_turns=_resolve_agent_max_turns(),
            env=env or {},
            stderr=_log_stderr,
            setting_sources=["user", "project"],
            resume=resume_candidate,
            continue_conversation=has_resume,
            include_partial_messages=True,
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

            async for message in query(prompt=prompt, options=options):
                logger.warning(
                    "[claude-sdk] message type=%s payload=%r",
                    type(message).__name__,
                    message,
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
