"""Real stopped worker receipts gate explicit abandonment without rewriting history."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.repository_code_abandon import RepositoryAbandonment, abandon
from onefoundry.repository_code_contract import RepositoryCodeTarget, RepositoryRunnerCapability
from onefoundry.self_extension_dispatch import step as dispatch
from onefoundry.self_extension_resume import require_resumed
from onefoundry.task_session_attempts import TaskSessionAttempts
from onefoundry.task_session_runs import bind
from onefoundry.task_session_tools import TaskSessionTools
from onefoundry.task_session_uncertainty import unresolved
from onefoundry.workflow_schemas import ApprovalDecision
from runner.repository_code_service import process_job
from tests.test_capability_setup_postgres import setup_task as setup_task
from tests.test_self_extension_postgres import payload
from tests.test_team_workflow_postgres import ORIGIN


@pytest.fixture
def uncertain(setup_task: tuple[Any, ...]) -> tuple[Any, ...]:
    client, gateway, project, profiles, tools, claimed, request, repo = setup_task
    pid = UUID(project)
    base = f"/api/projects/{project}"
    chief = next(p for p in client.get(base + "/profiles").json() if p["id"] == profiles["Chief of Staff"])
    response = client.patch(
        base + f"/profiles/{profiles['Engineer']}",
        json={
            "model": chief["model"],
            "model_account_id": chief["model_account_id"],
            "autonomy_level": "supervised",
        },
        headers=ORIGIN,
    )
    assert response.status_code == 200
    target = RepositoryCodeTarget(
        source_id=UUID(request["source_id"]),
        task_scope_id=claimed["work_order_id"],
        revision=request["revision"],
        adapter_id="codex_cli",
        provider_url="https://api.openai.com/v1/responses",
        allowed_paths=["README.md"],
        test_image="sha256:" + "a" * 64,
        test_commands=[["python", "-c", "assert False"]],
    )
    approval = payload(
        tools.invoke("onefoundry.propose_maintenance_target", target.model_dump(mode="json"), "target")
    )["approval_id"]
    gateway.decide_approval(pid, UUID(approval), ApprovalDecision(decision="approved"))
    request = payload(
        tools.invoke(
            "onefoundry.request_extension",
            {
                "operation": "tool:fixture_missing_v1",
                "target_approval_id": approval,
                "objective": "Implement the approved fixture.",
                "acceptance_check": "Checks pass.",
                "checkpoint": "Continue original task.",
            },
            "request",
        )
    )
    attempts = TaskSessionAttempts(gateway)
    attempts.settle(pid, claimed["id"], "done", public_reply="Engineer assigned.")
    child = attempts.claim_next(pid)
    assert child is not None
    bind(gateway, pid, child)
    spool = gateway._execution_gateway().spool.root
    for name in ("jobs", "claims", "receipts", "artifacts"):
        (spool / name).mkdir(parents=True, exist_ok=True)
    report = RepositoryRunnerCapability(
        observed_at=datetime.now(UTC),
        isolated=True,
        provider_only_egress=True,
        credentials_separate=True,
        receipts_separate=True,
        source_root_ready=True,
        adapter_id="codex_cli",
        model_id=chief["model"],
        provider_url=target.provider_url,
        image_id=target.test_image,
        proof_sha256="f" * 64,
    )
    (spool / "repository_capabilities.json").write_text(report.model_dump_json())
    engineering = TaskSessionTools(gateway, pid, child["session_id"], child["id"])
    payload(
        engineering.invoke(
            "onefoundry.build_extension", {"target_approval_id": approval, "adapter_id": "codex_cli"}, "build"
        )
    )
    attempts.settle(pid, child["id"], "done", public_reply="Waiting for isolated worker.")
    assert dispatch(gateway)
    manifest = next((spool / "jobs").glob("*.json"))
    job = json.loads(manifest.read_text())

    def unknown(*args: Any) -> Any:
        raise TimeoutError("fixture uncertain provider")

    def check(*args: Any, **kwargs: Any) -> tuple[dict[str, Any], bytes]:
        return {"name": "baseline", "status": "failed", "exit_code": 1}, b"fixture failure"

    receipt = process_job(manifest, spool, repo.parent, unknown, test_runner=check)
    assert receipt is not None and receipt["status"] == "unknown"
    detail = gateway._execution_gateway().reconcile(pid, UUID(job["run_id"]))
    attempt = detail["attempts"][-1]
    data = RepositoryAbandonment(
        client_action_id=uuid4(),
        decision="abandon_unverified",
        reason="Worker stopped; disposable changes discarded; no candidate accepted.",
        request_sha256=attempt["request_sha256"],
        receipt_sha256=attempt["receipt_sha256"],
        disposable_work_discarded=True,
    )
    return client, gateway, pid, UUID(job["run_id"]), UUID(job["attempt_id"]), data, claimed, spool


def test_abandon_is_scoped_exact_once_and_keeps_unknown_receipt(uncertain: tuple[Any, ...]) -> None:
    client, gateway, pid, run, attempt, data, original, spool = uncertain
    receipt = (spool / "receipts" / f"{attempt}.json").read_bytes()
    with gateway.engine.connect() as c:
        assert unresolved(c, pid, root_id=original["work_order_id"])
        with pytest.raises(Conflict):
            require_resumed(c, pid, original["work_order_id"])
    with pytest.raises(NotFound):
        abandon(gateway, uuid4(), run, attempt, data)
    with pytest.raises(Conflict):
        abandon(gateway, pid, run, attempt, data.model_copy(update={"receipt_sha256": "0" * 64}))
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(lambda _: abandon(gateway, pid, run, attempt, data), range(2)))
    assert rows[0] == rows[1] and rows[0]["status"] == "unknown"
    with gateway.engine.connect() as c:
        assert not unresolved(c, pid, root_id=original["work_order_id"])
        with pytest.raises(Conflict):
            require_resumed(c, pid, original["work_order_id"])
        assert (
            c.scalar(
                text("SELECT status FROM external_code_attempts WHERE project_id=:p AND id=:a"),
                {"p": pid, "a": attempt},
            )
            == "unknown"
        )
        assert (
            c.scalar(
                text(
                    "SELECT count(*) FROM events WHERE project_id=:p "
                    "AND event_type='repository_code_abandoned'"
                ),
                {"p": pid},
            )
            == 1
        )
    assert (spool / "receipts" / f"{attempt}.json").read_bytes() == receipt
    assert len(list((spool / "jobs").glob("*.json"))) == 1
    response = client.post(
        f"/api/projects/{pid}/runs/{run}/repository-code/{attempt}/abandon",
        json=data.model_dump(mode="json"),
        headers=ORIGIN,
    )
    assert response.status_code == 200
    with pytest.raises(Conflict):
        abandon(
            gateway,
            pid,
            run,
            attempt,
            data.model_copy(update={"reason": "A different decision cannot reuse the same acknowledgement."}),
        )


def test_missing_receipt_and_agent_actor_cannot_abandon(uncertain: tuple[Any, ...]) -> None:
    _client, gateway, pid, run, attempt, data, _original, spool = uncertain
    actor = gateway.actor_id
    gateway.actor_id = "profile:" + str(uuid4())
    with pytest.raises(Conflict):
        abandon(gateway, pid, run, attempt, data)

    gateway.actor_id = actor
    (spool / "receipts" / f"{attempt}.json").unlink()
    with pytest.raises(Conflict):
        abandon(gateway, pid, run, attempt, data)


def test_abandonment_blocks_assignment_and_rejects_malformed_decision(uncertain: tuple[Any, ...]) -> None:
    from onefoundry.task_session_assignments import TaskSessionAssignments

    client, gateway, pid, run, attempt, data, original, _spool = uncertain
    owner = TaskSessionAssignments(gateway)
    assert not owner.block_failed_coding(pid, attempt)
    response = client.post(
        f"/api/projects/{pid}/runs/{run}/repository-code/{attempt}/abandon",
        json={**data.model_dump(mode="json"), "disposable_work_discarded": False},
        headers=ORIGIN,
    )
    assert response.status_code == 422
    abandon(gateway, pid, run, attempt, data)
    assert owner.block_failed_coding(pid, attempt)
    assert not owner.block_failed_coding(pid, attempt)
    with gateway.engine.connect() as connection:
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM task_session_assignments WHERE project_id=:p "
                    "AND root_work_order_id=:root AND status='blocked'"
                ),
                {"p": pid, "root": original["work_order_id"]},
            )
            == 1
        )
