from __future__ import annotations

import asyncio
import io
import json
import shlex
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mini_agent.messages import ModelResponse, ToolCall
from mini_agent.tools.shell import make_bash_tool
from mini_agent.web_server import WebApp, WebHandler, LocalServer


class FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.closed = False

    async def complete(self, messages, tools):
        self.calls += 1
        return self.responses.pop(0)

    async def close(self):
        self.closed = True


async def until(predicate, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition timed out")


class WebAppTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = None

    async def asyncTearDown(self):
        if self.app:
            await self.app.shutdown()
        self.temp.cleanup()

    def create(self, responses, configured=True):
        model = FakeModel(responses)
        self.app = WebApp(self.root, model_factory=lambda: model,
                          model_name="fake", configured=configured, secret="test-secret-value")
        return model

    async def test_multiline_task_is_one_request(self):
        model = self.create([ModelResponse(text="Done")])
        prompt = "第一行\n第二行\n第三行"
        result = await self.app.start(prompt, save_session=False)
        run = self.app.runs[result["id"]]
        await run.task
        self.assertEqual(run.prompt, prompt)
        self.assertEqual(model.calls, 1)
        self.assertEqual(run.status, "finished")
        self.assertTrue(model.closed)

    async def test_approve_edit_and_continue(self):
        (self.root / "a.py").write_text("x = 1\n")
        model = self.create([
            ModelResponse(tool_calls=[ToolCall("c1", "edit", {"path":"a.py", "old_text":"x = 1", "new_text":"x = 2"})]),
            ModelResponse(text="已修改"),
        ])
        result = await self.app.start("修复", save_session=False)
        run = self.app.runs[result["id"]]
        await until(lambda: run.pending is not None)
        self.assertEqual(run.status, "awaiting_approval")
        self.assertIn("+x = 2", run.pending["diff"])
        self.assertEqual((self.root / "a.py").read_text(), "x = 1\n")
        await self.app.approve(run.id, run.pending["id"], True)
        await run.task
        self.assertEqual((self.root / "a.py").read_text(), "x = 2\n")
        self.assertEqual(model.calls, 2)
        self.assertEqual(run.status, "finished")
        self.assertIsNone(run.pending)

    async def test_denial_stops_batch_without_alternative_tool(self):
        model = self.create([ModelResponse(tool_calls=[
            ToolCall("c1", "write", {"path":"a.py", "content":"denied"}),
            ToolCall("c2", "write", {"path":"b.py", "content":"must not run"}),
        ])])
        result = await self.app.start("write", save_session=False)
        run = self.app.runs[result["id"]]
        await until(lambda: run.pending is not None)
        await self.app.approve(run.id, run.pending["id"], False)
        await run.task
        self.assertEqual(run.status, "blocked")
        self.assertEqual(model.calls, 1)
        self.assertFalse((self.root / "a.py").exists())
        self.assertFalse((self.root / "b.py").exists())

    async def test_stale_approval_is_rejected(self):
        self.create([ModelResponse(tool_calls=[ToolCall("1","write",{"path":"a","content":"x"})])])
        result = await self.app.start("write", save_session=False)
        run = self.app.runs[result["id"]]
        await until(lambda: run.pending is not None)
        with self.assertRaisesRegex(ValueError, "过期"):
            await self.app.approve(run.id, "wrong-id", True)
        with self.assertRaises(ValueError):
            await self.app.approve(run.id, run.pending["id"], "yes")
        self.assertFalse((self.root / "a").exists())

    async def test_cancel_pending_approval(self):
        self.create([ModelResponse(tool_calls=[ToolCall("1","write",{"path":"a","content":"x"})])])
        result = await self.app.start("write", save_session=False)
        run = self.app.runs[result["id"]]
        await until(lambda: run.pending is not None)
        await self.app.cancel(run.id)
        await run.task
        self.assertEqual(run.status, "cancelled")
        self.assertIsNone(run.pending)
        self.assertFalse((self.root / "a").exists())

    async def test_no_parallel_runs_or_workspace_switch(self):
        self.create([ModelResponse(tool_calls=[ToolCall("1","write",{"path":"a","content":"x"})])])
        result = await self.app.start("write", save_session=False)
        run = self.app.runs[result["id"]]
        await until(lambda: run.pending is not None)
        with self.assertRaises(ValueError):
            await self.app.start("second")
        with self.assertRaises(ValueError):
            await self.app.set_workspace(str(self.root))

    async def test_readonly_rejects_write_without_prompt(self):
        model = self.create([ModelResponse(tool_calls=[ToolCall("1","write",{"path":"a","content":"x"})])])
        result = await self.app.start("write", "read_only", save_session=False)
        run = self.app.runs[result["id"]]
        await run.task
        self.assertEqual(run.status, "blocked")
        self.assertIsNone(run.pending)
        self.assertEqual(model.calls, 1)

    async def test_files_hide_credentials_and_confine_paths(self):
        self.create([])
        (self.root / ".env").write_text("private")
        (self.root / "a.py").write_text("print(1)")
        (self.root / "folder").mkdir()
        result = await self.app.files()
        names = [f["name"] for f in result["files"]]
        self.assertNotIn(".env", names)
        self.assertIn("a.py", names)
        for path in (".env", "../.env", "/etc/passwd"):
            with self.assertRaises(ValueError):
                await self.app.file(path)
        preview = await self.app.file("a.py")
        self.assertEqual(preview["content"], "print(1)")

    async def test_model_output_secret_is_redacted_in_snapshot(self):
        self.create([ModelResponse(text="test-secret-value")])
        response = await self.app.start("read", save_session=False)
        await self.app.runs[response["id"]].task
        result = await self.app.state(response["id"])
        serialized = json.dumps(result)
        self.assertNotIn("test-secret-value", serialized)
        self.assertIn("[REDACTED]", serialized)

    async def test_missing_config_fails_before_start(self):
        self.create([], configured=False)
        with self.assertRaises(ValueError):
            await self.app.start("task")
        self.assertEqual(self.app.runs, {})

    async def test_bad_limits_and_mode(self):
        self.create([])
        for limit in (0, 51, True, "20"):
            with self.assertRaises(ValueError):
                await self.app.start("task", limit=limit)
        with self.assertRaises(ValueError):
            await self.app.start("task", mode="auto")

    async def test_cancel_shell_kills_child(self):
        marker = self.root / "done"
        started = self.root / "started"
        script = f"from pathlib import Path; import time; Path({str(started)!r}).touch(); time.sleep(0.7); Path({str(marker)!r}).touch()"
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
        tool = make_bash_tool(self.root)
        task = asyncio.create_task(tool.handler(command))
        await until(started.exists)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.75)
        self.assertFalse(marker.exists())

    async def test_shell_output_is_bounded(self):
        tool = make_bash_tool(self.root)
        script = "print('A'*100000)"
        result = await tool.handler(f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}")
        self.assertTrue(result.ok)
        self.assertTrue(result.truncated)
        self.assertLess(len(result.data["stdout"]), 41000)


class FakeSocket:
    def __init__(self, request):
        self.input = io.BytesIO(request)
        self.output = bytearray()

    def makefile(self, *args, **kwargs):
        return self.input

    def sendall(self, data):
        self.output.extend(data)


class HTTPBoundaryTests(unittest.TestCase):
    def request(self, path="/", *, host="127.0.0.1:8765", token="", origin="", method="GET", body=b"", content_type="application/json"):
        app = WebApp(Path("."), model_factory=lambda:None)
        server = SimpleNamespace(server_port=8765, token="correct-token", app=app,
                                 dispatch=lambda coro:asyncio.run(coro))
        headers = [f"{method} {path} HTTP/1.1", f"Host: {host}", "Connection: close"]
        if token:
            headers.append("X-Miniagent-Token: " + token)
        if origin:
            headers.append("Origin: " + origin)
        if method == "POST":
            headers.extend(["Content-Type: " + content_type, f"Content-Length: {len(body)}"])
        socket = FakeSocket(("\r\n".join(headers) + "\r\n\r\n").encode() + body)
        WebHandler(socket, ("127.0.0.1", 1234), server)
        return bytes(socket.output)

    def test_home_contains_session_token_and_security_headers(self):
        response = self.request()
        self.assertIn(b"200 OK", response)
        self.assertIn(b"correct-token", response)
        self.assertNotIn(b"__SESSION_TOKEN__", response)
        self.assertIn(b"Content-Security-Policy", response)
        self.assertIn(b"frame-ancestors 'none'", response)

    def test_cross_origin_and_dns_rebinding_are_denied(self):
        self.assertIn(b"403", self.request(origin="https://evil.example"))
        self.assertIn(b"403", self.request(host="evil.example:8765"))

    def test_api_requires_session_token(self):
        self.assertIn(b"403", self.request("/api/state"))
        response = self.request("/api/state", token="correct-token")
        self.assertIn(b"200 OK", response)

    def test_static_allowlist_does_not_expose_env(self):
        self.assertIn(b"404", self.request("/.env"))
        self.assertIn(b"404", self.request("/../pyproject.toml"))
        self.assertIn(b"200 OK", self.request("/styles.css"))
        self.assertIn(b"200 OK", self.request("/app.js"))

    def test_mutation_requires_token_and_json(self):
        self.assertIn(b"403", self.request("/api/run", method="POST", body=b"{}"))
        self.assertIn(b"415", self.request("/api/run", method="POST", token="correct-token", body=b"{}", content_type="text/plain"))
        self.assertIn(b"400", self.request("/api/run", method="POST", token="correct-token", body=b"not-json"))

    def test_bind_failure_closes_without_secondary_error(self):
        app = WebApp(Path("."), model_factory=lambda:None)
        with patch.object(LocalServer, "server_bind", side_effect=PermissionError("blocked")):
            with self.assertRaises(PermissionError):
                LocalServer(app, 0)
