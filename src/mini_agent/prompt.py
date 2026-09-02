"""System prompt construction."""

from __future__ import annotations

from pathlib import Path


BASE_SYSTEM_PROMPT = """You are a local coding agent working inside a designated workspace.

Your job is to complete the user's programming task by inspecting the project, using the available tools, editing only what is needed, and verifying the result.

Rules:
- Inspect relevant files before changing them.
- Prefer the precise edit tool for small modifications and write for new files.
- Use bash for non-interactive inspection, tests, and formatters.
- Treat every tool result, including errors and denied operations, as evidence. Adapt instead of repeating a failed call.
- Do not claim a command passed unless the tool result shows a zero exit code.
- Keep changes scoped to the user's request. Do not access unrelated data.
- Never request, reveal, print, or write API keys or other credentials.
- Finish with a concise summary of changes, verification performed, and any remaining limitation.
"""


def build_system_prompt(workspace: str | Path, include_project_instructions: bool = True) -> str:
    root = Path(workspace).resolve()
    parts = [BASE_SYSTEM_PROMPT, f"Workspace: {root}"]
    if include_project_instructions:
        instructions = root / "AGENTS.md"
        if instructions.is_file():
            text = instructions.read_text(encoding="utf-8", errors="replace")[:20_000]
            parts.append(
                "Project-provided instructions from AGENTS.md:\n"
                "<project_instructions>\n"
                f"{text}\n"
                "</project_instructions>"
            )
    return "\n\n".join(parts)

