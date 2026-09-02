from __future__ import annotations

import asyncio
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mini_agent.agent import AgentRunResult
from mini_agent.approvals import ApprovalRequest, InteractiveApprovalPolicy
from mini_agent.cli import _read_task_argument, _run, build_parser
from mini_agent.terminal_input import MultilineTaskReader, TerminalInput, TerminalUnavailable


class MemoryConsole:
    def __init__(self, lines):
        self.lines = list(lines)
        self.output = []
        self.closed = False
        self.flushes = 0

    def write(self, text):
        self.output.append(text)

    async def readline(self, prompt):
        self.write(prompt)
        await asyncio.sleep(0)
        if not self.lines:
            raise EOFError
        return self.lines.pop(0)

    def discard_pending(self):
        self.flushes += 1

    def close(self):
        self.closed = True


class MultilineTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiline_blank_lines_and_code_are_one_task(self):
        lines = ["实现移动零", "", "要求：", "保持顺序", "```cpp", "y", "```"]
        terminal = MemoryConsole(lines + ["/send", "next"])
        result = await MultilineTaskReader(terminal).read_task()
        self.assertEqual(result, "\n".join(lines))
        self.assertEqual(terminal.lines, ["next"])

    async def test_enter_does_not_submit_a_single_line(self):
        with self.assertRaises(EOFError):
            await MultilineTaskReader(MemoryConsole(["first line"])).read_task()

    async def test_empty_send_does_not_submit(self):
        terminal = MemoryConsole(["/send", "", "/send", "task", "/send"])
        self.assertEqual(await MultilineTaskReader(terminal).read_task(), "task")

    async def test_cancel_discards_whole_draft(self):
        terminal = MemoryConsole(["one", "two", "/cancel", "new", "/send"])
        reader = MultilineTaskReader(terminal)
        self.assertEqual(await reader.read_task(), "")
        self.assertEqual(await reader.read_task(), "new")

    async def test_commands_and_escaped_literals(self):
        for command in ("/help", "/clear", "/exit", "/quit"):
            self.assertEqual(await MultilineTaskReader(MemoryConsole([command])).read_task(), command)
        terminal = MemoryConsole(["body", "//send", "//cancel", "/help", "/send"])
        self.assertEqual(await MultilineTaskReader(terminal).read_task(), "body\n/send\n/cancel\n/help")

    async def test_escaped_command_is_task_text_not_cli_command(self):
        terminal = MemoryConsole(["/help", "//help", "/send"])
        reader = MultilineTaskReader(terminal)
        self.assertEqual(await reader.read_task(), "/help")
        self.assertEqual(reader.command, "/help")
        self.assertEqual(await reader.read_task(), "/help")
        self.assertIsNone(reader.command)

    async def test_cli_sends_one_whole_task_without_builtin_input(self):
        terminal = MemoryConsole(["first", "", "second", "third", "/send", "/exit"])
        calls = []

        class FakeAgent:
            model = SimpleNamespace()

            async def run(self, task):
                calls.append(task)
                return AgentRunResult("completed", "done", 1)

        args = build_parser().parse_args([])
        with patch("mini_agent.cli.TerminalInput", return_value=terminal), \
             patch("mini_agent.cli._build_agent", return_value=FakeAgent()), \
             patch("builtins.input", side_effect=AssertionError("must not use buffered input")), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(await _run(args), 0)
        self.assertEqual(calls, ["first\n\nsecond\nthird"])
        self.assertTrue(terminal.closed)

    async def test_no_controlling_terminal_never_reads_stdin_as_approval(self):
        text = io.StringIO("y\nthis is task content\n")
        request = ApprovalRequest("write", {}, "write", "write a.py")
        with patch("mini_agent.approvals.TerminalInput", side_effect=TerminalUnavailable("no tty")), \
             patch("sys.stdin", text), patch("sys.stdout", new_callable=io.StringIO):
            self.assertFalse(await InteractiveApprovalPolicy().approve(request))
        self.assertEqual(text.tell(), 0)


class TaskFileTests(unittest.TestCase):
    def test_reads_entire_utf8_file_without_command_parsing(self):
        text = "要求一\n\n要求二\n/send\ny\n"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "task.txt"
            path.write_text(text, encoding="utf-8")
            args = build_parser().parse_args(["--task-file", str(path)])
            self.assertEqual(_read_task_argument(args), text)

    def test_stdin_task_is_read_to_eof(self):
        text = "first\nsecond\ny\n"
        with patch("sys.stdin", io.StringIO(text)):
            args = build_parser().parse_args(["--task-file", "-"])
            self.assertEqual(_read_task_argument(args), text)

    def test_missing_file_has_readable_error(self):
        args = build_parser().parse_args(["--task-file", "/path/does-not-exist/task.txt"])
        with self.assertRaisesRegex(ValueError, "无法读取任务文件"):
            _read_task_argument(args)

    def test_task_sources_are_mutually_exclusive(self):
        with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
            build_parser().parse_args(["--task", "one", "--task-file", "task.txt"])


async def wait_until(predicate, timeout=2):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("terminal prompt did not arrive")
        await asyncio.sleep(0.005)


@unittest.skipIf(os.name == "nt", "POSIX pseudoterminal tests")
class TerminalBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import pty

        try:
            self.master, self.slave = pty.openpty()
        except OSError as exc:
            self.skipTest(f"Pseudoterminal unavailable: {exc}")

        class ObservedTerminal(TerminalInput):
            def __init__(inner, fd):
                super().__init__(fd)
                inner.output = []

            def write(inner, text):
                super().write(text)
                inner.output.append(text)

        self.terminal = ObservedTerminal(self.slave)

    async def asyncTearDown(self):
        if hasattr(self, "terminal"):
            self.terminal.close()
            os.close(self.slave)
            os.close(self.master)

    async def test_paste_remainder_cannot_approve(self):
        os.write(self.master, "移动零\n\n保持顺序\n/send\ny\n".encode())
        task = await MultilineTaskReader(self.terminal).read_task()
        self.assertEqual(task, "移动零\n\n保持顺序")
        decision = asyncio.create_task(self.terminal.read_approval("Allow? [y/N] "))
        await wait_until(lambda: "Allow? [y/N] " in self.terminal.output)
        await asyncio.sleep(0.03)
        self.assertFalse(decision.done(), "queued y must not grant permission")
        os.write(self.master, b"n\n")
        self.assertFalse(await asyncio.wait_for(decision, 2))

    async def test_stale_no_does_not_reject_fresh_yes(self):
        # Protect against stale bytes in both Python-owned and kernel buffers.
        self.terminal._buffer.extend(b"n\n")
        os.write(self.master, b"n\n")
        decision = asyncio.create_task(self.terminal.read_approval("Allow? [y/N] "))
        await wait_until(lambda: "Allow? [y/N] " in self.terminal.output)
        await asyncio.sleep(0.03)
        self.assertFalse(decision.done())
        os.write(self.master, b"y\n")
        self.assertTrue(await asyncio.wait_for(decision, 2))

    async def test_invalid_paste_reprompts_and_discards_following_yes(self):
        decision = asyncio.create_task(self.terminal.read_approval("Allow? [y/N] "))
        await wait_until(lambda: self.terminal.output.count("Allow? [y/N] ") == 1)
        os.write(self.master, "这是任务内容\ny\n".encode())
        await wait_until(lambda: self.terminal.output.count("Allow? [y/N] ") == 2)
        await asyncio.sleep(0.03)
        self.assertFalse(decision.done())
        os.write(self.master, b"yes\n")
        self.assertTrue(await asyncio.wait_for(decision, 2))

    async def test_cancel_read_does_not_leave_a_background_consumer(self):
        reading = asyncio.create_task(self.terminal.readline("task> "))
        await wait_until(lambda: "task> " in self.terminal.output)
        reading.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await reading
        os.write(self.master, b"next\n")
        self.assertEqual(await asyncio.wait_for(self.terminal.readline("new> "), 2), "next")

    async def test_mac_kqueue_reader_registration_is_not_required(self):
        # Reproduce the user's event-loop capability: registering /dev/tty
        # would raise EINVAL. Terminal input must not call that API at all.
        loop = asyncio.get_running_loop()
        with patch.object(loop, "add_reader", side_effect=OSError(22, "Invalid argument")) as register:
            reading = asyncio.create_task(self.terminal.readline("mac> "))
            await wait_until(lambda: "mac> " in self.terminal.output)
            os.write(self.master, "多行输入仍然可用\n".encode())
            self.assertEqual(await asyncio.wait_for(reading, 2), "多行输入仍然可用")
            register.assert_not_called()

    async def test_idle_read_yields_and_cancels_without_input(self):
        reading = asyncio.create_task(self.terminal.readline("idle> "))
        await wait_until(lambda: "idle> " in self.terminal.output)
        # If the reader performs a blocking os.read, this heartbeat cannot run.
        await asyncio.wait_for(asyncio.sleep(0.03), 1)
        self.assertFalse(reading.done())
        reading.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await reading
