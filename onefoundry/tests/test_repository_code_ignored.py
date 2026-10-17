"""Git ignore rules cannot alter a pinned tree or hide an out-of-scope change."""

from pathlib import Path

import pytest

from onefoundry.repository_code_contract import RepositoryCodeManifest, digest
from runner.external_cli import CliResult
from runner.fixture_git import Deadline, ExecutionError, _git
from runner.isolated_fixture_test import run_isolated_fixture_test
from runner.local_source_stage import StageError
from runner.repository_code_service import process_job
from runner.repository_code_stage import candidate, stage
from tests.test_repository_code import fake_provider, fake_test, job_at


def pinned_ignored_fixture(tmp_path: Path, *, tracked: bool) -> tuple[RepositoryCodeManifest, Path]:
    job, root, _spool = job_at(tmp_path)
    repo = root / "repo"
    (repo / ".gitignore").write_text("assets/\n")
    (repo / "assets").mkdir()
    if tracked:
        (repo / "assets/committed.txt").write_text("Committed evidence.\n")
    deadline = Deadline(10)
    _git(repo, deadline, "add", "--force", "--all")
    _git(repo, deadline, "commit", "-qm", "Ignored tracked fixture")
    revision = _git(repo, deadline, "rev-parse", "HEAD").decode()
    value = job.model_dump(mode="json", exclude={"request_sha256"})
    value["source"]["revision"] = revision
    value["target"]["revision"] = revision
    return RepositoryCodeManifest.model_validate({**value, "request_sha256": digest(value)}), root


def test_staging_preserves_committed_files_matching_ignore_rules(tmp_path: Path) -> None:
    job, root = pinned_ignored_fixture(tmp_path, tracked=True)
    (root / "repo/assets/committed.txt").write_text("Dirty operator edit.\n")
    (root / "repo/assets/untracked.txt").write_text("Unapproved local file.\n")
    repo = tmp_path / "staged"
    result, baseline = stage(job, root, repo)
    assert _git(repo, Deadline(10), "rev-parse", f"{baseline}^{{tree}}").decode() == result.source_tree
    assert (repo / "assets/committed.txt").read_text() == "Committed evidence.\n"
    assert not (repo / "assets/untracked.txt").exists()


def test_ignore_rules_cannot_hide_unapproved_candidate_files(tmp_path: Path) -> None:
    job, root = pinned_ignored_fixture(tmp_path, tracked=False)
    repo = tmp_path / "staged"
    _, baseline = stage(job, root, repo)
    (repo / "calculator.py").write_text("def add(a, b):\n    return a+b\n")
    (repo / "assets").mkdir()
    (repo / "assets/unapproved.txt").write_text("Outside approved paths.\n")
    with pytest.raises(StageError, match="outside_scope"):
        candidate(job, repo, baseline, Deadline(10))


@pytest.mark.parametrize("phase", ["before", "after", "provider"])
def test_owner_failure_is_receipted_without_replay(tmp_path: Path, phase: str) -> None:
    job, root, spool = job_at(tmp_path)
    launches: list[str] = []

    def provider(manifest: RepositoryCodeManifest, repo: Path, deadline: Deadline) -> CliResult:
        launches.append("started")
        if phase == "provider":
            raise ExecutionError("fixture_provider_failed")
        return fake_provider(manifest, repo, deadline)

    def checks(
        repo: Path, deadline: Deadline, limit: int, name: str, **kwargs: object
    ) -> tuple[dict[str, object], bytes]:
        if name == phase:
            raise ExecutionError("fixture_test_container_failed")
        return fake_test(repo, deadline, limit, name, **kwargs)

    path = spool / "jobs" / f"{job.attempt_id}.json"
    receipt = process_job(path, spool, root, provider, test_runner=checks)
    assert receipt is not None
    assert receipt["status"] == ("unknown" if phase == "provider" else "failed")
    assert receipt["reason_code"] == (
        "fixture_provider_failed" if phase == "provider" else "fixture_test_container_failed"
    )
    assert len(launches) == (0 if phase == "before" else 1)
    assert process_job(path, spool, root, provider, test_runner=checks) is None
    assert len(launches) == (0 if phase == "before" else 1)


def test_fixture_creation_uses_the_configured_job_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runner import docker_egress_proof as docker

    monkeypatch.setattr(docker, "_inspect", lambda *_args: {})
    deadline = Deadline(90)
    monkeypatch.setattr(deadline, "remaining", lambda: 90.0)

    def create(*_args: str, **kwargs: object) -> str:
        assert kwargs.get("timeout") == 90
        raise docker.ProofError("controlled_container_failure")

    monkeypatch.setattr(docker, "_docker", create)
    with pytest.raises(ExecutionError, match="fixture_test_container_failed"):
        run_isolated_fixture_test(tmp_path, deadline, 4096, "before", image_id="sha256:" + "a" * 64)
