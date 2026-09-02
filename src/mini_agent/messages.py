"""Small provider-neutral message and tool result models."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

    def to_openai(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, ensure_ascii=False),
            },
        }


@dataclass(slots=True)
class ModelResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str | None = None

    def to_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": self.text or None,
        }
        if self.tool_calls:
            message["tool_calls"] = [call.to_openai() for call in self.tool_calls]
        return message


@dataclass(slots=True)
class ToolResult:
    ok: bool
    tool: str
    data: dict[str, Any] | None = None
    error: str | None = None
    truncated: bool = False

    def serialize(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, default=str)

    @classmethod
    def failure(cls, tool: str, error: str) -> "ToolResult":
        return cls(ok=False, tool=tool, error=error)

