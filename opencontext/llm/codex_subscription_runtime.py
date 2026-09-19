# -*- coding: utf-8 -*-
"""Small JSONL transport for the official Codex app-server.

The runtime deliberately owns only the app-server process and JSON-RPC
transport.  Authentication remains Codex's responsibility; this module never
handles ChatGPT cookies, access tokens, or API keys.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Union


class CodexSubscriptionError(RuntimeError):
    """Base error for the local Codex app-server bridge."""


class CodexSubscriptionRpcError(CodexSubscriptionError):
    """A JSON-RPC request returned an error."""

    def __init__(self, method: str, error: Mapping[str, Any]):
        self.method = method
        self.error = dict(error)
        message = self.error.get("message") or "Codex app-server request failed"
        super().__init__(f"{method}: {message}")


class _ProcessClosed(CodexSubscriptionError):
    pass


@dataclass
class _Pending:
    method: str
    event: threading.Event
    response: Optional[Mapping[str, Any]] = None
    error: Optional[BaseException] = None


class CodexTurnHandle:
    """Notifications belonging to one app-server turn."""

    _END = object()

    def __init__(self, runtime: "CodexSubscriptionRuntime", thread_id: str, turn_id: str, q):
        self.runtime = runtime
        self.thread_id = thread_id
        self.turn_id = turn_id
        self._queue = q
        self._closed = False

    def next_event(self, timeout: Optional[float] = None) -> Mapping[str, Any]:
        try:
            event = self._queue.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("Timed out waiting for Codex turn notification")
        if event is self._END:
            raise _ProcessClosed("Codex app-server closed while a turn was active")
        return event

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.runtime.unsubscribe(self._queue)
            close_thread = getattr(self.runtime, "unsubscribe_thread", None)
            if close_thread:
                close_thread(self.thread_id)


class CodexSubscriptionRuntime:
    """One long-lived, isolated ``codex app-server --stdio`` process.

    Calls from different asyncio loops are safe: all wire I/O happens in one
    reader thread and writes are serialized only for individual JSONL frames.
    """

    def __init__(self, config: Optional[Mapping[str, Any]] = None):
        self.config = dict(config or {})
        self._process: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._stderr_reader: Optional[threading.Thread] = None
        self._write_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._pending: Dict[str, _Pending] = {}
        self._subscribers: set[queue.Queue] = set()
        self._next_id = 0
        self._closed = False
        self._started = False
        self._stderr = deque(maxlen=100)
        self._refcount = 0

    @property
    def process(self) -> Optional[subprocess.Popen]:
        return self._process

    @property
    def stderr_tail(self) -> list[str]:
        return list(self._stderr)

    def _paths(self) -> tuple[Path, Path]:
        context_root = os.environ.get("CONTEXT_PATH")
        # Packaged MineContext supplies an absolute userData path.  Development
        # uses CONTEXT_PATH="."; never place Codex auth state in the Git tree.
        if context_root and Path(context_root).is_absolute():
            local_app = Path(context_root)
        else:
            local_app = (
                Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()))
                / "MineContext"
            )
        home = Path(self.config.get("codex_home") or local_app / "codex-home").expanduser()
        cwd = Path(self.config.get("cwd") or local_app / "codex-workspace").expanduser()
        home.mkdir(parents=True, exist_ok=True)
        cwd.mkdir(parents=True, exist_ok=True)
        return home, cwd

    @staticmethod
    def _find_cli(explicit: Optional[str] = None) -> str:
        candidates = [explicit, os.environ.get("CODEX_CLI_PATH")]
        if os.name == "nt":
            local_appdata = os.environ.get("LOCALAPPDATA")
            if local_appdata:
                bin_root = Path(local_appdata) / "OpenAI" / "Codex" / "bin"
                # Prefer the native binary.  The npm wrapper's cmd.exe
                # quoting is fragile for paths containing spaces.
                candidates.extend(str(path) for path in sorted(bin_root.glob("*/codex.exe"), reverse=True))
            candidates.extend(["codex.exe", "codex.ps1", "codex.cmd", "codex"])
        else:
            candidates.append("codex")
        for candidate in candidates:
            if not candidate:
                continue
            path = shutil.which(str(candidate)) if not Path(str(candidate)).exists() else str(candidate)
            if path:
                return path
        raise FileNotFoundError("Could not find the official Codex CLI (codex.cmd/codex.exe/codex.ps1)")

    def _command(self) -> Union[list[str], str]:
        cli = self._find_cli(self.config.get("cli_path"))
        suffix = Path(cli).suffix.lower()
        args = ["app-server", "--stdio"]
        # The bridge is an inference-only client.  These feature flags are
        # supported by the installed CLI and make the restriction explicit;
        # server-side request handling below remains fail-closed as a second
        # boundary.
        for feature in self.config.get(
            "disable_features",
            [
                "apps", "plugins", "skill_search", "view_image", "image_generation",
                "multi_agent", "sleep_tool", "request_permissions_tool", "shell_tool",
                "unified_exec", "browser_use", "computer_use",
            ],
        ):
            args.extend(["--disable", str(feature)])
        args.extend(["-c", "tools.web_search=false"])
        args.extend(["-c", "project_doc_max_bytes=0"])
        if self.config.get("force_chatgpt_login", True):
            args.extend(["-c", 'forced_login_method="chatgpt"'])
        args.extend(list(self.config.get("extra_args", [])))
        if suffix in {".ps1"}:
            powershell = shutil.which("pwsh") or shutil.which("powershell")
            if not powershell:
                raise FileNotFoundError("PowerShell is required to launch codex.ps1")
            return [powershell, "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", cli, *args]
        if suffix in {".cmd", ".bat"}:
            # cmd.exe is used only as the Windows launcher; stdin/stdout stay
            # attached to the app-server process. Popen must receive the full
            # command line as a string here; a list makes cmd.exe split a
            # quoted batch path again when more than one argument follows it.
            comspec = os.environ.get("COMSPEC", "cmd.exe")
            inner = subprocess.list2cmdline([cli, *args])
            return f'{subprocess.list2cmdline([comspec])} /d /s /c "{inner}"'
        return [cli, *args]

    def start(self) -> "CodexSubscriptionRuntime":
        with self._state_lock:
            if self._started and self._process and self._process.poll() is None:
                return self
            if self._closed:
                raise CodexSubscriptionError("Codex runtime is closed")
            home, cwd = self._paths()
            env = os.environ.copy()
            env["CODEX_HOME"] = str(home)
            # Avoid accidentally selecting a global OpenAI-compatible API
            # route.  Codex's managed ChatGPT auth is the only supported auth.
            for key in (
                "OPENAI_API_KEY",
                "OPENAI_BASE_URL",
                "OPENAI_ORG_ID",
                "OPENAI_PROJECT_ID",
                "CODEX_API_KEY",
            ):
                env.pop(key, None)
            command = self._command()
            self._process = subprocess.Popen(
                command,
                cwd=str(cwd),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self._started = True
            self._reader = threading.Thread(target=self._read_stdout, name="minecontext-codex-rpc", daemon=True)
            self._stderr_reader = threading.Thread(target=self._read_stderr, name="minecontext-codex-stderr", daemon=True)
            self._reader.start()
            self._stderr_reader.start()
        try:
            self.request(
                "initialize",
                {
                    "clientInfo": {"name": "minecontext", "title": "MineContext", "version": "0.1.0"},
                },
            )
            self.notify("initialized")
        except Exception:
            self.close()
            raise
        return self

    def _read_stdout(self) -> None:
        process = self._process
        if not process or not process.stdout:
            return
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._dispatch(message)
        finally:
            with self._state_lock:
                if self._process is not process:
                    return
            self._fail_all(_ProcessClosed("Codex app-server exited"))
            with self._state_lock:
                for subscriber in list(self._subscribers):
                    subscriber.put(CodexTurnHandle._END)

    def _read_stderr(self) -> None:
        process = self._process
        if process and process.stderr:
            for line in process.stderr:
                self._stderr.append(line.rstrip())

    def _dispatch(self, message: Mapping[str, Any]) -> None:
        if "id" in message and "method" in message:
            self._reply_server_request(message)
            return
        if "id" in message:
            request_id = str(message["id"])
            with self._state_lock:
                pending = self._pending.get(request_id)
            if pending:
                pending.response = message
                pending.event.set()
            return
        if "method" in message:
            with self._state_lock:
                subscribers = list(self._subscribers)
            for subscriber in subscribers:
                subscriber.put(dict(message))

    def _reply_server_request(self, message: Mapping[str, Any]) -> None:
        # MineContext never grants shell, file-write, browser, MCP, or user
        # input authority to Codex.  Returning an explicit JSON-RPC error is
        # safer than leaving the turn waiting forever.
        self._write(
            {
                "jsonrpc": "2.0",
                "id": message.get("id"),
                "error": {"code": -32001, "message": "Server requests are disabled by MineContext"},
            }
        )

    def _write(self, message: Mapping[str, Any]) -> None:
        with self._write_lock:
            process = self._process
            if not process or process.poll() is not None or not process.stdin:
                raise _ProcessClosed("Codex app-server is not running")
            process.stdin.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
            process.stdin.flush()

    def notify(self, method: str, params: Optional[Mapping[str, Any]] = None) -> None:
        message: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = dict(params)
        self._write(message)

    def request(self, method: str, params: Optional[Mapping[str, Any]] = None, timeout: Optional[float] = None) -> Mapping[str, Any]:
        if not self._started or not self._process or self._process.poll() is not None:
            with self._state_lock:
                self._started = False
            self.start()
        with self._state_lock:
            self._next_id += 1
            request_id = str(self._next_id)
            pending = _Pending(method=method, event=threading.Event())
            self._pending[request_id] = pending
        try:
            message: Dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
            if params is not None:
                message["params"] = dict(params)
            self._write(message)
            wait_for = float(timeout if timeout is not None else self.config.get("timeout", 300))
            if not pending.event.wait(wait_for):
                raise TimeoutError(f"Timed out waiting for Codex request: {method}")
            if pending.error:
                raise pending.error
            response = pending.response or {}
            if "error" in response:
                raise CodexSubscriptionRpcError(method, response["error"])
            return response.get("result", {})
        finally:
            with self._state_lock:
                self._pending.pop(request_id, None)

    async def request_async(self, method: str, params: Optional[Mapping[str, Any]] = None, timeout: Optional[float] = None) -> Mapping[str, Any]:
        return await asyncio.to_thread(self.request, method, params, timeout)

    def _fail_all(self, error: BaseException) -> None:
        with self._state_lock:
            for pending in self._pending.values():
                pending.error = error
                pending.event.set()

    def subscribe(self) -> queue.Queue:
        subscriber: queue.Queue = queue.Queue()
        with self._state_lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue) -> None:
        with self._state_lock:
            self._subscribers.discard(subscriber)

    def unsubscribe_thread(self, thread_id: str) -> None:
        if not self._process or self._process.poll() is not None or self._closed:
            return
        try:
            self.request("thread/unsubscribe", {"threadId": thread_id}, timeout=min(float(self.config.get("timeout", 300)), 10))
        except Exception:
            pass

    def start_turn(self, params: Mapping[str, Any]) -> CodexTurnHandle:
        thread_id = str(params["threadId"])
        subscriber = self.subscribe()
        try:
            result = self.request("turn/start", params)
            turn = result.get("turn", result) if isinstance(result, Mapping) else {}
            turn_id = str(turn.get("id") or result.get("turnId") or "")
            if not turn_id:
                raise CodexSubscriptionError("Codex turn/start returned no turn id")
            return CodexTurnHandle(self, thread_id, turn_id, subscriber)
        except Exception:
            self.unsubscribe(subscriber)
            raise

    def interrupt(self, thread_id: str, turn_id: str) -> Mapping[str, Any]:
        return self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    def cancel_current_turn(self, handle: Optional[CodexTurnHandle]) -> None:
        if handle:
            try:
                self.interrupt(handle.thread_id, handle.turn_id)
            except Exception:
                pass

    # Authentication/model operations are intentionally thin wrappers.  The
    # official app-server owns browser login, refresh, and credential storage.
    def account_read(self, refresh_token: bool = False) -> Mapping[str, Any]:
        return self.request("account/read", {"refreshToken": refresh_token})

    def login_start(self, *, device_code: bool = False) -> Mapping[str, Any]:
        return self.request("account/login/start", {"type": "chatgptDeviceCode" if device_code else "chatgpt"})

    def login_cancel(self, login_id: str) -> Mapping[str, Any]:
        if not login_id:
            raise ValueError("login_id is required to cancel a Codex login")
        return self.request("account/login/cancel", {"loginId": login_id})

    def logout(self) -> Mapping[str, Any]:
        return self.request("account/logout", None)

    def list_models(self, *, include_hidden: bool = False, limit: Optional[int] = None) -> Mapping[str, Any]:
        params: Dict[str, Any] = {"includeHidden": include_hidden}
        if limit is not None:
            params["limit"] = limit
        return self.request("model/list", params)

    def get_status(self) -> Mapping[str, Any]:
        """Compatibility name for settings routes; account/read is authoritative."""
        return self.account_read()

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            process = self._process
            self._fail_all(_ProcessClosed("Codex runtime closed"))
        if process and process.poll() is None:
            # EOF gives both the native binary and cmd/PowerShell wrappers a
            # chance to stop the real app-server child and release its SQLite
            # files.  Killing only the wrapper can orphan that child.
            if process.stdin:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        with self._state_lock:
            self._started = False
            self._process = None
            self._subscribers.clear()

    def acquire(self) -> "CodexSubscriptionRuntime":
        with self._state_lock:
            if self._closed:
                raise CodexSubscriptionError("Codex runtime is closed")
            self._refcount += 1
        return self

    def release(self) -> None:
        with self._state_lock:
            self._refcount = max(0, self._refcount - 1)
            should_close = self._refcount == 0
        if should_close:
            self.close()


_shared_runtime: Optional[CodexSubscriptionRuntime] = None
_shared_lock = threading.Lock()


def get_codex_subscription_runtime(config: Optional[Mapping[str, Any]] = None) -> CodexSubscriptionRuntime:
    """Return the process-shared runtime, creating it lazily."""
    global _shared_runtime
    with _shared_lock:
        if _shared_runtime is None or _shared_runtime._closed:
            _shared_runtime = CodexSubscriptionRuntime(config)
        return _shared_runtime


def reset_codex_subscription_runtime() -> None:
    """Test/process-shutdown helper; it never touches auth files."""
    global _shared_runtime
    with _shared_lock:
        runtime, _shared_runtime = _shared_runtime, None
    if runtime:
        runtime.close()
