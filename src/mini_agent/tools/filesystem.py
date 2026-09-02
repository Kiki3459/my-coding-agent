"""Workspace-confined file tools."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from ..messages import ToolResult
from ..registry import Tool

_MAX_READ_CHARS = 40_000


def safe_path(root: Path, raw_path: str) -> Path:
    """Resolve a path and reject absolute, parent, and symlink escapes."""
    root = root.resolve()
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise ValueError(f"Path escapes workspace: {raw_path}")
    return resolved


def _truncate(text: str, limit: int = _MAX_READ_CHARS) -> tuple[str, bool, int]:
    if len(text) <= limit:
        return text, False, 0
    head_size = limit * 2 // 3
    tail_size = limit - head_size
    omitted = len(text) - limit
    marker = f"\n... [{omitted} characters omitted] ...\n"
    return text[:head_size] + marker + text[-tail_size:], True, omitted


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def make_read_tool(workspace: str | Path) -> Tool:
    root = Path(workspace).resolve()

    def read(path: str, offset: int = 1, limit: int = 400) -> ToolResult:
        try:
            file_path = safe_path(root, path)
            if not file_path.exists():
                return ToolResult.failure("read", f"File does not exist: {path}")
            if not file_path.is_file():
                return ToolResult.failure("read", f"Not a regular file: {path}")
            if offset < 1 or limit < 1:
                return ToolResult.failure("read", "offset and limit must be positive")
            lines = file_path.read_text(encoding="utf-8").splitlines()
            selected = lines[offset - 1 : offset - 1 + limit]
            numbered = "\n".join(
                f"{line_no:>6} | {line}"
                for line_no, line in enumerate(selected, start=offset)
            )
            rendered, truncated, omitted = _truncate(numbered)
            return ToolResult(
                ok=True,
                tool="read",
                data={
                    "path": path,
                    "content": rendered,
                    "total_lines": len(lines),
                    "returned_lines": len(selected),
                    "omitted_characters": omitted,
                },
                truncated=truncated or offset - 1 + limit < len(lines),
            )
        except UnicodeDecodeError as exc:
            return ToolResult.failure("read", f"File is not valid UTF-8: {exc}")
        except (OSError, ValueError) as exc:
            return ToolResult.failure("read", str(exc))

    return Tool(
        name="read",
        description="Read a UTF-8 text file inside the workspace with line numbers.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative path"},
                "offset": {"type": "integer", "description": "First line, one-based"},
                "limit": {"type": "integer", "description": "Maximum number of lines"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        handler=read,
        risk="read",
    )


def make_write_tool(workspace: str | Path) -> Tool:
    root = Path(workspace).resolve()

    def write(path: str, content: str) -> ToolResult:
        try:
            file_path = safe_path(root, path)
            if file_path.exists() and not file_path.is_file():
                return ToolResult.failure("write", f"Not a regular file: {path}")
            _atomic_write(file_path, content)
            return ToolResult(
                ok=True,
                tool="write",
                data={
                    "path": path,
                    "bytes": len(content.encode("utf-8")),
                    "lines": len(content.splitlines()),
                },
            )
        except (OSError, ValueError) as exc:
            return ToolResult.failure("write", str(exc))

    return Tool(
        name="write",
        description="Create or replace a UTF-8 text file inside the workspace.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        handler=write,
        risk="write",
    )


def make_edit_tool(workspace: str | Path) -> Tool:
    root = Path(workspace).resolve()

    def edit(path: str, old_text: str, new_text: str) -> ToolResult:
        try:
            file_path = safe_path(root, path)
            if not file_path.exists() or not file_path.is_file():
                return ToolResult.failure("edit", f"File does not exist: {path}")
            if not old_text:
                return ToolResult.failure("edit", "old_text must not be empty")
            content = file_path.read_text(encoding="utf-8")
            count = content.count(old_text)
            if count == 0:
                return ToolResult.failure(
                    "edit", "old_text was not found; read the file and match whitespace exactly"
                )
            if count > 1:
                return ToolResult.failure(
                    "edit", f"old_text matched {count} places; include more surrounding context"
                )
            updated = content.replace(old_text, new_text, 1)
            _atomic_write(file_path, updated)
            return ToolResult(
                ok=True,
                tool="edit",
                data={"path": path, "replacements": 1},
            )
        except UnicodeDecodeError as exc:
            return ToolResult.failure("edit", f"File is not valid UTF-8: {exc}")
        except (OSError, ValueError) as exc:
            return ToolResult.failure("edit", str(exc))

    return Tool(
        name="edit",
        description="Replace one exact, unique text fragment in a workspace file.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
            "additionalProperties": False,
        },
        handler=edit,
        risk="write",
    )

