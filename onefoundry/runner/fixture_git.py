"""Fixed Git fixture stages executed without a provider, network, or shell."""

from __future__ import annotations

import hashlib
import os
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, cast

from runner.contract import Job

FIXTURE = Path(__file__).parent / "fixtures" / "engineer_qa_v1"
REPAIR_FROM = "    return value\n"
REPAIR_TO = "    return value + 1\n"
TEST_COMMAND = (sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v")
MAX_COMMAND_OUTPUT_BYTES = 65536
TRACE_OBSERVER: ContextVar[Callable[[tuple[str, ...], str, int | None, bytes, bytes, str], None] | None] = (
    ContextVar("fixture_trace_observer", default=None)
)


class ExecutionError(Exception):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class UnknownOutcome(ExecutionError):
    """A persistent merge may have changed main before verification failed."""

    def __init__(self, reason_code: str, observed_head_commit: str | None = None) -> None:
        super().__init__(reason_code)
        self.observed_head_commit = observed_head_commit


@dataclass
class StageOutput:
    branch: str | None = None
    commit: str | None = None
    baseline_commit: str | None = None
    merged_commit: str | None = None
    diff_sha256: str | None = None
    tests: list[dict[str, Any]] = field(default_factory=list)
    artifacts: dict[str, bytes] = field(default_factory=dict)


class Deadline:
    def __init__(self, seconds: int) -> None:
        self.end = time.monotonic() + seconds

    def remaining(self) -> float:
        remaining = self.end - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("wall_time_exceeded")
        return remaining


def _safe_env(workdir: Path) -> dict[str, str]:
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(workdir),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "OneFoundry fixture",
        "GIT_AUTHOR_EMAIL": "fixture@invalid.example",
        "GIT_COMMITTER_NAME": "OneFoundry fixture",
        "GIT_COMMITTER_EMAIL": "fixture@invalid.example",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
    }


def _run(
    command: tuple[str, ...],
    cwd: Path,
    deadline: Deadline,
    check: bool = True,
    *,
    output_limit: int = MAX_COMMAND_OUTPUT_BYTES,
) -> subprocess.CompletedProcess[bytes]:
    if type(output_limit) is not int or output_limit < 1:
        raise ValueError("command output limit must be a positive integer")
    started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    process = subprocess.Popen(  # noqa: S603 - fixed commands and validated fixture refs only
        command,
        cwd=cwd,
        env=_safe_env(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    assert process.stdout is not None and process.stderr is not None
    output = {process.stdout: bytearray(), process.stderr: bytearray()}
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            selector.register(process.stderr, selectors.EVENT_READ)
            while selector.get_map():
                ready = selector.select(deadline.remaining())
                if not ready:
                    raise TimeoutError("wall_time_exceeded")
                for key, _ in ready:
                    pipe = cast(BinaryIO, key.fileobj)
                    current = len(output[process.stdout]) + len(output[process.stderr])
                    chunk = os.read(pipe.fileno(), min(8192, output_limit + 1 - current))
                    if not chunk:
                        selector.unregister(pipe)
                        continue
                    output[pipe].extend(chunk)
                    if current + len(chunk) > output_limit:
                        raise ExecutionError("fixture_command_output_limit")
        process.wait(timeout=deadline.remaining())
    except BaseException as exc:
        outcome = "timed_out" if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)) else "finished"
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
        observer = TRACE_OBSERVER.get()
        if observer is not None:
            observer(
                command,
                started_at,
                None,
                bytes(output[process.stdout]),
                bytes(output[process.stderr]),
                outcome,
            )
        if isinstance(exc, subprocess.TimeoutExpired):
            raise TimeoutError("wall_time_exceeded") from exc
        raise
    finally:
        process.stdout.close()
        process.stderr.close()
    code = process.returncode
    assert code is not None
    result = subprocess.CompletedProcess(
        command, code, bytes(output[process.stdout]), bytes(output[process.stderr])
    )
    observer = TRACE_OBSERVER.get()
    if observer is not None:
        observer(command, started_at, result.returncode, result.stdout, result.stderr, "finished")
    if check and result.returncode != 0:
        raise ExecutionError("fixture_command_failed")
    return result


def _git(cwd: Path, deadline: Deadline, *args: str) -> bytes:
    return _run(("git", "-c", "core.hooksPath=/dev/null", *args), cwd, deadline).stdout.strip()


def _new_repo(destination: Path, deadline: Deadline) -> str:
    shutil.copytree(
        FIXTURE, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache")
    )
    _git(destination, deadline, "init", "-q", "-b", "main")
    _git(destination, deadline, "add", "--", "calculator.py", "tests/test_calculator.py")
    _git(destination, deadline, "commit", "-qm", "Synthetic baseline")
    return _git(destination, deadline, "rev-parse", "HEAD").decode("ascii")


def _test(repo: Path, deadline: Deadline, log_limit: int, name: str) -> tuple[dict[str, Any], bytes]:
    result = _run(TEST_COMMAND, repo, deadline, check=False)
    log = result.stdout + result.stderr
    if len(log) > log_limit:
        raise ExecutionError("test_log_limit_exceeded")
    return {
        "name": name,
        "status": "passed" if result.returncode == 0 else "failed",
        "exit_code": result.returncode,
    }, log


def _repair(repo: Path) -> None:
    path = repo / "calculator.py"
    source = path.read_text(encoding="utf-8")
    if source.count(REPAIR_FROM) != 1:
        raise ExecutionError("fixture_source_changed")
    path.write_text(source.replace(REPAIR_FROM, REPAIR_TO), encoding="utf-8")


def _diff(repo: Path, deadline: Deadline, branch: str, limit: int) -> tuple[bytes, str]:
    diff = _git(repo, deadline, "diff", "main..." + branch, "--", "calculator.py")
    if not diff or len(diff) > limit:
        raise ExecutionError("invalid_diff")
    return diff + b"\n", hashlib.sha256(diff + b"\n").hexdigest()


def engineer(job: Job, deadline: Deadline) -> StageOutput:
    with tempfile.TemporaryDirectory(prefix="onefoundry-fixture-") as temporary:
        repo = Path(temporary) / "repo"
        baseline = _new_repo(repo, deadline)
        before, before_log = _test(repo, deadline, job.limits.max_log_bytes, "before_repair")
        if before["status"] != "failed" or b"FAIL" not in before_log:
            raise ExecutionError("baseline_did_not_fail_expected_test")
        branch = f"fixture/repair-{job.job_id}"
        _git(repo, deadline, "checkout", "-qb", branch)
        _repair(repo)
        after, after_log = _test(repo, deadline, job.limits.max_log_bytes, "after_repair")
        if after["status"] != "passed":
            raise ExecutionError("repair_test_failed")
        _git(repo, deadline, "add", "--", "calculator.py")
        _git(repo, deadline, "commit", "-qm", "Scripted fixture repair")
        commit = _git(repo, deadline, "rev-parse", "HEAD").decode("ascii")
        diff, digest = _diff(repo, deadline, branch, job.limits.max_diff_bytes)
        bundle_path = Path(temporary) / "repair.bundle"
        _git(repo, deadline, "bundle", "create", str(bundle_path), "main", branch)
        return StageOutput(
            branch=branch,
            commit=commit,
            baseline_commit=baseline,
            diff_sha256=digest,
            tests=[before, after],
            artifacts={
                "before_log": before_log,
                "after_log": after_log,
                "diff": diff,
                "bundle": bundle_path.read_bytes(),
            },
        )


def _verified_bundle(job: Job, spool: Path) -> bytes:
    if job.source_job_id is None:
        raise ExecutionError("missing_source_job")
    receipt_path = spool / "receipts" / f"{job.source_job_id}.json"
    if receipt_path.is_symlink() or not receipt_path.is_file() or receipt_path.stat().st_size > 32768:
        raise ExecutionError("source_receipt_missing")
    import json

    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ExecutionError("source_receipt_invalid") from exc
    if not isinstance(receipt, dict) or any(
        receipt.get(key) != expected
        for key, expected in (
            ("job_id", job.source_job_id),
            ("project_id", job.project_id),
            ("run_id", job.run_id),
            ("stage", "engineer"),
            ("status", "done"),
            ("branch", job.expected_branch),
            ("commit", job.expected_commit),
            ("diff_sha256", job.expected_diff_sha256),
            ("baseline_commit", job.expected_baseline_commit),
        )
    ):
        raise ExecutionError("source_receipt_mismatch")
    entries = receipt.get("artifacts")
    if not isinstance(entries, list):
        raise ExecutionError("source_bundle_missing")
    bundle_entries = [item for item in entries if isinstance(item, dict) and item.get("kind") == "bundle"]
    if len(bundle_entries) != 1:
        raise ExecutionError("source_bundle_missing")
    entry = bundle_entries[0]
    expected_path = f"artifacts/{job.source_job_id}/repair.bundle"
    if entry.get("path") != expected_path:
        raise ExecutionError("source_bundle_path_mismatch")
    path = spool / expected_path
    if path.parent.is_symlink() or path.parent.parent.is_symlink() or path.is_symlink():
        raise ExecutionError("source_bundle_path_mismatch")
    if not path.is_file() or path.stat().st_size > 65536:
        raise ExecutionError("source_bundle_missing")
    data = path.read_bytes()
    if entry.get("byte_size") != len(data) or entry.get("sha256") != hashlib.sha256(data).hexdigest():
        raise ExecutionError("source_bundle_hash_mismatch")
    return data


def _load_candidate(repo: Path, job: Job, bundle: bytes, deadline: Deadline) -> tuple[str, str]:
    with tempfile.NamedTemporaryFile(suffix=".bundle", delete=False) as handle:
        handle.write(bundle)
        bundle_path = Path(handle.name)
    try:
        assert job.expected_branch is not None
        _git(
            repo,
            deadline,
            "fetch",
            str(bundle_path),
            f"{job.expected_branch}:refs/heads/{job.expected_branch}",
        )
        commit = _git(repo, deadline, "rev-parse", job.expected_branch).decode("ascii")
        parent = _git(repo, deadline, "rev-parse", f"{job.expected_branch}^").decode("ascii")
        if commit != job.expected_commit or parent != job.expected_baseline_commit:
            raise ExecutionError("candidate_commit_mismatch")
        changed = _git(repo, deadline, "diff", "--name-only", "main..." + job.expected_branch)
        if changed != b"calculator.py":
            raise ExecutionError("candidate_files_mismatch")
        diff, digest = _diff(repo, deadline, job.expected_branch, job.limits.max_diff_bytes)
        if digest != job.expected_diff_sha256:
            raise ExecutionError("candidate_diff_mismatch")
        return commit, digest
    finally:
        bundle_path.unlink(missing_ok=True)


def qa(job: Job, spool: Path, deadline: Deadline) -> StageOutput:
    bundle = _verified_bundle(job, spool)
    with tempfile.TemporaryDirectory(prefix="onefoundry-qa-") as temporary:
        repo = Path(temporary) / "repo"
        baseline = _new_repo(repo, deadline)
        if baseline != job.expected_baseline_commit:
            raise ExecutionError("baseline_commit_mismatch")
        commit, digest = _load_candidate(repo, job, bundle, deadline)
        assert job.expected_branch is not None
        _git(repo, deadline, "checkout", "-q", job.expected_branch)
        test, log = _test(repo, deadline, job.limits.max_log_bytes, "qa_fixture_test")
        if test["status"] != "passed":
            raise ExecutionError("qa_test_failed")
        return StageOutput(
            branch=job.expected_branch,
            commit=commit,
            baseline_commit=baseline,
            diff_sha256=digest,
            tests=[test],
            artifacts={"qa_log": log},
        )


def merge(job: Job, spool: Path, deadline: Deadline) -> StageOutput:
    if job.approval_id is None:
        raise ExecutionError("merge_requires_approval")
    bundle = _verified_bundle(job, spool)
    repos = spool / "repos"
    if repos.is_symlink():
        raise ExecutionError("persistent_repo_root_invalid")
    repo = repos / job.run_id
    if repo.is_symlink():
        raise ExecutionError("persistent_repo_invalid")
    if not repo.exists():
        repo.parent.mkdir(parents=True, exist_ok=True)
        _new_repo(repo, deadline)
    if repo.is_symlink() or not (repo / ".git").is_dir():
        raise ExecutionError("persistent_repo_invalid")
    baseline = _git(repo, deadline, "rev-parse", "main").decode("ascii")
    if baseline != job.expected_baseline_commit:
        raise ExecutionError("merge_target_head_changed")
    if _git(repo, deadline, "status", "--porcelain"):
        raise ExecutionError("merge_target_dirty")
    commit, digest = _load_candidate(repo, job, bundle, deadline)
    assert job.expected_branch is not None
    _git(repo, deadline, "checkout", "-q", "main")
    try:
        _git(repo, deadline, "merge", "--ff-only", job.expected_branch)
    except (ExecutionError, TimeoutError) as exc:
        try:
            head = _git(repo, Deadline(5), "rev-parse", "HEAD").decode("ascii")
        except (ExecutionError, TimeoutError) as readback_exc:
            raise UnknownOutcome("merge_command_outcome_unknown") from readback_exc
        if head == baseline:
            raise ExecutionError("merge_command_failed_before_change") from exc
        raise UnknownOutcome("merge_command_outcome_unknown", head) from exc
    try:
        merged = _git(repo, deadline, "rev-parse", "HEAD").decode("ascii")
    except (ExecutionError, TimeoutError, OSError, subprocess.SubprocessError) as exc:
        raise UnknownOutcome("merge_readback_unavailable") from exc
    if merged != commit:
        raise UnknownOutcome("merge_readback_mismatch", merged)
    try:
        test, log = _test(repo, deadline, job.limits.max_log_bytes, "merged_fixture_test")
    except (ExecutionError, TimeoutError, OSError, subprocess.SubprocessError) as exc:
        raise UnknownOutcome("merge_verification_incomplete", merged) from exc
    if test["status"] != "passed":
        raise UnknownOutcome("merged_test_failed_after_mutation", merged)
    return StageOutput(
        branch=job.expected_branch,
        commit=commit,
        baseline_commit=baseline,
        merged_commit=merged,
        diff_sha256=digest,
        tests=[test],
        artifacts={"merge_log": log},
    )


def execute(job: Job, spool: Path, deadline: Deadline) -> StageOutput:
    if job.stage == "engineer":
        return engineer(job, deadline)
    if job.stage == "qa":
        return qa(job, spool, deadline)
    return merge(job, spool, deadline)
