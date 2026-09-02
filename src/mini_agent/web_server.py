"""Loopback-only web control plane. No web framework or agent SDK required."""

from __future__ import annotations

import argparse
import asyncio
import difflib
import inspect
import json
import mimetypes
import os
import secrets
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from .agent import Agent
from .approvals import ApprovalRequest, ReadOnlyPolicy
from .approval_description import describe_operation
from .cli import _load_env_file
from .context import ContextManager
from .model import OpenAIModelClient
from .prompt import build_system_prompt
from .registry import ToolRegistry
from .session import NullSessionLogger, SessionLogger
from .tools import make_bash_tool, make_edit_tool, make_read_tool, make_write_tool
from .tools.filesystem import safe_path

ASSETS = Path(__file__).parent / "web"
BLOCKED_NAMES = {".git", ".ssh", ".mini_agent", ".venv", "node_modules", "__pycache__", ".DS_Store"}
ACTIVE = {"running", "awaiting_approval", "stopping"}


def visible_path(root: Path, path: str) -> Path:
    target = safe_path(root, path)
    parts = Path(path).parts + target.relative_to(root).parts
    if any(part in BLOCKED_NAMES or (part.startswith(".env") and part != ".env.example")
           or part.endswith((".pem", ".key")) for part in parts):
        raise ValueError("此路径属于凭据或内部目录，网页不展示。")
    return target


@dataclass
class WebRun:
    id: str
    prompt: str
    workspace: str
    mode: str
    limit: int
    created: float = field(default_factory=time.time)
    status: str = "running"
    iteration: int = 0
    result: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    pending: dict[str, Any] | None = None
    approval: asyncio.Future | None = None
    task: asyncio.Task | None = None
    ended: float | None = None
    sequence: int = 0

    def emit(self, event: str, payload: dict[str, Any]) -> None:
        self.sequence += 1
        self.events.append({"seq": self.sequence, "type": event,
                            "time": time.time(), "data": payload})
        if len(self.events) > 600:
            self.events = self.events[-600:]
        if event == "iteration":
            self.iteration = payload["iteration"]

    def overview(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.prompt[:60], "status": self.status,
                "created": self.created, "workspace": self.workspace,
                "iteration": self.iteration, "limit": self.limit, "ended": self.ended}

    def snapshot(self) -> dict[str, Any]:
        return {**self.overview(), "prompt": self.prompt, "mode": self.mode,
                "result": self.result, "events": self.events, "pending": self.pending}


class WebApprovalPolicy:
    def __init__(self, run: WebRun) -> None:
        self.run = run

    async def approve(self, request: ApprovalRequest) -> bool:
        run = self.run
        future = asyncio.get_running_loop().create_future()
        run.approval = future
        pending = {"id": uuid4().hex, "tool": request.tool, "risk": request.risk,
                   "summary": request.summary, "arguments": request.arguments,
                   "description": describe_operation(request.tool, request.arguments),
                   "diff": ""}
        if request.tool in {"edit", "write"}:
            try:
                root = Path(run.workspace)
                path = visible_path(root, request.arguments["path"])
                if path.exists() and path.stat().st_size > 1_000_000:
                    raise ValueError("文件过大，无法生成预览")
                before = path.read_text(encoding="utf-8") if path.exists() else ""
                if len(before) > 1_000_000:
                    raise ValueError("文件过大，无法生成预览")
                after = (request.arguments["content"] if request.tool == "write" else
                         before.replace(request.arguments["old_text"], request.arguments["new_text"], 1))
                pending["diff"] = "".join(difflib.unified_diff(
                    before.splitlines(keepends=True), after.splitlines(keepends=True),
                    fromfile="before/" + request.arguments["path"],
                    tofile="after/" + request.arguments["path"],
                ))[:40_000]
            except (OSError, ValueError, KeyError) as exc:
                pending["diff"] = f"预览不可用：{exc}"
        run.pending = pending
        run.status = "awaiting_approval"
        run.emit("approval_requested", {"tool": request.tool, "id": pending["id"]})
        try:
            allowed = await future
            run.emit("approval_resolved", {"tool": request.tool, "allowed": allowed})
            return allowed
        finally:
            run.pending = None
            run.approval = None
            if run.status == "awaiting_approval":
                run.status = "running"


class WebApp:
    """State lives on one asyncio loop; HTTP threads submit bounded requests."""

    def __init__(self, workspace: Path, *, model_factory: Callable,
                 model_name: str = "", configured: bool = True, secret: str = "") -> None:
        self.workspace = workspace.resolve()
        self.model_factory = model_factory
        self.model_name = model_name
        self.configured = configured
        self.secret = secret
        self.runs: dict[str, WebRun] = {}

    def _clean(self, value: Any) -> Any:
        # The current model credential must never be returned in API responses.
        if isinstance(value, str):
            return value.replace(self.secret, "[REDACTED]") if self.secret else value
        if isinstance(value, dict):
            return {key: self._clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._clean(item) for item in value]
        return value

    def active_run(self) -> WebRun | None:
        return next((r for r in self.runs.values() if r.status in ACTIVE), None)

    async def state(self, run_id: str = "") -> dict[str, Any]:
        selected = self.runs.get(run_id)
        return self._clean({
            "workspace": str(self.workspace), "model": self.model_name,
            "configured": self.configured,
            "runs": [run.overview() for run in reversed(list(self.runs.values()))],
            "active_id": self.active_run().id if self.active_run() else None,
            "run": selected.snapshot() if selected else None,
        })

    async def set_workspace(self, path: str) -> dict:
        if self.active_run():
            raise ValueError("请先停止当前任务，再切换工作区。")
        root = Path(path).expanduser().resolve()
        if not root.is_dir() or root == Path(root.anchor):
            raise ValueError("请选择存在的项目文件夹，不要使用磁盘根目录。")
        self.workspace = root
        return {"workspace": str(root)}

    async def files(self, path: str = "") -> dict:
        folder = visible_path(self.workspace, path or ".")
        if not folder.is_dir():
            raise ValueError("不是文件夹")
        children = []
        for child in sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            relative = child.relative_to(self.workspace).as_posix()
            try:
                resolved = visible_path(self.workspace, relative)
            except ValueError:
                continue
            children.append({"name": child.name, "path": relative, "directory": resolved.is_dir()})
            if len(children) == 250:
                break
        return {"path": path, "files": children, "limit": 250}

    async def file(self, path: str) -> dict:
        target = visible_path(self.workspace, path)
        if not target.is_file() or target.stat().st_size > 1_000_000:
            raise ValueError("只支持预览 1 MB 以内的文本文件。")
        raw = target.read_text(encoding="utf-8")
        return self._clean({"path": path, "content": raw[:80_000], "truncated": len(raw) > 80_000})

    async def start(self, prompt: str, mode: str = "ask", limit: int = 20,
                    save_session: bool = True) -> dict:
        if not self.configured:
            raise ValueError("请先在项目 .env 中配置 OPENAI_API_KEY 和 OPENAI_MODEL，再重启服务。")
        if self.active_run():
            raise ValueError("已有任务正在运行，请先完成或停止它。")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 30_000:
            raise ValueError("请输入 1–30000 个字符的任务。")
        if mode not in {"ask", "read_only"} or type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("运行参数无效。")
        if type(save_session) is not bool:
            raise ValueError("日志参数无效。")
        run = WebRun(uuid4().hex, prompt.strip(), str(self.workspace), mode, limit)
        self.runs[run.id] = run
        while len(self.runs) > 30:
            self.runs.pop(next(iter(self.runs)))
        run.emit("user", {"text": run.prompt})
        run.task = asyncio.create_task(self._execute(run, save_session))
        return {"id": run.id}

    async def _execute(self, run: WebRun, save_session: bool) -> None:
        model = None
        try:
            model = self.model_factory()
            policy = ReadOnlyPolicy() if run.mode == "read_only" else WebApprovalPolicy(run)
            registry = ToolRegistry(policy)
            root = Path(run.workspace)
            for tool in (make_read_tool(root), make_write_tool(root), make_edit_tool(root), make_bash_tool(root)):
                # Prevent direct file tools from exposing credentials in the web UI.
                if tool.name != "bash":
                    original = tool.handler

                    def guarded(_handler=original, **args):
                        visible_path(root, args["path"])
                        return _handler(**args)

                    tool.handler = guarded
                registry.register(tool)
            session = SessionLogger(root, f"web-{run.id}") if save_session else NullSessionLogger()
            agent = Agent(
                model=model, registry=registry, system_prompt=build_system_prompt(root),
                max_iterations=run.limit, session=session,
                context_manager=ContextManager(), event_handler=run.emit,
            )
            result = await agent.run(run.prompt)
            # "finished" is deliberately not a claim that verification succeeded.
            run.status = "finished" if result.status == "completed" else result.status
            run.result = result.text
        except asyncio.CancelledError:
            run.status = "cancelled"
            run.result = "任务已停止。正在运行的命令已终止；此前完成的文件修改不会自动撤销。"
        except Exception as exc:
            run.status = "error"
            run.result = f"任务未完成：{type(exc).__name__}: {exc}"
        finally:
            run.pending = None
            run.approval = None
            run.ended = time.time()
            run.emit("run_end", {"status": run.status, "text": run.result})
            if model is not None and hasattr(model, "close"):
                try:
                    closing = model.close()
                    if inspect.isawaitable(closing):
                        await closing
                except Exception:
                    pass

    async def approve(self, run_id: str, approval_id: str, allow: bool) -> dict:
        run = self.runs.get(run_id)
        if type(allow) is not bool:
            raise ValueError("审批结果必须为布尔值。")
        if not run or not run.pending or run.pending["id"] != approval_id:
            raise ValueError("审批已过期，请刷新后查看当前请求。")
        if not run.approval or run.approval.done():
            raise ValueError("该请求已经处理。")
        run.approval.set_result(allow)
        return {"ok": True}

    async def cancel(self, run_id: str) -> dict:
        run = self.runs.get(run_id)
        if not run or run.status not in ACTIVE or not run.task:
            raise ValueError("任务不在运行中。")
        run.status = "stopping"
        run.task.cancel()
        return {"ok": True}

    async def shutdown(self) -> None:
        tasks = [run.task for run in self.runs.values() if run.task and not run.task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, app: WebApp, port: int = 8765) -> None:
        self.app = app
        self.token = secrets.token_urlsafe(32)
        self.loop = asyncio.new_event_loop()
        self.worker = threading.Thread(target=self.loop.run_forever, daemon=True)
        super().__init__(("127.0.0.1", port), WebHandler)
        self.worker.start()

    def dispatch(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout=15)

    def server_close(self) -> None:
        # TCPServer calls server_close even when bind failed during construction.
        if not self.worker.is_alive():
            if not self.loop.is_closed():
                self.loop.close()
            super().server_close()
            return
        try:
            self.dispatch(self.app.shutdown())
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.worker.join(timeout=5)
            if not self.worker.is_alive():
                self.loop.close()
            super().server_close()


class WebHandler(BaseHTTPRequestHandler):
    server: LocalServer

    def log_message(self, fmt, *args) -> None:
        # Do not log credentials, query strings, or model-generated text.
        pass

    def _trusted(self) -> bool:
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        return (host in hosts and (not origin or origin in {f"http://{h}" for h in hosts})
                and self.headers.get("Sec-Fetch-Site") != "cross-site")

    def _send(self, data: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, value, status=200):
        self._send(json.dumps(self.server.app._clean(value), ensure_ascii=False).encode(),
                   "application/json; charset=utf-8", status)

    def _authorized(self) -> bool:
        return self._trusted() and secrets.compare_digest(
            self.headers.get("X-Miniagent-Token", ""), self.server.token
        )

    def do_GET(self) -> None:
        if not self._trusted():
            self._json({"error": "Untrusted host or origin"}, 403)
            return
        url = urlsplit(self.path)
        if url.path.startswith("/api/"):
            if not self._authorized():
                self._json({"error": "网页会话已失效，请刷新页面。"}, 403)
                return
            query = parse_qs(url.query)
            app = self.server.app
            try:
                if url.path == "/api/state":
                    result = self.server.dispatch(app.state(query.get("run", [""])[0]))
                elif url.path == "/api/files":
                    result = self.server.dispatch(app.files(query.get("path", [""])[0]))
                elif url.path == "/api/file":
                    result = self.server.dispatch(app.file(query.get("path", [""])[0]))
                else:
                    self._json({"error": "Not found"}, 404)
                    return
                self._json(result)
            except Exception as exc:
                self._json({"error": str(exc)}, 400)
            return

        name = "index.html" if url.path == "/" else url.path.lstrip("/")
        if name not in {"index.html", "app.js", "styles.css", "favicon.svg"}:
            self._json({"error": "Not found"}, 404)
            return
        path = ASSETS / name
        if not path.is_file():
            self._json({"error": "Web assets are missing"}, 404)
            return
        content = path.read_bytes()
        if name == "index.html":
            content = content.replace(b"__SESSION_TOKEN__", self.server.token.encode())
        self._send(content, (mimetypes.guess_type(path)[0] or "text/plain") + "; charset=utf-8")

    def do_POST(self) -> None:
        if not self._authorized():
            self._json({"error": "Untrusted request; refresh the local page"}, 403)
            return
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            self._json({"error": "Expected JSON"}, 415)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 150_000:
                raise ValueError("请求大小无效")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object")
            app = self.server.app
            if self.path == "/api/workspace":
                action = app.set_workspace(body["path"])
            elif self.path == "/api/run":
                action = app.start(body.get("prompt", ""), body.get("mode", "ask"),
                                   body.get("limit", 20), body.get("save_session", True))
            elif self.path == "/api/approval":
                action = app.approve(body["run_id"], body["approval_id"], body["allow"])
            elif self.path == "/api/cancel":
                action = app.cancel(body["run_id"])
            else:
                self._json({"error": "Not found"}, 404)
                return
            self._json(self.server.dispatch(action))
        except Exception as exc:
            self._json({"error": str(exc)}, 400)


def main() -> None:
    parser = argparse.ArgumentParser(description="Mini Agent local web console")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="Open the local page in your browser")
    args = parser.parse_args()
    root = Path(args.workspace).expanduser().resolve()
    if not root.is_dir() or root == Path(root.anchor):
        parser.error("--workspace must be an existing project folder, not the filesystem root")
    _load_env_file(args.env_file)
    key = os.environ.get("OPENAI_API_KEY", "")
    model = os.environ.get("OPENAI_MODEL", "")
    base_url = os.environ.get("OPENAI_BASE_URL") or None
    app = WebApp(root, model_factory=lambda: OpenAIModelClient(
        api_key=key, model=model, base_url=base_url,
    ), model_name=model, configured=bool(key and model), secret=key)
    try:
        server = LocalServer(app, args.port)
    except OSError as exc:
        parser.exit(1, f"无法启动本地网页服务：{exc}\n请检查端口是否占用，或从普通终端启动。\n")
    address = f"http://127.0.0.1:{server.server_port}"
    print(f"Mini Agent 网页操作台：{address}", flush=True)
    print("仅本机可访问。按 Ctrl+C 停止服务。不要将此端口代理到公网。")
    if not app.configured:
        print("尚未配置模型；可浏览界面，配置 .env 并重启后可运行任务。")
    if args.open:
        webbrowser.open(address)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
