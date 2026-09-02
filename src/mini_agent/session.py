"""Append-only JSONL session log."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class SessionLogger:
    def __init__(self, workspace: str | Path, session_id: str | None = None) -> None:
        root = Path(workspace).resolve() / ".mini_agent" / "sessions"
        root.mkdir(parents=True, exist_ok=True)
        session_id = session_id or datetime.now().strftime("%Y%m%d-%H%M%S")
        self.path = root / f"{session_id}.jsonl"

    def append(self, message: dict[str, Any]) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": message,
        }
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        fd = os.open(self.path, flags, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)


class NullSessionLogger:
    path: Path | None = None

    def append(self, message: dict[str, Any]) -> None:
        return None

