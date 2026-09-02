"""The intentionally small TAOR/ReAct tool-use loop."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Callable

from .context import ContextManager
from .messages import ModelResponse, ToolCall
from .model import ModelClient
from .registry import ToolRegistry
from .session import NullSessionLogger, SessionLogger

EventHandler = Callable[[str, dict[str, Any]], None]


@dataclass(slots=True)
class AgentRunResult:
    status: str
    text: str
    iterations: int


class Agent:
    def __init__(
        self,
        *,
        model: ModelClient,
        registry: ToolRegistry,
        system_prompt: str,
        context_manager: ContextManager | None = None,
        session: SessionLogger | NullSessionLogger | None = None,
        max_iterations: int = 20,
        event_handler: EventHandler | None = None,
        retry_attempts: int = 2,
    ) -> None:
        if max_iterations < 1:
            raise ValueError("max_iterations must be positive")
        self.model = model
        self.registry = registry
        self.context = context_manager or ContextManager()
        self.session = session or NullSessionLogger()
        self.max_iterations = max_iterations
        self.event_handler = event_handler
        self.retry_attempts = max(0, retry_attempts)
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt}
        ]

    def reset(self) -> None:
        self.messages = self.messages[:1]

    async def run(self, user_input: str) -> AgentRunResult:
        if not user_input.strip():
            return AgentRunResult("error", "The task is empty.", 0)
        self._append({"role": "user", "content": user_input})
        repeated_invalid_signature: str | None = None
        repeated_invalid_count = 0

        for iteration in range(1, self.max_iterations + 1):
            self._emit("iteration", {"iteration": iteration})
            try:
                view = await self.context.prepare(self.messages, self._summarize)
                response = await self._call_model(view, self.registry.schemas())
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                text = f"Model request failed after retries: {type(exc).__name__}: {exc}"
                self._emit("end", {"status": "error", "text": text})
                return AgentRunResult("error", text, iteration)

            self._append(response.to_message())
            if not response.tool_calls:
                text = response.text.strip() or "The model ended without a final message."
                self._emit("end", {"status": "completed", "text": text})
                return AgentRunResult("completed", text, iteration)

            for call_index, call in enumerate(response.tool_calls):
                self._emit(
                    "tool_start",
                    {"name": call.name, "call_id": call.id, "arguments": _redacted_arguments(call)},
                )
                result = await self.registry.execute(call)
                self._emit(
                    "tool_end",
                    {
                        "name": call.name,
                        "call_id": call.id,
                        "ok": result.ok,
                        "error": result.error,
                        "data": result.data,
                    },
                )
                self._append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": result.serialize(),
                    }
                )

                # A denied operation is a permission boundary, not a technical
                # error to route around using another tool. Complete the batch's
                # message pairs and stop before asking the model to try again.
                if result.error == "Operation denied by the active approval policy":
                    from .messages import ToolResult

                    for skipped in response.tool_calls[call_index + 1 :]:
                        self._append({
                            "role": "tool", "tool_call_id": skipped.id,
                            "content": ToolResult.failure(
                                skipped.name, "Skipped because an operation was denied"
                            ).serialize(),
                        })
                    text = "操作已拒绝，本次任务已停止。没有改用其他工具重试；此前已经完成的修改不会自动撤销。"
                    self._emit("end", {"status": "blocked", "text": text})
                    return AgentRunResult("blocked", text, iteration)

                signature = _invalid_signature(call, result.error) if not result.ok else None
                if signature and signature == repeated_invalid_signature:
                    repeated_invalid_count += 1
                elif signature:
                    repeated_invalid_signature = signature
                    repeated_invalid_count = 1
                else:
                    repeated_invalid_signature = None
                    repeated_invalid_count = 0

                if repeated_invalid_count >= 3:
                    text = "Stopped after the model repeated the same invalid tool call three times."
                    self._emit("end", {"status": "repeated_invalid_call", "text": text})
                    return AgentRunResult("repeated_invalid_call", text, iteration)

        text = (
            f"Stopped after reaching the maximum of {self.max_iterations} iterations. "
            "The task may be incomplete."
        )
        self._emit("end", {"status": "iteration_limit", "text": text})
        return AgentRunResult("iteration_limit", text, self.max_iterations)

    async def _call_model(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelResponse:
        last_error: Exception | None = None
        for attempt in range(self.retry_attempts + 1):
            try:
                return await self.model.complete(messages, tools)
            except Exception as exc:
                last_error = exc
                if attempt >= self.retry_attempts:
                    raise
                self._emit("retry", {"attempt": attempt + 1, "error": str(exc)})
                await asyncio.sleep(0.5 * (2**attempt))
        assert last_error is not None
        raise last_error

    async def _summarize(self, rendered_history: str) -> str:
        prompt = (
            "Summarize the earlier coding-agent history below. Preserve the user's goal, "
            "files changed, important tool results, failed attempts, decisions, and remaining "
            "work. Do not invent facts. Return only the concise summary.\n\n"
            f"<history>\n{rendered_history}\n</history>"
        )
        response = await self.model.complete([{"role": "user", "content": prompt}], [])
        return response.text

    def _append(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        self.session.append(message)

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self.event_handler:
            self.event_handler(event, payload)


def _invalid_signature(call: ToolCall, error: str | None) -> str | None:
    if not error:
        return None
    invalid_prefixes = (
        "Unknown tool",
        "Invalid tool argument",
        "Missing required arguments",
        "Unknown arguments",
        "Argument '",
    )
    if not error.startswith(invalid_prefixes):
        return None
    return json.dumps(
        {"name": call.name, "arguments": call.arguments},
        sort_keys=True,
        ensure_ascii=False,
    )


def _redacted_arguments(call: ToolCall) -> dict[str, Any]:
    args = dict(call.arguments)
    if call.name == "write" and "content" in args:
        args["content"] = f"<{len(str(args['content']))} characters>"
    if call.name == "edit":
        if "old_text" in args:
            args["old_text"] = f"<{len(str(args['old_text']))} characters>"
        if "new_text" in args:
            args["new_text"] = f"<{len(str(args['new_text']))} characters>"
    return args
