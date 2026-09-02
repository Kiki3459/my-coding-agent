"""Cancellable shell execution with bounded output and process-group cleanup."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
from pathlib import Path

from ..messages import ToolResult
from ..registry import Tool

_MAX_OUTPUT_CHARS = 40_000
_MAX_TIMEOUT_SECONDS = 120
_DANGEROUS_FRAGMENTS = ("rm -rf /", "mkfs", "shutdown", "reboot", ":(){:|:&};:")


async def _capture(stream: asyncio.StreamReader) -> tuple[str, bool]:
    # Drain the pipe continuously but retain only bounded head and tail buffers.
    head = bytearray()
    tail = bytearray()
    total = 0
    limit = _MAX_OUTPUT_CHARS // 2
    while chunk := await stream.read(8192):
        total += len(chunk)
        remaining = limit - len(head)
        head.extend(chunk[:remaining] if remaining > 0 else b"")
        rest = chunk[max(0, remaining):]
        tail.extend(rest)
        if len(tail) > limit:
            del tail[:-limit]
    truncated = total > len(head) + len(tail)
    marker = f"\n... [{total - len(head) - len(tail)} bytes omitted] ...\n" if truncated else ""
    return head.decode("utf-8", "replace") + marker + tail.decode("utf-8", "replace"), truncated


async def _kill_group(process: asyncio.subprocess.Process) -> None:
    # Children can keep pipes open even after their parent exits.
    try:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(process.pid), "/T", "/F",
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, FileNotFoundError):
        pass
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.wait()


def make_bash_tool(workspace: str | Path) -> Tool:
    root = Path(workspace).resolve()

    async def bash(command: str, timeout: int = 30) -> ToolResult:
        if not command.strip():
            return ToolResult.failure("bash", "command must not be empty")
        if any(fragment in command.lower() for fragment in _DANGEROUS_FRAGMENTS):
            return ToolResult.failure("bash", "Command blocked by the basic danger filter")
        if timeout < 1 or timeout > _MAX_TIMEOUT_SECONDS:
            return ToolResult.failure("bash", "timeout must be between 1 and 120 seconds")

        # Never inherit model credentials into model-generated commands.
        env = {key: value for key, value in os.environ.items()
               if not any(word in key.upper() for word in ("API_KEY", "SECRET", "TOKEN", "PASSWORD"))}
        kwargs = dict(cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                      stdin=subprocess.DEVNULL, env=env)
        if os.name == "nt":
            argv = ["cmd.exe", "/d", "/s", "/c", command]
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            argv = ["/bin/sh", "-c", command]
            kwargs["start_new_session"] = True
        try:
            process = await asyncio.create_subprocess_exec(*argv, **kwargs)
        except OSError as exc:
            return ToolResult.failure("bash", f"Could not start command: {exc}")

        out_task = asyncio.create_task(_capture(process.stdout))
        err_task = asyncio.create_task(_capture(process.stderr))

        async def collect():
            await process.wait()
            return await out_task, await err_task

        collector = asyncio.create_task(collect())
        timed_out = False
        try:
            (out, out_cut), (err, err_cut) = await asyncio.wait_for(
                asyncio.shield(collector), timeout=timeout
            )
        except asyncio.TimeoutError:
            timed_out = True
            await _kill_group(process)
            (out, out_cut), (err, err_cut) = await collector
        except asyncio.CancelledError:
            await _kill_group(process)
            await collector
            raise

        error = f"Command timed out after {timeout} seconds" if timed_out else (
            f"Command exited with code {process.returncode}" if process.returncode else None
        )
        return ToolResult(
            ok=not error, tool="bash", error=error, truncated=out_cut or err_cut,
            data={"exit_code": process.returncode, "stdout": out, "stderr": err},
        )

    return Tool(
        name="bash",
        description="Run a non-interactive shell command in the workspace, for tests and inspection.",
        parameters={
            "type": "object",
            "properties": {"command": {"type": "string"},
                           "timeout": {"type": "integer", "minimum": 1, "maximum": 120}},
            "required": ["command"], "additionalProperties": False,
        },
        handler=bash, risk="shell",
    )
