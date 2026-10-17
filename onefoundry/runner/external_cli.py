"""Bounded, provider-owned coding CLI invocation for the external runner.

This module does not select a repository or grant network access. The caller
must supply a disposable checkout and an admitted executable.
"""

from __future__ import annotations

import json
import os
import selectors
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from runner.provider_progress import LiveProgressParser, ProviderProgress, extract_progress

AdapterId = Literal["codex_cli", "claude_code"]
MAX_RECORDED_TOKENS = 2_147_483_647


@dataclass(frozen=True)
class CliRequest:
    adapter_id: AdapterId
    executable: Path
    repository: Path
    private_home: Path
    prompt: str
    wall_seconds: int
    max_output_bytes: int
    on_progress: Callable[[ProviderProgress], object] | None = None


@dataclass(frozen=True)
class CliResult:
    status: Literal["done", "unknown"]
    reason_code: str
    provider_session_id: str | None
    tokens_used: int | None
    cost_usd: float | None
    sanitized_log: bytes
    progress: tuple[ProviderProgress, ...] = ()
    progress_omitted: int = 0


def build_argv(request: CliRequest) -> list[str]:
    """Return fixed noninteractive arguments; task text always travels on stdin."""
    executable = str(request.executable)
    if request.adapter_id == "codex_cli":
        return [
            executable,
            "--ask-for-approval",
            "never",
            "exec",
            "--json",
            "--sandbox",
            "workspace-write",
            "--cd",
            str(request.repository),
            "--ignore-user-config",
            "--ignore-rules",
            "--ephemeral",
            "-",
        ]
    if request.adapter_id == "claude_code":
        # Claude's --restricted mode permits tools named in --tools. Keep Bash
        # absent; the runner, not Claude, owns the fixed fixture test command.
        return [
            executable,
            "--print",
            "--output-format",
            "stream-json",
            "--input-format",
            "text",
            "--permission-mode",
            "dontAsk",
            "--permission-prompts",
            "none",
            "--bare",
            "--restricted",
            "--strict-mcp-config",
            "--tools",
            "Edit,Read,Write",
            "--allowed-tools",
            "Edit,Read,Write",
            "--no-session-persistence",
        ]
    raise ValueError("unsupported_adapter")


def _positive_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _claude_tokens(usage: dict[str, object]) -> int | None:
    if "cache_creation" in usage and "cache_creation_input_tokens" not in usage:
        return None
    if "cache_read" in usage and "cache_read_input_tokens" not in usage:
        return None
    fields = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    values: list[int] = []
    for field in fields:
        if field not in usage and field.startswith("cache_"):
            values.append(0)
            continue
        count = _positive_int(usage.get(field))
        if count is None:
            return None
        values.append(count)
    total = sum(values)
    return total if total <= MAX_RECORDED_TOKENS else None


def _parse_events(adapter_id: AdapterId, raw: bytes) -> tuple[bool, str | None, int | None, float | None]:
    completed = False
    session: str | None = None
    tokens: int | None = None
    cost: float | None = None
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        if adapter_id == "codex_cli":
            if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
                session = event["thread_id"]
            if event.get("type") == "turn.completed":
                completed = True
                usage = event.get("usage")
                if isinstance(usage, dict):
                    input_count = _positive_int(usage.get("input_tokens"))
                    output_count = _positive_int(usage.get("output_tokens"))
                    if input_count is not None and output_count is not None:
                        total = input_count + output_count
                        tokens = total if total <= MAX_RECORDED_TOKENS else None
        else:
            if event.get("type") == "system" and isinstance(event.get("session_id"), str):
                session = event["session_id"]
            if event.get("type") == "result":
                completed = event.get("is_error") is False and event.get("subtype") == "success"
                if isinstance(event.get("session_id"), str):
                    session = event["session_id"]
                usage = event.get("usage")
                if isinstance(usage, dict):
                    tokens = _claude_tokens(usage)
                amount = event.get("total_cost_usd")
                if (
                    isinstance(amount, (int, float))
                    and not isinstance(amount, bool)
                    and 0 <= amount < 1_000_000
                ):
                    cost = float(amount)
    return completed, session, tokens, cost


def _stop(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        process.wait(timeout=5)


def _validate_request(request: CliRequest) -> None:
    if (
        not request.executable.is_file()
        or not os.access(request.executable, os.X_OK)
        or not request.repository.is_dir()
        or request.repository.is_symlink()
        or not request.private_home.is_dir()
        or request.private_home.is_symlink()
        or request.private_home.resolve() == Path.home().resolve()
        or not 1 <= request.wall_seconds <= 3600
        or not 512 <= request.max_output_bytes <= 1_000_000
        or not request.prompt
        or len(request.prompt.encode("utf-8")) > 65536
    ):
        raise ValueError("invalid_cli_request")


def run_cli(request: CliRequest, *, environment: dict[str, str]) -> CliResult:
    """Invoke once in a caller-supplied boundary; never retry."""
    _validate_request(request)
    argv = build_argv(request)
    isolated_environment = dict(environment)
    isolated_environment["HOME"] = str(request.private_home)
    isolated_environment["CODEX_HOME"] = str(request.private_home / "codex")
    isolated_environment["CLAUDE_CONFIG_DIR"] = str(request.private_home / "claude")
    for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR"):
        # Exclusive creation rejects a pre-existing directory or symlink that
        # could redirect the CLI into the operator's host credentials.
        os.mkdir(isolated_environment[name], mode=0o700)
    return _invoke_argv(request, argv, request.repository, isolated_environment)


def run_trusted_cli(request: CliRequest, *, operator_home: Path, runtime_path: str) -> CliResult:
    """Use one existing operator login store without copying credentials."""
    _validate_request(request)
    auth_directory = operator_home / (".codex" if request.adapter_id == "codex_cli" else ".claude")
    try:
        home_info = operator_home.lstat()
        auth_info = auth_directory.lstat()
    except OSError as exc:
        raise ValueError("trusted_cli_login_unavailable") from exc
    if (
        not operator_home.is_absolute()
        or not stat.S_ISDIR(home_info.st_mode)
        or not stat.S_ISDIR(auth_info.st_mode)
        or home_info.st_uid != os.geteuid()
        or auth_info.st_uid != os.geteuid()
        or not runtime_path
        or len(runtime_path) > 4096
        or any(ord(char) < 32 for char in runtime_path)
    ):
        raise ValueError("trusted_cli_login_unavailable")
    environment = {
        "PATH": runtime_path,
        "HOME": str(operator_home),
        "LANG": "C.UTF-8",
        "GIT_TERMINAL_PROMPT": "0",
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    argv = build_argv(request)
    if request.adapter_id == "codex_cli":
        environment["CODEX_HOME"] = str(auth_directory)
    else:
        environment["CLAUDE_CONFIG_DIR"] = str(auth_directory)
        # Claude --bare suppresses OAuth/keychain reads, so local login needs it absent.
        argv.remove("--bare")
        argv.insert(2, "--verbose")
    return _invoke_argv(request, argv, request.repository, environment)


def _invoke_argv(request: CliRequest, argv: list[str], cwd: Path, environment: dict[str, str]) -> CliResult:
    """Capture one process tree; never persist raw provider output."""
    with tempfile.TemporaryFile() as prompt_file:
        prompt_file.write(request.prompt.encode("utf-8"))
        prompt_file.seek(0)
        process = subprocess.Popen(  # noqa: S603 - validated executable, fixed argv, no shell
            argv,
            cwd=cwd,
            stdin=prompt_file,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            shell=False,
            start_new_session=True,
            close_fds=True,
        )
        assert process.stdout is not None and process.stderr is not None
        captured = bytearray()
        stdout = bytearray()
        progress_parser = LiveProgressParser(request.adapter_id) if request.on_progress else None
        deadline = time.monotonic() + request.wall_seconds
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        reason: str | None = None
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    reason = "provider_timeout"
                    break
                for key, _ in selector.select(min(remaining, 0.25)):
                    data = os.read(key.fd, min(8192, request.max_output_bytes + 1 - len(captured)))
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    captured.extend(data)
                    if key.data == "stdout":
                        stdout.extend(data)
                        if progress_parser is not None and request.on_progress is not None:
                            try:
                                for item in progress_parser.feed(data):
                                    request.on_progress(item)
                            except Exception:
                                reason = "provider_progress_unknown"
                                break
                    if len(captured) > request.max_output_bytes:
                        reason = "provider_output_limit"
                        break
                if reason is not None:
                    break
            if reason is not None:
                _stop(process)
            else:
                remaining = deadline - time.monotonic()
                try:
                    process.wait(timeout=max(0, remaining))
                except subprocess.TimeoutExpired:
                    reason = "provider_timeout"
                    _stop(process)
        finally:
            selector.close()
            process.stdout.close()
            process.stderr.close()
            _stop(process)
    completed, session, tokens, cost = _parse_events(request.adapter_id, bytes(stdout))
    progress = extract_progress(request.adapter_id, bytes(stdout))
    if reason is None and process.returncode != 0:
        reason = "provider_exit_nonzero"
    if reason is None and not completed:
        reason = "provider_terminal_unproved"
    status: Literal["done", "unknown"] = "done" if reason is None else "unknown"
    return CliResult(
        status,
        reason or "provider_completed",
        session,
        tokens,
        cost,
        f"adapter={request.adapter_id} outcome={reason or 'provider_completed'}\n".encode(),
        progress.items,
        progress.omitted,
    )
