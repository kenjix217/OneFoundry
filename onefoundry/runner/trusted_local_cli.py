"""One trusted host-native Codex or Claude turn for the fixed coding fixture.

This mode deliberately shares the operator's OS account and CLI login store.
It grants no container or credential isolation and never runs in the app process.
"""

from __future__ import annotations

import json
import os
import selectors
import signal
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runner.external_cli import CliRequest, CliResult, run_trusted_cli
from runner.external_service import _manifest, _now, process_external_job
from runner.fixture_git import _test
from runner.service import _atomic_bytes


class TrustedLocalInvalid(ValueError):
    """A local CLI executable, login store, or manifest path is unavailable."""


def _owned_directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()


def _spool_directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and os.access(path, os.W_OK | os.X_OK)


@dataclass(frozen=True)
class TrustedLocalConfig:
    spool: Path
    operator_home: Path
    runtime_path: str
    codex_executable: Path | None = None
    claude_executable: Path | None = None
    auth_check_seconds: int = 5
    auth_cache_seconds: int = 30

    def validate(self) -> None:
        if (
            not self.spool.is_absolute()
            or not _spool_directory(self.spool)
            or not self.operator_home.is_absolute()
            or not _owned_directory(self.operator_home)
            or self.operator_home.resolve() != self.operator_home
            or not isinstance(self.runtime_path, str)
            or not self.runtime_path
            or len(self.runtime_path) > 4096
            or any(ord(char) < 32 for char in self.runtime_path)
            or type(self.auth_check_seconds) is not int
            or not 1 <= self.auth_check_seconds <= 30
            or type(self.auth_cache_seconds) is not int
            or not 1 <= self.auth_cache_seconds <= 300
            or self.codex_executable is None
            and self.claude_executable is None
        ):
            raise TrustedLocalInvalid("trusted_local_config_invalid")
        for name in ("jobs", "claims", "receipts", "artifacts"):
            if not _spool_directory(self.spool / name):
                raise TrustedLocalInvalid("trusted_local_spool_invalid")
        for executable in (self.codex_executable, self.claude_executable):
            if executable is None:
                continue
            try:
                resolved = executable.resolve(strict=True)
                info = resolved.lstat()
            except OSError as exc:
                raise TrustedLocalInvalid("trusted_local_executable_invalid") from exc
            if (
                not executable.is_absolute()
                or not resolved.is_absolute()
                or not stat.S_ISREG(info.st_mode)
                or not os.access(resolved, os.X_OK)
            ):
                raise TrustedLocalInvalid("trusted_local_executable_invalid")

    def executable_for(self, adapter_id: str) -> Path:
        configured = (
            self.codex_executable
            if adapter_id == "codex_cli"
            else self.claude_executable
            if adapter_id == "claude_code"
            else None
        )
        if configured is None:
            raise TrustedLocalInvalid("trusted_local_adapter_unavailable")
        resolved = configured.resolve(strict=True)
        if not stat.S_ISREG(resolved.lstat().st_mode) or not os.access(resolved, os.X_OK):
            raise TrustedLocalInvalid("trusted_local_executable_invalid")
        return resolved


def _auth_status(config: TrustedLocalConfig, adapter_id: str) -> bool:
    """Read a small CLI status result without logging account details."""
    executable = config.executable_for(adapter_id)
    argv = (
        [str(executable), "login", "status"]
        if adapter_id == "codex_cli"
        else [str(executable), "auth", "status", "--json"]
    )
    environment = {
        "PATH": config.runtime_path,
        "HOME": str(config.operator_home),
        "LANG": "C.UTF-8",
        "CODEX_HOME": str(config.operator_home / ".codex"),
        "CLAUDE_CONFIG_DIR": str(config.operator_home / ".claude"),
    }
    process = subprocess.Popen(  # noqa: S603 - configured local executable, fixed status argv
        argv,
        cwd=config.operator_home,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=environment,
        start_new_session=True,
        close_fds=True,
    )
    assert process.stdout is not None
    output = bytearray()
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + config.auth_check_seconds
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            for key, _ in selector.select(remaining):
                chunk = os.read(key.fd, 4097 - len(output))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(chunk)
                if len(output) > 4096:
                    return False
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            return False
        if process.returncode != 0:
            return False
        if adapter_id == "codex_cli":
            return True
        try:
            status = json.loads(output)
        except (UnicodeDecodeError, ValueError):
            return False
        return isinstance(status, dict) and status.get("loggedIn") is True
    finally:
        selector.close()
        process.stdout.close()
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)


class AuthReadinessCache:
    """Cache local status probes between capability heartbeats."""

    def __init__(self) -> None:
        self._results: dict[str, tuple[float, bool]] = {}

    def ready(self, config: TrustedLocalConfig, adapter_id: str) -> bool:
        now = time.monotonic()
        cached = self._results.get(adapter_id)
        if cached is not None and now - cached[0] < config.auth_cache_seconds:
            return cached[1]
        result = _auth_status(config, adapter_id)
        self._results[adapter_id] = (time.monotonic(), result)
        return result


def run_trusted_local_cli(request: CliRequest, *, config: TrustedLocalConfig) -> CliResult:
    """Use the operator's existing login directory with bounded JSONL capture."""
    config.validate()
    if request.executable != config.executable_for(request.adapter_id):
        raise TrustedLocalInvalid("trusted_local_executable_mismatch")
    auth_directory = config.operator_home / (".codex" if request.adapter_id == "codex_cli" else ".claude")
    if not _owned_directory(auth_directory):
        raise TrustedLocalInvalid("trusted_local_login_unavailable")
    return run_trusted_cli(request, operator_home=config.operator_home, runtime_path=config.runtime_path)


def process_trusted_local_job(path: Path, config: TrustedLocalConfig) -> dict[str, Any] | None:
    """Claim one exact pending work order and keep all results in shared spool."""
    config.validate()
    if path.parent != config.spool / "jobs" or path.is_symlink():
        raise TrustedLocalInvalid("trusted_local_job_path_invalid")
    job = _manifest(path)
    executable = config.executable_for(job["adapter_id"])
    auth_directory = config.operator_home / (".codex" if job["adapter_id"] == "codex_cli" else ".claude")
    if not _owned_directory(auth_directory):
        raise TrustedLocalInvalid("trusted_local_login_unavailable")
    if not _auth_status(config, job["adapter_id"]):
        raise TrustedLocalInvalid("trusted_local_login_unavailable")
    return process_external_job(
        path,
        config.spool,
        executable=executable,
        invoke=lambda request: run_trusted_local_cli(request, config=config),
        test_runner=_test,
        launch_kind="trusted_local_cli_v1",
    )


def write_trusted_capabilities(
    config: TrustedLocalConfig, *, active: bool = True, auth_cache: AuthReadinessCache | None = None
) -> dict[str, Any]:
    """Publish current local executable/login readiness for the shared gateway."""
    config.validate()
    adapters: list[dict[str, Any]] = []
    for adapter_id in ("codex_cli", "claude_code"):
        reason: str | None
        if not active:
            reason = "trusted_local_worker_stopped"
        else:
            try:
                config.executable_for(adapter_id)
            except (TrustedLocalInvalid, OSError):
                reason = "trusted_local_adapter_unavailable"
            else:
                auth_directory = config.operator_home / (".codex" if adapter_id == "codex_cli" else ".claude")
                if not _owned_directory(auth_directory):
                    reason = "trusted_local_login_unavailable"
                elif auth_cache is None:
                    reason = None if _auth_status(config, adapter_id) else "trusted_local_login_unavailable"
                else:
                    reason = (
                        None if auth_cache.ready(config, adapter_id) else "trusted_local_login_unavailable"
                    )
        adapters.append({"adapter_id": adapter_id, "available": reason is None, "reason": reason})
    payload = {
        "schema_version": 1,
        "observed_at": _now(),
        "mode": "trusted_local_cli_v1",
        "isolated": False,
        "auth_ready": any(item["available"] for item in adapters),
        "adapters": adapters,
    }
    _atomic_bytes(
        config.spool / "capabilities.json",
        (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        mode=0o640,
    )
    return payload
