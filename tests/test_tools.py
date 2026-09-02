from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from mini_agent.approvals import AutoApprovePolicy, ReadOnlyPolicy
from mini_agent.messages import ToolCall
from mini_agent.registry import ToolRegistry
from mini_agent.tools import make_bash_tool, make_edit_tool, make_read_tool, make_write_tool


class FileToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def _execute(self, tool, args, policy=None):
        registry = ToolRegistry(policy or AutoApprovePolicy())
        registry.register(tool)
        return await registry.execute(ToolCall("call-1", tool.name, args))

    async def test_read_with_line_numbers_and_limit(self) -> None:
        (self.root / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
        result = await self._execute(
            make_read_tool(self.root), {"path": "a.txt", "offset": 2, "limit": 1}
        )
        self.assertTrue(result.ok)
        self.assertIn("2 | two", result.data["content"])
        self.assertTrue(result.truncated)

    async def test_parent_escape_is_rejected(self) -> None:
        result = await self._execute(make_read_tool(self.root), {"path": "../secret"})
        self.assertFalse(result.ok)
        self.assertIn("escapes workspace", result.error)

    async def test_absolute_escape_is_rejected(self) -> None:
        result = await self._execute(make_read_tool(self.root), {"path": "/etc/passwd"})
        self.assertFalse(result.ok)
        self.assertIn("escapes workspace", result.error)

    @unittest.skipIf(os.name == "nt", "symlink setup differs on Windows")
    async def test_symlink_escape_is_rejected(self) -> None:
        outside = self.root.parent / f"outside-{self.root.name}.txt"
        outside.write_text("secret", encoding="utf-8")
        try:
            (self.root / "link.txt").symlink_to(outside)
            result = await self._execute(make_read_tool(self.root), {"path": "link.txt"})
            self.assertFalse(result.ok)
            self.assertIn("escapes workspace", result.error)
        finally:
            outside.unlink(missing_ok=True)

    async def test_write_and_read_back(self) -> None:
        result = await self._execute(
            make_write_tool(self.root), {"path": "src/new.py", "content": "x = 1\n"}
        )
        self.assertTrue(result.ok)
        self.assertEqual((self.root / "src/new.py").read_text(), "x = 1\n")

    async def test_write_denied_by_read_only_policy(self) -> None:
        result = await self._execute(
            make_write_tool(self.root),
            {"path": "new.py", "content": "x = 1\n"},
            ReadOnlyPolicy(),
        )
        self.assertFalse(result.ok)
        self.assertFalse((self.root / "new.py").exists())

    async def test_edit_requires_unique_match(self) -> None:
        path = self.root / "a.py"
        path.write_text("x = 1\nx = 1\n", encoding="utf-8")
        result = await self._execute(
            make_edit_tool(self.root),
            {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"},
        )
        self.assertFalse(result.ok)
        self.assertIn("matched 2", result.error)

    async def test_edit_unique_match(self) -> None:
        path = self.root / "a.py"
        path.write_text("x = 1\ny = 1\n", encoding="utf-8")
        result = await self._execute(
            make_edit_tool(self.root),
            {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"},
        )
        self.assertTrue(result.ok)
        self.assertIn("x = 2", path.read_text())

    async def test_unknown_and_invalid_arguments(self) -> None:
        registry = ToolRegistry(AutoApprovePolicy())
        registry.register(make_read_tool(self.root))
        unknown = await registry.execute(ToolCall("1", "nope", {}))
        missing = await registry.execute(ToolCall("2", "read", {}))
        bad_type = await registry.execute(ToolCall("3", "read", {"path": 42}))
        self.assertFalse(unknown.ok)
        self.assertIn("Missing required", missing.error)
        self.assertIn("must be string", bad_type.error)


class BashToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    async def _run(self, command: str, timeout: int = 10):
        registry = ToolRegistry(AutoApprovePolicy())
        registry.register(make_bash_tool(self.root))
        return await registry.execute(
            ToolCall("bash-1", "bash", {"command": command, "timeout": timeout})
        )

    async def test_success_and_nonzero_exit(self) -> None:
        success = await self._run(f'"{sys.executable}" -c "print(123)"')
        failure = await self._run(f'"{sys.executable}" -c "import sys; sys.exit(7)"')
        self.assertTrue(success.ok)
        self.assertIn("123", success.data["stdout"])
        self.assertFalse(failure.ok)
        self.assertEqual(failure.data["exit_code"], 7)

    async def test_timeout(self) -> None:
        result = await self._run(
            f'"{sys.executable}" -c "import time; time.sleep(2)"', timeout=1
        )
        self.assertFalse(result.ok)
        self.assertIn("timed out", result.error)

