"""Advance only an explicitly configured, clean maintenance clone with its release."""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from onefoundry.extension_activation_contract import ExtensionActivationJob
from onefoundry.extension_qa_contract import ExtensionQaJob, ExtensionQaReceipt
from onefoundry.runner_spool import RunnerSpool
from runner.fixture_git import Deadline, _git
from runner.repository_code_stage import candidate, stage

SOURCE_EFFECT = (
    "Advance the dedicated maintenance checkout to this candidate; restore it on failed activation."
)


@dataclass(frozen=True)
class SourceCheckout:
    path: Path
    previous: str
    candidate: str
    seconds: int

    def move(self, expected: str, target: str) -> None:
        deadline = Deadline(self.seconds)
        if _git(self.path, deadline, "rev-parse", "HEAD").decode() != expected or _git(
            self.path, deadline, "status", "--porcelain", "--untracked-files=all", "--ignored=matching"
        ):
            raise ValueError("activation_maintenance_source_changed")
        # This is the private configured clone, after exact clean-HEAD validation.
        # No operator working checkout or uncommitted content is overwritten.
        _git(self.path, deadline, "reset", "--hard", target)
        if _git(self.path, deadline, "rev-parse", "HEAD").decode() != target:
            raise ValueError("activation_maintenance_source_update_unknown")

    def advance(self) -> None:
        self.move(self.previous, self.candidate)

    def restore(self) -> None:
        current = _git(self.path, Deadline(self.seconds), "rev-parse", "HEAD").decode()
        if current == self.previous:
            return
        self.move(self.candidate, self.previous)


def prepare(job: ExtensionActivationJob, spool: Path, source_root: Path, alias: str) -> SourceCheckout:
    """Reconstruct the exact QA candidate before installation; do not check it out yet."""
    if source_root.resolve() != source_root or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", alias):
        raise ValueError("activation_maintenance_source_path_invalid")
    if SOURCE_EFFECT not in job.release.configuration_effects:
        raise ValueError("activation_maintenance_source_effect_unapproved")
    transport = RunnerSpool(str(spool))
    qa_job = ExtensionQaJob.model_validate_json(
        transport._read_regular(spool / "jobs" / f"{job.release.qa_job_id}.json", 8192)
    )
    raw = transport.read_receipt(qa_job.job_id)
    if raw is None or raw[1] != job.qa_receipt_sha256:
        raise ValueError("activation_maintenance_source_QA_changed")
    qa = ExtensionQaReceipt.model_validate(raw[0])
    engineer = qa_job.engineer_job
    if (
        qa.status != "passed"
        or qa.request_sha256 != qa_job.request_sha256
        or engineer.project_id != job.project_id
        or engineer.run_id != job.release.coding_run_id
        or engineer.source["relative_path"] != alias
        or qa_job.candidate_commit != job.candidate_commit
        or qa.candidate_commit != job.candidate_commit
        or qa_job.diff_sha256 != job.diff_sha256
        or qa.diff_sha256 != job.diff_sha256
    ):
        raise ValueError("activation_maintenance_source_identity_changed")
    path = source_root / alias
    if path.resolve() != path or not (path / ".git").is_dir() or (path / ".git").is_symlink():
        raise ValueError("activation_maintenance_source_path_invalid")
    seconds = engineer.stage_limits.stage_seconds
    deadline = Deadline(seconds)
    if _git(path, deadline, "rev-parse", "HEAD").decode() != engineer.source["revision"] or _git(
        path, deadline, "status", "--porcelain", "--untracked-files=all", "--ignored=matching"
    ):
        raise ValueError("activation_maintenance_source_changed")
    patch = transport.read_artifact(
        engineer.attempt_id,
        f"artifacts/{engineer.attempt_id}/change.diff",
        qa_job.diff_byte_size,
        job.diff_sha256,
    )
    with tempfile.TemporaryDirectory(prefix="onefoundry-source-activation-") as temporary:
        repo = Path(temporary) / "repo"
        _, baseline = stage(engineer, source_root, repo)
        if baseline != qa_job.baseline_commit:
            raise ValueError("activation_maintenance_baseline_changed")
        diff = Path(temporary) / "candidate.diff"
        diff.write_bytes(patch)
        _git(repo, deadline, "apply", "--index", str(diff))
        observed, observed_patch = candidate(engineer, repo, baseline, deadline)
        if observed != job.candidate_commit or observed_patch != patch:
            raise ValueError("activation_maintenance_candidate_changed")
        _git(path, deadline, "fetch", "--no-tags", str(repo), observed)
    _git(
        path,
        deadline,
        "update-ref",
        f"refs/onefoundry/checkpoints/{job.approval_id}",
        engineer.source["revision"],
    )
    return SourceCheckout(path, engineer.source["revision"], job.candidate_commit, seconds)
