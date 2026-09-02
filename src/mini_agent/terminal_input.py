"""Explicit multiline task framing and fresh, controlling-terminal approvals.

Task text never shares Python's buffered stdin reader with approval decisions.
POSIX input uses a private /dev/tty descriptor and cancellable select polling;
Windows uses the console's wide-character API, with no background input thread.
"""

from __future__ import annotations

import asyncio
import os
import select
import sys
from typing import Protocol


class TerminalUnavailable(RuntimeError):
    pass


class LineReader(Protocol):
    async def readline(self, prompt: str) -> str: ...

    def write(self, text: str) -> None: ...


class MultilineTaskReader:
    """Accumulate every line, including blank lines, until an explicit /send."""

    def __init__(self, terminal: LineReader) -> None:
        self.terminal = terminal
        self.command: str | None = None

    async def read_task(self) -> str:
        self.command = None
        lines: list[str] = []
        while True:
            # EOF always discards an unfinished draft; it is never an implicit send.
            line = await self.terminal.readline("\nagent> " if not lines else "...> ")
            if line == "/send":
                task = "\n".join(lines)
                if task.strip():
                    return task
                self.terminal.write("任务为空，请输入要求后再用 /send 提交。\n")
                lines.clear()
                continue
            if line == "/cancel":
                self.terminal.write("已取消草稿，未向模型发送任何内容。\n")
                return ""
            if not lines and line in {"/help", "/clear", "/exit", "/quit"}:
                self.command = line
                return line
            # Escape literal control lines in task text with an extra slash.
            if line in {"//send", "//cancel", "//help", "//clear", "//exit", "//quit"}:
                line = line[1:]
            lines.append(line)


class TerminalInput:
    def __init__(self, fd: int | None = None) -> None:
        self._buffer = bytearray()
        self._lock = asyncio.Lock()
        self._closed = False
        self._after_cr = False
        self._windows = os.name == "nt"
        self._owned = fd is None
        if self._windows:
            if not sys.stdin.isatty():
                raise TerminalUnavailable("没有可用的交互控制台。")
            self.fd = None
        else:
            try:
                self.fd = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY) if fd is None else fd
                if not os.isatty(self.fd):
                    raise OSError("not a terminal")
            except OSError as exc:
                if self._owned and getattr(self, "fd", None) is not None:
                    os.close(self.fd)
                raise TerminalUnavailable("无法打开独立交互终端 /dev/tty。") from exc

    def write(self, text: str) -> None:
        if self._windows:
            sys.stdout.write(text)
            sys.stdout.flush()
        else:
            data = text.encode("utf-8")
            while data:
                data = data[os.write(self.fd, data):]

    def discard_pending(self) -> None:
        """Discard both our bytes and terminal type-ahead before a new decision."""
        self._buffer.clear()
        if self._windows:
            import msvcrt

            while msvcrt.kbhit():
                msvcrt.getwch()
        else:
            import termios

            termios.tcflush(self.fd, termios.TCIFLUSH)

    async def readline(self, prompt: str) -> str:
        async with self._lock:
            self.write(prompt)
            return await self._readline()

    async def read_approval(self, prompt: str) -> bool:
        async with self._lock:
            while True:
                # Do this before displaying the question. Anything already queued
                # cannot be consent to a decision that has not been displayed yet.
                self.discard_pending()
                self.write(prompt)
                try:
                    answer = (await self._readline()).strip().lower()
                except EOFError:
                    return False
                if answer in {"y", "yes"}:
                    return True
                if answer in {"", "n", "no"}:
                    return False
                self.write("输入无效。请在审批提示出现后单独输入 y 或 n；其他文字不会被当作审批。\n")

    async def _readline(self) -> str:
        if self._closed:
            raise EOFError
        if self._windows:
            return await self._windows_line()
        return await self._posix_line()

    async def _posix_line(self) -> str:
        def take_line() -> str | None:
            if b"\n" not in self._buffer:
                return None
            line, _, rest = self._buffer.partition(b"\n")
            self._buffer = bytearray(rest)
            return line.rstrip(b"\r").decode("utf-8", errors="replace")

        # macOS kqueue can reject /dev/tty (the controlling-terminal alias)
        # with EINVAL, even though it accepts the underlying /dev/ttysNNN.
        # Do not register it through loop.add_reader / DefaultSelector.
        # A zero-timeout select checks readiness without blocking the event
        # loop; the sleep is just a cancellable wait, NOT a task delimiter.
        while not self._closed:
            ready = take_line()
            if ready is not None:
                return ready
            try:
                readable, _, _ = select.select([self.fd], [], [], 0)
                if not readable:
                    await asyncio.sleep(0.02)
                    continue
                data = os.read(self.fd, 65536)
                if not data:
                    raise EOFError
                self._buffer.extend(data)
            except (InterruptedError, BlockingIOError):
                await asyncio.sleep(0.02)
            except OSError as exc:
                raise TerminalUnavailable(f"终端读取失败：{exc}") from exc
        raise EOFError

    async def _windows_line(self) -> str:
        import msvcrt

        chars: list[str] = []
        while True:
            if not msvcrt.kbhit():
                await asyncio.sleep(0.02)
                continue
            char = msvcrt.getwch()
            if self._after_cr:
                self._after_cr = False
                if char == "\n":
                    continue
            if char == "\x03":
                raise KeyboardInterrupt
            if char in {"\x1a", "\x04"} and not chars:
                raise EOFError
            if char in {"\r", "\n"}:
                self._after_cr = char == "\r"
                self.write("\n")
                return "".join(chars)
            if char in {"\x00", "\xe0"}:
                # Function and arrow keys use a two-character console sequence.
                while not msvcrt.kbhit():
                    await asyncio.sleep(0.02)
                msvcrt.getwch()
                continue
            if char == "\b":
                if chars:
                    chars.pop()
                    self.write("\b \b")
                continue
            chars.append(char)
            self.write(char)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._windows and self._owned:
            os.close(self.fd)
