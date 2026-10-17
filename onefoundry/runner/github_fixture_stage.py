"""Offline proof of exact Git revision staging from a trusted synthetic local remote.

This module is deliberately not wired to HTTP, credentials, or the runner spool.
It proves the pinned-fetch and safe-tree contract without a live GitHub call.
"""

from __future__ import annotations

import os
import resource
import select
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import UUID

from onefoundry.source_github import BRANCH, REPO_PART, SHA
from runner.local_source_stage import StageError, StageLimits, StageResult, stage_local_source

GIT = "/usr/bin/git"


def _fixture_git_config(path: Path, remote: Path) -> None:
    """Trust only this disposable fixture mount for Git's local transport child."""
    value = str(remote)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise StageError("fixture_remote_invalid")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(f'[safe]\n\tdirectory = "{escaped}"\n'.encode())
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _git_env(safe_config: Path) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(safe_config),
        "GIT_ALLOW_PROTOCOL": "file",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
    }


def _git(repo: Path, safe_config: Path, deadline: float, file_limit: int, *args: str) -> str:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise StageError("source_stage_timeout")

    def bound_file() -> None:
        resource.setrlimit(resource.RLIMIT_FSIZE, (file_limit, file_limit))

    command = [
        GIT,
        "-C",
        str(repo),
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "transfer.fsckObjects=true",
        "-c",
        "fetch.fsckObjects=true",
        *args,
    ]
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed git, synthetic admitted remote only
            command,
            env=_git_env(safe_config),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            preexec_fn=bound_file,
        )
        assert process.stdout is not None
        output = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StageError("source_stage_timeout")
            ready, _, _ = select.select([process.stdout], [], [], remaining)
            if not ready:
                raise StageError("source_stage_timeout")
            chunk = os.read(process.stdout.fileno(), 4097 - len(output))
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > 4096:
                raise StageError("source_output_limit")
        process.wait(timeout=max(0.001, deadline - time.monotonic()))
        if process.returncode != 0:
            raise StageError("source_git_failed")
        try:
            return output.decode("utf-8").strip()
        except UnicodeDecodeError:
            raise StageError("source_git_output_invalid") from None
    except (OSError, subprocess.TimeoutExpired):
        raise StageError("source_git_unavailable") from None
    finally:
        if process is not None:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            if process.stdout is not None:
                process.stdout.close()


def _remote_head(
    repo: Path, remote: Path, safe_config: Path, branch: str, deadline: float, file_limit: int
) -> str:
    result = _git(
        repo,
        safe_config,
        deadline,
        file_limit,
        "ls-remote",
        "--heads",
        str(remote),
        f"refs/heads/{branch}",
    )
    fields = result.split("\t")
    if len(fields) != 2 or fields[1] != f"refs/heads/{branch}" or SHA.fullmatch(fields[0]) is None:
        raise StageError("source_branch_unavailable")
    return fields[0]


def _git_bytes(git_dir: Path, maximum: int) -> None:
    total = 0
    for root, dirs, files in os.walk(git_dir, followlinks=False):
        for name in dirs + files:
            entry = Path(root) / name
            info = entry.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise StageError("source_repo_invalid")
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
                if total > maximum:
                    raise StageError("source_fetch_limit")


def stage_pinned_fixture(
    *,
    remote: Path,
    project_id: UUID,
    source_id: UUID,
    account_id: UUID,
    owner: str,
    repo_name: str,
    branch: str,
    saved_revision: str,
    destination: Path,
    limits: StageLimits,
    max_git_bytes: int,
) -> StageResult:
    """Fetch a local fixture branch at its saved SHA into a fresh disposable repo.

    No credential is accepted. The production transport will need a separately
    isolated supervisor with a protected credential channel before it can use
    this contract for GitHub.
    """
    del project_id, account_id  # Identity is carried by the proposal; no external request is made.
    limits.validate()
    if (
        not REPO_PART.fullmatch(owner)
        or not REPO_PART.fullmatch(repo_name)
        or not BRANCH.fullmatch(branch)
        or SHA.fullmatch(saved_revision) is None
    ):
        raise StageError("invalid_source_fields")
    if type(max_git_bytes) is not int or max_git_bytes < 1024:
        raise StageError("invalid_fetch_limit")
    if not remote.is_absolute() or remote.is_symlink() or not remote.is_dir() or remote.resolve() != remote:
        raise StageError("fixture_remote_invalid")
    if (
        not destination.is_absolute()
        or destination.exists()
        or destination.is_symlink()
        or destination.parent.resolve() != destination.parent
    ):
        raise StageError("stage_destination_invalid")
    deadline = time.monotonic() + limits.wall_seconds
    with tempfile.TemporaryDirectory(prefix="onefoundry-github-fixture-", dir=destination.parent) as temp:
        safe_config = Path(temp) / "fixture-gitconfig"
        _fixture_git_config(safe_config, remote)
        repo = Path(temp) / "repo"
        repo.mkdir(mode=0o700)
        _git(repo, safe_config, deadline, max_git_bytes, "init", "-q", "-b", branch)
        if _remote_head(repo, remote, safe_config, branch, deadline, max_git_bytes) != saved_revision:
            raise StageError("source_revision_drift")
        _git(
            repo,
            safe_config,
            deadline,
            max_git_bytes,
            "fetch",
            "-q",
            "--no-tags",
            "--depth=1",
            "--no-recurse-submodules",
            str(remote),
            saved_revision,
        )
        fetched = _git(
            repo,
            safe_config,
            deadline,
            max_git_bytes,
            "rev-parse",
            "--verify",
            "FETCH_HEAD^{commit}",
        )
        if fetched != saved_revision:
            raise StageError("source_revision_drift")
        if _remote_head(repo, remote, safe_config, branch, deadline, max_git_bytes) != saved_revision:
            raise StageError("source_revision_drift")
        _git_bytes(repo / ".git", max_git_bytes)
        _git(repo, safe_config, deadline, max_git_bytes, "update-ref", "HEAD", saved_revision)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise StageError("source_stage_timeout")
        result = stage_local_source(
            {
                "source_id": str(source_id),
                "root_id": "workspace",
                "relative_path": "repo",
                "branch": branch,
                "revision": saved_revision,
            },
            Path(temp),
            destination,
            StageLimits(limits.max_files, limits.max_bytes, limits.max_tree_bytes, remaining),
        )
        try:
            _git_bytes(repo / ".git", max_git_bytes)
            if _remote_head(repo, remote, safe_config, branch, deadline, max_git_bytes) != saved_revision:
                raise StageError("source_revision_drift")
        except Exception:
            shutil.rmtree(destination)
            raise
        return result
