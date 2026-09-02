"""Permission policies for tools with side effects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .terminal_input import TerminalInput, TerminalUnavailable


@dataclass(slots=True)
class ApprovalRequest:
    tool: str
    arguments: dict[str, Any]
    risk: str
    summary: str


class ApprovalPolicy(Protocol):
    async def approve(self, request: ApprovalRequest) -> bool: ...


class InteractiveApprovalPolicy:
    def __init__(self, terminal: TerminalInput | None = None) -> None:
        self.terminal = terminal

    async def approve(self, request: ApprovalRequest) -> bool:
        prompt = (
            f"\n[approval:{request.risk}] {request.summary}\n"
            "Allow? [y/N] "
        )
        terminal = self.terminal
        owned = terminal is None
        try:
            if terminal is None:
                terminal = TerminalInput()
            return await terminal.read_approval(prompt)
        except (TerminalUnavailable, EOFError, OSError):
            # In particular, never consume redirected task text as approval.
            print("无法从交互终端确认，本次操作已拒绝。审批不会读取管道中的任务内容。")
            return False
        finally:
            if owned and terminal is not None:
                terminal.close()


class ReadOnlyPolicy:
    async def approve(self, request: ApprovalRequest) -> bool:
        return False


class AutoApprovePolicy:
    async def approve(self, request: ApprovalRequest) -> bool:
        return True
