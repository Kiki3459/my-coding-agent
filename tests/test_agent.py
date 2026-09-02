from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mini_agent.agent import Agent
from mini_agent.approvals import AutoApprovePolicy
from mini_agent.context import ContextManager
from mini_agent.messages import ModelResponse, ToolCall, ToolResult
from mini_agent.registry import Tool, ToolRegistry
from mini_agent.session import SessionLogger


class FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, messages, tools):
        self.calls.append((messages, tools))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def echo_tool() -> Tool:
    return Tool(
        name="echo",
        description="Echo text",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        handler=lambda text: ToolResult(ok=True, tool="echo", data={"text": text}),
    )


def make_agent(model, *, max_iterations=20, session=None, context=None):
    registry = ToolRegistry(AutoApprovePolicy())
    registry.register(echo_tool())
    return Agent(
        model=model,
        registry=registry,
        system_prompt="system",
        max_iterations=max_iterations,
        session=session,
        context_manager=context,
        retry_attempts=0,
    )


class AgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_then_final_answer(self) -> None:
        model = FakeModel(
            [
                ModelResponse(tool_calls=[ToolCall("1", "echo", {"text": "hi"})]),
                ModelResponse(text="done"),
            ]
        )
        result = await make_agent(model).run("work")
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.text, "done")
        second_messages = model.calls[1][0]
        self.assertEqual(second_messages[-1]["role"], "tool")
        self.assertEqual(second_messages[-1]["tool_call_id"], "1")

    async def test_multiple_tool_calls_keep_ids(self) -> None:
        model = FakeModel(
            [
                ModelResponse(
                    tool_calls=[
                        ToolCall("a", "echo", {"text": "one"}),
                        ToolCall("b", "echo", {"text": "two"}),
                    ]
                ),
                ModelResponse(text="finished"),
            ]
        )
        await make_agent(model).run("work")
        tool_messages = [m for m in model.calls[1][0] if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tool_messages], ["a", "b"])

    async def test_iteration_limit(self) -> None:
        model = FakeModel(
            [
                ModelResponse(tool_calls=[ToolCall(str(i), "echo", {"text": "x"})])
                for i in range(3)
            ]
        )
        result = await make_agent(model, max_iterations=3).run("loop")
        self.assertEqual(result.status, "iteration_limit")
        self.assertEqual(result.iterations, 3)

    async def test_repeated_invalid_call_stops(self) -> None:
        model = FakeModel(
            [
                ModelResponse(tool_calls=[ToolCall(str(i), "missing", {})])
                for i in range(3)
            ]
        )
        result = await make_agent(model).run("loop")
        self.assertEqual(result.status, "repeated_invalid_call")
        self.assertEqual(result.iterations, 3)

    async def test_model_error_is_returned(self) -> None:
        model = FakeModel([RuntimeError("offline")])
        result = await make_agent(model).run("work")
        self.assertEqual(result.status, "error")
        self.assertIn("offline", result.text)

    async def test_session_log_does_not_invent_environment_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            logger = SessionLogger(temp, "test")
            model = FakeModel([ModelResponse(text="done")])
            await make_agent(model, session=logger).run("safe task")
            content = logger.path.read_text(encoding="utf-8")
            self.assertNotIn("OPENAI_API_KEY", content)
            self.assertIn("safe task", content)


class ContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_compaction_falls_back_when_summary_fails(self) -> None:
        context = ContextManager(budget_tokens=1000, keep_recent_messages=4)
        messages = [{"role": "system", "content": "system"}]
        messages.append({"role": "user", "content": "original task"})
        for i in range(20):
            messages.append({"role": "assistant", "content": f"step {i} " + "x" * 300})

        async def broken_summary(text):
            raise RuntimeError("summary unavailable")

        view = await context.prepare(messages, broken_summary)
        self.assertLess(len(view), len(messages))
        self.assertEqual(view[0]["role"], "system")
        self.assertIn("Summary of earlier work", view[1]["content"])
        self.assertIn("original task", str(view))

