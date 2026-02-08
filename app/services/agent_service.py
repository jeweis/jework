from collections.abc import AsyncGenerator
import logging
import os
from typing import Any, Callable

from app.core.errors import AgentInvocationError

logger = logging.getLogger(__name__)
DEFAULT_AGENT_MAX_TURNS = 20
DEFAULT_READ_ONLY_TOOLS = ["Read", "Glob", "Grep", "WebSearch", "WebFetch"]


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


async def stream_agent_response(
    prompt: str,
    cwd: str,
    env: dict[str, str] | None = None,
    resume_session_id: str | None = None,
    on_claude_session_id: Callable[[str], None] | None = None,
) -> AsyncGenerator[dict[str, str], None]:
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

    def _log_stderr(line: str) -> None:
        logger.error("[claude-cli] %s", line)

    options = ClaudeAgentOptions(
        cwd=cwd,
        allowed_tools=_resolve_allowed_tools(),
        max_turns=_resolve_agent_max_turns(),
        env=env or {},
        stderr=_log_stderr,
        setting_sources=["user", "project"],
        resume=resume_session_id,
        continue_conversation=bool(resume_session_id),
        include_partial_messages=True,
    )

    try:
        logger.warning(
            "[claude-sdk] query start prompt=%r cwd=%s allowed_tools=%s",
            prompt,
            cwd,
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
                        index = event_payload.get("index")
                        name = block.get("name")
                        if isinstance(index, int):
                            tool_inputs[index] = ""
                            if isinstance(name, str) and name:
                                tool_names[index] = name
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
                        index = event_payload.get("index")
                        part = delta.get("partial_json")
                        if isinstance(index, int) and isinstance(part, str) and part:
                            current = tool_inputs.get(index, "")
                            current += part
                            tool_inputs[index] = current
                            name = tool_names.get(index, "Read")
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

        logger.warning("[claude-sdk] query finished cwd=%s", cwd)
    except Exception as exc:
        logger.exception("[claude-sdk] query failed cwd=%s", cwd)
        raise AgentInvocationError(str(exc)) from exc
