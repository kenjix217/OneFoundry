"""Manual workflow, project boundaries, events, and approval one-shot behavior."""

import hashlib
import json
import os
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import cast
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from onefoundry.main import create_app
from onefoundry.settings import Settings
from onefoundry.tool_gateway import Conflict, ToolGateway
from onefoundry.workflow_schemas import ApprovalDecision, WorkOrderCreate


@pytest.fixture
def settings() -> Settings:
    return Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url="http://localhost:8765") as test_client:
        yield test_client


def post(client: TestClient, path: str, body: Mapping[str, object] | None = None) -> Response:
    return cast(
        Response,
        client.post(path, json=body, headers={"origin": "http://localhost:8765"}),
    )


def project_and_profile(client: TestClient, name: str) -> tuple[str, str]:
    project = post(client, "/api/projects", {"name": name}).json()
    profiles = client.get(f"/api/projects/{project['id']}/profiles").json()
    engineer = next(profile for profile in profiles if profile["name"] == "Engineer")
    return project["id"], engineer["id"]


def create_running_work_order(client: TestClient, name: str) -> tuple[str, str, str, str]:
    project_id, profile_id = project_and_profile(client, name)
    order = post(
        client,
        f"/api/projects/{project_id}/work-orders",
        {"title": "Launch plan", "instructions": "Prepare draft", "profile_id": profile_id},
    ).json()
    order_id = order["id"]
    assert post(client, f"/api/projects/{project_id}/work-orders/{order_id}/ready").status_code == 200
    run = post(client, f"/api/projects/{project_id}/work-orders/{order_id}/runs").json()
    return project_id, order_id, run["id"], profile_id


def test_work_order_assignment_and_block_round_trip(client: TestClient, settings: Settings) -> None:
    project_id, engineer_id = project_and_profile(client, "Control round trip")
    root = f"/api/projects/{project_id}"
    qa_id = next(p["id"] for p in client.get(f"{root}/profiles").json() if p["name"] == "QA")
    order = post(
        client,
        f"{root}/work-orders",
        {
            "title": "Plan",
            "instructions": "Draft plan",
            "profile_id": engineer_id,
        },
    ).json()
    path = f"{root}/work-orders/{order['id']}"
    assert order["version"] == 1 and order["block_reason"] is None
    assigned = post(
        client,
        f"{path}/assign",
        {
            "profile_id": qa_id,
            "expected_version": order["version"],
        },
    )
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["profile_id"] == qa_id and assigned.json()["version"] == 2
    assert (
        post(
            client,
            f"{path}/assign",
            {
                "profile_id": engineer_id,
                "expected_version": 1,
            },
        ).status_code
        == 409
    )
    blocked = post(
        client,
        f"{path}/block",
        {
            "reason": "Waiting for operator input",
            "expected_version": 2,
        },
    )
    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["status"] == "blocked"
    assert blocked.json()["blocked_from_status"] == "draft"
    assert blocked.json()["block_reason"] == "Waiting for operator input"
    assert post(client, f"{path}/ready").status_code == 409
    assert post(client, f"{path}/runs").status_code == 409
    assert post(client, f"{path}/unblock", {"expected_version": 2}).status_code == 409
    restored = post(client, f"{path}/unblock", {"expected_version": 3})
    assert restored.status_code == 200, restored.text
    assert restored.json()["status"] == "draft"
    assert restored.json()["block_reason"] is None
    assert restored.json()["blocked_from_status"] is None
    assert restored.json()["version"] == 4
    ready = post(client, f"{path}/ready").json()
    assert ready["version"] == 5
    ready_block = post(
        client,
        f"{path}/block",
        {
            "reason": "Review",
            "expected_version": ready["version"],
        },
    ).json()
    assert ready_block["blocked_from_status"] == "ready"
    assert (
        post(
            client,
            f"{path}/unblock",
            {
                "expected_version": ready_block["version"],
            },
        ).json()["status"]
        == "ready"
    )
    run = post(client, f"{path}/runs").json()
    assert run["profile_id"] == qa_id
    current = next(o for o in client.get(f"{root}/work-orders").json() if o["id"] == order["id"])
    assert current["version"] == 8
    assert (
        post(
            client,
            f"{path}/assign",
            {
                "profile_id": engineer_id,
                "expected_version": current["version"],
            },
        ).status_code
        == 409
    )
    engine = create_engine(settings.database_url)
    with engine.connect() as connection:
        events = cast(
            list[str],
            connection.execute(
                text(
                    "SELECT event_type FROM events WHERE project_id=:project_id AND work_order_id=:order_id "
                    "AND event_type IN ('work_order_assigned','work_order_blocked','work_order_unblocked') "
                    "ORDER BY created_at"
                ),
                {"project_id": UUID(project_id), "order_id": UUID(order["id"])},
            )
            .scalars()
            .all(),
        )
    engine.dispose()
    assert events == [
        "work_order_assigned",
        "work_order_blocked",
        "work_order_unblocked",
        "work_order_blocked",
        "work_order_unblocked",
    ]


def test_blocked_manual_run_keeps_same_run_and_stops_actions(client: TestClient) -> None:
    project_id, order_id, run_id, engineer_id = create_running_work_order(client, "Manual pause")
    root = f"/api/projects/{project_id}"
    path = f"{root}/work-orders/{order_id}"
    qa_id = next(p["id"] for p in client.get(f"{root}/profiles").json() if p["name"] == "QA")
    approval = post(
        client,
        f"{root}/runs/{run_id}/approval-requests",
        {
            "action_type": "other",
            "target": "local",
            "content": "Draft only",
        },
    ).json()
    current = next(o for o in client.get(f"{root}/work-orders").json() if o["id"] == order_id)
    blocked = post(
        client,
        f"{path}/block",
        {
            "reason": "Operator hold",
            "expected_version": current["version"],
        },
    )
    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["blocked_from_status"] == "running"
    assert post(client, f"{root}/runs/{run_id}/handoffs", {"profile_id": qa_id}).status_code == 409
    assert post(client, f"{root}/runs/{run_id}/finish", {"outcome": "done"}).status_code == 409
    assert (
        post(
            client,
            f"{root}/runs/{run_id}/approval-requests",
            {
                "action_type": "other",
                "target": "local",
                "content": "Another draft",
            },
        ).status_code
        == 409
    )
    assert (
        post(
            client,
            f"{root}/approvals/{approval['id']}/decision",
            {
                "decision": "approved",
            },
        ).status_code
        == 409
    )
    assert post(client, f"{path}/runs").status_code == 409
    resumed = post(
        client,
        f"{path}/unblock",
        {
            "expected_version": blocked.json()["version"],
        },
    )
    assert resumed.status_code == 200 and resumed.json()["status"] == "running"
    assert (
        post(
            client,
            f"{root}/approvals/{approval['id']}/decision",
            {
                "decision": "rejected",
            },
        ).status_code
        == 200
    )
    assert post(client, f"{root}/runs/{run_id}/handoffs", {"profile_id": qa_id}).status_code == 200
    assert post(client, f"{root}/runs/{run_id}/finish", {"outcome": "done"}).status_code == 200
    assert (
        post(
            client,
            f"{path}/block",
            {
                "reason": "Late",
                "expected_version": resumed.json()["version"],
            },
        ).status_code
        == 409
    )
    assert client.get(f"{root}/runs/{run_id}").json()["id"] == run_id
    assert engineer_id != qa_id


def test_control_invalid_cross_project_and_concurrent_assign(client: TestClient) -> None:
    project_id, engineer_id = project_and_profile(client, "Control concurrency")
    other_id, other_profile = project_and_profile(client, "Control boundary")
    root = f"/api/projects/{project_id}"
    qa_id = next(p["id"] for p in client.get(f"{root}/profiles").json() if p["name"] == "QA")
    order = post(
        client,
        f"{root}/work-orders",
        {
            "title": "Concurrent",
            "instructions": "Plan",
            "profile_id": engineer_id,
        },
    ).json()
    path = f"{root}/work-orders/{order['id']}"
    assert (
        post(
            client,
            f"{path}/assign",
            {
                "profile_id": other_profile,
                "expected_version": 1,
            },
        ).status_code
        == 404
    )
    assert (
        post(
            client,
            f"/api/projects/{other_id}/work-orders/{order['id']}/block",
            {
                "reason": "No",
                "expected_version": 1,
            },
        ).status_code
        == 404
    )
    assert post(client, f"{path}/block", {"reason": "  ", "expected_version": 1}).status_code == 422
    assert post(client, f"{path}/block", {"reason": "Hold", "expected_version": 0}).status_code == 422
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda _: post(
                    client,
                    f"{path}/assign",
                    {
                        "profile_id": qa_id,
                        "expected_version": 1,
                    },
                ),
                range(2),
            )
        )
    assert sorted(response.status_code for response in responses) == [200, 409]
    current = next(o for o in client.get(f"{root}/work-orders").json() if o["id"] == order["id"])
    assert current["version"] == 2 and current["profile_id"] == qa_id


def test_start_and_block_contention_has_one_winner(client: TestClient) -> None:
    project_id, engineer_id = project_and_profile(client, "Start block contention")
    root = f"/api/projects/{project_id}"
    for index in range(3):
        order = post(
            client,
            f"{root}/work-orders",
            {
                "title": f"Race {index}",
                "instructions": "Plan",
                "profile_id": engineer_id,
            },
        ).json()
        path = f"{root}/work-orders/{order['id']}"
        ready = post(client, f"{path}/ready").json()
        gate = Barrier(2)

        def start(wait_gate: Barrier = gate, order_path: str = path) -> Response:
            wait_gate.wait()
            return post(client, f"{order_path}/runs")

        def block(
            wait_gate: Barrier = gate, order_path: str = path, version: int = ready["version"]
        ) -> Response:
            wait_gate.wait()
            return post(
                client,
                f"{order_path}/block",
                {
                    "reason": "Operator hold",
                    "expected_version": version,
                },
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            start_future = pool.submit(start)
            block_future = pool.submit(block)
            started, blocked = start_future.result(), block_future.result()
        assert sorted([started.status_code, blocked.status_code]) in ([200, 409], [201, 409])
        current = next(o for o in client.get(f"{root}/work-orders").json() if o["id"] == order["id"])
        if started.status_code == 201:
            assert blocked.status_code == 409
            assert current["status"] == "running"
            assert current["version"] == ready["version"] + 1
        else:
            assert blocked.status_code == 200 and started.status_code == 409
            assert current["status"] == "blocked"
            assert current["blocked_from_status"] == "ready"
            assert current["version"] == ready["version"] + 1


def test_manual_finish_and_block_contention_has_one_winner(client: TestClient) -> None:
    project_id, order_id, run_id, _ = create_running_work_order(client, "Finish block contention")
    root = f"/api/projects/{project_id}"
    path = f"{root}/work-orders/{order_id}"
    current = next(o for o in client.get(f"{root}/work-orders").json() if o["id"] == order_id)
    gate = Barrier(2)

    def finish() -> Response:
        gate.wait()
        return post(client, f"{root}/runs/{run_id}/finish", {"outcome": "done"})

    def block() -> Response:
        gate.wait()
        return post(
            client,
            f"{path}/block",
            {
                "reason": "Operator hold",
                "expected_version": current["version"],
            },
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        finish_future = pool.submit(finish)
        block_future = pool.submit(block)
        finished, blocked = finish_future.result(), block_future.result()
    assert sorted([finished.status_code, blocked.status_code]) == [200, 409]
    after = next(o for o in client.get(f"{root}/work-orders").json() if o["id"] == order_id)
    if finished.status_code == 200:
        assert after["status"] == "needs_review"
        assert blocked.status_code == 409
    else:
        assert after["status"] == "blocked"
        assert after["blocked_from_status"] == "running"
        assert finished.status_code == 409


def test_in_flight_synthetic_run_cannot_be_blocked(client: TestClient) -> None:
    project_id, order_id, run_id, _ = create_running_work_order(client, "In flight exclusion")
    bootstrap = Settings(
        db_user=os.environ.get("DB_USER", "onefoundry"), db_password=os.environ["DB_PASSWORD"]
    )
    engine = create_engine(bootstrap.database_url)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE runs SET mode='synthetic_text' WHERE project_id=:project_id AND id=:id"),
            {"project_id": UUID(project_id), "id": UUID(run_id)},
        )
    root = f"/api/projects/{project_id}"
    current = next(o for o in client.get(f"{root}/work-orders").json() if o["id"] == order_id)
    response = post(
        client,
        f"{root}/work-orders/{order_id}/block",
        {
            "reason": "Unsafe hold",
            "expected_version": current["version"],
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "in-flight execution cannot be blocked"
    assert (
        next(o for o in client.get(f"{root}/work-orders").json() if o["id"] == order_id)["status"]
        == "running"
    )
    engine.dispose()


def test_manual_run_handoff_approvals_and_timeline(client: TestClient, settings: Settings) -> None:
    project_id, engineer_id = project_and_profile(client, "Workflow")
    path = f"/api/projects/{project_id}"
    assert client.get(f"{path}/approvals?status=pending").json() == []
    profiles = client.get(f"{path}/profiles").json()
    qa_id = next(profile["id"] for profile in profiles if profile["name"] == "QA")
    order_response = post(
        client,
        f"{path}/work-orders",
        {"title": "Launch plan", "instructions": "Prepare a local draft", "profile_id": engineer_id},
    )
    assert order_response.status_code == 201
    order = order_response.json()
    order_id = order["id"]
    assert order["status"] == "draft"
    assert post(client, f"{path}/work-orders/{order_id}/runs").status_code == 409
    assert post(client, f"{path}/work-orders/{order_id}/ready").json()["status"] == "ready"
    assert post(client, f"{path}/work-orders/{order_id}/ready").status_code == 409
    run_response = post(client, f"{path}/work-orders/{order_id}/runs")
    assert run_response.status_code == 201
    run = run_response.json()
    run_id = run["id"]
    assert run["status"] == "running"
    assert run["mode"] == "manual_planning"
    assert run["tokens_used"] == 0 and run["cost_usd"] == 0
    assert run["work_order_instructions"] == "Prepare a local draft"
    assert post(client, f"{path}/work-orders/{order_id}/runs").status_code == 409
    assert post(client, f"{path}/runs/{run_id}/handoffs", {"profile_id": engineer_id}).status_code == 409
    handoff = post(client, f"{path}/runs/{run_id}/handoffs", {"profile_id": qa_id})
    assert handoff.status_code == 200
    assert handoff.json()["id"] == run_id and handoff.json()["profile_id"] == qa_id
    proposal = {"action_type": "email", "target": "local-review@example.test", "content": "Draft text only"}
    first = post(client, f"{path}/runs/{run_id}/approval-requests", proposal)
    assert first.status_code == 201
    first_body = first.json()
    digest = hashlib.sha256(
        json.dumps(proposal, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert first_body["proposal_digest"] == digest
    assert first_body["status"] == "pending" and first_body["decided_at"] is None
    pending_response = client.get(f"{path}/approvals?status=pending")
    assert pending_response.status_code == 200
    assert [row["id"] for row in pending_response.json()] == [first_body["id"]]
    assert post(client, f"{path}/runs/{run_id}/finish", {"outcome": "done"}).status_code == 409
    approved = post(client, f"{path}/approvals/{first_body['id']}/decision", {"decision": "approved"})
    assert approved.status_code == 200 and approved.json()["status"] == "approved"
    assert approved.json()["proposal_digest"] == digest
    assert (
        post(client, f"{path}/approvals/{first_body['id']}/decision", {"decision": "rejected"}).status_code
        == 409
    )
    second = post(
        client,
        f"{path}/runs/{run_id}/approval-requests",
        {"action_type": "publish", "target": "local-preview", "content": "Another draft"},
    ).json()
    rejected = post(client, f"{path}/approvals/{second['id']}/decision", {"decision": "rejected"})
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"
    assert len(client.get(f"{path}/approvals?status=approved").json()) == 1
    assert len(client.get(f"{path}/approvals?status=rejected").json()) == 1
    finished = post(client, f"{path}/runs/{run_id}/finish", {"outcome": "done", "reason": "Reviewed"})
    assert finished.status_code == 200 and finished.json()["status"] == "done"
    assert finished.json()["outcome_reason"] == "Reviewed"
    assert post(client, f"{path}/runs/{run_id}/finish", {"outcome": "done"}).status_code == 409
    assert post(client, f"{path}/runs/{run_id}/approval-requests", proposal).status_code == 409
    detail = client.get(f"{path}/runs/{run_id}").json()
    types = [event["event_type"] for event in detail["events"]]
    assert "work_order_created" in types and "work_order_ready" in types
    assert "run_started" in types and "run_handed_off" in types and "run_finished" in types
    assert types.count("approval_requested") == 2 and types.count("approval_decided") == 2
    assert any(event["run_id"] is None for event in detail["events"])
    assert all(event["actor_id"] == "operator" and event["created_at"] for event in detail["events"])
    assert all("Draft text only" not in json.dumps(event["payload"]) for event in detail["events"])
    assert all("local-review@example.test" not in json.dumps(event["payload"]) for event in detail["events"])
    assert all("Reviewed" not in json.dumps(event["payload"]) for event in detail["events"])
    assert detail["cost_usd"] == 0 and detail["tokens_used"] == 0
    assert len(client.get(f"{path}/runs?query=Launch").json()) == 1
    assert len(client.get(f"{path}/runs?query=run_handed_off").json()) == 1
    assert client.get(f"{path}/runs?query=missing").json() == []
    assert client.get(f"{path}/runs/{run_id}").json()["status"] == "done"
    engine = create_engine(settings.database_url)
    with engine.connect() as connection:
        status = connection.scalar(
            text("SELECT status FROM work_orders WHERE project_id = :project_id AND id = :id"),
            {"project_id": UUID(project_id), "id": UUID(order_id)},
        )
    assert status == "needs_review"
    engine.dispose()


def test_run_audit_snapshots_survive_profile_and_task_edits(client: TestClient, settings: Settings) -> None:
    project_id, engineer_id = project_and_profile(client, "Audit history")
    root = f"/api/projects/{project_id}"
    profiles = client.get(f"{root}/profiles").json()
    qa_id = next(profile["id"] for profile in profiles if profile["name"] == "QA")

    def configure(profile_id: str, model: str, instructions: str) -> None:
        response = client.patch(
            f"{root}/profiles/{profile_id}",
            json={
                "model": model,
                "instructions": instructions,
                "memory_scope": "project",
                "allowed_tools": ["local_read"],
                "autonomy_level": "supervised",
            },
            headers={"origin": "http://localhost:8765"},
        )
        assert response.status_code == 200, response.text

    configure(engineer_id, "model-before", "Engineer instructions before")
    order = post(
        client,
        f"{root}/work-orders",
        {"title": "Original task", "instructions": "Original task instructions", "profile_id": engineer_id},
    ).json()
    assert post(client, f"{root}/work-orders/{order['id']}/ready").status_code == 200
    run = post(client, f"{root}/work-orders/{order['id']}/runs").json()
    run_id = run["id"]
    configure(engineer_id, "model-after", "Engineer instructions after")
    configure(qa_id, "qa-before", "QA instructions before")
    assert post(client, f"{root}/runs/{run_id}/handoffs", {"profile_id": qa_id}).status_code == 200
    configure(qa_id, "qa-after", "QA instructions after")

    # Work-order edits are not exposed by the app. Simulate an owner edit to
    # prove run history and search do not join the mutable task row.
    bootstrap = create_engine(
        Settings(db_user="onefoundry", db_password=os.environ["DB_PASSWORD"]).database_url
    )
    with bootstrap.begin() as connection:
        connection.execute(
            text(
                "UPDATE work_orders SET title='Changed task', instructions='Changed instructions' "
                "WHERE project_id=:project_id AND id=:id"
            ),
            {"project_id": UUID(project_id), "id": UUID(order["id"])},
        )
    for statement in (
        "UPDATE run_profile_snapshots SET profile_model='forged' WHERE run_id=:run_id",
        "DELETE FROM run_profile_snapshots WHERE run_id=:run_id",
    ):
        with pytest.raises(DBAPIError):
            with bootstrap.begin() as connection:
                connection.execute(text(statement), {"run_id": UUID(run_id)})
    bootstrap.dispose()

    detail = client.get(f"{root}/runs/{run_id}").json()
    assert detail["work_order_title"] == "Original task"
    assert detail["work_order_instructions"] == "Original task instructions"
    assert detail["profile_model"] == "qa-before"
    snapshots = detail["profile_snapshots"]
    assert [item["profile_name"] for item in snapshots] == ["Engineer", "QA"]
    assert [item["profile_model"] for item in snapshots] == ["model-before", "qa-before"]
    assert [item["profile_instructions"] for item in snapshots] == [
        "Engineer instructions before",
        "QA instructions before",
    ]
    assert all(item["memory_scope"] == "project" for item in snapshots)
    assert all(item["allowed_tools"] == ["local_read"] for item in snapshots)
    assert all(item["autonomy_level"] == "supervised" for item in snapshots)
    assert [item["transition_index"] for item in snapshots] == [0, 1]
    assert [item["work_order_title"] for item in client.get(f"{root}/runs?query=Original").json()] == [
        "Original task"
    ]
    assert client.get(f"{root}/runs?query=Changed").json() == []


def test_pre_migration_run_does_not_claim_unknown_snapshots(client: TestClient, settings: Settings) -> None:
    project_id, profile_id = project_and_profile(client, "Earlier run")
    root = f"/api/projects/{project_id}"
    order = post(
        client,
        f"{root}/work-orders",
        {"title": "Current title", "instructions": "Current instructions", "profile_id": profile_id},
    ).json()
    run_id = uuid4()
    engine = create_engine(settings.database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runs (id, project_id, actor_id, work_order_id, profile_id, status, mode) "
                "VALUES (:id, :project_id, 'operator', :order_id, :profile_id, 'running', 'manual_planning')"
            ),
            {
                "id": run_id,
                "project_id": UUID(project_id),
                "order_id": UUID(order["id"]),
                "profile_id": UUID(profile_id),
            },
        )
    engine.dispose()
    detail = client.get(f"{root}/runs/{run_id}").json()
    assert detail["work_order_title"] is None
    assert detail["work_order_instructions"] is None
    assert detail["profile_model"] is None
    assert detail["profile_snapshots"] == []
    assert client.get(f"{root}/runs?query=Current title").json() == []


def test_project_scope_invalid_input_and_secret_rejection(client: TestClient) -> None:
    first, first_profile = project_and_profile(client, "First workflow")
    second, second_profile = project_and_profile(client, "Second workflow")
    first_path = f"/api/projects/{first}"
    assert (
        post(
            client,
            f"{first_path}/work-orders",
            {"title": "Wrong", "instructions": "Local", "profile_id": second_profile},
        ).status_code
        == 404
    )
    assert (
        post(
            client,
            f"{first_path}/work-orders",
            {"title": " ", "instructions": "Local", "profile_id": first_profile},
        ).status_code
        == 422
    )
    assert (
        post(
            client,
            f"{first_path}/work-orders",
            {"title": "Safe", "instructions": "api_key=abcdef", "profile_id": first_profile},
        ).status_code
        == 422
    )
    project_id, order_id, run_id, _ = create_running_work_order(client, "Secret checks")
    path = f"/api/projects/{project_id}"
    assert (
        post(client, f"{path}/runs/{run_id}/finish", {"outcome": "done", "reason": "token=abc"}).status_code
        == 422
    )
    assert (
        post(
            client,
            f"{path}/runs/{run_id}/approval-requests",
            {"action_type": "email", "target": "recipient", "content": "Bearer abcdefghijklmnop"},
        ).status_code
        == 422
    )
    assert (
        post(
            client,
            f"{path}/runs/{run_id}/approval-requests",
            {"action_type": "deploy", "target": "password=unsafe", "content": "draft"},
        ).status_code
        == 422
    )
    # Build synthetic provider-shaped values without embedding apparent credentials.
    for prefix in ("github_pat_", "xoxb-", "sk_" + "live_"):
        token = prefix + "X" * 24
        assert (
            post(
                client,
                f"{path}/runs/{run_id}/approval-requests",
                {"action_type": "other", "target": token, "content": "Draft"},
            ).status_code
            == 422
        )
        assert (
            post(client, f"{path}/runs/{run_id}/finish", {"outcome": "done", "reason": token}).status_code
            == 422
        )
        detail = client.get(f"{path}/runs/{run_id}").json()
        assert all(token not in json.dumps(event["payload"]) for event in detail["events"])
    assert post(client, f"{path}/runs/{run_id}/handoffs", {"profile_id": second_profile}).status_code == 404
    assert client.get(f"/api/projects/{second}/runs/{run_id}").status_code == 404
    assert (
        post(client, f"/api/projects/{second}/runs/{run_id}/finish", {"outcome": "done"}).status_code == 404
    )
    assert client.get(f"/api/projects/{uuid4()}/runs").status_code == 404
    assert client.get(f"/api/projects/{uuid4()}/work-orders").status_code == 404
    assert client.get(f"/api/projects/{uuid4()}/approvals").status_code == 404
    assert client.get(f"{path}/work-orders").json()[0]["id"] == order_id


def test_concurrent_decision_is_one_shot(client: TestClient, settings: Settings) -> None:
    project_id, _, run_id, _ = create_running_work_order(client, "Decision race")
    approval = post(
        client,
        f"/api/projects/{project_id}/runs/{run_id}/approval-requests",
        {"action_type": "merge", "target": "local-branch", "content": "Proposed diff"},
    ).json()
    engine = create_engine(settings.database_url)
    gateway = ToolGateway(engine, "operator")

    def decide() -> str:
        try:
            gateway.decide_approval(
                UUID(project_id), UUID(approval["id"]), ApprovalDecision(decision="approved")
            )
            return "approved"
        except Conflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: decide(), range(2)))
    assert sorted(outcomes) == ["approved", "conflict"]
    detail = client.get(f"/api/projects/{project_id}/runs/{run_id}").json()
    assert sum(event["event_type"] == "approval_decided" for event in detail["events"]) == 1
    engine.dispose()


def test_identical_proposals_have_distinct_event_links(client: TestClient) -> None:
    project_id, _, run_id, _ = create_running_work_order(client, "Identical drafts")
    path = f"/api/projects/{project_id}"
    proposal = {"action_type": "email", "target": "local-review", "content": "Same draft"}
    first = post(client, f"{path}/runs/{run_id}/approval-requests", proposal).json()
    second = post(client, f"{path}/runs/{run_id}/approval-requests", proposal).json()
    assert first["id"] != second["id"]
    assert first["proposal_digest"] == second["proposal_digest"]
    assert (
        post(client, f"{path}/approvals/{first['id']}/decision", {"decision": "approved"}).status_code == 200
    )
    assert (
        post(client, f"{path}/approvals/{second['id']}/decision", {"decision": "rejected"}).status_code == 200
    )
    events = client.get(f"{path}/runs/{run_id}").json()["events"]
    approval_events = [
        event for event in events if event["event_type"] in {"approval_requested", "approval_decided"}
    ]
    assert len(approval_events) == 4
    for approval, decision in ((first, "approved"), (second, "rejected")):
        linked = [event for event in approval_events if event["payload"]["approval_id"] == approval["id"]]
        assert {event["event_type"] for event in linked} == {
            "approval_requested",
            "approval_decided",
        }
        assert all(event["payload"]["proposal_digest"] == approval["proposal_digest"] for event in linked)
        decided = next(event for event in linked if event["event_type"] == "approval_decided")
        assert decided["payload"]["decision"] == decision


def test_event_failure_rolls_back_owner_state(client: TestClient, settings: Settings) -> None:
    project_id, profile_id = project_and_profile(client, "Atomic work")
    engine = create_engine(settings.database_url)
    gateway = ToolGateway(engine, "operator")
    with patch.object(gateway, "_event", side_effect=RuntimeError("event storage failed")):
        with pytest.raises(RuntimeError, match="event storage failed"):
            gateway.create_work_order(
                UUID(project_id),
                WorkOrderCreate(title="Atomic", instructions="Plan only", profile_id=UUID(profile_id)),
            )
    assert client.get(f"/api/projects/{project_id}/work-orders").json() == []
    engine.dispose()


def test_database_rejects_mismatched_run_work_order_links(client: TestClient, settings: Settings) -> None:
    project_id, profile_id = project_and_profile(client, "Linked rows")
    path = f"/api/projects/{project_id}"
    order_ids: list[str] = []
    run_ids: list[str] = []
    for name in ("First task", "Second task"):
        order = post(
            client,
            f"{path}/work-orders",
            {"title": name, "instructions": "Local plan", "profile_id": profile_id},
        ).json()
        order_ids.append(order["id"])
        assert post(client, f"{path}/work-orders/{order['id']}/ready").status_code == 200
        run = post(client, f"{path}/work-orders/{order['id']}/runs").json()
        run_ids.append(run["id"])

    engine = create_engine(settings.database_url)
    ids = {
        "id": uuid4(),
        "project_id": UUID(project_id),
        "run_id": UUID(run_ids[0]),
        "work_order_id": UUID(order_ids[1]),
    }
    with pytest.raises(IntegrityError, match="foreign key"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO approval_requests "
                    "(id, project_id, actor_id, work_order_id, run_id, action_type, "
                    "target, content, proposal_digest, status) "
                    "VALUES (:id, :project_id, 'operator', :work_order_id, :run_id, "
                    "'other', 'local', 'draft', :digest, 'pending')"
                ),
                {**ids, "digest": "0" * 64},
            )
    with pytest.raises(IntegrityError, match="foreign key"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO events (id, project_id, actor_id, work_order_id, run_id, event_type) "
                    "VALUES (:id, :project_id, 'operator', :work_order_id, :run_id, 'test')"
                ),
                ids,
            )
    with pytest.raises(IntegrityError, match="check constraint"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO events (id, project_id, actor_id, run_id, event_type) "
                    "VALUES (:id, :project_id, 'operator', :run_id, 'test')"
                ),
                {"id": uuid4(), "project_id": UUID(project_id), "run_id": UUID(run_ids[0])},
            )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO events (id, project_id, actor_id, work_order_id, event_type) "
                "VALUES (:id, :project_id, 'operator', :work_order_id, 'pre_run_test')"
            ),
            {"id": uuid4(), "project_id": UUID(project_id), "work_order_id": UUID(order_ids[0])},
        )
    engine.dispose()
