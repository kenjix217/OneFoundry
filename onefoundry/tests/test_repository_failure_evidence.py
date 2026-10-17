"""Failed behavior checks retain reviewable evidence, never a successful build."""

import hashlib
import os
from pathlib import Path
from typing import Any

import pytest

from onefoundry.execution_gateway import ExecutionGateway
from onefoundry.external_code_schemas import ExternalCodeReceipt
from runner.repository_check_evidence import check_log
from runner.repository_code_service import process_job
from tests.test_repository_code import fake_provider, job_at


def test_failed_checks_retain_exact_patch_and_sanitized_assertions(tmp_path: Path) -> None:
    job, root, spool = job_at(tmp_path)

    def failed(*args: Any, **kwargs: Any) -> tuple[dict[str, Any], bytes]:
        return {"name": "observed", "status": "failed", "exit_code": 1}, (
            b"FAILED tests/test_calculator.py::test_add\nAssertionError: 4 != 5\n"
            b"password = private-fixture-value\n"
            b"database_url=postgresql://fixture:private-value@invalid/db\n"
        )

    value = process_job(
        spool / "jobs" / f"{job.attempt_id}.json", spool, root, fake_provider, test_runner=failed
    )
    assert value is not None and value["status"] == "failed"
    assert value["reason_code"] == "maintenance_candidate_checks_failed"
    assert value["commit"] and value["diff_sha256"]
    artifacts = {item["kind"]: item for item in value["artifacts"]}
    assert set(artifacts) == {"diff", "log"}
    for item in artifacts.values():
        raw = (spool / item["path"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == item["sha256"]
        assert len(raw) == item["byte_size"]
    log = (spool / artifacts["log"]["path"]).read_text()
    assert "candidate_0" in log and "AssertionError: 4 != 5" in log
    assert "private-fixture-value" not in log and "private-value" not in log
    receipt = ExternalCodeReceipt.model_validate(value)
    attempt = {
        "id": job.attempt_id,
        "adapter_id": job.adapter_id,
        "request_sha256": job.request_sha256,
        "fixture_id": "repository_code_v1",
        "source_id": job.target.source_id,
        "source_root_id": job.source["root_id"],
        "source_relative_path": job.source["relative_path"],
        "source_branch": job.source["branch"],
        "source_revision": job.source["revision"],
        "idempotency_key": job.idempotency_key,
        "intent_sha256": job.intent_sha256,
    }
    ExecutionGateway._validate_receipt(job.project_id, job.run_id, attempt, receipt)
    mutations: tuple[dict[str, Any], ...] = (
        {"status": "done"},
        {"status": "unknown"},
        {"reason_code": "maintenance_candidate_outside_scope"},
        {"provider_session_id": None},
        {"tests": []},
        {"commit": None},
    )
    for updates in mutations:
        with pytest.raises(ValueError):
            ExecutionGateway._validate_receipt(
                job.project_id, job.run_id, attempt, ExternalCodeReceipt.model_validate({**value, **updates})
            )


def test_uncertain_check_does_not_publish_candidate(tmp_path: Path) -> None:
    job, root, spool = job_at(tmp_path)

    def uncertain(
        repo: Path, deadline: Any, limit: int, name: str, **kwargs: Any
    ) -> tuple[dict[str, Any], bytes]:
        if name == "after":
            raise TimeoutError("private failure detail")
        return {"name": name, "status": "failed", "exit_code": 1}, b"known baseline failure"

    result = process_job(
        spool / "jobs" / f"{job.attempt_id}.json", spool, root, fake_provider, test_runner=uncertain
    )
    assert result is not None and result["status"] != "done"
    assert result["artifacts"] == [] and result["commit"] is None


def test_log_publication_failure_does_not_accept_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_repository_code import fake_test

    job, root, spool = job_at(tmp_path)

    def fail(*args: Any) -> None:
        raise OSError("private filesystem value")

    monkeypatch.setattr("runner.repository_code_service.publish_replace", fail)
    result = process_job(
        spool / "jobs" / f"{job.attempt_id}.json", spool, root, fake_provider, test_runner=fake_test
    )
    assert result is not None and result["status"] != "done"
    assert result["commit"] is None and result["artifacts"] == []


@pytest.mark.parametrize(
    "raw",
    [
        b"\xffinvalid",
        b"-----BEGIN PRIVATE KEY-----\nprotected body\n-----END PRIVATE KEY-----",
        b"pass\x00word=hidden-value",
    ],
)
def test_invalid_or_protected_log_is_withheld(raw: bytes) -> None:
    result = check_log([("candidate_0", "failed", raw)], b"", 512)
    assert b"withheld" in result
    assert b"protected body" not in result and b"hidden-value" not in result and b"\xff" not in result


def test_log_empty_truncated_and_failure_priority() -> None:
    assert check_log([("candidate_0", "passed", b"")], b"", 512) == b"candidate_0: passed\n"
    result = check_log(
        [
            ("candidate_0", "passed", b"x" * 480),
            ("candidate_1", "failed", "AssertionError: é\n".encode() * 20),
        ],
        b"",
        512,
    )
    assert len(result) <= 512 and result.startswith(b"candidate_1: failed")
    assert "configured log bound" in result.decode()
    with pytest.raises(ValueError, match="exceeds_bound"):
        check_log([("candidate_0", "failed", b"x" * 513)], b"", 512)


@pytest.mark.integration
def test_real_isolated_failed_assertion_is_retained(tmp_path: Path) -> None:
    image = os.environ.get("ONEFOUNDRY_REPOSITORY_QA_TEST_IMAGE")
    if image is None:
        pytest.fail("ONEFOUNDRY_REPOSITORY_QA_TEST_IMAGE required")
    job, root, spool = job_at(tmp_path, image=image)

    def still_wrong(*args: Any) -> Any:
        result = fake_provider(*args)
        (args[1] / "calculator.py").write_text("def add(a, b):\n    return a*b\n")
        return result

    value = process_job(spool / "jobs" / f"{job.attempt_id}.json", spool, root, still_wrong)
    assert value is not None and value["status"] == "failed"
    assert value["reason_code"] == "maintenance_candidate_checks_failed"
    log = next(item for item in value["artifacts"] if item["kind"] == "log")
    assert "AssertionError: 6 != 5" in (spool / log["path"]).read_text()
