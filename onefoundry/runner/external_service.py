"""One-shot external coding attempt on the fixed disposable Git fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from runner.action_trace import ActionTrace
from runner.contract import ContractError, reject_duplicate_keys
from runner.docker_egress_proof import ProofError
from runner.external_cli import CliRequest, CliResult
from runner.external_launch_journal import LaunchKind, PrelaunchInvalid, record_prelaunch, record_session
from runner.fixture_git import TRACE_OBSERVER, Deadline, ExecutionError, _diff, _git, _new_repo, _run
from runner.live_progress_spool import LiveProgressJournal
from runner.provider_progress import (
    STAGE_BASELINE_FAILED,
    STAGE_CLI_FINISHED,
    STAGE_TEST_PASSED,
    ProviderProgress,
)
from runner.service import _atomic_bytes, _atomic_new

Invoker = Callable[[CliRequest], CliResult]
FixtureTestRunner = Callable[[Path, Deadline, int, str], tuple[dict[str, Any], bytes]]


SAFE_CLI_LOG = re.compile(
    rb"(?:adapter=(?:codex_cli|claude_code) outcome=[a-z_]{1,80}|"
    rb"synthetic_cli=codex_cli model=gpt-5 command=fixture_calculator_repair "
    rb"image_sha256=sha256:[0-9a-f]{64} prompt_sha256=[0-9a-f]{64} "
    rb"allowed_provider_requests=2 blocked_other_connects=[0-9]{1,3}|"
    rb"synthetic_cli=claude_code model=claude-sonnet-4-5 command=fixture_calculator_repair "
    rb"image_sha256=sha256:[0-9a-f]{64} prompt_sha256=[0-9a-f]{64} "
    rb"allowed_provider_requests=[3-5] blocked_other_connects=[0-9]{1,3})\n"
)
FIELDS = {
    "schema_version",
    "job_id",
    "project_id",
    "run_id",
    "attempt_id",
    "adapter_id",
    "fixture_id",
    "stage",
    "actor_id",
    "task_title",
    "task_instructions",
    "limits",
    "request_sha256",
}


def _git_metadata_digest(repo: Path) -> str:
    """Read a small fixture Git directory without following untrusted links."""
    root = repo / ".git"
    if root.is_symlink() or not root.is_dir():
        raise ExecutionError("git_metadata_invalid")
    digest = hashlib.sha256()
    pending = [root]
    entries_seen = 0
    bytes_seen = 0
    while pending:
        directory = pending.pop()
        for entry in sorted(os.scandir(directory), key=lambda item: item.name):
            entries_seen += 1
            if entries_seen > 1024:
                raise ExecutionError("git_metadata_limit")
            relative = Path(entry.path).relative_to(root).as_posix().encode()
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                digest.update(b"d" + len(relative).to_bytes(4, "big") + relative)
                pending.append(Path(entry.path))
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1024 * 1024:
                raise ExecutionError("git_metadata_invalid")
            bytes_seen += info.st_size
            if bytes_seen > 4 * 1024 * 1024:
                raise ExecutionError("git_metadata_limit")
            descriptor = os.open(entry.path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                opened = os.fstat(descriptor)
                if (opened.st_dev, opened.st_ino, opened.st_size) != (
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                ):
                    raise ExecutionError("git_metadata_changed")
                data = os.read(descriptor, info.st_size + 1)
                if len(data) != info.st_size:
                    raise ExecutionError("git_metadata_changed")
            finally:
                os.close(descriptor)
            digest.update(b"f" + len(relative).to_bytes(4, "big") + relative)
            digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _manifest(path: Path) -> dict[str, Any]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 8192:
                raise ContractError("invalid_external_manifest")
            raw = os.read(descriptor, 8193)
            if len(raw) != info.st_size:
                raise ContractError("invalid_external_manifest")
        finally:
            os.close(descriptor)
        value = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise ContractError("invalid_external_manifest") from exc
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ContractError("invalid_external_manifest_fields")
    if (
        value["schema_version"] != 1
        or value["fixture_id"] != "engineer_qa_v1"
        or value["stage"] != "engineer"
    ):
        raise ContractError("unsupported_external_manifest")
    for field in ("job_id", "project_id", "run_id", "attempt_id"):
        try:
            if str(UUID(value[field], version=4)) != value[field]:
                raise ValueError(field)
        except (TypeError, ValueError, AttributeError) as exc:
            raise ContractError("invalid_external_identifiers") from exc
    if value["job_id"] != value["attempt_id"] or path.name != f"{value['job_id']}.json":
        raise ContractError("external_attempt_mismatch")
    if value["adapter_id"] not in ("codex_cli", "claude_code"):
        raise ContractError("unsupported_external_adapter")
    for field, size in (("actor_id", 128), ("task_title", 256), ("task_instructions", 4096)):
        if not isinstance(value[field], str) or not value[field] or len(value[field].encode()) > size:
            raise ContractError("invalid_external_task")
    limits = value["limits"]
    if not isinstance(limits, dict) or set(limits) != {"wall_seconds", "max_log_bytes", "max_diff_bytes"}:
        raise ContractError("invalid_external_limits")
    for name, lower, upper in (
        ("wall_seconds", 1, 3600),
        ("max_log_bytes", 512, 65536),
        ("max_diff_bytes", 512, 65536),
    ):
        if type(limits[name]) is not int or not lower <= limits[name] <= upper:
            raise ContractError("invalid_external_limits")
    supplied = value["request_sha256"]
    unsigned = {key: item for key, item in value.items() if key != "request_sha256"}
    digest = hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if supplied != digest:
        raise ContractError("external_request_digest_mismatch")
    return value


def _artifact(
    spool: Path, attempt_id: str, kind: str, name: str, data: bytes, *, mode: int = 0o600
) -> dict[str, Any]:
    relative = f"artifacts/{attempt_id}/{name}"
    _atomic_bytes(spool / relative, data, mode=mode)
    return {
        "kind": kind,
        "path": relative,
        "sha256": hashlib.sha256(data).hexdigest(),
        "byte_size": len(data),
    }


def process_external_job(
    path: Path,
    spool: Path,
    *,
    executable: Path | None = None,
    invoke: Invoker | None = None,
    test_runner: FixtureTestRunner | None = None,
    launch_kind: LaunchKind = "injected_cli",
    image_id: str | None = None,
    evidence_spool: Path | None = None,
) -> dict[str, Any] | None:
    """Claim and execute once with an explicitly supplied external invoker."""
    if (
        executable is None
        or test_runner is None
        or invoke is None
        or (launch_kind == "trusted_local_cli_v1" and (image_id is not None or evidence_spool is not None))
    ):
        return None
    job = _manifest(path)
    attempt_id = job["attempt_id"]
    output_spool = evidence_spool if evidence_spool is not None else spool
    group_readable = evidence_spool is not None or launch_kind == "trusted_local_cli_v1"
    evidence_mode = 0o640 if group_readable else 0o600
    if (output_spool / "receipts" / f"{attempt_id}.json").exists():
        return None
    try:
        _atomic_new(
            spool / "claims" / f"{attempt_id}.claim",
            attempt_id.encode("ascii"),
            mode=evidence_mode,
        )
    except FileExistsError:
        return None
    if evidence_spool is not None:
        _atomic_new(
            evidence_spool / "claims" / f"{attempt_id}.claim",
            attempt_id.encode("ascii"),
            mode=evidence_mode,
        )
    started_at = _now()
    status = "unknown"
    reason = "runner_failure_after_claim"
    session: str | None = None
    tokens: int | None = None
    cost: float | None = None
    baseline: str | None = None
    branch: str | None = None
    commit: str | None = None
    digest: str | None = None
    tests: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    trace = ActionTrace(repository=Path("."), private_root=Path(tempfile.gettempdir()))
    progress_journal: LiveProgressJournal | None = None
    trace_token = TRACE_OBSERVER.set(trace.command)
    try:
        progress_journal = LiveProgressJournal.create(
            output_spool,
            job["project_id"],
            job["run_id"],
            attempt_id,
            job["request_sha256"],
            group_readable=group_readable,
        )
        deadline = Deadline(job["limits"]["wall_seconds"])
        with tempfile.TemporaryDirectory(prefix="onefoundry-external-") as temporary:
            root = Path(temporary)
            repo = root / "repo"
            trace.repository = repo
            trace.private_root = root
            private_home = root / "private-home"
            private_home.mkdir(mode=0o700)
            baseline = _new_repo(repo, deadline)
            before, _ = test_runner(repo, deadline, job["limits"]["max_log_bytes"], "before")
            tests.append(before)
            if before["status"] != "failed":
                raise ExecutionError("fixture_baseline_unexpected")
            stage_progress = ProviderProgress(
                "runner_update", STAGE_BASELINE_FAILED, source="runner_observed"
            )
            progress_journal.append(stage_progress)
            trace.progress_event(stage_progress)
            branch = f"fixture/repair-{attempt_id}"
            _git(repo, deadline, "checkout", "-qb", branch)
            expected_git_metadata = _git_metadata_digest(repo)
            request = CliRequest(
                job["adapter_id"],
                executable,
                repo,
                private_home,
                f"{job['task_title']}\n\n{job['task_instructions']}\n",
                max(1, int(deadline.remaining())),
                job["limits"]["max_log_bytes"],
                progress_journal.append,
            )
            launch, launch_digest = record_prelaunch(
                output_spool,
                job,
                request,
                baseline,
                kind=launch_kind,
                image_id=image_id,
                evidence_file_mode=evidence_mode,
            )
            provider_started = _now()
            try:
                result = invoke(request)
            except BaseException:
                trace.provider(job["adapter_id"], provider_started, "unknown")
                raise
            trace.provider(
                job["adapter_id"], provider_started, result.status, result.progress, result.progress_omitted
            )
            record_session(output_spool, launch, launch_digest, result, evidence_file_mode=evidence_mode)
            session, tokens, cost = result.provider_session_id, result.tokens_used, result.cost_usd
            if result.status != "done":
                reason = result.reason_code
            else:
                stage_progress = ProviderProgress(
                    "runner_update", STAGE_CLI_FINISHED, source="runner_observed"
                )
                progress_journal.append(stage_progress)
                trace.progress_event(stage_progress)
            if result.status == "done" and _git_metadata_digest(repo) != expected_git_metadata:
                raise ExecutionError("git_metadata_changed")
            elif result.status == "done" and _git(repo, deadline, "rev-parse", "HEAD").decode() != baseline:
                reason = "provider_changed_baseline"
            elif result.status == "done" and (
                _run(
                    (
                        "git",
                        "-c",
                        "core.hooksPath=/dev/null",
                        "status",
                        "--porcelain",
                        "--untracked-files=all",
                    ),
                    repo,
                    deadline,
                ).stdout
                != b" M calculator.py\n"
            ):
                status, reason = "failed", "provider_changed_unexpected_files"
            elif result.status == "done" and (
                (repo / "calculator.py").is_symlink()
                or (repo / "calculator.py").read_bytes()
                != (b"def add_one(value: int) -> int:\n    return value + 1\n")
            ):
                status, reason = "failed", "provider_change_outside_fixture_contract"
            elif result.status == "done":
                after, after_log = test_runner(repo, deadline, job["limits"]["max_log_bytes"], "after")
                tests.append(after)
                if after["status"] != "passed":
                    status, reason = "failed", "fixture_test_failed"
                else:
                    stage_progress = ProviderProgress(
                        "runner_update", STAGE_TEST_PASSED, source="runner_observed"
                    )
                    progress_journal.append(stage_progress)
                    trace.progress_event(stage_progress)
                    _git(repo, deadline, "add", "--", "calculator.py")
                    _git(repo, deadline, "commit", "-qm", "Provider fixture repair")
                    commit = _git(repo, deadline, "rev-parse", "HEAD").decode()
                    diff, digest = _diff(repo, deadline, branch, job["limits"]["max_diff_bytes"])
                    if SAFE_CLI_LOG.fullmatch(result.sanitized_log) is None:
                        raise ExecutionError("provider_log_invalid")
                    recorded_log = result.sanitized_log + after_log
                    if len(recorded_log) > job["limits"]["max_log_bytes"]:
                        raise ExecutionError("test_log_limit_exceeded")
                    artifacts = [
                        _artifact(output_spool, attempt_id, "diff", "change.diff", diff, mode=evidence_mode),
                        _artifact(
                            output_spool, attempt_id, "log", "after.log", recorded_log, mode=evidence_mode
                        ),
                    ]
                    status, reason = "done", "completed"
    except PrelaunchInvalid:
        status, reason = (
            "unknown",
            "synthetic_codex_prelaunch_invalid"
            if launch_kind == "synthetic_codex_fixture"
            else "synthetic_claude_prelaunch_invalid"
            if launch_kind == "synthetic_claude_fixture"
            else "external_prelaunch_invalid",
        )
    except (ExecutionError, ProofError, TimeoutError, OSError, ValueError):
        # A claimed external invocation can have side effects before failing.
        status, reason = "unknown", "runner_failure_after_claim"
    finally:
        TRACE_OBSERVER.reset(trace_token)
        if progress_journal is not None:
            progress_journal.close()
    if status != "done":
        branch = commit = digest = None
        artifacts = []
    if trace.actions:
        artifacts.append(
            _artifact(output_spool, attempt_id, "trace", "actions.json", trace.encode(), mode=evidence_mode)
        )
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "job_id": attempt_id,
        "project_id": job["project_id"],
        "run_id": job["run_id"],
        "attempt_id": attempt_id,
        "adapter_id": job["adapter_id"],
        "fixture_id": "engineer_qa_v1",
        "request_sha256": job["request_sha256"],
        "status": status,
        "reason_code": reason,
        "started_at": started_at,
        "finished_at": _now(),
        "provider_session_id": session,
        "baseline_commit": baseline,
        "branch": branch,
        "commit": commit,
        "diff_sha256": digest,
        "artifacts": artifacts,
        "tests": tests,
        "tokens_used": tokens,
        "cost_usd": cost,
    }
    _atomic_new(
        output_spool / "receipts" / f"{attempt_id}.json",
        (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        mode=evidence_mode,
    )
    return receipt


def write_capabilities(spool: Path) -> None:
    """Publish truthful installed readiness; CLI presence alone grants nothing."""
    payload = {
        "schema_version": 1,
        "observed_at": _now(),
        "isolated": False,
        "auth_ready": False,
        "adapters": [
            {"adapter_id": adapter, "available": False, "reason": "isolation_not_proven"}
            for adapter in ("codex_cli", "claude_code")
        ],
    }
    _atomic_bytes(
        spool / "capabilities.json",
        (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="External coding runner readiness observer")
    parser.add_argument("--spool", type=Path, default=Path("/runner-spool/external"))
    parser.add_argument("--once", action="store_true")
    arguments = parser.parse_args()
    spool: Path = arguments.spool
    if spool.is_symlink():
        parser.error("spool may not be a symlink")
    spool.mkdir(parents=True, exist_ok=True)
    # No provider invocation is wired to this installed entrypoint. A later
    # isolated runner must provide a verified credential and egress boundary.
    while True:
        write_capabilities(spool)
        if arguments.once:
            return
        time.sleep(1)


if __name__ == "__main__":
    main()
