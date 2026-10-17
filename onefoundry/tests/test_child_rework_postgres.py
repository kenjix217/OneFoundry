"""Explicit child revision uses real PostgreSQL, receipts and the normal team SDK."""

import hashlib
import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from onefoundry.gateway_errors import Conflict
from onefoundry.main import create_app
from onefoundry.operator_message_contract import MAX_OPERATOR_MESSAGE_CHARS
from onefoundry.profile_text_runtime import ProfileTextRuntime
from onefoundry.settings import Settings
from onefoundry.task_session_assignments import AcceptanceKind, DelegateRequest, TaskSessionAssignments
from onefoundry.task_session_attempts import TaskSessionAttempts
from onefoundry.task_session_context import TaskSessionContext
from onefoundry.task_session_decisions import TaskSessionDecisions
from onefoundry.task_session_rework import ChildReworkCreate, return_child
from onefoundry.task_session_runs import bind as bind_team_run
from onefoundry.task_session_sdk_turn import TaskSessionSdkTurn, TeamModel
from onefoundry.task_session_tools import REPORT, ROLE, TaskSessionTools
from onefoundry.tool_gateway import ToolGateway
from tests.test_team_workflow_postgres import ORIGIN, project_with_models, settings_for


@pytest.fixture(autouse=True)
def quiet_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    async def start(_self: object) -> None:
        return None

    monkeypatch.setattr("onefoundry.coordination_launcher.CoordinationLauncher.start", start)


@dataclass
class CompletedChild:
    client: TestClient
    settings: Settings
    gateway: ToolGateway
    project: UUID
    order: UUID
    assignment: dict[str, Any]
    base: str

    def child(self) -> dict[str, Any]:
        return dict(self.client.get(f"{self.base}/sessions/{self.assignment['child_session_id']}").json())

    def root(self) -> dict[str, Any]:
        return dict(self.client.get(self.base).json())

    def request(
        self, body: str = "Use this complete revised specification and preserve prior evidence."
    ) -> dict[str, Any]:
        child = self.child()
        assignment = next(a for a in self.root()["assignments"] if a["id"] == str(self.assignment["id"]))
        return {
            "client_action_id": str(uuid4()),
            "expected_sequence": child["messages"][-1]["sequence"],
            "expected_result_message_id": assignment["result_message_id"],
            "body": body,
        }

    @property
    def path(self) -> str:
        return f"{self.base}/sessions/{self.assignment['child_session_id']}/return"


@pytest.fixture
def completed(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[CompletedChild]:
    settings = settings_for(tmp_path)
    kind: AcceptanceKind = "artifact" if getattr(request, "param", None) == "artifact" else "text_report"
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, profiles = project_with_models(client)
        response = client.post(
            f"/api/projects/{project}/work-orders",
            json={
                "title": "Review a public-source briefing",
                "instructions": "Use the supplied public source.",
                "profile_id": profiles["Chief of Staff"],
                "deliverable_kind": "artifact",
                "acceptance_checks": ["Exact supported source and independent review."],
            },
            headers=ORIGIN,
        )
        assert response.status_code == 201, response.text
        order = response.json()["id"]
        base = f"/api/projects/{project}/work-orders/{order}/team-session"
        ready = client.post(f"/api/projects/{project}/work-orders/{order}/ready", headers=ORIGIN)
        assert (
            client.post(
                base + "/activate", json={"expected_version": ready.json()["version"]}, headers=ORIGIN
            ).status_code
            == 200
        )
        assert (
            client.post(
                base + "/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN
            ).status_code
            == 202
        )
        gateway = ToolGateway(
            create_engine(settings.database_url), settings.operator_actor_id, live_text_settings=settings
        )
        attempts = TaskSessionAttempts(gateway)
        chief = attempts.claim_next(UUID(project))
        assert chief is not None
        assignment = TaskSessionAssignments(gateway).delegate(
            UUID(project),
            chief["session_id"],
            chief["id"],
            DelegateRequest(
                client_action_id=uuid4(),
                profile_id=UUID(profiles["GTM" if kind == "artifact" else "Researcher"]),
                objective="Check the public source",
                expected_result="A supported briefing",
                acceptance_check="Use only the exact source",
                acceptance_kind=kind,
            ),
        )
        attempts.settle(UUID(project), chief["id"], "done", public_reply="Work assigned.")
        worker = attempts.claim_next(UUID(project))
        assert worker is not None
        if kind == "artifact":
            bind_team_run(gateway, UUID(project), worker)
            tools = TaskSessionTools(gateway, UUID(project), worker["session_id"], worker["id"])
            artifact_reply = tools.invoke(
                ROLE,
                {
                    "tool_id": "write_text_artifact_v1",
                    "arguments": {"kind": "markdown", "content": "# Original artifact\nPublished reference."},
                },
                "initial-artifact",
            )
            assert artifact_reply["success"], artifact_reply
            artifact = json.loads(artifact_reply["contentItems"][0]["text"])
            result = tools.invoke(
                REPORT,
                {
                    "body": "Original artifact saved.",
                    "artifact_id": artifact["result"]["artifact_id"],
                    "artifact_run_id": artifact["run_id"],
                },
                "initial-result",
            )
            assert result["success"], result
        else:
            TaskSessionAssignments(gateway).report_text_result(
                UUID(project),
                assignment["child_session_id"],
                worker["id"],
                uuid4(),
                "Original supported result, retained as historical evidence.",
            )
        with gateway.engine.connect() as connection:
            assignment = dict(
                connection.execute(
                    text("SELECT * FROM task_session_assignments WHERE project_id=:p AND id=:a"),
                    {"p": UUID(project), "a": assignment["id"]},
                )
                .mappings()
                .one()
            )
        attempts.settle(UUID(project), worker["id"], "done", public_reply="Original result saved.")
        yield CompletedChild(client, settings, gateway, UUID(project), UUID(order), assignment, base)
        gateway.engine.dispose()


def test_closed_child_refuses_ordinary_send_until_explicit_return(completed: CompletedChild) -> None:
    child = completed.child()
    response = completed.client.post(
        f"{completed.base}/sessions/{child['session']['id']}/messages",
        json={
            "client_message_id": str(uuid4()),
            "expected_sequence": child["messages"][-1]["sequence"],
            "body": "The operator requests a source-backed revision.",
        },
        headers=ORIGIN,
    )
    assert response.status_code == 409
    assert "closed" in response.json()["detail"]
    assert completed.child() == child


def test_return_preserves_history_and_delivers_entire_specification(completed: CompletedChild) -> None:
    original = completed.child()
    body = (
        "BEGIN COMPLETE SPECIFICATION\n"
        + "Published reference; adoption date unverified.\n" * 130
        + "END COMPLETE SPECIFICATION"
    )
    payload = completed.request(body)
    response = completed.client.post(completed.path, json=payload, headers=ORIGIN)
    assert response.status_code == 202, response.text
    saved = response.json()
    assert saved["assignment_id"] == str(completed.assignment["id"])
    child = completed.child()
    assert child["messages"][:-1] == original["messages"]
    assert child["attempts"][:-1] == original["attempts"]
    assert child["tool_calls"] == original["tool_calls"]
    assert child["messages"][-1]["body"] == body
    assert child["session"]["status"] == "working"
    assignment = next(
        a for a in completed.root()["assignments"] if a["id"] == str(completed.assignment["id"])
    )
    assert assignment["status"] == "ready"
    assert assignment["result_message_id"] is None
    assert len(completed.root()["children"]) == 1
    claimed = TaskSessionAttempts(completed.gateway).claim_next(completed.project)
    assert claimed is not None and str(claimed["id"]) == saved["attempt_id"]
    context = TaskSessionContext(completed.gateway, completed.settings).prepare(
        completed.project, claimed["id"]
    )
    assert body in context.prompt
    assert UUID(saved["message_id"]) in context.message_ids
    TaskSessionAssignments(completed.gateway).report_text_result(
        completed.project,
        claimed["session_id"],
        claimed["id"],
        uuid4(),
        "Revised result with the supplied reference and adoption caveat.",
    )
    TaskSessionAttempts(completed.gateway).settle(
        completed.project, claimed["id"], "done", public_reply="Revision saved."
    )
    final = completed.child()
    assert final["session"]["status"] == "closed"
    final_assignment = next(
        a for a in completed.root()["assignments"] if a["id"] == str(completed.assignment["id"])
    )
    assert final_assignment["status"] == "satisfied"
    assert final_assignment["result_message_id"] != payload["expected_result_message_id"]
    assert any(m["id"] == payload["expected_result_message_id"] for m in final["messages"])


def test_return_is_idempotent_and_conflicting_identity_is_rejected(completed: CompletedChild) -> None:
    payload = completed.request()
    first = completed.client.post(completed.path, json=payload, headers=ORIGIN)
    assert first.status_code == 202, first.text
    after = completed.child()
    repeated = completed.client.post(completed.path, json=payload, headers=ORIGIN)
    assert repeated.status_code == 200 and repeated.json() == first.json()
    assert completed.child() == after
    changed = completed.client.post(
        completed.path,
        json={**payload, "body": "A different revision under a used identity."},
        headers=ORIGIN,
    )
    assert changed.status_code == 409
    assert completed.child() == after


@pytest.mark.parametrize(
    "field,value", [("expected_sequence", 0), ("expected_result_message_id", str(uuid4()))]
)
def test_return_refuses_stale_revision_boundary(completed: CompletedChild, field: str, value: Any) -> None:
    before = completed.child()
    response = completed.client.post(
        completed.path, json={**completed.request(), field: value}, headers=ORIGIN
    )
    assert response.status_code == 409, response.text
    assert completed.child() == before


@pytest.mark.parametrize(
    "body", ["", "  ", "password=actual-secret-placeholder", "x" * (MAX_OPERATOR_MESSAGE_CHARS + 1)]
)
def test_return_rejects_invalid_or_sensitive_input(completed: CompletedChild, body: str) -> None:
    before = completed.child()
    response = completed.client.post(completed.path, json=completed.request(body), headers=ORIGIN)
    assert response.status_code == 422, response.text
    assert completed.child() == before


def test_return_requires_operator_and_exact_scope(completed: CompletedChild) -> None:
    payload = completed.request()
    assert completed.client.post(completed.path, json=payload).status_code == 403
    wrong = completed.path.replace(str(completed.project), str(uuid4()))
    assert completed.client.post(wrong, json=payload, headers=ORIGIN).status_code == 404
    assert (
        completed.client.post(
            completed.base + "/sessions/" + completed.root()["session"]["id"] + "/return",
            json=payload,
            headers=ORIGIN,
        ).status_code
        == 409
    )


def test_old_result_retry_does_not_resatisfy_returned_assignment(completed: CompletedChild) -> None:
    before = completed.child()
    original_body = next(
        m["body"] for m in before["messages"] if m["id"] == str(completed.assignment["result_message_id"])
    )
    response = completed.client.post(completed.path, json=completed.request(), headers=ORIGIN)
    assert response.status_code == 202, response.text
    after = completed.child()
    repeated = TaskSessionAssignments(completed.gateway).report_text_result(
        completed.project,
        completed.assignment["child_session_id"],
        UUID(before["attempts"][0]["id"]),
        completed.assignment["result_action_id"],
        original_body,
    )
    assert repeated["result_message_id"] == completed.assignment["result_message_id"]
    assert completed.child() == after
    assert (
        next(a for a in completed.root()["assignments"] if a["id"] == str(completed.assignment["id"]))[
            "status"
        ]
        == "ready"
    )
    with pytest.raises(Conflict, match="different work"):
        TaskSessionAssignments(completed.gateway).report_text_result(
            completed.project,
            completed.assignment["child_session_id"],
            UUID(before["attempts"][0]["id"]),
            completed.assignment["result_action_id"],
            "Changed contents under the original receipt identity.",
        )


@pytest.mark.parametrize("same_identity", [True, False])
def test_concurrent_returns_queue_exactly_one_turn(completed: CompletedChild, same_identity: bool) -> None:
    payload = completed.request()
    other = payload if same_identity else {**payload, "client_action_id": str(uuid4())}

    def post(data: dict[str, Any]) -> Any:
        return completed.client.post(completed.path, json=data, headers=ORIGIN)

    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(post, [payload, other]))
    assert sorted(r.status_code for r in replies) == ([200, 202] if same_identity else [202, 409])
    child = completed.child()
    assert len([a for a in child["attempts"] if a["status"] == "queued"]) == 1
    assert len([m for m in child["messages"] if m["body"] == payload["body"]]) == 1


def test_event_owner_failure_rolls_back_return_then_retry_is_safe(
    completed: CompletedChild, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_event = ToolGateway._event

    def unavailable(
        self: ToolGateway,
        connection: Any,
        project_id: UUID,
        order_id: UUID | None,
        run_id: UUID | None,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        if event_type == "task_session_assignment_returned":
            raise OperationalError("fixture owner unavailable", None, RuntimeError("fixture event failure"))
        original_event(self, connection, project_id, order_id, run_id, event_type, payload)

    before = completed.child()
    root_before = completed.root()
    data = completed.request()
    monkeypatch.setattr(ToolGateway, "_event", unavailable)
    response = completed.client.post(completed.path, json=data, headers=ORIGIN)
    assert response.status_code == 503
    assert completed.child() == before and completed.root() == root_before
    monkeypatch.setattr(ToolGateway, "_event", original_event)
    assert completed.client.post(completed.path, json=data, headers=ORIGIN).status_code == 202
    assert len(completed.child()["attempts"]) == len(before["attempts"]) + 1


def test_disabled_worker_refuses_a_new_return(completed: CompletedChild) -> None:
    app = create_app(completed.settings.model_copy(update={"team_worker_enabled": False}))
    before = completed.child()
    with TestClient(app, base_url="http://localhost:8765") as client:
        response = client.post(completed.path, json=completed.request(), headers=ORIGIN)
    assert response.status_code == 409
    assert completed.child() == before


def test_retry_survives_app_restart_and_disabled_worker(completed: CompletedChild) -> None:
    payload = completed.request()
    first = completed.client.post(completed.path, json=payload, headers=ORIGIN)
    assert first.status_code == 202, first.text
    after = completed.child()
    app = create_app(completed.settings.model_copy(update={"team_worker_enabled": False}))
    with TestClient(app, base_url="http://localhost:8765") as client:
        repeated = client.post(completed.path, json=payload, headers=ORIGIN)
    assert repeated.status_code == 200 and repeated.json() == first.json()
    assert completed.child() == after


def test_rework_retry_survives_new_finite_context_limit(completed: CompletedChild) -> None:
    body = "Revise using each confirmed source. " + "source evidence " * 230
    assert len(body.encode()) > 2_048
    payload = completed.request(body)
    first = completed.client.post(completed.path, json=payload, headers=ORIGIN)
    assert first.status_code == 202, first.text
    after = completed.child()
    constrained = completed.settings.model_copy(update={"onefoundry_max_context_bytes": 2_048})
    with TestClient(create_app(constrained), base_url="http://localhost:8765") as client:
        replay = client.post(completed.path, json=payload, headers=ORIGIN)
        assert replay.status_code == 200 and replay.json() == first.json()
        different = client.post(
            completed.path, json={**payload, "client_action_id": str(uuid4())}, headers=ORIGIN
        )
        assert different.status_code == 409
        assert "configured context budget" in different.text
    assert completed.child() == after


def test_agent_gateway_cannot_return_operator_work(completed: CompletedChild) -> None:
    gateway = ToolGateway(
        completed.gateway.engine, "profile:fixture-agent", live_text_settings=completed.settings
    )
    before = completed.child()
    with pytest.raises(Conflict, match="only the operator"):
        return_child(
            gateway,
            completed.project,
            completed.order,
            completed.assignment["child_session_id"],
            ChildReworkCreate.model_validate(completed.request()),
        )
    assert completed.child() == before


@pytest.mark.parametrize("state", ["queued", "running", "unknown"])
def test_return_refuses_other_active_or_uncertain_team_turns(completed: CompletedChild, state: str) -> None:
    payload = completed.request()
    response = completed.client.post(
        completed.base + "/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN
    )
    assert response.status_code == 202, response.text
    attempts = TaskSessionAttempts(completed.gateway)
    if state != "queued":
        claimed = attempts.claim_next(completed.project)
        assert claimed is not None
        if state == "unknown":
            attempts.settle(completed.project, claimed["id"], "unknown", reason="fixture_interruption")
    before = completed.child()
    response = completed.client.post(completed.path, json=payload, headers=ORIGIN)
    assert response.status_code == 409, response.text
    assert completed.child() == before


def test_return_refuses_a_result_already_consumed_by_completed_work(completed: CompletedChild) -> None:
    payload = completed.request()
    assert (
        completed.client.post(
            completed.base + "/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN
        ).status_code
        == 202
    )
    attempts = TaskSessionAttempts(completed.gateway)
    chief = attempts.claim_next(completed.project)
    assert chief is not None
    profiles = completed.client.get(f"/api/projects/{completed.project}/profiles").json()
    profile = next(p for p in profiles if p["name"] == "Researcher")
    consumer = TaskSessionAssignments(completed.gateway).delegate(
        completed.project,
        chief["session_id"],
        chief["id"],
        DelegateRequest(
            client_action_id=uuid4(),
            profile_id=UUID(profile["id"]),
            objective="Review the confirmed prior result",
            expected_result="A saved review",
            acceptance_check="Use the declared input",
            depends_on=(completed.assignment["id"],),
        ),
    )
    attempts.settle(completed.project, chief["id"], "done", public_reply="Review assigned.")
    worker = attempts.claim_next(completed.project)
    assert worker is not None
    TaskSessionAssignments(completed.gateway).report_text_result(
        completed.project,
        consumer["child_session_id"],
        worker["id"],
        uuid4(),
        "Review used the original confirmed source result.",
    )
    attempts.settle(completed.project, worker["id"], "done", public_reply="Review saved.")
    before = completed.child()
    response = completed.client.post(completed.path, json=payload, headers=ORIGIN)
    assert response.status_code == 409 and "dependent work" in response.json()["detail"]
    assert completed.child() == before


def test_operator_cancellation_preserves_return_receipt_without_relaunch(completed: CompletedChild) -> None:
    payload = completed.request()
    first = completed.client.post(completed.path, json=payload, headers=ORIGIN)
    assert first.status_code == 202, first.text
    orders = completed.client.get(f"/api/projects/{completed.project}/work-orders").json()
    order = next(o for o in orders if o["id"] == str(completed.order))
    canceled = completed.client.post(
        completed.base + "/cancel",
        json={"client_action_id": str(uuid4()), "expected_version": order["version"]},
        headers=ORIGIN,
    )
    assert canceled.status_code == 200, canceled.text
    after = completed.child()
    assert after["attempts"][-1]["status"] == "failed"
    repeated = completed.client.post(completed.path, json=payload, headers=ORIGIN)
    assert repeated.status_code == 200 and repeated.json() == first.json()
    assert completed.child() == after
    assert TaskSessionAttempts(completed.gateway).claim_next(completed.project) is None


def test_required_revision_budget_failure_occurs_before_provider(completed: CompletedChild) -> None:
    body = "Complete source text. " * 600
    response = completed.client.post(completed.path, json=completed.request(body), headers=ORIGIN)
    assert response.status_code == 202, response.text
    model = ScriptedModel([ModelStep(output=[assistant_message("This must not be requested.")])])

    def factory(runtime: ProfileTextRuntime, _settings: Settings) -> TeamModel:
        return TeamModel(model, runtime.provider, runtime.model_id, False, False)

    settings = completed.settings.model_copy(update={"onefoundry_max_context_bytes": 2000})
    result = TaskSessionSdkTurn(completed.gateway, settings, model_factory=factory).run_next(
        completed.project
    )
    assert result is not None and result["outcome"] == "failed", result
    assert model.first_call is None
    assert completed.child()["attempts"][-1]["reason"] == "team_context_budget_exceeded"
    continued = completed.client.post(
        f"{completed.base}/sessions/{completed.assignment['child_session_id']}/continue",
        json={"client_attempt_id": str(uuid4())},
        headers=ORIGIN,
    )
    assert continued.status_code == 202, continued.text
    claimed = TaskSessionAttempts(completed.gateway).claim_next(completed.project)
    assert claimed is not None
    assert claimed["session_id"] == completed.assignment["child_session_id"]
    context = TaskSessionContext(
        completed.gateway, completed.settings.model_copy(update={"task_conversation_max_turns": 1})
    ).prepare(completed.project, claimed["id"])
    assert body.strip() in context.prompt
    assert "Operator revision instructions (required task data)" in context.prompt
    assert UUID(response.json()["message_id"]) in context.message_ids


@pytest.mark.parametrize("completed", ["artifact"], indirect=True)
def test_sdk_rework_preserves_original_artifact_and_wakes_chief_with_new_result(
    completed: CompletedChild,
) -> None:
    original = completed.child()
    envelope = json.loads(
        next(
            m["body"]
            for m in original["messages"]
            if m["id"] == str(completed.assignment["result_message_id"])
        )
    )
    old_artifact = envelope["artifact"]
    original_path = (
        f"/api/projects/{completed.project}/runs/{old_artifact['run_id']}"
        f"/workspace-artifacts/{old_artifact['artifact_id']}"
    )
    before = completed.client.get(original_path).json()
    body = (
        "BEGIN COMPLETE SOURCE\n"
        "Source has no adoption date; create the cited reference briefing.\nEND COMPLETE SOURCE"
    )
    returned = completed.client.post(completed.path, json=completed.request(body), headers=ORIGIN)
    assert returned.status_code == 202, returned.text
    captured: dict[str, str] = {}

    def report_saved(call: Any) -> list[Any]:
        assert model.first_call is not None
        first_input = model.first_call.input
        assert isinstance(first_input, list)
        prompt = first_input[0]["content"]
        assert isinstance(prompt, str)
        assert body in prompt
        reply = json.loads(
            next(
                x["output"]
                for x in reversed(call.input)
                if isinstance(x, dict) and x.get("type") == "function_call_output"
            )
        )
        assert reply["success"]
        saved = reply
        captured.update({"id": saved["result"]["artifact_id"], "run": saved["run_id"]})
        return [
            function_call(
                "report_result",
                {
                    "body": "Revised source briefing saved.",
                    "artifact_id": captured["id"],
                    "artifact_run_id": captured["run"],
                },
                call_id="revised-result",
            )
        ]

    model = ScriptedModel(
        [
            ModelStep(
                output=[
                    function_call(
                        "role_tool",
                        {
                            "tool_id": "write_text_artifact_v1",
                            "arguments": {
                                "kind": "markdown",
                                "content": (
                                    "# Revised source briefing\n"
                                    "Adoption date unverified [published reference]."
                                ),
                            },
                        },
                        call_id="revised-artifact",
                    )
                ]
            ),
            ModelStep.respond(report_saved),
            ModelStep(output=[assistant_message("Revised artifact saved for review.")]),
        ]
    )

    def factory(runtime: ProfileTextRuntime, _settings: Settings) -> TeamModel:
        return TeamModel(model, runtime.provider, runtime.model_id, False, False)

    result = TaskSessionSdkTurn(completed.gateway, completed.settings, model_factory=factory).run_next(
        completed.project
    )
    assert result is not None and result["outcome"] == "done", result
    assert completed.client.get(original_path).json() == before
    new_path = (
        f"/api/projects/{completed.project}/runs/{captured['run']}/workspace-artifacts/{captured['id']}"
    )
    new = completed.client.get(new_path).json()
    assert hashlib.sha256(new["content"].encode()).hexdigest() == new["sha256"]
    assert old_artifact["artifact_id"] != new["id"]
    child = completed.child()
    assert child["messages"][: len(original["messages"])] == original["messages"]
    assert len(completed.root()["children"]) == 1
    assert TaskSessionDecisions(completed.gateway).dispatch_next(completed.project) is not None
    chief_attempt = TaskSessionAttempts(completed.gateway).claim_next(completed.project)
    assert chief_attempt is not None
    context = TaskSessionContext(completed.gateway, completed.settings).prepare(
        completed.project, chief_attempt["id"]
    )
    revised_assignment = next(
        a for a in completed.root()["assignments"] if a["id"] == str(completed.assignment["id"])
    )
    revised_message = next(m for m in child["messages"] if m["id"] == revised_assignment["result_message_id"])
    revised_envelope = json.loads(revised_message["body"])
    assert revised_envelope["artifact"]["artifact_id"] == new["id"]
    assert revised_envelope["artifact"]["sha256"] == new["sha256"]
    assert revised_envelope["artifact"]["content"] == new["content"]
    assert revised_message["body"] in context.prompt
    assert UUID(revised_message["id"]) in context.message_ids
    assert "Operator returned assignment" in context.prompt
    assert completed.root()["session"]["work_order_status"] == "running"


def test_advertising_review_checkpoint_can_return_same_closed_strategist(
    completed: CompletedChild,
) -> None:
    # Match the saved advertising HTTP 409: an open Chief at needs_review,
    # with a closed, satisfied text-report child. Never mutate the live task.
    with completed.gateway.engine.begin() as connection:
        connection.execute(
            text("UPDATE work_orders SET status='needs_review' WHERE project_id=:p AND id=:o"),
            {"p": completed.project, "o": completed.order},
        )
    before = completed.child()
    assert completed.root()["session"]["status"] == "open"
    response = completed.client.post(completed.path, json=completed.request(), headers=ORIGIN)
    assert response.status_code == 202, response.text
    assert completed.root()["session"]["work_order_status"] == "running"
    assert completed.child()["messages"][:-1] == before["messages"]
    assert len(completed.root()["children"]) == 1
    assert TaskSessionDecisions(completed.gateway).dispatch_next(completed.project) is None


@pytest.mark.parametrize("root_status", ["done", "failed", "unknown"])
def test_return_refuses_terminal_root_without_changing_child(
    completed: CompletedChild, root_status: str
) -> None:
    payload = completed.request()
    with completed.gateway.engine.begin() as connection:
        connection.execute(
            text("UPDATE work_orders SET status=:status WHERE project_id=:p AND id=:o"),
            {"p": completed.project, "o": completed.order, "status": root_status},
        )
    before = completed.child()
    response = completed.client.post(completed.path, json=payload, headers=ORIGIN)
    assert response.status_code == 409 and "unaccepted" in response.json()["detail"]
    assert completed.child() == before


def test_return_refuses_closed_parent_even_with_running_root(completed: CompletedChild) -> None:
    payload = completed.request()
    with completed.gateway.engine.begin() as connection:
        connection.execute(
            text("UPDATE task_sessions SET status='closed' WHERE project_id=:p AND kind='main'"),
            {"p": completed.project},
        )
    before = completed.child()
    response = completed.client.post(completed.path, json=payload, headers=ORIGIN)
    assert response.status_code == 409 and "open team" in response.json()["detail"]
    assert completed.child() == before


def test_repeated_feedback_pins_latest_revision_and_preserves_each_result(
    completed: CompletedChild,
) -> None:
    original = completed.child()
    first_payload = completed.request("First revision: cite the published reference.")
    first_return = completed.client.post(completed.path, json=first_payload, headers=ORIGIN)
    assert first_return.status_code == 202, first_return.text
    attempts = TaskSessionAttempts(completed.gateway)
    first_attempt = attempts.claim_next(completed.project)
    assert first_attempt is not None
    result_action = uuid4()
    first_body = "First revised result, preserved when a second revision is requested."
    first_result = TaskSessionAssignments(completed.gateway).report_text_result(
        completed.project,
        first_attempt["session_id"],
        first_attempt["id"],
        result_action,
        first_body,
    )
    attempts.settle(completed.project, first_attempt["id"], "done", public_reply="First revision saved.")
    intermediate = completed.child()
    second_body = "Second revision: keep the complete published reference and its unresolved date."
    second_payload = completed.request(second_body)
    assert second_payload["expected_result_message_id"] == str(first_result["result_message_id"])
    second_return = completed.client.post(completed.path, json=second_payload, headers=ORIGIN)
    assert second_return.status_code == 202, second_return.text
    # Later coalesced operator messages exceed the ordinary history window.
    for body in ("Also keep the reference citation.", "Keep the uncertainty explicit."):
        current = completed.child()
        response = completed.client.post(
            f"{completed.base}/sessions/{first_attempt['session_id']}/messages",
            json={
                "client_message_id": str(uuid4()),
                "expected_sequence": current["messages"][-1]["sequence"],
                "body": body,
            },
            headers=ORIGIN,
        )
        assert response.status_code == 202, response.text
        assert response.json()["state"] == "coalesced"
    before_retry = completed.child()
    replay = completed.client.post(completed.path, json=first_payload, headers=ORIGIN)
    assert replay.status_code == 200 and replay.json() == first_return.json()
    old_result_retry = TaskSessionAssignments(completed.gateway).report_text_result(
        completed.project, first_attempt["session_id"], first_attempt["id"], result_action, first_body
    )
    assert old_result_retry["result_message_id"] == first_result["result_message_id"]
    assert completed.child() == before_retry
    second_attempt = attempts.claim_next(completed.project)
    assert second_attempt is not None and str(second_attempt["id"]) == second_return.json()["attempt_id"]
    context = TaskSessionContext(
        completed.gateway, completed.settings.model_copy(update={"task_conversation_max_turns": 1})
    ).prepare(completed.project, second_attempt["id"])
    assert second_body in context.prompt
    assert first_payload["body"] not in context.prompt
    assert UUID(second_return.json()["message_id"]) in context.message_ids
    assert context.omitted_count > 0
    assert completed.child()["messages"][: len(intermediate["messages"])] == intermediate["messages"]
    assert intermediate["messages"][: len(original["messages"])] == original["messages"]
    assert len(completed.root()["children"]) == 1
    assert len(completed.child()["attempts"]) == 3


def test_return_racing_cancellation_cannot_leave_runnable_revision(completed: CompletedChild) -> None:
    payload = completed.request()
    order = next(
        o
        for o in completed.client.get(f"/api/projects/{completed.project}/work-orders").json()
        if o["id"] == str(completed.order)
    )
    cancel_identity = str(uuid4())

    def revise() -> Any:
        return completed.client.post(completed.path, json=payload, headers=ORIGIN)

    def cancel() -> Any:
        return completed.client.post(
            completed.base + "/cancel",
            json={"client_action_id": cancel_identity, "expected_version": order["version"]},
            headers=ORIGIN,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        revision_future = executor.submit(revise)
        cancel_future = executor.submit(cancel)
        revision, cancellation = revision_future.result(), cancel_future.result()
    assert cancellation.status_code in (200, 409), cancellation.text
    assert revision.status_code in (202, 409), revision.text
    if cancellation.status_code == 409:
        # The revision won the project lock and bumped the root's version.
        # A stale cancellation must refresh, rather than bypass version checks.
        assert revision.status_code == 202
        current = next(
            o
            for o in completed.client.get(f"/api/projects/{completed.project}/work-orders").json()
            if o["id"] == str(completed.order)
        )
        assert current["version"] > order["version"]
        cancellation = completed.client.post(
            completed.base + "/cancel",
            json={"client_action_id": cancel_identity, "expected_version": current["version"]},
            headers=ORIGIN,
        )
    assert cancellation.status_code == 200, cancellation.text
    assert TaskSessionAttempts(completed.gateway).claim_next(completed.project) is None
    assert not any(a["status"] in ("queued", "running") for a in completed.child()["attempts"])
    assert len(completed.root()["children"]) == 1
    if revision.status_code == 202:
        before = completed.child()
        replay = completed.client.post(completed.path, json=payload, headers=ORIGIN)
        assert replay.status_code == 200 and replay.json() == revision.json()
        assert completed.child() == before
