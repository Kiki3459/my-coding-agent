"""Model boundary: the agent depends on this protocol, not an SDK response type."""

from __future__ import annotations

import json
from typing import Any, Protocol

from .messages import ModelResponse, ToolCall


class ModelClient(Protocol):
    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelResponse: ...


class OpenAIModelClient:
    """Thin adapter for OpenAI-compatible Chat Completions endpoints."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        timeout: float = 60.0,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        if not model:
            raise ValueError("model is required")
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover - exercised by real setup
            raise RuntimeError(
                "The 'openai' package is not installed. Run: pip install -e ."
            ) from exc

        kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = AsyncOpenAI(**kwargs)
        self.model = model

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        response = await self._client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        msg = choice.message
        calls: list[ToolCall] = []
        for raw_call in msg.tool_calls or []:
            try:
                arguments = json.loads(raw_call.function.arguments or "{}")
                if not isinstance(arguments, dict):
                    raise ValueError("tool arguments must be a JSON object")
            except (json.JSONDecodeError, ValueError) as exc:
                # Keep the malformed call visible to the registry so the model receives
                # a normal tool error instead of crashing the whole run.
                arguments = {"__parse_error__": str(exc)}
            calls.append(
                ToolCall(
                    id=raw_call.id,
                    name=raw_call.function.name,
                    arguments=arguments,
                )
            )
        return ModelResponse(
            text=msg.content or "",
            tool_calls=calls,
            stop_reason=choice.finish_reason,
        )

    async def close(self) -> None:
        await self._client.close()
