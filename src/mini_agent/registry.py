"""Tool registration, schema exposure, validation, approval, and execution."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .approvals import ApprovalPolicy, ApprovalRequest
from .messages import ToolCall, ToolResult

ToolHandler = Callable[..., ToolResult | Awaitable[ToolResult]]


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    risk: str = "read"

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self, approval_policy: ApprovalPolicy) -> None:
        self._tools: dict[str, Tool] = {}
        self.approval_policy = approval_policy

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

    async def execute(self, call: ToolCall) -> ToolResult:
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolResult.failure(call.name, f"Unknown tool: {call.name}")
        if "__parse_error__" in call.arguments:
            return ToolResult.failure(
                call.name,
                f"Invalid tool argument JSON: {call.arguments['__parse_error__']}",
            )

        validation_error = _validate_arguments(tool.parameters, call.arguments)
        if validation_error:
            return ToolResult.failure(call.name, validation_error)

        if tool.risk != "read":
            request = ApprovalRequest(
                tool=tool.name,
                arguments=call.arguments,
                risk=tool.risk,
                summary=_approval_summary(tool.name, call.arguments),
            )
            try:
                approved = await self.approval_policy.approve(request)
            except (EOFError, KeyboardInterrupt):
                approved = False
            if not approved:
                return ToolResult.failure(
                    call.name, "Operation denied by the active approval policy"
                )

        try:
            result = tool.handler(**call.arguments)
            if inspect.isawaitable(result):
                result = await result
            if not isinstance(result, ToolResult):
                return ToolResult.failure(
                    call.name, "Tool returned an invalid result object"
                )
            return result
        except Exception as exc:  # tool errors must not tear down the agent loop
            return ToolResult.failure(
                call.name, f"Unhandled tool error: {type(exc).__name__}: {exc}"
            )


def _approval_summary(name: str, arguments: dict[str, Any]) -> str:
    if name == "bash":
        return f"Run shell command: {arguments.get('command', '')}"
    if name in {"write", "edit"}:
        return f"{name} file: {arguments.get('path', '')}"
    return f"Run {name} with {arguments}"


def _validate_arguments(schema: dict[str, Any], args: dict[str, Any]) -> str | None:
    if not isinstance(args, dict):
        return "Tool arguments must be an object"
    required = schema.get("required", [])
    missing = [name for name in required if name not in args]
    if missing:
        return f"Missing required arguments: {', '.join(missing)}"

    properties = schema.get("properties", {})
    unknown = sorted(set(args) - set(properties))
    if unknown and schema.get("additionalProperties") is False:
        return f"Unknown arguments: {', '.join(unknown)}"

    python_types = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "object": dict,
        "array": list,
    }
    for name, value in args.items():
        expected_name = properties.get(name, {}).get("type")
        expected = python_types.get(expected_name)
        if expected and not isinstance(value, expected):
            return f"Argument '{name}' must be {expected_name}"
    return None

