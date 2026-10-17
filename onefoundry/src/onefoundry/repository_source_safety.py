"""Runner-owned complete source observations bind safe text to the exact patch."""

from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import Field, model_validator

from onefoundry.repository_code_contract import RepositoryCodeManifest
from onefoundry.workflow_schemas import StrictModel
from runner.fixture_git import Deadline, ExecutionError, _git, _run
from runner.local_source_stage import StageError
from runner.repository_source_view import sensitive_source


class SourceChange(StrictModel):
    path: str
    before: str | None
    after: str | None

    @model_validator(mode="after")
    def safe(self) -> SourceChange:
        for value in (self.before, self.after):
            if value is not None and sensitive_source(self.path, value.encode()):
                raise ValueError("maintenance source observation contains protected data")
        if self.before == self.after:
            raise ValueError("maintenance source observation has no change")
        return self


class SourceSafety(StrictModel):
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    diff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    changes: list[SourceChange] = Field(min_length=1, max_length=100)

    def validate_job(self, job: RepositoryCodeManifest, commit: str, diff: bytes) -> None:
        paths = [item.path for item in self.changes]
        if (
            self.request_sha256 != job.request_sha256
            or self.candidate_commit != commit
            or self.diff_sha256 != hashlib.sha256(diff).hexdigest()
            or len(set(paths)) != len(paths)
            or not set(paths) <= set(job.target.allowed_paths)
        ):
            raise ValueError("maintenance source safety identity differs from the exact candidate")
        for item in self.changes:
            for value in (item.before, item.after):
                if value is not None and len(value.encode()) > job.stage_limits.max_context_file_bytes:
                    raise ValueError("maintenance source safety observation exceeds configured bound")


def observe(job: RepositoryCodeManifest, repo: Path, baseline: str, commit: str, diff: bytes) -> bytes:
    deadline = Deadline(job.stage_limits.stage_seconds)
    names = _git(repo, deadline, "diff", "--name-only", "-z", baseline, commit).split(b"\0")[:-1]
    changes: list[SourceChange] = []
    for raw in names:
        path = raw.decode()
        versions: list[str | None] = []
        for revision in (baseline, commit):
            tree = _git(repo, deadline, "ls-tree", "-z", revision, "--", path)
            versions.append(
                _source_text(repo, revision, path, job.stage_limits.max_context_file_bytes, deadline)
                if tree
                else None
            )
        changes.append(SourceChange(path=path, before=versions[0], after=versions[1]))
    result = SourceSafety(
        request_sha256=job.request_sha256,
        candidate_commit=commit,
        diff_sha256=hashlib.sha256(diff).hexdigest(),
        changes=changes,
    )
    result.validate_job(job, commit, diff)
    raw_result = result.model_dump_json().encode()
    if len(raw_result) > job.stage_limits.max_tree_bytes:
        raise ValueError("maintenance source safety observation exceeds total bound")
    return raw_result


def _source_text(repo: Path, revision: str, path: str, limit: int, deadline: Deadline) -> str:
    try:
        result = _run(
            ("git", "-c", "core.hooksPath=/dev/null", "show", f"{revision}:{path}"),
            repo,
            deadline,
            output_limit=limit,
        )
    except ExecutionError as exc:
        if exc.reason_code == "fixture_command_output_limit":
            raise StageError("maintenance_source_observation_file_limit") from exc
        raise
    return result.stdout.decode("utf-8")
