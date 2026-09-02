"""Bounded context preparation with model summary and deterministic fallback."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

Summarizer = Callable[[str], Awaitable[str]]


class ContextManager:
    def __init__(
        self,
        *,
        budget_tokens: int = 24_000,
        keep_recent_messages: int = 12,
    ) -> None:
        if budget_tokens < 1_000:
            raise ValueError("budget_tokens must be at least 1000")
        self.budget_tokens = budget_tokens
        self.keep_recent_messages = max(4, keep_recent_messages)
        self._summary_cache: dict[str, str] = {}

    @staticmethod
    def estimate_tokens(messages: list[dict[str, Any]]) -> int:
        # This deliberately simple estimate is provider-neutral. The README
        # documents that it is a safety heuristic rather than an exact tokenizer.
        rendered = json.dumps(messages, ensure_ascii=False, default=str)
        return max(1, len(rendered) // 4)

    async def prepare(
        self,
        messages: list[dict[str, Any]],
        summarizer: Summarizer | None = None,
    ) -> list[dict[str, Any]]:
        if self.estimate_tokens(messages) <= self.budget_tokens:
            return list(messages)

        system = [m for m in messages if m.get("role") == "system"][:1]
        first_user_index = next(
            (i for i, m in enumerate(messages) if m.get("role") == "user"),
            0,
        )
        first_user = [messages[first_user_index]] if messages else []
        start = max(first_user_index + 1, len(messages) - self.keep_recent_messages)

        # Never begin the tail with an orphaned tool result. Walk back to the
        # assistant message that issued the corresponding tool call.
        while start > first_user_index + 1 and messages[start].get("role") == "tool":
            start -= 1

        old_messages = messages[first_user_index + 1 : start]
        tail = messages[start:]
        if not old_messages:
            return system + first_user + tail

        rendered = json.dumps(old_messages, ensure_ascii=False, default=str)
        cache_key = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
        summary = self._summary_cache.get(cache_key)
        if summary is None:
            if summarizer is not None:
                try:
                    summary = (await summarizer(rendered[-40_000:])).strip()
                except Exception:
                    summary = ""
            if not summary:
                summary = _fallback_summary(old_messages)
            self._summary_cache[cache_key] = summary

        summary_message = {
            "role": "system",
            "content": (
                "Summary of earlier work. Treat this as compressed history, not a "
                f"new user request:\n{summary}"
            ),
        }
        return system + [summary_message] + first_user + tail


def _fallback_summary(messages: list[dict[str, Any]]) -> str:
    """Lossy fallback used only when the summary model call fails."""
    points: list[str] = []
    for message in messages[-10:]:
        role = message.get("role", "unknown")
        content = str(message.get("content") or "")
        content = " ".join(content.split())
        if len(content) > 500:
            content = content[:500] + "..."
        if content:
            points.append(f"- {role}: {content}")
    return "Automatic summary was unavailable. Recent retained facts:\n" + "\n".join(points)

