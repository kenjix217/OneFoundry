"""A disposable source tree; no copied Git history, hooks, config or dirty files."""

from __future__ import annotations

import os
from pathlib import Path

from onefoundry.repository_code_contract import RepositoryCodeManifest
from onefoundry.role_repository_file import _valid_path
from runner.fixture_git import Deadline, _git
from runner.local_source_stage import StageError, StageLimits, StageResult, stage_local_source
from runner.repository_source_view import sensitive_source


def stage(job: RepositoryCodeManifest, source_root: Path, destination: Path) -> tuple[StageResult, str]:
    result = stage_local_source(
        job.source,
        source_root,
        destination,
        StageLimits(
            **job.stage_limits.model_dump(include={"max_files", "max_bytes", "max_tree_bytes"}),
            wall_seconds=job.stage_limits.stage_seconds,
        ),
    )
    # The trusted staging tree is never the coding process mount. Its filtered
    # view is prepared separately before provider launch.
    for directory, _, names in os.walk(destination, followlinks=False):
        for name in names:
            path = Path(directory) / name
            relative = path.relative_to(destination).as_posix()
            if not _valid_path(relative) and name not in (".env.example", ".env.sample"):
                raise StageError("maintenance_source_private_path")
    deadline = Deadline(job.stage_limits.stage_seconds)
    _git(destination, deadline, "init", "-q")
    # Every staged file came from the approved commit, including tracked files
    # that the source's ignore rules would hide from a freshly initialized index.
    _git(destination, deadline, "add", "--force", "--all")
    _git(destination, deadline, "commit", "--allow-empty", "-qm", "Pinned maintenance tree")
    if _git(destination, deadline, "rev-parse", "HEAD^{tree}").decode() != result.source_tree:
        raise StageError("maintenance_baseline_tree_mismatch")
    baseline = _git(destination, deadline, "rev-parse", "HEAD").decode()
    _git(destination, deadline, "checkout", "-qb", f"onefoundry/extension-{job.attempt_id}")
    return result, baseline


def candidate(
    job: RepositoryCodeManifest, repo: Path, baseline: str, deadline: Deadline
) -> tuple[str, bytes]:
    """Capture an exact scoped diff; repository code is never executed by the host."""
    if _git(repo, deadline, "rev-parse", "HEAD").decode() != baseline:
        raise StageError("maintenance_provider_changed_git_identity")
    # Include ignored new files so the scope check cannot silently miss them.
    _git(repo, deadline, "add", "--force", "--all")
    paths = _git(repo, deadline, "diff", "--cached", "--name-only", "-z").split(b"\0")[:-1]
    if not paths or any(path.decode() not in job.target.allowed_paths for path in paths):
        raise StageError("maintenance_candidate_outside_scope")
    for raw_path in paths:
        path = repo / raw_path.decode()
        if path.exists() and (path.is_symlink() or not path.is_file()):
            raise StageError("maintenance_candidate_file_invalid")
        if path.exists() and sensitive_source(raw_path.decode(), path.read_bytes()):
            raise StageError("maintenance_candidate_sensitive_content")
    # Git's patch is the candidate artifact. Binary/submodule changes cannot be
    # activated through this bounded text maintenance path.
    diff = (
        _git(repo, deadline, "diff", "--cached", "--no-ext-diff", "--no-textconv", "--full-index", "--binary")
        + b"\n"
    )
    if len(diff) > job.limits["max_diff_bytes"] or b"GIT binary patch" in diff:
        raise StageError("maintenance_candidate_diff_limit")
    _git(repo, deadline, "diff", "--cached", "--check")
    _git(repo, deadline, "commit", "-qm", "Maintenance candidate")
    return _git(repo, deadline, "rev-parse", "HEAD").decode(), diff
