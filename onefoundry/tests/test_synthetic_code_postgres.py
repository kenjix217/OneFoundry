"""Real PostgreSQL owner-path checks for offline Engineer and QA jobs."""

import hashlib
import json
import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from onefoundry.main import create_app
from onefoundry.settings import Settings

BASELINE = "a" * 40
CANDIDATE = "b" * 40
MERGED = CANDIDATE


@pytest.fixture
def spool(tmp_path: Path) -> Path:
    for name in ("jobs", "claims", "receipts", "artifacts"):
        (tmp_path / name).mkdir()
    return tmp_path


@pytest.fixture
def client(spool: Path) -> Iterator[TestClient]:
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        runner_spool_root=str(spool),
    )
    with TestClient(create_app(settings), base_url="http://localhost:8765") as value:
        yield value


def post(client: TestClient, path: str, body: dict[str, Any] | None = None) -> Any:
    return client.post(path, json=body, headers={"origin": "http://localhost:8765"})


def create_ready(
    client: TestClient, name: str = "Code", deliverable_kind: str = "operator_review"
) -> tuple[str, str]:
    project = post(client, "/api/projects", {"name": name}).json()
    project_id = cast(str, project["id"])
    profiles = client.get(f"/api/projects/{project_id}/profiles").json()
    engineer = next(item for item in profiles if item["name"] == "Engineer")
    order = post(
        client,
        f"/api/projects/{project_id}/work-orders",
        {
            "title": "Repair synthetic fixture",
            "instructions": "Use offline fixture only",
            "profile_id": engineer["id"],
            "deliverable_kind": deliverable_kind,
        },
    ).json()
    order_id = cast(str, order["id"])
    assert post(client, f"/api/projects/{project_id}/work-orders/{order_id}/ready").status_code == 200
    return project_id, order_id


def receipt(
    spool: Path,
    project_id: str,
    run_id: str,
    job: dict[str, Any],
    *,
    status: str = "done",
    bad_hash: bool = False,
    merged_commit_override: str | None = None,
) -> None:
    job_id = cast(str, job["id"])
    stage = cast(str, job["stage"])
    source_job_id = job["source_job_id"] if stage != "engineer" else job_id
    branch = f"fixture/repair-{source_job_id}"
    content = b"synthetic repair\n"
    directory = spool / "artifacts" / job_id
    directory.mkdir()
    digest = hashlib.sha256(content).hexdigest()
    names = {
        "engineer": [
            ("before_log", "before.log"),
            ("after_log", "after.log"),
            ("diff", "change.diff"),
            ("bundle", "repair.bundle"),
        ],
        "qa": [("qa_log", "qa.log")],
        "merge": [("merge_log", "merge.log")],
    }[stage]
    artifacts = []
    for kind, artifact_name in names:
        (directory / artifact_name).write_bytes(content)
        artifacts.append(
            {
                "kind": kind,
                "path": f"artifacts/{job_id}/{artifact_name}",
                "sha256": "0" * 64 if bad_hash and kind == names[0][0] else digest,
                "byte_size": len(content),
            }
        )
    tests = {
        "engineer": [
            {"name": "before_repair", "status": "failed", "exit_code": 1},
            {"name": "after_repair", "status": "passed", "exit_code": 0},
        ],
        "qa": [{"name": "qa_fixture_test", "status": "passed", "exit_code": 0}],
        "merge": [{"name": "merged_fixture_test", "status": "passed", "exit_code": 0}],
    }[stage]
    payload = {
        "schema_version": 1,
        "job_id": job_id,
        "project_id": project_id,
        "run_id": run_id,
        "fixture_id": "engineer_qa_v1",
        "stage": stage,
        "status": status,
        "reason_code": "fixture_complete" if status == "done" else "fixture_failed",
        "started_at": "2026-09-25T10:00:00Z",
        "finished_at": "2026-09-25T10:00:01Z",
        "elapsed_ms": 1000,
        "branch": branch,
        "commit": CANDIDATE,
        "baseline_commit": BASELINE,
        "merged_commit": (merged_commit_override or MERGED) if stage == "merge" else None,
        "observed_head_commit": (merged_commit_override or MERGED) if stage == "merge" else None,
        "diff_sha256": digest,
        "tests": tests,
        "artifacts": artifacts,
        "tokens_used": 0,
        "cost_usd": 0,
    }
    if status != "done":
        payload.update(
            {
                "branch": None,
                "commit": None,
                "baseline_commit": None,
                "diff_sha256": None,
                "tests": [],
                "artifacts": [],
            }
        )
    (spool / "receipts" / f"{job_id}.json").write_text(json.dumps(payload))


def test_engineer_qa_merge_approval_and_readback(client: TestClient, spool: Path) -> None:
    project_id, order_id = create_ready(client)
    root = f"/api/projects/{project_id}"
    eligible = client.get(f"{root}/work-orders/{order_id}/synthetic-code/eligibility")
    assert eligible.status_code == 200 and eligible.json()["eligible"] is True
    launched = post(
        client, f"{root}/work-orders/{order_id}/synthetic-code/runs", {"fixture_id": "engineer_qa_v1"}
    )
    assert launched.status_code == 201, launched.text
    run_id = launched.json()["run"]["id"]
    engineer = launched.json()["jobs"][0]
    manifest = json.loads((spool / "jobs" / f"{engineer['id']}.json").read_text())
    assert manifest["stage"] == "engineer" and manifest["expected_branch"] is None
    assert (
        post(
            client, f"{root}/work-orders/{order_id}/synthetic-code/runs", {"fixture_id": "engineer_qa_v1"}
        ).status_code
        == 409
    )
    qa_id = next(item["id"] for item in client.get(f"{root}/profiles").json() if item["name"] == "QA")
    configured = client.patch(
        f"{root}/profiles/{qa_id}",
        json={"model": "qa-before", "instructions": "QA review before"},
        headers={"origin": "http://localhost:8765"},
    )
    assert configured.status_code == 200, configured.text
    receipt(spool, project_id, run_id, engineer)
    after_engineer = post(client, f"{root}/runs/{run_id}/synthetic-code/reconcile").json()
    assert [item["stage"] for item in after_engineer["jobs"]] == ["engineer", "qa"]
    assert after_engineer["run"]["status"] == "running"
    changed = client.patch(
        f"{root}/profiles/{qa_id}",
        json={"model": "qa-after", "instructions": "QA review after"},
        headers={"origin": "http://localhost:8765"},
    )
    assert changed.status_code == 200, changed.text
    snapshots = client.get(f"{root}/runs/{run_id}").json()["profile_snapshots"]
    assert [item["profile_name"] for item in snapshots] == ["Engineer", "QA"]
    assert snapshots[1]["profile_model"] == "qa-before"
    assert snapshots[1]["profile_instructions"] == "QA review before"
    preview_id = next(item["id"] for item in after_engineer["jobs"][0]["artifacts"] if item["kind"] == "diff")
    preview = client.get(f"{root}/runs/{run_id}/synthetic-code/artifacts/{preview_id}")
    assert preview.status_code == 200 and preview.json()["text"] == "synthetic repair\n"
    qa = after_engineer["jobs"][1]
    receipt(spool, project_id, run_id, qa)
    passed = post(client, f"{root}/runs/{run_id}/synthetic-code/reconcile").json()
    assert passed["run"]["status"] == "done"
    approval = post(client, f"{root}/runs/{run_id}/synthetic-code/merge-approval")
    assert approval.status_code == 201 and approval.json()["synthetic_merge"] is True
    approval_id = approval.json()["id"]
    assert (
        approval_id
        == client.get(f"{root}/runs/{run_id}/synthetic-code").json()["synthetic_merge_approval_id"]
    )
    assert client.get(f"{root}/approvals").json()[0]["synthetic_merge"] is True
    decided = post(client, f"{root}/approvals/{approval_id}/decision", {"decision": "approved"})
    assert decided.status_code == 200 and decided.json()["synthetic_merge"] is True
    merging = client.get(f"{root}/runs/{run_id}/synthetic-code").json()
    merge = merging["jobs"][-1]
    assert merge["stage"] == "merge" and merge["status"] == "queued"
    merge_manifest = json.loads((spool / "jobs" / f"{merge['id']}.json").read_text())
    assert merge_manifest["approval_id"] == approval_id
    assert merge_manifest["expected_baseline_commit"] == BASELINE
    receipt(spool, project_id, run_id, merge)
    merged = post(client, f"{root}/runs/{run_id}/synthetic-code/reconcile").json()
    assert merged["jobs"][-1]["status"] == "done"
    assert merged["jobs"][-1]["merged_commit"] == MERGED
    assert post(client, f"{root}/runs/{run_id}/synthetic-code/reconcile").json() == merged


def test_declared_code_change_requires_passing_qa_receipt(client: TestClient, spool: Path) -> None:
    project_id, order_id = create_ready(client, "Verified code", "code_change")
    root = f"/api/projects/{project_id}"
    launched = post(
        client, f"{root}/work-orders/{order_id}/synthetic-code/runs", {"fixture_id": "engineer_qa_v1"}
    ).json()
    run_id = launched["run"]["id"]
    receipt(spool, project_id, run_id, launched["jobs"][0])
    qa = post(client, f"{root}/runs/{run_id}/synthetic-code/reconcile").json()["jobs"][1]
    assert client.get(f"{root}/work-orders").json()[0]["status"] == "running"
    receipt(spool, project_id, run_id, qa)
    completed = post(client, f"{root}/runs/{run_id}/synthetic-code/reconcile")
    assert completed.status_code == 200, completed.text
    assert completed.json()["run"]["status"] == "done"
    assert client.get(f"{root}/work-orders").json()[0]["status"] == "done"


def test_failed_qa_keeps_code_work_order_failed(client: TestClient, spool: Path) -> None:
    project_id, order_id = create_ready(client, "Failed QA", "code_change")
    root = f"/api/projects/{project_id}"
    launched = post(
        client, f"{root}/work-orders/{order_id}/synthetic-code/runs", {"fixture_id": "engineer_qa_v1"}
    ).json()
    run_id = launched["run"]["id"]
    receipt(spool, project_id, run_id, launched["jobs"][0])
    qa = post(client, f"{root}/runs/{run_id}/synthetic-code/reconcile").json()["jobs"][1]
    receipt(spool, project_id, run_id, qa, status="failed")
    failed = post(client, f"{root}/runs/{run_id}/synthetic-code/reconcile")
    assert failed.status_code == 200, failed.text
    assert failed.json()["run"]["status"] == "failed"
    assert client.get(f"{root}/work-orders").json()[0]["status"] == "failed"


def test_failure_unknown_rejection_and_project_bounds(client: TestClient, spool: Path) -> None:
    first, order_id = create_ready(client, "First")
    second, _ = create_ready(client, "Second")
    root = f"/api/projects/{first}"
    assert (
        post(
            client, f"{root}/work-orders/{order_id}/synthetic-code/runs", {"fixture_id": "wrong"}
        ).status_code
        == 422
    )
    launched = post(
        client, f"{root}/work-orders/{order_id}/synthetic-code/runs", {"fixture_id": "engineer_qa_v1"}
    ).json()
    run_id = launched["run"]["id"]
    assert client.get(f"/api/projects/{second}/runs/{run_id}/synthetic-code").status_code == 404
    engineer = launched["jobs"][0]
    receipt(spool, first, run_id, engineer, bad_hash=True)
    unknown = post(client, f"{root}/runs/{run_id}/synthetic-code/reconcile").json()
    assert unknown["run"]["status"] == "unknown"
    assert unknown["jobs"][0]["status"] == "unknown"
    assert len(list((spool / "jobs").glob("*.json"))) == 1
    assert post(client, f"{root}/runs/{run_id}/synthetic-code/merge-approval").status_code == 409
    assert (
        client.get(f"/api/projects/{second}/runs/{run_id}/synthetic-code/artifacts/{uuid4()}").status_code
        == 404
    )


def test_rejected_merge_never_queues_job(client: TestClient, spool: Path) -> None:
    project_id, order_id = create_ready(client, "Reject")
    root = f"/api/projects/{project_id}"
    launched = post(
        client, f"{root}/work-orders/{order_id}/synthetic-code/runs", {"fixture_id": "engineer_qa_v1"}
    ).json()
    run_id = launched["run"]["id"]
    receipt(spool, project_id, run_id, launched["jobs"][0])
    qa = post(client, f"{root}/runs/{run_id}/synthetic-code/reconcile").json()["jobs"][1]
    receipt(spool, project_id, run_id, qa)
    post(client, f"{root}/runs/{run_id}/synthetic-code/reconcile")
    approval = post(client, f"{root}/runs/{run_id}/synthetic-code/merge-approval").json()
    decision = post(client, f"{root}/approvals/{approval['id']}/decision", {"decision": "rejected"})
    assert decision.status_code == 200
    assert [item["stage"] for item in client.get(f"{root}/runs/{run_id}/synthetic-code").json()["jobs"]] == [
        "engineer",
        "qa",
    ]


def test_stale_claim_without_receipt_becomes_unknown_without_retry(client: TestClient, spool: Path) -> None:
    project_id, order_id = create_ready(client, "Stale claim")
    root = f"/api/projects/{project_id}"
    launched = post(
        client, f"{root}/work-orders/{order_id}/synthetic-code/runs", {"fixture_id": "engineer_qa_v1"}
    ).json()
    run_id = launched["run"]["id"]
    job_id = launched["jobs"][0]["id"]
    assert (
        post(client, f"{root}/runs/{run_id}/synthetic-code/reconcile").json()["jobs"][0]["status"] == "queued"
    )
    claim = spool / "claims" / f"{job_id}.claim"
    claim.write_text(job_id)
    stale = time.time() - 1000
    os.utime(claim, (stale, stale))
    unknown = post(client, f"{root}/runs/{run_id}/synthetic-code/reconcile").json()
    assert unknown["run"]["status"] == "unknown"
    assert unknown["jobs"][0]["reason_code"] == "claimed_receipt_missing"
    assert post(client, f"{root}/runs/{run_id}/synthetic-code/reconcile").json() == unknown
    assert len(list((spool / "jobs").glob("*.json"))) == 1


def test_unclaimed_timeout_fences_runner_before_unknown(
    client: TestClient, spool: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, order_id = create_ready(client, "Unclaimed")
    root = f"/api/projects/{project_id}"
    launched = post(
        client, f"{root}/work-orders/{order_id}/synthetic-code/runs", {"fixture_id": "engineer_qa_v1"}
    ).json()
    run_id = launched["run"]["id"]
    job_id = launched["jobs"][0]["id"]
    real_now = datetime.now(UTC)

    class LaterDatetime(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> "LaterDatetime":
            target = real_now + timedelta(seconds=1000)
            return cls.fromtimestamp(target.timestamp(), tz or UTC)

    monkeypatch.setattr("onefoundry.synthetic_code_store.datetime", LaterDatetime)
    unknown = post(client, f"{root}/runs/{run_id}/synthetic-code/reconcile").json()
    assert unknown["run"]["status"] == "unknown"
    assert unknown["jobs"][0]["reason_code"] == "unclaimed_dispatch_expired"
    assert (spool / "claims" / f"{job_id}.claim").read_text() == job_id
    assert not (spool / "jobs" / f"{job_id}.json").exists()
    assert post(client, f"{root}/runs/{run_id}/synthetic-code/reconcile").json() == unknown


def test_merge_readback_must_equal_candidate_commit(client: TestClient, spool: Path) -> None:
    project_id, order_id = create_ready(client, "Bad merge readback")
    root = f"/api/projects/{project_id}"
    launched = post(
        client, f"{root}/work-orders/{order_id}/synthetic-code/runs", {"fixture_id": "engineer_qa_v1"}
    ).json()
    run_id = launched["run"]["id"]
    receipt(spool, project_id, run_id, launched["jobs"][0])
    qa = post(client, f"{root}/runs/{run_id}/synthetic-code/reconcile").json()["jobs"][1]
    receipt(spool, project_id, run_id, qa)
    post(client, f"{root}/runs/{run_id}/synthetic-code/reconcile")
    approval = post(client, f"{root}/runs/{run_id}/synthetic-code/merge-approval").json()
    post(client, f"{root}/approvals/{approval['id']}/decision", {"decision": "approved"})
    merge = client.get(f"{root}/runs/{run_id}/synthetic-code").json()["jobs"][-1]
    receipt(spool, project_id, run_id, merge, merged_commit_override="c" * 40)
    unknown = post(client, f"{root}/runs/{run_id}/synthetic-code/reconcile").json()
    assert unknown["run"]["status"] == "done"
    assert unknown["jobs"][-1]["status"] == "unknown"
    assert unknown["jobs"][-1]["reason_code"] == "receipt_invalid"


def test_unknown_merge_preserves_observed_head_without_claiming_success(
    client: TestClient, spool: Path
) -> None:
    project_id, order_id = create_ready(client, "Unknown merge head")
    root = f"/api/projects/{project_id}"
    launched = post(
        client, f"{root}/work-orders/{order_id}/synthetic-code/runs", {"fixture_id": "engineer_qa_v1"}
    ).json()
    run_id = launched["run"]["id"]
    receipt(spool, project_id, run_id, launched["jobs"][0])
    qa = post(client, f"{root}/runs/{run_id}/synthetic-code/reconcile").json()["jobs"][1]
    receipt(spool, project_id, run_id, qa)
    post(client, f"{root}/runs/{run_id}/synthetic-code/reconcile")
    approval = post(client, f"{root}/runs/{run_id}/synthetic-code/merge-approval").json()
    post(client, f"{root}/approvals/{approval['id']}/decision", {"decision": "approved"})
    merge = client.get(f"{root}/runs/{run_id}/synthetic-code").json()["jobs"][-1]
    receipt(spool, project_id, run_id, merge)
    path = spool / "receipts" / f"{merge['id']}.json"
    payload = json.loads(path.read_text())
    payload.update(
        {
            "status": "unknown",
            "reason_code": "merge_verification_incomplete",
            "branch": None,
            "commit": None,
            "baseline_commit": None,
            "merged_commit": None,
            "diff_sha256": None,
            "tests": [],
            "artifacts": [],
            "observed_head_commit": CANDIDATE,
        }
    )
    path.write_text(json.dumps(payload))
    result = post(client, f"{root}/runs/{run_id}/synthetic-code/reconcile").json()
    assert result["run"]["status"] == "done"
    assert result["jobs"][-1]["status"] == "unknown"
    assert result["jobs"][-1]["merged_commit"] is None
    assert result["jobs"][-1]["observed_head_commit"] == CANDIDATE
    events = client.get(f"{root}/runs/{run_id}").json()["events"]
    assert any(event["payload"].get("observed_head_commit") == CANDIDATE for event in events)


def test_database_rejects_cross_run_source_in_same_project(client: TestClient) -> None:
    project_id, first_order = create_ready(client, "Run provenance")
    root = f"/api/projects/{project_id}"
    first = post(
        client, f"{root}/work-orders/{first_order}/synthetic-code/runs", {"fixture_id": "engineer_qa_v1"}
    ).json()
    profiles = client.get(f"{root}/profiles").json()
    engineer = next(item for item in profiles if item["name"] == "Engineer")
    second_order = post(
        client,
        f"{root}/work-orders",
        {
            "title": "Second fixture",
            "instructions": "Separate run",
            "profile_id": engineer["id"],
        },
    ).json()["id"]
    post(client, f"{root}/work-orders/{second_order}/ready")
    second = post(
        client, f"{root}/work-orders/{second_order}/synthetic-code/runs", {"fixture_id": "engineer_qa_v1"}
    ).json()
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"), db_password=os.environ["APP_DB_PASSWORD"]
    )
    engine = create_engine(settings.database_url)
    try:
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO runner_jobs "
                        "(id,project_id,actor_id,run_id,stage,status,source_job_id) "
                        "VALUES (:id,:project_id,'operator',:run_id,'qa','queued',:source_job_id)"
                    ),
                    {
                        "id": uuid4(),
                        "project_id": UUID(project_id),
                        "run_id": UUID(second["run"]["id"]),
                        "source_job_id": UUID(first["jobs"][0]["id"]),
                    },
                )
    finally:
        engine.dispose()
