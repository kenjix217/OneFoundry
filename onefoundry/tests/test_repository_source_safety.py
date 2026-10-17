"""Complete approved source observations use their configured file bound."""

import json
from pathlib import Path
from typing import Any

import pytest

from onefoundry.repository_code_contract import RepositoryCodeManifest, digest
from onefoundry.repository_source_safety import SourceSafety
from runner.fixture_git import TRACE_OBSERVER, Deadline, _git
from runner.repository_code_service import process_job
from tests.test_repository_code import fake_provider, fake_test, job_at


@pytest.mark.parametrize("padding", ["\n\n", "# approved fixture text\n" * 4000], ids=["whitespace", "large"])
def test_complete_source_evidence_preserves_large_files_and_whitespace(tmp_path: Path, padding: str) -> None:
    job, root, spool = job_at(tmp_path)
    repo = root / "repo"
    before = padding + "def add(a, b):\n    return a-b\n\n"
    (repo / "calculator.py").write_text(before)
    deadline = Deadline(30)
    _git(repo, deadline, "add", "--all")
    _git(repo, deadline, "commit", "-qm", "Larger approved source")
    revision = _git(repo, deadline, "rev-parse", "HEAD").decode()
    value = job.model_dump(mode="json", exclude={"request_sha256"})
    value["source"]["revision"] = value["target"]["revision"] = revision
    value["stage_limits"]["max_context_file_bytes"] = 131072
    value["target"]["execution_policy"]["max_context_file_bytes"] = 131072
    job = RepositoryCodeManifest.model_validate({**value, "request_sha256": digest(value)})
    path = spool / "jobs" / f"{job.attempt_id}.json"
    path.write_text(job.model_dump_json())
    trace: list[tuple[str, int]] = []

    def provider(*args: Any) -> Any:
        observed = fake_provider(*args)
        (args[1] / "calculator.py").write_text(before.replace("return a-b", "return a+b"))
        return observed

    def observe_command(
        command: tuple[str, ...],
        _started: str,
        _code: int | None,
        stdout: bytes,
        _stderr: bytes,
        _outcome: str,
    ) -> None:
        trace.append((command[3], len(stdout)))

    token = TRACE_OBSERVER.set(observe_command)
    try:
        receipt = process_job(path, spool, root, provider, test_runner=fake_test)
    finally:
        TRACE_OBSERVER.reset(token)
    assert receipt is not None
    assert receipt["status"] == "done", (receipt["reason_code"], trace[-3:])
    patch = next(item for item in receipt["artifacts"] if item["kind"] == "diff")
    assert patch["byte_size"] < 1024
    safety = SourceSafety.model_validate_json(
        (spool / "claims" / f"{job.attempt_id}.source-safety.json").read_bytes()
    )
    assert safety.changes[0].before == before
    assert safety.changes[0].after == before.replace("return a-b", "return a+b")
    assert json.loads(path.read_text())["request_sha256"] == job.request_sha256


def test_source_observation_enforces_its_exact_file_bound(tmp_path: Path) -> None:
    from onefoundry.repository_source_safety import _source_text
    from runner.fixture_git import ExecutionError
    from runner.local_source_stage import StageError

    _job, root, _spool = job_at(tmp_path)
    repo = root / "repo"
    deadline = Deadline(30)
    body = (repo / "calculator.py").read_bytes()
    assert _source_text(repo, "HEAD", "calculator.py", len(body), deadline).encode() == body
    with pytest.raises(StageError, match="maintenance_source_observation_file_limit"):
        _source_text(repo, "HEAD", "calculator.py", len(body) - 1, deadline)
    with pytest.raises(ExecutionError, match="fixture_command_failed"):
        _source_text(repo, "HEAD", "missing.py", 4096, deadline)
    (repo / "empty.txt").write_bytes(b"")
    _git(repo, deadline, "add", "--all")
    _git(repo, deadline, "commit", "-qm", "Empty approved file")
    assert _source_text(repo, "HEAD", "empty.txt", 1, deadline) == ""


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_command_output_limit_rejects_invalid_values_before_launch(tmp_path: Path, limit: Any) -> None:
    from runner.fixture_git import _run

    with pytest.raises(ValueError, match="positive integer"):
        _run(("not-an-executable",), tmp_path, Deadline(1), output_limit=limit)


def test_default_command_bound_is_unchanged(tmp_path: Path) -> None:
    import sys

    from runner.fixture_git import MAX_COMMAND_OUTPUT_BYTES, ExecutionError, _run

    command = (sys.executable, "-c", f"print('x' * {MAX_COMMAND_OUTPUT_BYTES})")
    with pytest.raises(ExecutionError, match="fixture_command_output_limit"):
        _run(command, tmp_path, Deadline(5))
    value = _run(command, tmp_path, Deadline(5), output_limit=MAX_COMMAND_OUTPUT_BYTES + 1)
    assert len(value.stdout) == MAX_COMMAND_OUTPUT_BYTES + 1
