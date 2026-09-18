"""LLM client for the agents: a minimal provider interface plus the OpenAI implementation.

Provider: OpenAI (owner decision 2026-09-18; see DECISIONS "S3"). Plain chat-completions
tool calling, no agent framework (CLAUDE.md out-of-scope list). Prompt caching: OpenAI caches
long identical prompt prefixes automatically, so the system prompt + profile are kept as a
stable prefix and the variable conversation follows.

Messages use the OpenAI chat format (role/content/tool_calls/tool_call_id) end to end.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Protocol


class LlmError(RuntimeError):
    pass


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON text from the model


@dataclass(frozen=True)
class LlmReply:
    content: str | None
    tool_calls: tuple[ToolCall, ...]
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0

    def as_message(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            msg["tool_calls"] = [
                {"id": t.id, "type": "function", "function": {"name": t.name, "arguments": t.arguments}}
                for t in self.tool_calls
            ]
        return msg


class LlmClient(Protocol):
    provider: str

    def chat(self, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LlmReply: ...

    def available_models(self) -> set[str]: ...


@dataclass
class OpenAIClient:
    api_key: str
    provider: str = "openai"
    timeout_s: float = 120.0
    _client: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        from openai import OpenAI

        self._client = OpenAI(api_key=self.api_key, timeout=self.timeout_s, max_retries=2)

    def __repr__(self) -> str:  # never print the key
        return "OpenAIClient(api_key=<redacted>)"

    def available_models(self) -> set[str]:
        return {m.id for m in self._client.models.list()}

    def chat(self, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LlmReply:
        started = time.monotonic()
        try:
            resp = self._client.chat.completions.create(
                model=model, messages=messages, tools=tools, tool_choice="auto", parallel_tool_calls=False,
            )
        except Exception as exc:  # surfaced as LlmError; message text is provider-side, no cell data
            raise LlmError(f"{type(exc).__name__}: {getattr(exc, 'message', '')}"[:300]) from exc
        choice = resp.choices[0].message
        usage = resp.usage
        details = getattr(usage, "prompt_tokens_details", None)
        return LlmReply(
            content=choice.content,
            tool_calls=tuple(
                ToolCall(tc.id, tc.function.name, tc.function.arguments or "{}")
                for tc in (choice.tool_calls or []) if getattr(tc, "function", None) is not None
            ),
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            cached_tokens=(getattr(details, "cached_tokens", 0) or 0) if details else 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            latency_ms=int((time.monotonic() - started) * 1000),
        )


def tool_spec(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
    }}


def parse_args(call: ToolCall) -> dict[str, Any]:
    try:
        args = json.loads(call.arguments or "{}")
    except json.JSONDecodeError as exc:
        raise LlmError(f"tool {call.name}: arguments are not valid JSON ({exc.msg})") from exc
    if not isinstance(args, dict):
        raise LlmError(f"tool {call.name}: arguments must be a JSON object")
    return args
