"""Offline-testable, read-only Claude Code print-mode control adapter.

The caller admits the executable, repository, model, credential source and OS
boundary. This module never reads the operator's Claude configuration and is
not wired into a live OneFoundry launch path.

The caller must admit an installed Claude Code version >= 2.1.259 before
launch: ``--restricted`` requires >= 2.1.248 and ``--permission-prompts none``
requires >= 2.1.259. This module does not probe the executable or trust its
version string as a substitute for an installed capability test.
"""

from __future__ import annotations

import json
import math
import os
import selectors
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Literal
from uuid import UUID

MAX_PROMPT_BYTES = 32768
MAX_SESSION_TOKENS = 2_147_483_647


@dataclass(frozen=True)
class ClaudeRequest:
    executable: Path
    repository: Path
    private_home: Path
    session_id: UUID
    model: str
    prompt: str
    wall_seconds: int
    max_output_bytes: int
    max_turns: int


@dataclass(frozen=True)
class ClaudeOutcome:
    status: Literal["done", "unknown"]
    reason_code: str
    session_id: str | None
    tokens_used: int | None
    cost_usd: float | None


def _private_directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return (
        path.is_absolute()
        and stat.S_ISDIR(info.st_mode)
        and stat.S_IMODE(info.st_mode) == 0o700
        and info.st_uid == os.geteuid()
    )


def _validate(request: ClaudeRequest) -> None:
    if (
        not request.executable.is_file()
        or not os.access(request.executable, os.X_OK)
        or not request.repository.is_absolute()
        or not request.repository.is_dir()
        or request.repository.is_symlink()
        or not _private_directory(request.private_home)
        or request.private_home.resolve() == Path.home().resolve()
        or request.session_id.version != 4
        or not request.model
        or any(char.isspace() for char in request.model)
        or not request.prompt
        or len(request.prompt.encode("utf-8")) > MAX_PROMPT_BYTES
        or not 1 <= request.wall_seconds <= 3600
        or not 512 <= request.max_output_bytes <= 1_000_000
        or not 1 <= request.max_turns <= 20
    ):
        raise ValueError("invalid Claude request")


def build_argv(request: ClaudeRequest) -> list[str]:
    """Use documented noninteractive flags; never place task text in argv."""
    _validate(request)
    return [
        str(request.executable),
        "--print",
        "--output-format",
        "stream-json",
        "--input-format",
        "text",
        "--session-id",
        str(request.session_id),
        "--model",
        request.model,
        "--max-turns",
        str(request.max_turns),
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
        "--bare",
        "--restricted",
        "--strict-mcp-config",
        "--disallowedTools",
        "mcp__*",
        "--tools",
        "Read,Glob,Grep",
        "--no-session-persistence",
    ]


def _positive(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= MAX_SESSION_TOKENS else None


def _usage(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("invalid usage")
    if "cache_creation" in value and "cache_creation_input_tokens" not in value:
        raise ValueError("ambiguous cache usage")
    if "cache_read" in value and "cache_read_input_tokens" not in value:
        raise ValueError("ambiguous cache usage")
    total = 0
    for name in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        count = _positive(value.get(name, 0 if name.startswith("cache_") else None))
        if count is None:
            raise ValueError("invalid usage")
        total += count
    if total > MAX_SESSION_TOKENS:
        raise ValueError("usage overflow")
    return total


def _cost(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid cost")
    amount = float(value)
    if not math.isfinite(amount) or not 0 <= amount < 1_000_000:
        raise ValueError("invalid cost")
    return amount


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


@dataclass
class _Observation:
    session: str | None = None
    tokens: int | None = None
    cost: float | None = None
    result_seen: bool = False
    reason: str | None = None


def _observe_event(raw: bytes, request: ClaudeRequest, state: _Observation) -> None:
    event = json.loads(raw)
    if not isinstance(event, dict):
        raise ValueError("invalid event")
    event_type = event.get("type")
    if event_type == "system":
        observed = event.get("session_id")
        if observed is not None:
            if observed != str(request.session_id):
                raise ValueError("session mismatch")
            state.session = observed
    elif event_type == "result":
        if state.result_seen:
            raise ValueError("duplicate result")
        observed = event.get("session_id", state.session)
        if observed != str(request.session_id):
            raise ValueError("session mismatch")
        state.session = observed
        state.result_seen = True
        if event.get("subtype") != "success" or event.get("is_error") is not False:
            state.reason = "provider_result_error"
        else:
            state.tokens = _usage(event.get("usage"))
            state.cost = _cost(event.get("total_cost_usd"))
    elif event_type in {"assistant", "user", "stream_event"}:
        if event_type == "assistant":
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "tool_use":
                        if item.get("name") not in {"Read", "Glob", "Grep"}:
                            raise ValueError("unapproved tool")
    else:
        raise ValueError("unexpected event")


def _write_nonblocking(descriptor: int, data: bytes) -> int:
    return os.write(descriptor, data)


def _send_prompt(
    process: subprocess.Popen[bytes], prompt: bytes, deadline: float, cancel: Event | None
) -> str | None:
    """Deliver stdin without allowing an unresponsive child to hold the runner."""
    assert process.stdin is not None
    descriptor = process.stdin.fileno()
    os.set_blocking(descriptor, False)
    offset = 0
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdin, selectors.EVENT_WRITE)
        while offset < len(prompt):
            if cancel is not None and cancel.is_set():
                return "cancelled_before_terminal"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "provider_timeout"
            if not selector.select(min(remaining, 0.1)):
                continue
            try:
                written = _write_nonblocking(descriptor, prompt[offset : offset + 8192])
            except BlockingIOError:
                continue
            except (BrokenPipeError, OSError):
                return "provider_io_failure"
            if written <= 0:
                return "provider_io_failure"
            offset += written
    process.stdin.close()
    return None


def run_claude(
    request: ClaudeRequest, *, environment: dict[str, str], cancel: Event | None = None
) -> ClaudeOutcome:
    """Run once; a missing terminal receipt, denial, or cancellation is unknown."""
    argv = build_argv(request)
    config_dir = request.private_home / "claude"
    config_dir.mkdir(mode=0o700, exist_ok=False)
    isolated_environment = dict(environment)
    isolated_environment["HOME"] = str(request.private_home)
    isolated_environment["CLAUDE_CONFIG_DIR"] = str(config_dir)
    process = subprocess.Popen(  # noqa: S603 - admitted executable and fixed argv; no shell
        argv,
        cwd=request.repository,
        env=isolated_environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        close_fds=True,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    state = _Observation()
    total_bytes = 0
    line_buffer = bytearray()
    deadline = time.monotonic() + request.wall_seconds
    try:
        state.reason = _send_prompt(process, request.prompt.encode("utf-8"), deadline, cancel)
        if state.reason is None:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, "stdout")
                selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                while selector.get_map():
                    if cancel is not None and cancel.is_set():
                        state.reason = "cancelled_before_terminal"
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        state.reason = "provider_timeout"
                        break
                    for key, _ in selector.select(min(remaining, 0.1)):
                        chunk = os.read(key.fd, 8192)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        total_bytes += len(chunk)
                        if total_bytes > request.max_output_bytes:
                            state.reason = "provider_output_limit"
                            break
                        if key.data == "stdout":
                            line_buffer.extend(chunk)
                            while b"\n" in line_buffer:
                                raw, _, tail = line_buffer.partition(b"\n")
                                line_buffer = bytearray(tail)
                                try:
                                    _observe_event(bytes(raw), request, state)
                                except (UnicodeDecodeError, ValueError):
                                    state.reason = "invalid_provider_stream"
                                    break
                        if state.reason is not None:
                            break
                    if state.reason is not None:
                        break
        if state.reason is None and line_buffer:
            state.reason = "invalid_provider_stream"
        if state.reason is None:
            try:
                process.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                state.reason = "provider_timeout"
        if state.reason is None and process.returncode != 0:
            state.reason = "provider_exit_nonzero"
        if state.reason is None and not state.result_seen:
            state.reason = "provider_terminal_unproved"
    except (BrokenPipeError, OSError):
        state.reason = "provider_io_failure"
    finally:
        _stop(process)
        if not process.stdin.closed:
            process.stdin.close()
        process.stdout.close()
        process.stderr.close()
    return ClaudeOutcome(
        "done" if state.reason is None else "unknown",
        state.reason or "provider_completed",
        state.session,
        state.tokens if state.reason is None else None,
        state.cost if state.reason is None else None,
    )
