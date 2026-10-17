"""Networkless OS boundary for offline fake coding CLI acceptance.

The installed external runner does not call this module. It deliberately has no
credential mount or provider network path; successful fake runs prove only this
local boundary, not readiness for a real coding provider.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from runner.external_cli import CliRequest, CliResult, _invoke_argv, _validate_request, build_argv


@dataclass(frozen=True)
class SandboxLimits:
    max_processes: int
    max_address_bytes: int
    max_file_bytes: int

    def validate(self) -> None:
        if not (2 <= self.max_processes <= 64):
            raise ValueError("invalid_process_limit")
        if not (128 * 1024 * 1024 <= self.max_address_bytes <= 2 * 1024**3):
            raise ValueError("invalid_address_limit")
        if not (4096 <= self.max_file_bytes <= 1024 * 1024):
            raise ValueError("invalid_file_limit")


def _mount_runtime(argv: list[str]) -> None:
    # The system interpreter and its libraries are read-only. No host /etc,
    # /home, Docker socket, source checkout, or receipt spool is mounted.
    for directory in ("/usr", "/bin", "/lib", "/lib64"):
        if Path(directory).exists():
            argv.extend(("--ro-bind", directory, directory))


def run_fake_cli_isolated(
    request: CliRequest, *, bubblewrap: Path, prlimit: Path, limits: SandboxLimits
) -> CliResult:
    """Run one admitted fake CLI in a fresh filesystem/PID/network namespace."""
    _validate_request(request)
    limits.validate()
    if not bubblewrap.is_file() or not os.access(bubblewrap, os.X_OK):
        raise ValueError("isolation_unavailable")
    if not prlimit.is_file() or not os.access(prlimit, os.X_OK):
        raise ValueError("resource_limit_unavailable")
    if request.repository.resolve() == request.private_home.resolve():
        raise ValueError("isolation_paths_overlap")
    # Reject an already-symlinked fake executable. This path is not fd-pinned;
    # live promotion requires pinned executable or image provenance.
    if request.executable.is_symlink() or not stat.S_ISREG(request.executable.stat().st_mode):
        raise ValueError("invalid_fake_executable")
    executable = request.executable.resolve(strict=True)
    for name in ("codex", "claude"):
        (request.private_home / name).mkdir(mode=0o700)
    inside = CliRequest(
        request.adapter_id,
        Path("/provider-cli"),
        Path("/work"),
        Path("/home/runner"),
        request.prompt,
        request.wall_seconds,
        request.max_output_bytes,
    )
    argv = [
        str(bubblewrap),
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--dir",
        "/work",
        "--dir",
        "/home",
        "--dir",
        "/proc",
        "--dir",
        "/dev",
        "--tmpfs",
        "/tmp",  # noqa: S108 - private tmpfs inside a fresh mount namespace
    ]
    _mount_runtime(argv)
    argv.extend(
        (
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--bind",
            str(request.repository.resolve(strict=True)),
            "/work",
            "--bind",
            str(request.private_home.resolve(strict=True)),
            "/home/runner",
            "--ro-bind",
            str(executable),
            "/provider-cli",
            "--setenv",
            "HOME",
            "/home/runner",
            "--setenv",
            "CODEX_HOME",
            "/home/runner/codex",
            "--setenv",
            "CLAUDE_CONFIG_DIR",
            "/home/runner/claude",
            "--setenv",
            "PATH",
            "/usr/local/bin:/usr/bin:/bin",
            "--setenv",
            "PYTHONDONTWRITEBYTECODE",
            "1",
            "--chdir",
            "/work",
            "--",
            str(prlimit),
            f"--nproc={limits.max_processes}:{limits.max_processes}",
            f"--as={limits.max_address_bytes}:{limits.max_address_bytes}",
            f"--fsize={limits.max_file_bytes}:{limits.max_file_bytes}",
            "--",
            *build_argv(inside),
        )
    )
    return _invoke_argv(
        request,
        argv,
        request.repository,
        {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
    )
