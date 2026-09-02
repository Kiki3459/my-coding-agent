"""Command-line interface for the coding agent."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from .agent import Agent
from .approvals import AutoApprovePolicy, InteractiveApprovalPolicy, ReadOnlyPolicy
from .context import ContextManager
from .model import OpenAIModelClient
from .prompt import build_system_prompt
from .registry import ToolRegistry
from .session import NullSessionLogger, SessionLogger
from .terminal_input import MultilineTaskReader, TerminalInput, TerminalUnavailable
from .tools import make_bash_tool, make_edit_tool, make_read_tool, make_write_tool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mini-agent",
        description="A small coding agent powered by native model tool calling.",
    )
    parser.add_argument("workspace", nargs="?", default=".", help="Project directory")
    task_source = parser.add_mutually_exclusive_group()
    task_source.add_argument("--task", help="Run one task and exit instead of opening a REPL")
    task_source.add_argument(
        "--task-file", help="Read one complete UTF-8 task from a file; '-' reads all stdin"
    )
    parser.add_argument("--model", help="Overrides OPENAI_MODEL")
    parser.add_argument("--base-url", help="Overrides OPENAI_BASE_URL")
    parser.add_argument("--env-file", default=".env", help="Environment file to load")
    parser.add_argument("--max-iterations", type=int, default=20)
    parser.add_argument("--context-budget", type=int, default=24_000)
    parser.add_argument("--session-id")
    parser.add_argument("--no-session", action="store_true")
    parser.add_argument("--no-project-instructions", action="store_true")
    policy = parser.add_mutually_exclusive_group()
    policy.add_argument(
        "--yes",
        action="store_true",
        help="Auto-approve writes and shell commands; use only in a trusted workspace",
    )
    policy.add_argument(
        "--read-only",
        action="store_true",
        help="Reject writes, edits, and shell commands",
    )
    return parser


def _load_env_file(path: str | Path) -> None:
    env_path = Path(path)
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"").strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _event_printer(event: str, payload: dict[str, Any]) -> None:
    if event == "iteration":
        print(f"\n[iteration {payload['iteration']}]")
    elif event == "tool_start":
        args = json.dumps(payload["arguments"], ensure_ascii=False)
        print(f"[tool] {payload['name']} {args}")
    elif event == "tool_end":
        marker = "ok" if payload["ok"] else "error"
        data = payload.get("data") or {}
        detail = ""
        if payload["name"] == "bash" and "exit_code" in data:
            detail = f" exit_code={data['exit_code']}"
        elif "path" in data:
            detail = f" path={data['path']}"
        if payload.get("error"):
            detail += f" {payload['error']}"
        print(f"[result:{marker}]{detail}")
        if payload["name"] == "bash":
            if data.get("stdout"):
                print(_display_excerpt("stdout", str(data["stdout"])))
            if data.get("stderr"):
                print(_display_excerpt("stderr", str(data["stderr"])))
    elif event == "retry":
        print(f"[retry] model call failed; retrying ({payload['attempt']})")


def _display_excerpt(label: str, text: str, limit: int = 2_000) -> str:
    if len(text) > limit:
        text = text[:limit] + f"\n... [{len(text) - limit} more characters]"
    return f"--- {label} ---\n{text}"


def _build_agent(args: argparse.Namespace, terminal: TerminalInput | None = None) -> Agent:
    _load_env_file(args.env_file)
    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError(f"Workspace is not a directory: {workspace}")

    api_key = os.getenv("OPENAI_API_KEY", "")
    model_name = args.model or os.getenv("OPENAI_MODEL", "")
    base_url = args.base_url or os.getenv("OPENAI_BASE_URL") or None
    if not api_key:
        raise ValueError("OPENAI_API_KEY is missing")
    if not model_name:
        raise ValueError("OPENAI_MODEL is missing")

    if args.yes:
        approval = AutoApprovePolicy()
    elif args.read_only:
        approval = ReadOnlyPolicy()
    else:
        approval = InteractiveApprovalPolicy(terminal)

    registry = ToolRegistry(approval)
    for tool in (
        make_read_tool(workspace),
        make_write_tool(workspace),
        make_edit_tool(workspace),
        make_bash_tool(workspace),
    ):
        registry.register(tool)

    model = OpenAIModelClient(
        api_key=api_key,
        model=model_name,
        base_url=base_url,
    )
    session = (
        NullSessionLogger()
        if args.no_session
        else SessionLogger(workspace, session_id=args.session_id)
    )
    return Agent(
        model=model,
        registry=registry,
        system_prompt=build_system_prompt(
            workspace,
            include_project_instructions=not args.no_project_instructions,
        ),
        context_manager=ContextManager(budget_tokens=args.context_budget),
        session=session,
        max_iterations=args.max_iterations,
        event_handler=_event_printer,
    )


def _read_task_argument(args: argparse.Namespace) -> str | None:
    if args.task is not None:
        return args.task
    if args.task_file is not None:
        try:
            if args.task_file == "-":
                return sys.stdin.read()
            return Path(args.task_file).expanduser().read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"无法读取任务文件：{exc}") from exc
    return None


async def _run(args: argparse.Namespace) -> int:
    one_shot = _read_task_argument(args)
    terminal = None
    agent = None
    try:
        if one_shot is None:
            try:
                terminal = TerminalInput()
            except TerminalUnavailable as exc:
                raise ValueError(
                    "交互模式需要终端。请在普通终端启动，或使用 --task / --task-file。"
                    "无交互终端时审批默认拒绝；不要把任务文本作为审批输入。"
                ) from exc
        agent = _build_agent(args, terminal=terminal)
        if one_shot is not None:
            result = await agent.run(one_shot)
            print(f"\n[final:{result.status}]\n{result.text}")
            return 0 if result.status == "completed" else 1

        reader = MultilineTaskReader(terminal)
        terminal.write(
            "Mini Coding Agent\n"
            "多行输入：粘贴完整任务后，单独输入 /send 并回车提交。\n"
            "/cancel 放弃草稿；/help 帮助；/clear 清空对话；/exit 退出。\n"
            "审批独立读取：请等 Allow? 提示出现后，再输入 y 或 n。\n"
        )
        while True:
            try:
                task = await reader.read_task()
            except EOFError:
                terminal.write("\n已退出；未提交的草稿已丢弃。\n")
                return 0
            if not task.strip():
                continue
            if reader.command in {"/exit", "/quit"}:
                return 0
            if reader.command == "/help":
                terminal.write(
                    "直接输入或粘贴任意多行要求（包含空行和代码）。\n"
                    "最后另起一行输入 /send 才会发送；/cancel 取消草稿。\n"
                    "如正文需要字面量 /send，请写 //send。\n"
                    "/clear 清空对话；/exit 退出；Ctrl+C 中止程序。\n"
                    "也可用 --task-file requirements.txt 提交整个任务文件。\n"
                )
                continue
            if reader.command == "/clear":
                agent.reset()
                terminal.write("Conversation context cleared.\n")
                continue
            result = await agent.run(task)
            print(f"\n[final:{result.status}]\n{result.text}", flush=True)
            # No next task or approval is inferred from type-ahead entered while
            # the previous task was executing.
            terminal.discard_pending()
    finally:
        if terminal is not None:
            terminal.close()
        if agent is not None and hasattr(agent.model, "close"):
            await agent.model.close()


def main() -> None:
    args = build_parser().parse_args()
    try:
        exit_code = asyncio.run(_run(args))
    except (ValueError, RuntimeError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        raise SystemExit(130)
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
