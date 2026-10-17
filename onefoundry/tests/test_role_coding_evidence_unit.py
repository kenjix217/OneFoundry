"""Contract tests for the bounded role-facing coding evidence projection."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, text

from onefoundry.role_coding_evidence import (
    CodingAttempt,
    coding_evidence,
    parse_coding_evidence,
    validate_coding_evidence,
)
from onefoundry.role_tool_admission import RunContext


@pytest.mark.parametrize("target,bound", [("engineer_qa_v1", 8), ("repository_code_v1", 16)])
def test_projection_keeps_each_receipt_contract_bound(target: str, bound: int) -> None:
    value: dict[str, Any] = {
        "attempt_id": str(uuid4()),
        "adapter_id": "codex_cli",
        "target_id": target,
        "status": "failed",
        "reason_code": "maintenance_candidate_checks_failed",
        "source_kind": "local",
        "source_id": str(uuid4()),
        "source_branch": "main",
        "source_revision": "a" * 40,
        "source_tree": None,
        "baseline_commit": None,
        "candidate_commit": None,
        "diff_sha256": None,
        "receipt_sha256": "f" * 64,
        "receipt_event_verified": True,
        "verified_outcome": "failed",
        "tests_source": "reconciled_runner_receipt",
        "independent_qa_verified": False,
        "live_isolation_proven": False,
        "artifacts": [],
        "tests": [{"name": f"check_{i}", "status": "failed", "exit_code": 1} for i in range(bound)],
    }
    CodingAttempt.model_validate(value)
    with pytest.raises(ValidationError):
        CodingAttempt.model_validate(
            {**value, "tests": [*value["tests"], {"name": "extra", "status": "failed", "exit_code": 1}]}
        )


def _context(project_id: str, *, max_items: int = 10) -> RunContext:
    return RunContext(
        project_id=UUID(project_id),
        run_id=uuid4(),
        work_order_id=uuid4(),
        profile_id=uuid4(),
        snapshot_tools=frozenset(),
        current_tools=frozenset(),
        snapshot_memory_scope="project",
        current_memory_scope="project",
        max_items=max_items,
        current_role_name="QA",
        current_role_kind="builtin",
        snapshot_role_name="QA",
    )


def test_arguments_require_exact_uuid() -> None:
    run_id = uuid4()
    assert parse_coding_evidence({"run_id": str(run_id)}) == {"run_id": str(run_id)}
    for invalid in ({}, {"run_id": "bad"}, {"run_id": str(run_id), "path": "/secret"}):
        with pytest.raises(ValidationError):
            parse_coding_evidence(invalid)


def test_projection_is_scoped_bounded_and_excludes_private_fields() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    project, other, run, manual, attempt = [str(uuid4()) for _ in range(5)]
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE runs (id TEXT, project_id TEXT, mode TEXT, status TEXT)"))
        connection.execute(
            text("""CREATE TABLE external_code_attempts (
            id TEXT, project_id TEXT, run_id TEXT, adapter_id TEXT, fixture_id TEXT, status TEXT,
            reason_code TEXT, source_id TEXT, source_branch TEXT, source_revision TEXT,
            snapshot_source_id TEXT, snapshot_branch TEXT, snapshot_revision TEXT, snapshot_tree TEXT,
            baseline_commit TEXT, "commit" TEXT, diff_sha256 TEXT, receipt_sha256 TEXT, tests TEXT,
            provider_session_id TEXT, snapshot_owner TEXT, snapshot_repo TEXT, cost_usd TEXT)""")
        )
        connection.execute(
            text("""CREATE TABLE external_code_artifacts (
            id TEXT, project_id TEXT, run_id TEXT, attempt_id TEXT, kind TEXT, sha256 TEXT,
            byte_size INTEGER, path TEXT)""")
        )
        connection.execute(
            text("CREATE TABLE events (project_id TEXT, run_id TEXT, event_type TEXT, payload TEXT)")
        )
        connection.execute(
            text("INSERT INTO runs VALUES (:id,:project,'external_code','done')"),
            {"id": run, "project": project},
        )
        connection.execute(
            text("INSERT INTO runs VALUES (:id,:project,'manual_planning','done')"),
            {"id": manual, "project": project},
        )
        connection.execute(
            text("INSERT INTO runs VALUES (:id,:project,'external_code','done')"),
            {"id": run, "project": other},
        )
        connection.execute(
            text("""INSERT INTO external_code_attempts VALUES (
            :id,:project,:run,'codex_cli','github_snapshot_codex_fixture_v1','done','completed',
            NULL,NULL,NULL,:source,'main',:revision,:tree,:baseline,:candidate,:diff,:receipt,:tests,
            'private-session','private-owner','private-repo','17.00')"""),
            {
                "id": attempt,
                "project": project,
                "run": run,
                "source": str(uuid4()),
                "revision": "a" * 40,
                "tree": "b" * 40,
                "baseline": "c" * 40,
                "candidate": "d" * 40,
                "diff": "e" * 64,
                "receipt": "f" * 64,
                "tests": json.dumps([{"name": "pytest", "status": "passed", "exit_code": 0}]),
            },
        )
        for index in range(3):
            connection.execute(
                text(
                    "INSERT INTO external_code_artifacts VALUES "
                    "(:id,:project,:run,:attempt,'log',:sha,10,:path)"
                ),
                {
                    "id": str(uuid4()),
                    "project": project,
                    "run": run,
                    "attempt": attempt,
                    "sha": "1" * 64,
                    "path": f"private/{index}",
                },
            )
        connection.execute(
            text("INSERT INTO events VALUES (:project,:run,'external_code_receipt',:payload)"),
            {
                "project": project,
                "run": run,
                "payload": json.dumps(
                    {
                        "attempt_id": attempt,
                        "adapter_id": "codex_cli",
                        "receipt_sha256": "f" * 64,
                        "status": "done",
                        "reason_code": "completed",
                        "diff_sha256": "e" * 64,
                        "tests": [{"name": "pytest", "status": "passed", "exit_code": 0}],
                    }
                ),
            },
        )
        result = coding_evidence(connection, _context(project), {"run_id": run})
        validate_coding_evidence(result)
        assert result["found"] is True and result["run_status"] == "done"
        assert result["has_more"] is False and len(result["attempts"]) == 1
        saved = result["attempts"][0]
        assert saved["source_kind"] == "synthetic_github_snapshot"
        assert saved["tests_source"] == "reconciled_runner_receipt"
        assert saved["receipt_event_verified"] is True
        assert saved["verified_outcome"] == "done"
        assert saved["independent_qa_verified"] is False
        assert saved["live_isolation_proven"] is False
        assert len(saved["artifacts"]) == 2
        assert saved["tests"] == [{"name": "pytest", "status": "passed", "exit_code": 0}]
        assert "private" not in json.dumps(result)
        assert coding_evidence(connection, _context(other), {"run_id": manual})["found"] is False
        assert coding_evidence(connection, _context(project), {"run_id": manual})["found"] is False
        assert coding_evidence(connection, _context(project), {"run_id": str(uuid4())})["found"] is False
        connection.execute(
            text("UPDATE events SET payload=:payload"),
            {
                "payload": json.dumps(
                    {
                        "attempt_id": attempt,
                        "adapter_id": "codex_cli",
                        "receipt_sha256": "f" * 64,
                        "status": "done",
                        "reason_code": "completed",
                        "diff_sha256": "0" * 64,
                        "tests": [{"name": "pytest", "status": "passed", "exit_code": 0}],
                    }
                )
            },
        )
        mismatched = coding_evidence(connection, _context(project), {"run_id": run})["attempts"][0]
        assert mismatched["tests_source"] == "none" and mismatched["tests"] == []
        assert mismatched["receipt_event_verified"] is False
        assert mismatched["status"] == "done"
        assert mismatched["verified_outcome"] is None
        assert mismatched["candidate_commit"] is None
        assert mismatched["diff_sha256"] is None
        assert mismatched["artifacts"] == []
        connection.execute(text("DELETE FROM events"))
        unproven = coding_evidence(connection, _context(project), {"run_id": run})["attempts"][0]
        assert unproven["tests_source"] == "none" and unproven["tests"] == []
        assert unproven["receipt_event_verified"] is False
        assert unproven["verified_outcome"] is None
        assert unproven["receipt_sha256"] is None
        for _ in range(4):
            connection.execute(
                text("""INSERT INTO external_code_attempts VALUES (
                :id,:project,:run,'claude_code','engineer_qa_v1','queued',NULL,
                NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'[]',
                'private-session','private-owner','private-repo','17.00')"""),
                {"id": str(uuid4()), "project": project, "run": run},
            )
        bounded = coding_evidence(connection, _context(project, max_items=2), {"run_id": run})
        assert len(bounded["attempts"]) == 2 and bounded["has_more"] is True


def test_validator_rejects_extra_data_and_unproved_flags() -> None:
    base = {"run_id": str(uuid4()), "found": False, "run_status": None, "attempts": [], "has_more": False}
    validate_coding_evidence(base)
    with pytest.raises(ValidationError):
        validate_coding_evidence({**base, "path": "/private"})
    with pytest.raises(ValidationError):
        validate_coding_evidence(
            {**base, "found": True, "run_status": "done", "attempts": [{"independent_qa_verified": True}]}
        )
