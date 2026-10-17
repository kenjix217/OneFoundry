"""Offline-testable Codex app-server control channel for OneFoundry-owned sessions.

The caller owns OS isolation, executable admission, credentials, network policy,
repository admission, and durable records. This module never discovers host auth.
Protocol: https://developers.openai.com/codex/app-server (stdio JSONL v2).
"""

from __future__ import annotations

import json
import os
import re
import selectors
import signal
import subprocess
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

ApprovalDecision = Literal["accept", "decline", "cancel"]
ApprovalCallback = Callable[[str, Mapping[str, Any]], ApprovalDecision]
DynamicToolCallback = Callable[[str, Mapping[str, Any], str], Mapping[str, Any]]
MAX_MESSAGE_BYTES = 65536
MAX_PENDING_EVENTS = 256
MAX_SESSION_EVENTS = 4096
APPROVAL_METHODS = frozenset({"item/commandExecution/requestApproval", "item/fileChange/requestApproval"})


class AppServerError(Exception):
    """A bounded transport or protocol failure; raw server content is withheld."""


class AppServerTimeout(AppServerError):
    """The requested response or next event was not observed by its deadline."""


class AppServerThreadNotFound(AppServerError):
    """The saved thread has no local rollout to resume."""


class CodexAppServer:
    """One stdio connection, owned process, and one active OneFoundry thread/turn.

    Only an admitted supervisor should instantiate this class. ``argv`` and
    ``environment`` are explicit so operator HOME/auth is never inherited here.
    The callback is a decision source, not an OneFoundry approval implementation.
    """

    protocol_version = 1

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        approval_callback: ApprovalCallback | None = None,
        dynamic_tool_callback: DynamicToolCallback | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        if (
            not argv
            or any(not isinstance(part, str) or not part for part in argv)
            or not cwd.is_absolute()
            or not cwd.is_dir()
            or cwd.is_symlink()
            or not 0 < timeout_seconds <= 300
        ):
            raise ValueError("invalid app-server process configuration")
        self._argv = tuple(argv)
        self._cwd = cwd
        self._environment = dict(environment)
        self._approval_callback = approval_callback
        self._dynamic_tool_callback = dynamic_tool_callback
        self._admitted_dynamic_tools: frozenset[str] = frozenset()
        self._dynamic_call_results: dict[str, tuple[str, dict[str, Any]]] = {}
        self._timeout_seconds = timeout_seconds
        self._process: subprocess.Popen[bytes] | None = None
        self._selector: selectors.BaseSelector | None = None
        self._buffer = bytearray()
        self._events: deque[dict[str, Any]] = deque()
        self._next_id = 1
        self._event_count = 0
        self.thread_id: str | None = None
        self.turn_id: str | None = None
        self._repository: Path | None = None
        self._initialized = False

    def __enter__(self) -> CodexAppServer:
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def connect(self) -> None:
        if self._process is not None:
            raise AppServerError("app-server already started")
        try:
            process = subprocess.Popen(  # noqa: S603 - supervisor admits argv; no shell
                self._argv,
                cwd=self._cwd,
                env=self._environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
            assert process.stdout is not None
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            self._process = process
            self._selector = selector
            params: dict[str, Any] = {
                "clientInfo": {"name": "onefoundry", "title": "OneFoundry", "version": "0.1.0"}
            }
            if self._dynamic_tool_callback is not None:
                params["capabilities"] = {"experimentalApi": True}
            self._request("initialize", params)
            self._send({"method": "initialized"})
            self._initialized = True
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        process, selector = self._process, self._selector
        self._process = None
        self._selector = None
        self._initialized = False
        if selector is not None:
            selector.close()
        if process is not None:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired as exc:
                raise AppServerError("app-server process did not exit") from exc
            if process.stdin is not None:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()

    def _send(self, value: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise AppServerError("app-server unavailable")
        raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
        if len(raw) > MAX_MESSAGE_BYTES:
            raise AppServerError("outbound app-server message too large")
        try:
            process.stdin.write(raw)
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise AppServerError("app-server write failed") from exc

    def _read(self, deadline: float) -> dict[str, Any]:
        process, selector = self._process, self._selector
        if process is None or process.stdout is None or selector is None:
            raise AppServerError("app-server unavailable")
        while True:
            end = self._buffer.find(b"\n")
            if end >= 0:
                raw = bytes(self._buffer[:end])
                del self._buffer[: end + 1]
                if len(raw) > MAX_MESSAGE_BYTES:
                    raise AppServerError("app-server message too large")
                try:
                    message = json.loads(raw)
                except (UnicodeDecodeError, ValueError) as exc:
                    raise AppServerError("invalid app-server JSON") from exc
                if not isinstance(message, dict):
                    raise AppServerError("invalid app-server message")
                return message
            if len(self._buffer) >= MAX_MESSAGE_BYTES:
                raise AppServerError("app-server message too large")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppServerTimeout("app-server response timed out")
            if not selector.select(remaining):
                raise AppServerTimeout("app-server response timed out")
            chunk = os.read(process.stdout.fileno(), min(8192, MAX_MESSAGE_BYTES + 1 - len(self._buffer)))
            if not chunk:
                raise AppServerError("app-server stream ended")
            self._buffer.extend(chunk)

    def _queue_event(self, message: dict[str, Any]) -> None:
        self._event_count += 1
        if len(self._events) >= MAX_PENDING_EVENTS or self._event_count > MAX_SESSION_EVENTS:
            raise AppServerError("app-server event limit exceeded")
        self._events.append(message)

    def _handle_server_request(self, message: dict[str, Any]) -> None:
        request_id, method, params = message.get("id"), message.get("method"), message.get("params")
        if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
            raise AppServerError("invalid app-server request id")
        if not isinstance(method, str) or not isinstance(params, dict):
            raise AppServerError("invalid app-server request")
        if method == "item/tool/call":
            self._handle_dynamic_tool(request_id, params)
            return
        if method not in APPROVAL_METHODS:
            self._send({"id": request_id, "error": {"code": -32601, "message": "Unsupported request"}})
            return
        if (
            params.get("threadId") != self.thread_id
            or params.get("turnId") != self.turn_id
            or not isinstance(params.get("itemId"), str)
        ):
            raise AppServerError("app-server approval scope mismatch")
        decision: ApprovalDecision = "decline"
        if self._approval_callback is not None:
            decision = self._approval_callback(method, params)
        if decision not in ("accept", "decline", "cancel"):
            raise AppServerError("invalid approval decision")
        self._send({"id": request_id, "result": {"decision": decision}})

    def _handle_dynamic_tool(self, request_id: str | int, params: dict[str, Any]) -> None:
        callback = self._dynamic_tool_callback
        namespace, tool, call_id = params.get("namespace"), params.get("tool"), params.get("callId")
        arguments = params.get("arguments")
        if (
            callback is None
            or params.get("threadId") != self.thread_id
            or params.get("turnId") != self.turn_id
            or not isinstance(tool, str)
            or not isinstance(call_id, str)
            or not call_id
            or not isinstance(arguments, dict)
            or (namespace is not None and not isinstance(namespace, str))
        ):
            raise AppServerError("app-server dynamic tool scope mismatch")
        name = f"{namespace}.{tool}" if namespace else tool
        if name not in self._admitted_dynamic_tools:
            raise AppServerError("app-server dynamic tool is not admitted")
        argument_key = json.dumps(
            {"name": name, "arguments": arguments}, sort_keys=True, separators=(",", ":")
        )
        prior = self._dynamic_call_results.get(call_id)
        if prior is not None:
            if prior[0] != argument_key:
                raise AppServerError("app-server dynamic call identity changed")
            result = prior[1]
        else:
            received = callback(name, arguments, call_id)
            if not isinstance(received, dict):
                raise AppServerError("invalid dynamic tool response")
            result = dict(received)
            items = result.get("contentItems")
            if (
                type(result.get("success")) is not bool
                or not isinstance(items, list)
                or any(
                    not isinstance(item, dict)
                    or item.get("type") != "inputText"
                    or not isinstance(item.get("text"), str)
                    for item in items
                )
            ):
                raise AppServerError("invalid dynamic tool response")
            if len(self._dynamic_call_results) >= MAX_SESSION_EVENTS:
                raise AppServerError("dynamic tool call limit exceeded")
            self._dynamic_call_results[call_id] = (argument_key, result)
        self._send({"id": request_id, "result": result})

    @staticmethod
    def _tool_names(specs: Sequence[Mapping[str, Any]]) -> frozenset[str]:
        names: set[str] = set()
        for spec in specs:
            kind, name = spec.get("type"), spec.get("name")
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name):
                raise ValueError("invalid dynamic tool name")
            if kind == "function":
                entries = [name]
            elif kind == "namespace" and isinstance(spec.get("tools"), list):
                entries = []
                for tool in spec["tools"]:
                    if not isinstance(tool, dict) or tool.get("type") != "function":
                        raise ValueError("invalid dynamic tool namespace")
                    child = tool.get("name")
                    if not isinstance(child, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", child):
                        raise ValueError("invalid dynamic tool name")
                    entries.append(f"{name}.{child}")
            else:
                raise ValueError("invalid dynamic tool specification")
            if any(entry in names for entry in entries):
                raise ValueError("duplicate dynamic tool name")
            names.update(entries)
        return frozenset(names)

    def _dispatch(self, message: dict[str, Any]) -> dict[str, Any] | None:
        if "method" in message:
            if "id" in message:
                self._handle_server_request(message)
            else:
                if not isinstance(message["method"], str):
                    raise AppServerError("invalid app-server notification")
                self._queue_event(message)
            return None
        return message

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self._timeout_seconds
        while True:
            response = self._dispatch(self._read(deadline))
            if response is None:
                continue
            if response.get("id") != request_id:
                raise AppServerError("app-server response id mismatch")
            if "error" in response:
                error = response["error"]
                if (
                    method == "thread/resume"
                    and isinstance(error, dict)
                    and error.get("code") == -32600
                    and isinstance(error.get("message"), str)
                    and error["message"].startswith("no rollout found for thread id ")
                ):
                    raise AppServerThreadNotFound("saved Codex thread has no local rollout")
                raise AppServerError(f"app-server {method} rejected request")
            result = response.get("result")
            if not isinstance(result, dict):
                raise AppServerError("invalid app-server response")
            return result

    def account_ready(self) -> bool:
        self._require_initialized()
        result = self._request("account/read", {"refreshToken": False})
        account = result.get("account")
        return isinstance(account, dict) and isinstance(account.get("type"), str)

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise AppServerError("app-server is not initialized")

    def start_thread(
        self, *, repository: Path, model: str, dynamic_tools: Sequence[Mapping[str, Any]] | None = None
    ) -> str:
        self._require_initialized()
        if (
            self.thread_id is not None
            or not repository.is_absolute()
            or not repository.is_dir()
            or repository.is_symlink()
        ):
            raise ValueError("invalid or existing app-server thread")
        if not model or any(char.isspace() for char in model):
            raise ValueError("invalid model")
        params: dict[str, Any] = {
            "cwd": str(repository),
            "model": model,
            "sandbox": "read-only",
            "approvalPolicy": "never",
        }
        if dynamic_tools is not None:
            if self._dynamic_tool_callback is None:
                raise ValueError("dynamic tools need an admitted callback")
            self._admitted_dynamic_tools = self._tool_names(dynamic_tools)
            params["dynamicTools"] = list(dynamic_tools)
        result = self._request("thread/start", params)
        thread = result.get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("id"), str) or not thread["id"]:
            raise AppServerError("invalid app-server thread")
        self.thread_id = thread["id"]
        self._repository = repository
        return self.thread_id

    def resume_thread(
        self, thread_id: str, *, repository: Path, admitted_dynamic_tools: Sequence[str] = ()
    ) -> str:
        self._require_initialized()
        if (
            self.thread_id is not None
            or not thread_id
            or len(thread_id) > 200
            or not repository.is_absolute()
            or not repository.is_dir()
            or repository.is_symlink()
        ):
            raise ValueError("invalid or existing app-server thread")
        if admitted_dynamic_tools and self._dynamic_tool_callback is None:
            raise ValueError("dynamic tools need an admitted callback")
        if any(
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}(\.[A-Za-z_][A-Za-z0-9_]{0,63})?", name)
            for name in admitted_dynamic_tools
        ):
            raise ValueError("invalid admitted dynamic tool")
        self._admitted_dynamic_tools = frozenset(admitted_dynamic_tools)
        result = self._request(
            "thread/resume",
            {
                "threadId": thread_id,
                "cwd": str(repository),
                "sandbox": "read-only",
                "approvalPolicy": "never",
            },
        )
        thread = result.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            raise AppServerError("app-server resumed a different thread")
        self.thread_id = thread_id
        self._repository = repository
        return thread_id

    def start_turn(self, prompt: str, *, repository: Path, effort: str | None = None) -> str:
        self._require_initialized()
        if self.thread_id is None or self.turn_id is not None:
            raise AppServerError("thread unavailable or turn already active")
        if (
            not prompt
            or len(prompt.encode("utf-8")) > 32768
            or repository != self._repository
            or repository.is_symlink()
            or (effort is not None and not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", effort))
        ):
            raise ValueError("invalid turn input")
        params: dict[str, Any] = {
            "threadId": self.thread_id,
            "input": [{"type": "text", "text": prompt}],
            "cwd": str(repository),
            "approvalPolicy": "never",
            "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
        }
        if effort is not None:
            params["effort"] = effort
        result = self._request("turn/start", params)
        turn = result.get("turn")
        if not isinstance(turn, dict) or not isinstance(turn.get("id"), str) or not turn["id"]:
            raise AppServerError("invalid app-server turn")
        self.turn_id = turn["id"]
        return self.turn_id

    def next_event(self, *, timeout_seconds: float) -> dict[str, Any]:
        """Read one bounded raw notification; caller owns redaction and storage."""
        self._require_initialized()
        if not 0 < timeout_seconds <= 300:
            raise ValueError("invalid event timeout")
        deadline = time.monotonic() + timeout_seconds
        while True:
            if self._events:
                event = self._events.popleft()
            else:
                response = self._dispatch(self._read(deadline))
                if response is not None:
                    raise AppServerError("unexpected app-server response")
                if not self._events:
                    continue
                event = self._events.popleft()
            if event.get("method") == "turn/completed":
                params = event.get("params")
                turn = params.get("turn") if isinstance(params, dict) else None
                if not isinstance(turn, dict) or turn.get("id") != self.turn_id:
                    raise AppServerError("app-server terminal turn mismatch")
                if turn.get("status") not in ("completed", "failed", "interrupted"):
                    raise AppServerError("invalid app-server terminal status")
                self.turn_id = None
                self._dynamic_call_results.clear()
            return event

    def interrupt(self) -> None:
        self._require_initialized()
        if self.thread_id is None or self.turn_id is None:
            raise AppServerError("no active turn to interrupt")
        self._request("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id})
