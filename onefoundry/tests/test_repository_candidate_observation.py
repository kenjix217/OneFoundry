"""A rejected candidate must retain the completed provider observation."""

from pathlib import Path
from typing import Any

import pytest

from runner.repository_code_service import process_job
from runner.repository_codex_bridge import RepositoryCodexBridge
from tests.test_repository_code import fake_test, job_at
from tests.test_repository_codex_observation import events
from tests.test_repository_provider import config_at


@pytest.mark.parametrize("effect", ["outside_scope", "protected_content", "io_failure"])
def test_candidate_rejection_preserves_provider_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, effect: str
) -> None:
    job, root, spool = job_at(tmp_path)
    config = config_at(tmp_path, job, root, spool, job.target.test_image, job.target.test_image)

    def boundary(self: Any, view: Path, *args: Any, **kwargs: Any) -> tuple[bytes, dict[str, bool]]:
        (view / "calculator.py").write_text("def add(a,b):\n    return a+b\n")
        if effect == "outside_scope":
            (view / "unexpected.py").write_text("unapproved = True\n")
        if effect == "protected_content":
            (view / "calculator.py").write_text('password = "private fixture value"\n')
        return events(), {}

    def fail_copy(*args: Any) -> None:
        raise OSError("private filesystem detail must not be retained")

    monkeypatch.setattr(RepositoryCodexBridge, "_boundary", boundary)
    if effect == "io_failure":
        monkeypatch.setattr("runner.repository_codex_bridge.copy_changes", fail_copy)
    receipt = process_job(
        spool / "jobs" / f"{job.attempt_id}.json",
        spool,
        root,
        RepositoryCodexBridge(config),
        test_runner=fake_test,
    )
    assert receipt is not None
    assert receipt["status"] == "failed"
    assert receipt["provider_session_id"] is not None
    assert receipt["reason_code"] == (
        "maintenance_candidate_copy_failed" if effect == "io_failure" else f"maintenance_candidate_{effect}"
    )
    assert receipt["artifacts"] == [] and receipt["commit"] is None
    saved = (spool / "claims" / f"{job.attempt_id}.repository-session.json").read_text()
    assert '"status": "done"' in saved and "private filesystem" not in saved


def test_rejected_candidate_observation_still_validates_session_identity(tmp_path: Path) -> None:
    from runner.external_cli import CliResult
    from runner.repository_candidate_observation import CandidateRejected

    job, root, spool = job_at(tmp_path)

    def invalid(*args: Any) -> Any:
        raise CandidateRejected(
            "maintenance_candidate_outside_scope",
            CliResult("done", "completed", "invalid private session value", None, None, b""),
        )

    receipt = process_job(
        spool / "jobs" / f"{job.attempt_id}.json", spool, root, invalid, test_runner=fake_test
    )
    assert receipt is not None
    assert receipt["status"] == "unknown"
    assert receipt["reason_code"] == "maintenance_provider_observation_invalid"
    assert receipt["provider_session_id"] is None
    saved = (spool / "claims" / f"{job.attempt_id}.repository-session.json").read_text()
    assert "invalid private session value" not in saved
