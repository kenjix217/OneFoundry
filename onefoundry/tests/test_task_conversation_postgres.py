"""Persisted task chat with a fake provider; never uses a live account."""

import hashlib
import io
import json
import os
from pathlib import Path
from typing import Any, Literal, cast
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from onefoundry import chief_followup, coordination_autostart
from onefoundry.inference import AuthenticationError, TextRequest, TextResult, TransportError
from onefoundry.inference.subscription_codex import CodexStreamFailure, CodexStreamIncomplete
from onefoundry.input_file_bytes import InputFileBytes
from onefoundry.input_file_extract import ExtractionResult
from onefoundry.input_file_worker import process_one
from onefoundry.main import create_app
from onefoundry.model_providers import DiscoveredModel
from onefoundry.settings import Settings
from onefoundry.tool_gateway import ToolGateway

ORIGIN = {"origin": "http://localhost:8765"}


class FakeTransport:
    def __init__(self, results: list[TextResult | Exception]) -> None:
        self.results = results
        self.calls: list[TextRequest] = []

    def generate(self, request: TextRequest) -> TextResult:
        self.calls.append(request)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_task_chat_saves_public_updates_while_provider_is_running(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, _profile_id, order_id, base = _setup(client)
        path = f"{base}/work-orders/{order_id}/conversation"
        conversation_id = client.post(path, json={}, headers=ORIGIN).json()["id"]
        observed: list[dict[str, Any]] = []

        class UpdatingTransport:
            def generate(self, request: TextRequest) -> TextResult:
                assert request.timeout_seconds == settings.task_chat_timeout_seconds
                assert request.progress_callback is not None
                request.progress_callback("I will check the assignment and evidence.")
                request.progress_callback("token=secret-value")
                observed.append(client.get(path).json())
                return TextResult(
                    '{"reply":"The task has no assigned producer yet.","decision":"none"}',
                    "response-progress",
                    40,
                    20,
                    "completed",
                )

        with patch(
            "onefoundry.task_conversation_store._provider_transport", return_value=UpdatingTransport()
        ):
            response = client.post(
                path + "/turns",
                json={
                    "conversation_id": conversation_id,
                    "client_turn_id": str(uuid4()),
                    "expected_sequence": 0,
                    "text": "What is the assignment status?",
                },
                headers=ORIGIN,
            )
        assert response.status_code == 201, response.text
        running = observed[0]["turns"][0]
        assert running["status"] == "running"
        assert running["progress_updates"] == ["I will check the assignment and evidence."]
        saved = client.get(path).json()["turns"][0]
        assert saved["progress_updates"] == running["progress_updates"]
        with create_engine(settings.database_url).connect() as connection:
            events: Any = (
                connection.execute(
                    text(
                        "SELECT payload FROM events WHERE project_id=:p "
                        "AND event_type='task_conversation_progress'"
                    ),
                    {"p": UUID(project_id)},
                )
                .scalars()
                .all()
            )
        assert len(events) == 1
        assert "secret-value" not in json.dumps(events)


def _settings(
    tmp_path: Path, max_turns: int = 100, mode: Literal["off", "draft_only", "auto"] = "off"
) -> Settings:
    root = tmp_path / "model-secrets"
    root.mkdir(mode=0o700, exist_ok=True)
    input_root = tmp_path / "input-files"
    input_root.mkdir(mode=0o700, exist_ok=True)
    return Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(root),
        input_file_root=str(input_root),
        task_conversation_max_turns=max_turns,
        coordination_mode=mode,
        onefoundry_coordinator_poll_seconds=30 if mode == "auto" else 5,
    )


def _client(tmp_path: Path, max_turns: int = 100) -> TestClient:
    return TestClient(create_app(_settings(tmp_path, max_turns)), base_url="http://localhost:8765")


def _setup(client: TestClient) -> tuple[str, str, str, str]:
    project = client.post("/api/projects", json={"name": "Task conversation"}, headers=ORIGIN).json()
    project_id = project["id"]
    base = f"/api/projects/{project_id}"
    account = client.post(
        f"{base}/model/accounts",
        json={"provider": "openai", "label": "Fake account", "api_key": "test-only-api-key"},
        headers=ORIGIN,
    )
    assert account.status_code == 201, account.text
    account_id = account.json()["id"]
    with patch(
        "onefoundry.model_gateway.discover_models",
        return_value=[DiscoveredModel("gpt-test", "GPT Test", "text")],
    ):
        assert client.post(f"{base}/model/accounts/{account_id}/refresh", headers=ORIGIN).status_code == 200
    profile = client.post(
        f"{base}/profiles",
        json={
            "name": "Advisor",
            "instructions": "Answer the operator plainly.",
            "model": "gpt-test",
            "model_account_id": account_id,
        },
        headers=ORIGIN,
    )
    assert profile.status_code == 201, profile.text
    profile_id = profile.json()["id"]
    order = client.post(
        f"{base}/work-orders",
        json={"title": "Plan outreach", "instructions": "Help with a plan", "profile_id": profile_id},
        headers=ORIGIN,
    )
    assert order.status_code == 201, order.text
    return project_id, profile_id, order.json()["id"], base


def _send_decision(
    client: TestClient, path: str, conversation_id: str, sequence: int, fake: FakeTransport, message: str
) -> dict[str, Any]:
    with patch("onefoundry.task_conversation_store._provider_transport", return_value=fake):
        response = client.post(
            path + "/turns",
            json={
                "conversation_id": conversation_id,
                "client_turn_id": str(uuid4()),
                "expected_sequence": sequence,
                "text": message,
            },
            headers=ORIGIN,
        )
    assert response.status_code == 201, response.text
    return cast(dict[str, Any], response.json()["turn"])


def test_invalid_team_proposal_is_readable_and_creates_no_work(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _project_id, _profile_id, order_id, base = _setup(client)
        path = f"{base}/work-orders/{order_id}/conversation"
        conversation_id = client.post(path, json={}, headers=ORIGIN).json()["id"]
        proposal = {
            "reply": "I'm starting Researcher and QA now.",
            "decision": "delegate_team",
            "team": [
                {
                    "work_order": {
                        "title": "Review the product",
                        "deliverable": "A source-backed feature list",
                        "acceptance_checks": ["Evidence recorded"],
                    },
                    "autonomy": "manual",
                }
            ],
        }
        turn = _send_decision(
            client,
            path,
            conversation_id,
            0,
            FakeTransport([TextResult(json.dumps(proposal), "invalid-team", 50, 20, "completed")]),
            "Coordinate the review team.",
        )
        assert "I'm starting" not in turn["assistant_text"]
        assert "rejected" in turn["assistant_text"]
        assert turn["automation_decision"]["status"] == "rejected"
        assert turn["automation_decision"]["error_code"] == "invalid_action_proposal"
        assert {issue["field"] for issue in turn["automation_decision"]["validation_errors"]} >= {
            "team.0.work_order.instructions",
            "team.0.work_order.profile_id",
            "team.0.autonomy",
        }
        assert turn["automation_decision"]["assignments"] == []
        assert len(client.get(f"{base}/work-orders").json()) == 1


def test_chief_chat_assigns_agent_who_creates_rule_on_its_run(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, agent_id, _order_id, base = _setup(client)
        profiles = client.get(f"{base}/profiles").json()
        chief = next(item for item in profiles if item["name"] == "Chief of Staff")
        agent = next(item for item in profiles if item["id"] == agent_id)
        patched = client.patch(
            f"{base}/profiles/{chief['id']}",
            json={"model": agent["model"], "model_account_id": agent["model_account_id"]},
            headers=ORIGIN,
        )
        assert patched.status_code == 200, patched.text
        chief_order = client.post(
            f"{base}/work-orders",
            json={
                "title": "Coordinate research",
                "instructions": "Assign recurring research",
                "profile_id": chief["id"],
            },
            headers=ORIGIN,
        )
        assert chief_order.status_code == 201, chief_order.text
        path = f"{base}/work-orders/{chief_order.json()['id']}/conversation"
        conversation = client.post(path, json={}, headers=ORIGIN)
        assert conversation.status_code == 201, conversation.text
        fake = FakeTransport(
            [
                TextResult(
                    json.dumps(
                        {
                            "reply": "I will assign the recurring brief to Advisor.",
                            "decision": "delegate_automation",
                            "delegate_autonomy": "auto_start",
                            "delegate": {
                                "profile_id": agent_id,
                                "title": "Set up a recurring brief",
                                "instructions": (
                                    "Decide whether an hourly competitor brief needs an Automation rule."
                                ),
                            },
                        }
                    ),
                    "chief-delegate",
                    10,
                    7,
                    "completed",
                )
            ]
        )
        turn = _send_decision(
            client, path, conversation.json()["id"], 0, fake, "Ask Advisor to set up an hourly brief."
        )
        decision = turn["automation_decision"]
        assert decision["status"] == "saved" and decision["decision"] == "delegate_automation"
        child_id = decision["assigned_work_order_id"]
        assert "has not run yet" in turn["assistant_text"]
        child = next(item for item in client.get(f"{base}/work-orders").json() if item["id"] == child_id)
        assert child["status"] == "draft" and child["profile_id"] == agent_id
        assert child["parent_work_order_id"] == chief_order.json()["id"]
        assert client.post(f"{base}/work-orders/{child_id}/ready", headers=ORIGIN).status_code == 200
        agent_reply = FakeTransport(
            [
                TextResult(
                    json.dumps(
                        {
                            "reply": "I will maintain the hourly brief.",
                            "decision": "create_rule",
                            "create": {
                                "name": "Hourly competitor brief",
                                "signal_type": "schedule.fired",
                                "schedule": {"kind": "interval", "every_hours": 1},
                                "target_profile_id": agent_id,
                                "title": "Write competitor brief",
                                "instructions": "Summarize competitor developments.",
                            },
                        }
                    ),
                    "assigned-agent-rule",
                    12,
                    8,
                    "completed",
                )
            ]
        )
        with patch("onefoundry.live_text_store._provider_transport", return_value=agent_reply):
            run = client.post(f"{base}/work-orders/{child_id}/live-text-run", headers=ORIGIN)
        assert run.status_code == 201, run.text
        assert run.json()["output"]["automation_decision"]["status"] == "saved"
        rules = client.get(f"{base}/coordination/rules").json()
        assert len(rules) == 1 and rules[0]["name"] == "Hourly competitor brief"
        assert rules[0]["last_agent_action"]["run_id"] == run.json()["run"]["id"]


def test_chief_auto_assignment_uses_bounded_launcher_to_run_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_background_launcher(_self: object) -> None:
        return None

    # This test drives the real owner explicitly; a background poll would race
    # its fake transport and consume the same persisted launch first.
    monkeypatch.setattr("onefoundry.coordination_launcher.CoordinationLauncher.start", no_background_launcher)
    settings = _settings(tmp_path, mode="auto")
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, agent_id, _order_id, base = _setup(client)
        profiles = client.get(f"{base}/profiles").json()
        chief = next(item for item in profiles if item["name"] == "Chief of Staff")
        agent = next(item for item in profiles if item["id"] == agent_id)
        patched = client.patch(
            f"{base}/profiles/{chief['id']}",
            json={"model": agent["model"], "model_account_id": agent["model_account_id"]},
            headers=ORIGIN,
        )
        assert patched.status_code == 200, patched.text
        chief_order = client.post(
            f"{base}/work-orders",
            json={"title": "Coordinate brief", "instructions": "Assign Advisor", "profile_id": chief["id"]},
            headers=ORIGIN,
        )
        assert chief_order.status_code == 201, chief_order.text
        path = f"{base}/work-orders/{chief_order.json()['id']}/conversation"
        conversation_id = client.post(path, json={}, headers=ORIGIN).json()["id"]
        decision = FakeTransport(
            [
                TextResult(
                    json.dumps(
                        {
                            "reply": "I will ask Advisor to set up the brief now.",
                            "decision": "delegate_automation",
                            "delegate_autonomy": "auto_start",
                            "delegate": {
                                "profile_id": agent_id,
                                "title": "Set up recurring brief",
                                "instructions": "Decide whether an hourly brief needs a rule and save it.",
                            },
                        }
                    ),
                    "chief-auto-delegate",
                    10,
                    7,
                    "completed",
                )
            ]
        )
        turn = _send_decision(client, path, conversation_id, 0, decision, "Have Advisor set it up now.")
        assert turn["automation_decision"]["status"] == "saved"
        child_id = turn["automation_decision"]["assigned_work_order_id"]
        assert "has not run yet" in turn["assistant_text"]
        child = next(item for item in client.get(f"{base}/work-orders").json() if item["id"] == child_id)
        assert child["status"] == "ready" and child["profile_id"] == agent_id

        engine = create_engine(settings.database_url)
        launcher = ToolGateway(engine, settings.coordination_launcher_actor_id, live_text_settings=settings)
        launcher.reconcile_task_conversations()
        try:
            with engine.connect() as connection:
                queued = (
                    connection.execute(
                        text(
                            "SELECT state,signal_id,rule_id FROM coordination_intents "
                            "WHERE project_id=:p AND action_kind='auto_start' AND work_order_id=:work"
                        ),
                        {"p": UUID(project_id), "work": UUID(child_id)},
                    )
                    .mappings()
                    .one()
                )
            assert queued["state"] == "pending"
            assert queued["signal_id"] is None and queued["rule_id"] is None
            agent_reply = FakeTransport(
                [
                    TextResult(
                        json.dumps(
                            {
                                "reply": "I will keep the hourly brief current.",
                                "decision": "create_rule",
                                "create": {
                                    "name": "Advisor hourly brief",
                                    "signal_type": "schedule.fired",
                                    "schedule": {"kind": "interval", "every_hours": 1},
                                    "target_profile_id": agent_id,
                                    "title": "Write hourly brief",
                                    "instructions": "Summarize new developments.",
                                },
                            }
                        ),
                        "auto-agent-rule",
                        10,
                        7,
                        "completed",
                    )
                ]
            )
            with patch("onefoundry.live_text_store._provider_transport", return_value=agent_reply):
                assert coordination_autostart.step(launcher, settings, UUID(project_id))
                assert not coordination_autostart.step(launcher, settings, UUID(project_id))
            assert len(agent_reply.calls) == 1
            with engine.connect() as connection:
                state = connection.scalar(
                    text(
                        "SELECT state FROM coordination_intents WHERE project_id=:p "
                        "AND action_kind='auto_start' AND work_order_id=:work"
                    ),
                    {"p": UUID(project_id), "work": UUID(child_id)},
                )
            assert state == "done"
            rules = client.get(f"{base}/coordination/rules").json()
            assert len(rules) == 1 and rules[0]["name"] == "Advisor hourly brief"
            assert rules[0]["last_agent_action"]["run_id"]
            with engine.connect() as connection:
                pending = (
                    connection.execute(
                        text(
                            "SELECT state,child_status FROM chief_followup_intents "
                            "WHERE project_id=:p AND child_work_order_id=:child"
                        ),
                        {"p": UUID(project_id), "child": UUID(child_id)},
                    )
                    .mappings()
                    .one()
                )
            assert pending["state"] == "pending" and pending["child_status"] == "needs_review"
            followup_reply = FakeTransport(
                [
                    TextResult(
                        json.dumps({"reply": "The child needs evidence review.", "decision": "none"}),
                        "chief-followup",
                        12,
                        8,
                        "completed",
                    )
                ]
            )
            with patch("onefoundry.task_conversation_store._provider_transport", return_value=followup_reply):
                assert chief_followup.step(launcher, settings, UUID(project_id))
                assert not chief_followup.step(launcher, settings, UUID(project_id))
            assert len(followup_reply.calls) == 1
            assert "Child assignments and current evidence" in followup_reply.calls[0].prompt
            assert "Set up recurring brief" in followup_reply.calls[0].prompt
            assert (
                "Project update: Set up recurring brief is now needs_review" in followup_reply.calls[0].prompt
            )
            assert "Operator: Have Advisor set it up now." in followup_reply.calls[0].prompt
            chat = client.get(path).json()
            assert len(chat["turns"]) == 2
            assert chat["turns"][1]["source_kind"] == "system_followup"
            assert chat["turns"][1]["automation_decision"]["status"] == "none"
            with engine.connect() as connection:
                finished = (
                    connection.execute(
                        text(
                            "SELECT state,turn_id FROM chief_followup_intents "
                            "WHERE project_id=:p AND child_work_order_id=:child"
                        ),
                        {"p": UUID(project_id), "child": UUID(child_id)},
                    )
                    .mappings()
                    .one()
                )
            assert finished["state"] == "done"
            assert str(finished["turn_id"]) == chat["turns"][1]["id"]

            newer = client.post(
                f"{base}/work-orders/{chief_order.json()['id']}/conversations",
                json={"id": str(uuid4()), "title": "Separate Chief chat"},
                headers=ORIGIN,
            )
            assert newer.status_code == 201, newer.text
            current_child = next(
                item for item in client.get(f"{base}/work-orders").json() if item["id"] == child_id
            )
            accepted = client.post(
                f"{base}/work-orders/{child_id}/accept",
                json={"expected_version": current_child["version"], "reason": "Reviewed the brief."},
                headers=ORIGIN,
            )
            assert accepted.status_code == 200, accepted.text
            assert chief_followup.step(launcher, settings, UUID(project_id))
            assert (
                client.get(
                    f"{base}/work-orders/{chief_order.json()['id']}/conversations/{newer.json()['id']}"
                ).json()["turns"]
                == []
            )
            with engine.connect() as connection:
                later = (
                    connection.execute(
                        text(
                            "SELECT state,error_code FROM chief_followup_intents "
                            "WHERE project_id=:p AND child_work_order_id=:child "
                            "AND child_status='done'"
                        ),
                        {"p": UUID(project_id), "child": UUID(child_id)},
                    )
                    .mappings()
                    .one()
                )
            assert dict(later) == {"state": "blocked", "error_code": "source_conversation_inactive"}

            engineer_id = next(item["id"] for item in profiles if item["name"] == "Engineer")
            coding_decision = FakeTransport(
                [
                    TextResult(
                        json.dumps(
                            {
                                "reply": "I assigned implementation to Engineer.",
                                "decision": "delegate_automation",
                                "delegate_autonomy": "auto_start",
                                "delegate": {
                                    "profile_id": engineer_id,
                                    "title": "Implement brief dashboard",
                                    "instructions": "Implement and test the dashboard in a coding workspace.",
                                    "deliverable_kind": "code_change",
                                },
                            }
                        ),
                        "chief-coding-delegate",
                        12,
                        8,
                        "completed",
                    )
                ]
            )
            coding_turn = _send_decision(
                client,
                path,
                newer.json()["id"],
                0,
                coding_decision,
                "Have Engineer implement the dashboard.",
            )
            assert coding_turn["automation_decision"]["status"] == "saved"
            assert "No live-text launch was queued" in coding_turn["assistant_text"]
            coding_child_id = coding_turn["automation_decision"]["assigned_work_order_id"]
            coding_child = next(
                item for item in client.get(f"{base}/work-orders").json() if item["id"] == coding_child_id
            )
            assert coding_child["status"] == "ready" and coding_child["deliverable_kind"] == "code_change"
            with engine.connect() as connection:
                launches = connection.scalar(
                    text(
                        "SELECT count(*) FROM coordination_intents WHERE project_id=:p "
                        "AND work_order_id=:work AND action_kind='auto_start'"
                    ),
                    {"p": UUID(project_id), "work": UUID(coding_child_id)},
                )
            assert launches == 0

            # A team cutover must not launch the old one-follow-up-per-child path.
            team_path = f"{base}/work-orders/{chief_order.json()['id']}/team-session"
            opened = client.post(team_path, headers=ORIGIN)
            assert opened.status_code == 201, opened.text
            bootstrap_url = settings.database_url.set(
                username=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"]
            )
            bootstrap = create_engine(bootstrap_url)
            try:
                with bootstrap.begin() as connection:
                    connection.execute(
                        text("UPDATE task_sessions SET execution_mode='team' WHERE project_id=:p AND id=:s"),
                        {"p": UUID(project_id), "s": UUID(opened.json()["session"]["id"])},
                    )
                    connection.execute(
                        text(
                            "UPDATE chief_followup_intents SET state='pending',error_code=NULL "
                            "WHERE project_id=:p AND child_work_order_id=:child AND child_status='done'"
                        ),
                        {"p": UUID(project_id), "child": UUID(child_id)},
                    )
                assert chief_followup.step(launcher, settings, UUID(project_id))
                with engine.connect() as connection:
                    old_intent = (
                        connection.execute(
                            text(
                                "SELECT state,error_code FROM chief_followup_intents "
                                "WHERE project_id=:p AND child_work_order_id=:child "
                                "AND child_status='done'"
                            ),
                            {"p": UUID(project_id), "child": UUID(child_id)},
                        )
                        .mappings()
                        .one()
                    )
                assert dict(old_intent) == {"state": "blocked", "error_code": "team_session_active"}
                with bootstrap.begin() as connection:
                    connection.execute(
                        text(
                            "UPDATE work_orders SET status='needs_review',version=version+1 "
                            "WHERE project_id=:p AND id=:child"
                        ),
                        {"p": UUID(project_id), "child": UUID(child_id)},
                    )
                with engine.begin() as connection:
                    before = connection.scalar(
                        text(
                            "SELECT count(*) FROM chief_followup_intents "
                            "WHERE project_id=:p AND child_work_order_id=:child"
                        ),
                        {"p": UUID(project_id), "child": UUID(child_id)},
                    )
                    chief_followup.enqueue(launcher, connection, UUID(project_id), UUID(child_id))
                    after = connection.scalar(
                        text(
                            "SELECT count(*) FROM chief_followup_intents "
                            "WHERE project_id=:p AND child_work_order_id=:child"
                        ),
                        {"p": UUID(project_id), "child": UUID(child_id)},
                    )
                assert before == after
            finally:
                bootstrap.dispose()
        finally:
            engine.dispose()


def test_chief_chat_saves_multiple_turn_bound_assignments_atomically(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        project_id, agent_id, _order_id, base = _setup(client)
        profiles = client.get(f"{base}/profiles").json()
        chief = next(item for item in profiles if item["name"] == "Chief of Staff")
        qa = next(item for item in profiles if item["name"] == "QA")
        advisor = next(item for item in profiles if item["id"] == agent_id)
        assert (
            client.patch(
                f"{base}/profiles/{chief['id']}",
                json={"model": advisor["model"], "model_account_id": advisor["model_account_id"]},
                headers=ORIGIN,
            ).status_code
            == 200
        )
        parent = client.post(
            f"{base}/work-orders",
            json={
                "title": "Deliver demo",
                "instructions": "Coordinate a reviewed demo",
                "profile_id": chief["id"],
            },
            headers=ORIGIN,
        ).json()
        path = f"{base}/work-orders/{parent['id']}/conversation"
        conversation_id = client.post(path, json={}, headers=ORIGIN).json()["id"]
        team: list[dict[str, Any]] = [
            {
                "work_order": {
                    "profile_id": agent_id,
                    "title": "Research product claims",
                    "instructions": "Save evidence for the demo claims.",
                    "deliverable_kind": "document",
                    "acceptance_checks": ["Sources are linked"],
                },
                "autonomy": "auto_start",
            },
            {
                "work_order": {
                    "profile_id": qa["id"],
                    "title": "Review demo draft",
                    "instructions": "Check a playable draft and record findings.",
                    "deliverable_kind": "operator_review",
                }
            },
        ]
        fake = FakeTransport(
            [
                TextResult(
                    json.dumps(
                        {"reply": "I assigned research and QA.", "decision": "delegate_team", "team": team}
                    ),
                    "chief-team",
                    20,
                    10,
                    "completed",
                )
            ]
        )
        turn = _send_decision(client, path, conversation_id, 0, fake, "Assign the team to finish the demo.")
        receipt = turn["automation_decision"]
        assert receipt["status"] == "saved" and receipt["decision"] == "delegate_team"
        assert [item["title"] for item in receipt["assignments"]] == [
            "Research product claims",
            "Review demo draft",
        ]
        assert all(item["status"] == "draft" for item in receipt["assignments"])
        assert "0 queued" in turn["assistant_text"]
        engine = create_engine(
            Settings(
                db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
                db_password=os.environ["APP_DB_PASSWORD"],
            ).database_url
        )
        try:
            with engine.connect() as connection:
                linked = (
                    connection.execute(
                        text(
                            "SELECT l.position,l.work_order_id,d.turn_id FROM task_assignment_links l "
                            "JOIN task_automation_decisions d ON d.project_id=l.project_id "
                            "AND d.id=l.decision_id "
                            "WHERE l.project_id=:p ORDER BY l.position"
                        ),
                        {"p": UUID(project_id)},
                    )
                    .mappings()
                    .all()
                )
                assert len(linked) == 2
                assert [row["position"] for row in linked] == [1, 2]
                assert all(str(row["turn_id"]) == turn["id"] for row in linked)
                assert {str(row["work_order_id"]) for row in linked} == {
                    item["id"] for item in receipt["assignments"]
                }
        finally:
            engine.dispose()
        refreshed = client.get(path).json()
        assert len(refreshed["turns"][0]["automation_decision"]["assignments"]) == 2

        bad = FakeTransport(
            [
                TextResult(
                    json.dumps(
                        {
                            "reply": "I will assign two more.",
                            "decision": "delegate_team",
                            "team": [
                                {
                                    "work_order": {
                                        "profile_id": agent_id,
                                        "title": "Write script",
                                        "instructions": "Write the script.",
                                    }
                                },
                                {
                                    "work_order": {
                                        "profile_id": str(uuid4()),
                                        "title": "Capture video",
                                        "instructions": "Save a playable video.",
                                    }
                                },
                            ],
                        }
                    ),
                    "chief-team-invalid",
                    20,
                    10,
                    "completed",
                )
            ]
        )
        rejected = _send_decision(client, path, conversation_id, 1, bad, "Assign script and capture.")
        assert rejected["automation_decision"]["status"] == "rejected"
        assert rejected["automation_decision"]["assignments"] == []
        repeated = FakeTransport(
            [
                TextResult(
                    json.dumps(
                        {
                            "reply": "I will assign research again.",
                            "decision": "delegate_automation",
                            "delegate": team[0]["work_order"],
                        }
                    ),
                    "chief-team-repeat",
                    12,
                    7,
                    "completed",
                )
            ]
        )
        duplicate = _send_decision(
            client, path, conversation_id, 2, repeated, "Check the research assignment."
        )
        assert duplicate["automation_decision"]["status"] == "rejected"
        assert "already has a child assignment" in duplicate["assistant_text"]
        children = [
            item
            for item in client.get(f"{base}/work-orders").json()
            if item["parent_work_order_id"] == parent["id"]
        ]
        assert len(children) == 2


def test_non_chief_chat_cannot_assign_automation_to_another_agent(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _project_id, agent_id, order_id, base = _setup(client)
        path = f"{base}/work-orders/{order_id}/conversation"
        conversation = client.post(path, json={}, headers=ORIGIN)
        fake = FakeTransport(
            [
                TextResult(
                    json.dumps(
                        {
                            "reply": "I will assign this to another agent.",
                            "decision": "delegate_automation",
                            "delegate": {
                                "profile_id": agent_id,
                                "title": "Make a daily rule",
                                "instructions": "Create a daily brief rule.",
                            },
                        }
                    ),
                    "non-chief-delegate",
                    8,
                    6,
                    "completed",
                )
            ]
        )
        turn = _send_decision(client, path, conversation.json()["id"], 0, fake, "Assign an agent.")
        assert turn["automation_decision"]["status"] == "rejected"
        assert turn["automation_decision"]["assigned_work_order_id"] is None
        assert "only Chief of Staff" in turn["assistant_text"]
        assert len(client.get(f"{base}/work-orders").json()) == 1


def test_goal_change_and_rule_creation_save_together_or_neither(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _project_id, agent_id, order_id, base = _setup(client)
        path = f"{base}/work-orders/{order_id}/conversation"
        conversation_id = client.post(path, json={}, headers=ORIGIN).json()["id"]
        before = client.get(base).json()
        original_plan = client.get(f"{base}/plan").json()
        brief = {
            "goal": "Monitor competitors",
            "desired_outcome": "An hourly research brief",
            "expected_version": before["version"],
        }
        rule = {
            "name": "Hourly research",
            "signal_type": "schedule.fired",
            "schedule": {"kind": "interval", "every_hours": 1},
            "target_profile_id": agent_id,
            "title": "Prepare research brief",
            "instructions": "Summarize competitor changes.",
        }
        fake = FakeTransport(
            [
                TextResult(
                    json.dumps(
                        {
                            "reply": "I will update the goal and keep the brief current.",
                            "decision": "create_rule",
                            "project_brief": brief,
                            "project_plan": {
                                "body": "Researcher sends the competitor brief every hour.",
                                "expected_version": original_plan["version"],
                            },
                            "create": rule,
                        }
                    ),
                    "goal-rule-pair",
                    10,
                    8,
                    "completed",
                ),
                TextResult(
                    json.dumps(
                        {
                            "reply": "I will change the goal and add the same rule again.",
                            "decision": "create_rule",
                            "project_brief": {
                                **brief,
                                "goal": "Change the goal again",
                                "expected_version": before["version"] + 1,
                            },
                            "project_plan": {
                                "body": "Replace the original plan.",
                                "expected_version": original_plan["version"] + 1,
                            },
                            "create": rule,
                        }
                    ),
                    "duplicate-goal-rule-pair",
                    10,
                    8,
                    "completed",
                ),
            ]
        )
        first = _send_decision(
            client, path, conversation_id, 0, fake, "Change our goal and send an hourly brief."
        )
        assert first["automation_decision"]["status"] == "saved"
        assert first["automation_decision"]["rule_version"] == 1
        assert first["automation_decision"]["project_version"] == before["version"] + 1
        assert first["automation_decision"]["plan_version"] == original_plan["version"] + 1
        assert client.get(base).json()["goal"] == "Monitor competitors"
        assert (
            client.get(f"{base}/plan").json()["body"] == "Researcher sends the competitor brief every hour."
        )
        second = _send_decision(
            client, path, conversation_id, 1, fake, "Change the goal and add that rule again."
        )
        assert second["automation_decision"]["status"] == "rejected"
        assert second["automation_decision"]["project_version"] is None
        assert second["automation_decision"]["plan_version"] is None
        assert len(client.get(f"{base}/coordination/rules").json()) == 1
        assert client.get(base).json()["version"] == before["version"] + 1
        assert client.get(base).json()["goal"] == "Monitor competitors"
        assert (
            client.get(f"{base}/plan").json()["body"] == "Researcher sends the competitor brief every hour."
        )


def test_agent_can_change_project_plan_without_creating_rule(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _project_id, agent_id, order_id, base = _setup(client)
        path = f"{base}/work-orders/{order_id}/conversation"
        conversation_id = client.post(path, json={}, headers=ORIGIN).json()["id"]
        controls = client.get(f"{base}/coordination").json()
        off = client.put(
            f"{base}/coordination/agent-rule-access",
            json={"enabled": False, "expected_version": controls["version"]},
            headers=ORIGIN,
        )
        assert off.status_code == 200
        fake = FakeTransport(
            [
                TextResult(
                    json.dumps(
                        {
                            "reply": "I updated the one-time launch plan.",
                            "decision": "update_project_plan",
                            "project_plan": {
                                "body": "Researcher gathers evidence once; GTM drafts the launch note.",
                                "expected_version": 1,
                            },
                        }
                    ),
                    "plan-only",
                    10,
                    8,
                    "completed",
                )
            ]
        )
        turn = _send_decision(client, path, conversation_id, 0, fake, "Change our launch plan.")
        assert turn["automation_decision"]["status"] == "saved"
        assert turn["automation_decision"]["plan_version"] == 2
        assert turn["automation_decision"]["rule_id"] is None
        saved = client.get(f"{base}/plan").json()
        assert saved["body"] == "Researcher gathers evidence once; GTM drafts the launch note."
        assert saved["actor_id"] == f"profile:{agent_id}"
        assert client.get(f"{base}/coordination/rules").json() == []


def test_agent_revision_refuses_rule_details_truncated_from_context(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _project_id, agent_id, order_id, base = _setup(client)
        original = {
            "name": "Long brief",
            "signal_type": "schedule.fired",
            "schedule": {"kind": "daily", "at": "09:00"},
            "target_profile_id": agent_id,
            "title": "Write a long brief",
            "instructions": "A" * 501,
        }
        saved = client.post(f"{base}/coordination/rules", json=original, headers=ORIGIN)
        assert saved.status_code == 201, saved.text
        rule_id = saved.json()["id"]
        path = f"{base}/work-orders/{order_id}/conversation"
        conversation_id = client.post(path, json={}, headers=ORIGIN).json()["id"]
        fake = FakeTransport(
            [
                TextResult(
                    json.dumps(
                        {
                            "reply": "I will revise it.",
                            "decision": "revise_rule",
                            "rule_id": rule_id,
                            "revise": {**original, "title": "Change long brief", "expected_version": 1},
                        }
                    ),
                    "truncated-rule",
                    10,
                    7,
                    "completed",
                )
            ]
        )
        turn = _send_decision(client, path, conversation_id, 0, fake, "Shorten that long brief.")
        assert '"instructions_complete":false' in fake.calls[0].prompt
        assert turn["automation_decision"]["status"] == "rejected"
        assert "rule details exceed chat context limit" in turn["assistant_text"]
        assert client.get(f"{base}/coordination/rules").json()[0]["version"] == 1


def test_agent_chat_creates_revises_disables_and_reads_back_rule(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, profile_id, order_id, base = _setup(client)
        path = f"{base}/work-orders/{order_id}/conversation"
        conversation_id = client.post(path, json={}, headers=ORIGIN).json()["id"]
        rule = {
            "name": "Weekly competitor brief",
            "signal_type": "schedule.fired",
            "schedule": {"kind": "weekly", "at": "09:00", "weekday": 0},
            "target_profile_id": profile_id,
            "title": "Write competitor brief",
            "instructions": "Summarize competitor developments for the operator.",
        }
        first = _send_decision(
            client,
            path,
            conversation_id,
            0,
            FakeTransport(
                [
                    TextResult(
                        json.dumps(
                            {
                                "reply": "I can set up a weekly brief.",
                                "decision": "create_rule",
                                "create": rule,
                            }
                        ),
                        "rule-create",
                        20,
                        20,
                        "completed",
                    )
                ]
            ),
            "Please brief me every Monday at 9 AM UTC.",
        )
        assert first["source_kind"] == "operator_ui"
        receipt = first["automation_decision"]
        assert receipt["status"] == "saved" and receipt["decision"] == "create_rule"
        rule_id = receipt["rule_id"]
        assert "Automation rule created" in first["assistant_text"]
        listed = client.get(f"{base}/coordination/rules").json()
        assert len(listed) == 1 and listed[0]["id"] == rule_id
        assert listed[0]["last_agent_action"]["turn_id"] == first["id"]
        assert listed[0]["last_agent_action"]["source_kind"] == "operator_ui"
        engine = create_engine(settings.database_url)
        with engine.connect() as connection:
            actor = connection.scalar(
                text("SELECT actor_id FROM coordination_rules WHERE id=:id"), {"id": rule_id}
            )
            assert actor == f"profile:{profile_id}"
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM task_automation_decisions WHERE project_id=:p"),
                    {"p": project_id},
                )
                == 1
            )
        engine.dispose()
        revision_model = FakeTransport(
            [
                TextResult(
                    json.dumps(
                        {
                            "reply": "I will change the brief.",
                            "decision": "revise_rule",
                            "rule_id": rule_id,
                            "revise": {
                                **rule,
                                "title": "Write shorter competitor brief",
                                "expected_version": 1,
                            },
                        }
                    ),
                    "rule-revise",
                    20,
                    20,
                    "completed",
                )
            ]
        )
        revised = _send_decision(
            client,
            path,
            conversation_id,
            1,
            revision_model,
            "Make that weekly brief shorter.",
        )
        assert revised["automation_decision"]["status"] == "saved"
        assert revised["automation_decision"]["rule_version"] == 2
        assert "Summarize competitor developments for the operator." in revision_model.calls[0].prompt
        assert '"instructions_complete":true' in revision_model.calls[0].prompt
        disabled = _send_decision(
            client,
            path,
            conversation_id,
            2,
            FakeTransport(
                [
                    TextResult(
                        json.dumps(
                            {
                                "reply": "I will stop it.",
                                "decision": "disable_rule",
                                "rule_id": rule_id,
                                "expected_version": 2,
                            }
                        ),
                        "rule-disable",
                        20,
                        20,
                        "completed",
                    )
                ]
            ),
            "Stop the weekly brief.",
        )
        assert disabled["automation_decision"]["status"] == "saved"
        latest_rule = client.get(f"{base}/coordination/rules").json()[0]
        assert latest_rule["enabled"] is False
        assert latest_rule["last_agent_action"]["turn_id"] == disabled["id"]
        assert client.get(path).json()["turns"][-1]["automation_decision"]["rule_id"] == rule_id
        stale = _send_decision(
            client,
            path,
            conversation_id,
            3,
            FakeTransport(
                [
                    TextResult(
                        json.dumps(
                            {
                                "reply": "I will change the brief again.",
                                "decision": "revise_rule",
                                "rule_id": rule_id,
                                "revise": {**rule, "expected_version": 1},
                            }
                        ),
                        "stale-rule",
                        12,
                        12,
                        "completed",
                    )
                ]
            ),
            "Make another change to the brief.",
        )
        assert stale["automation_decision"]["status"] == "rejected"
        assert client.get(f"{base}/coordination/rules").json()[0]["version"] == 3


def test_agent_rule_switch_rejection_and_plain_reply_are_visible(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, profile_id, order_id, base = _setup(client)
        path = f"{base}/work-orders/{order_id}/conversation"
        conversation_id = client.post(path, json={}, headers=ORIGIN).json()["id"]
        status = client.get(f"{base}/coordination").json()
        assert status["agent_rules_enabled"] is True
        off = client.put(
            f"{base}/coordination/agent-rule-access",
            json={"enabled": False, "expected_version": status["version"]},
            headers=ORIGIN,
        )
        assert off.status_code == 200 and off.json()["agent_rules_enabled"] is False
        rule = {
            "name": "Daily status",
            "signal_type": "schedule.fired",
            "schedule": {"kind": "daily", "at": "09:00"},
            "target_profile_id": profile_id,
            "title": "Status",
            "instructions": "Write a status update.",
        }
        rejected = _send_decision(
            client,
            path,
            conversation_id,
            0,
            FakeTransport(
                [
                    TextResult(
                        json.dumps(
                            {"reply": "I will set that up.", "decision": "create_rule", "create": rule}
                        ),
                        "blocked-rule",
                        8,
                        8,
                        "completed",
                    )
                ]
            ),
            "Send a status every day.",
        )
        assert rejected["automation_decision"]["status"] == "rejected"
        assert "No project or Automation change was saved" in rejected["assistant_text"]
        assert client.get(f"{base}/coordination/rules").json() == []
        plain = _send_decision(
            client,
            path,
            conversation_id,
            1,
            FakeTransport([TextResult("Here is a one-time answer.", "plain-reply", 8, 8, "completed")]),
            "Answer this once.",
        )
        assert plain["automation_decision"]["status"] == "unavailable"
        assert plain["automation_decision"]["error_code"] == "unstructured_reply"
        assert plain["assistant_text"] == "Here is a one-time answer."
        one_time = _send_decision(
            client,
            path,
            conversation_id,
            2,
            FakeTransport(
                [
                    TextResult(
                        json.dumps(
                            {
                                "reply": "This is a one-time answer.",
                                "decision": "none",
                            }
                        ),
                        "no-rule",
                        8,
                        8,
                        "completed",
                    )
                ]
            ),
            "Do this only once.",
        )
        assert one_time["automation_decision"]["status"] == "none"
        assert client.get(f"{base}/coordination/rules").json() == []


def test_agent_chat_updates_saved_project_goal_with_version(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, _profile_id, order_id, base = _setup(client)
        path = f"{base}/work-orders/{order_id}/conversation"
        conversation_id = client.post(path, json={}, headers=ORIGIN).json()["id"]
        project = client.get(f"{base}").json()
        changed = _send_decision(
            client,
            path,
            conversation_id,
            0,
            FakeTransport(
                [
                    TextResult(
                        json.dumps(
                            {
                                "reply": "I will update the project goal.",
                                "decision": "update_project_goal",
                                "project_brief": {
                                    "goal": "Improve research coverage",
                                    "desired_outcome": "A reliable weekly summary",
                                    "expected_version": project["version"],
                                },
                            }
                        ),
                        "goal-change",
                        16,
                        12,
                        "completed",
                    )
                ]
            ),
            "Change the project goal to improve research coverage and aim for a reliable weekly summary.",
        )
        assert changed["automation_decision"]["status"] == "saved"
        saved = client.get(base).json()
        assert saved["goal"] == "Improve research coverage"
        assert saved["desired_outcome"] == "A reliable weekly summary"
        assert saved["version"] == changed["automation_decision"]["project_version"]


def test_agent_chat_rejects_equivalent_existing_rule(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        _project_id, profile_id, order_id, base = _setup(client)
        existing = {
            "name": "Existing brief",
            "signal_type": "schedule.fired",
            "schedule": {"kind": "daily", "at": "09:00"},
            "target_profile_id": profile_id,
            "title": "Write daily brief",
            "instructions": "Summarize project work.",
        }
        created = client.post(f"{base}/coordination/rules", json=existing, headers=ORIGIN)
        assert created.status_code == 201, created.text
        path = f"{base}/work-orders/{order_id}/conversation"
        conversation_id = client.post(path, json={}, headers=ORIGIN).json()["id"]
        duplicate = _send_decision(
            client,
            path,
            conversation_id,
            0,
            FakeTransport(
                [
                    TextResult(
                        json.dumps(
                            {
                                "reply": "I will add the daily brief.",
                                "decision": "create_rule",
                                "create": {**existing, "name": "Another daily brief"},
                            }
                        ),
                        "duplicate-rule",
                        12,
                        12,
                        "completed",
                    )
                ]
            ),
            "Give me a daily brief at 9 AM.",
        )
        assert duplicate["automation_decision"]["status"] == "rejected"
        assert len(client.get(f"{base}/coordination/rules").json()) == 1


def test_chat_attachment_enters_prompt_and_submission_identity(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, _profile_id, order_id, base = _setup(client)
        file_response = client.post(
            f"{base}/input-files?scope=staged&work_order_id={order_id}",
            content=b"Target rural districts first.",
            headers={**ORIGIN, "X-File-Name": "strategy.md", "Content-Type": "application/octet-stream"},
        )
        assert file_response.status_code == 201, file_response.text
        file_id = file_response.json()["id"]
        engine = create_engine(settings.database_url)
        assert process_one(engine, InputFileBytes(settings.input_file_root), settings)
        engine.dispose()
        path = f"{base}/work-orders/{order_id}/conversation"
        conversation_id = client.post(path, json={}, headers=ORIGIN).json()["id"]
        key = str(uuid4())
        request = {
            "conversation_id": conversation_id,
            "client_turn_id": key,
            "expected_sequence": 0,
            "text": "Draft the plan",
            "attachment_ids": [file_id],
        }
        fake = FakeTransport([TextResult("Draft ready.", "reply", 8, 2, "completed")])
        with patch("onefoundry.task_conversation_store._provider_transport", return_value=fake):
            sent = client.post(path + "/turns", json=request, headers=ORIGIN)
        assert sent.status_code == 201, sent.text
        assert "Target rural districts first." in fake.calls[0].prompt
        assert "strategy.md" in fake.calls[0].prompt
        assert sent.json()["turn"]["attachments"][0]["id"] == file_id
        changed = client.post(path + "/turns", json={**request, "attachment_ids": []}, headers=ORIGIN)
        assert changed.status_code == 409


def test_chat_image_uses_verified_original_and_readback(tmp_path: Path) -> None:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(buffer, format="PNG")
    original = buffer.getvalue()
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, _profile_id, order_id, base = _setup(client)
        upload = client.post(
            f"{base}/input-files?scope=staged&work_order_id={order_id}",
            content=original,
            headers={**ORIGIN, "X-File-Name": "diagram.png", "Content-Type": "application/octet-stream"},
        )
        assert upload.status_code == 201, upload.text
        file_id = upload.json()["id"]
        engine = create_engine(settings.database_url)
        with patch(
            "onefoundry.input_file_worker.extract_input",
            return_value=ExtractionResult(
                "visual",
                "image/png",
                (),
            ),
        ):
            assert process_one(engine, InputFileBytes(settings.input_file_root), settings)
        engine.dispose()
        path = f"{base}/work-orders/{order_id}/conversation"
        conversation_id = client.post(path, json={}, headers=ORIGIN).json()["id"]
        fake = FakeTransport([TextResult("I see a diagram.", "visual-reply", 10, 3, "completed")])
        with patch("onefoundry.task_conversation_store._provider_transport", return_value=fake):
            sent = client.post(
                path + "/turns",
                json={
                    "conversation_id": conversation_id,
                    "client_turn_id": str(uuid4()),
                    "expected_sequence": 0,
                    "text": "Describe it",
                    "attachment_ids": [file_id],
                },
                headers=ORIGIN,
            )
        assert sent.status_code == 201, sent.text
        assert len(fake.calls) == 1
        assert fake.calls[0].images[0].content == original
        assert fake.calls[0].images[0].mime_type == "image/png"
        assert sent.json()["turn"]["used_sections"][0]["kind"] == "visual"
        assert sent.json()["turn"]["used_sections"][0]["chunk_sha256"] == upload.json()["sha256"]


def test_conversation_two_turns_reload_idempotency_scope_and_provenance(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        project_id, profile_id, order_id, base = _setup(client)
        path = f"{base}/work-orders/{order_id}/conversation"
        assert client.get(path).status_code == 404
        opened = client.post(path, json={}, headers=ORIGIN)
        assert opened.status_code == 201 and opened.json()["turns"] == []
        conversation_id = opened.json()["id"]
        assert opened.json()["profile_id"] == profile_id
        assert client.post(path, json={}, headers=ORIGIN).status_code == 200
        fake = FakeTransport(
            [
                TextResult("First answer.", "response-one", 9, 3, "completed"),
                TextResult("Second answer.", "response-two", 18, 4, "completed"),
            ]
        )
        first_key = str(uuid4())
        with patch("onefoundry.task_conversation_store._provider_transport", return_value=fake):
            first = client.post(
                path + "/turns",
                json={
                    "conversation_id": conversation_id,
                    "client_turn_id": first_key,
                    "expected_sequence": 0,
                    "text": "First question",
                },
                headers=ORIGIN,
            )
            assert first.status_code == 201, first.text
            duplicate = client.post(
                path + "/turns",
                json={
                    "conversation_id": conversation_id,
                    "client_turn_id": first_key,
                    "expected_sequence": 0,
                    "text": "First question",
                },
                headers=ORIGIN,
            )
            assert duplicate.status_code == 200
            second = client.post(
                path + "/turns",
                json={
                    "conversation_id": conversation_id,
                    "client_turn_id": str(uuid4()),
                    "expected_sequence": 1,
                    "text": "Follow up",
                },
                headers=ORIGIN,
            )
        assert second.status_code == 201, second.text
        assert len(fake.calls) == 2
        assert "First question" in fake.calls[1].prompt
        assert "First answer." in fake.calls[1].prompt
        assert "Follow up" in fake.calls[1].prompt
        saved = client.get(path).json()
        assert saved["status"] == "open" and len(saved["turns"]) == 2
        assert saved["turns"][0]["client_turn_id"] == first_key
        assert saved["turns"][0]["assistant_actor_id"] == f"profile:{profile_id}"
        assert saved["turns"][0]["assistant_sha256"] == hashlib.sha256(b"First answer.").hexdigest()
        assert saved["turns"][1]["input_tokens"] == 18
        assert client.get(f"{base}/work-orders").json()[0]["status"] == "draft"
        assert (
            client.post(
                path + "/turns",
                json={
                    "conversation_id": conversation_id,
                    "client_turn_id": first_key,
                    "expected_sequence": 0,
                    "text": "Different",
                },
                headers=ORIGIN,
            ).status_code
            == 409
        )
        assert (
            client.post(
                path + "/turns",
                json={
                    "conversation_id": conversation_id,
                    "client_turn_id": first_key,
                    "expected_sequence": 2,
                    "text": "First question",
                },
                headers=ORIGIN,
            ).status_code
            == 409
        )
        assert (
            client.post(
                path + "/turns",
                json={
                    "conversation_id": conversation_id,
                    "client_turn_id": str(uuid4()),
                    "expected_sequence": 0,
                    "text": "Stale",
                },
                headers=ORIGIN,
            ).status_code
            == 409
        )
        other = client.post("/api/projects", json={"name": "Other chat project"}, headers=ORIGIN).json()
        assert (
            client.get(f"/api/projects/{other['id']}/work-orders/{order_id}/conversation").status_code == 404
        )
        assert project_id == saved["project_id"]


def test_conversation_selects_skill_for_current_operator_message(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _project_id, profile_id, order_id, base = _setup(client)
        created = client.post(
            f"{base}/skills",
            json={"name": "Market sizing", "description": "Estimate market size and assumptions"},
            headers=ORIGIN,
        ).json()
        version = client.post(
            f"{base}/skills/{created['id']}/versions",
            json={"content": "# Market sizing\nState assumptions behind the estimate."},
            headers=ORIGIN,
        ).json()
        assert client.get(f"{base}/profiles/{profile_id}/skills").json() == []
        path = f"{base}/work-orders/{order_id}/conversation"
        conversation_id = client.post(path, json={}, headers=ORIGIN).json()["id"]
        fake = FakeTransport([TextResult("Estimate drafted.", "estimate", 8, 3, "completed")])
        with patch("onefoundry.task_conversation_store._provider_transport", return_value=fake):
            response = client.post(
                path + "/turns",
                json={
                    "conversation_id": conversation_id,
                    "client_turn_id": str(uuid4()),
                    "expected_sequence": 0,
                    "text": "Please estimate market size for this product",
                },
                headers=ORIGIN,
            )
        assert response.status_code == 201, response.text
        assert version["content"] in fake.calls[0].prompt
        selected = client.get(path).json()["turns"][0]["selected_skills"]
        assert selected == [
            {
                "skill_id": created["id"],
                "skill_name": "Market sizing",
                "version": version["version"],
                "sha256": version["sha256"],
                "selection_source": "automatic",
            }
        ]


def test_conversation_drops_automatic_skills_to_fit_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SKILL_AUTO_MAX_COUNT", "10")
    monkeypatch.setenv("SKILL_AUTO_MAX_CONTENT_BYTES", "48000")
    monkeypatch.setenv("LIVE_TEXT_MAX_PROMPT_BYTES", "4300")
    with _client(tmp_path) as client:
        _project_id, _profile_id, order_id, base = _setup(client)
        for index in range(10):
            named = client.post(
                f"{base}/skills",
                json={"name": f"Market {index:02d} " + "界" * 110, "description": "Market estimates"},
                headers=ORIGIN,
            )
            assert named.status_code == 201, named.text
            version = client.post(
                f"{base}/skills/{named.json()['id']}/versions",
                json={"content": "# Market\n" + "x" * 191},
                headers=ORIGIN,
            )
            assert version.status_code == 201, version.text
        path = f"{base}/work-orders/{order_id}/conversation"
        conversation_id = client.post(path, json={}, headers=ORIGIN).json()["id"]
        fake = FakeTransport([TextResult("Ready.", "bounded", 8, 3, "completed")])
        with patch("onefoundry.task_conversation_store._provider_transport", return_value=fake):
            sent = client.post(
                path + "/turns",
                json={
                    "conversation_id": conversation_id,
                    "client_turn_id": str(uuid4()),
                    "expected_sequence": 0,
                    "text": "Estimate market size",
                },
                headers=ORIGIN,
            )
        assert sent.status_code == 201, sent.text
        assert 0 < len(sent.json()["turn"]["selected_skills"]) < 10
        assert len(fake.calls[0].prompt.encode()) + len(b"Answer the operator plainly.") <= 4300


def test_conversation_rejects_invalid_and_preserves_unknown_without_retry(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _project_id, _profile_id, order_id, base = _setup(client)
        path = f"{base}/work-orders/{order_id}/conversation"
        assert client.post(path, json={}, headers=ORIGIN).status_code == 201
        conversation_id = client.get(path).json()["id"]
        for value in (" ", "api_key=secret-value", "Bearer abcdefghijklmnopqrstuvwxyz"):
            assert (
                client.post(
                    path + "/turns",
                    json={
                        "conversation_id": conversation_id,
                        "client_turn_id": str(uuid4()),
                        "expected_sequence": 0,
                        "text": value,
                    },
                    headers=ORIGIN,
                ).status_code
                == 422
            )
        fake = FakeTransport([TransportError("lost response")])
        key = str(uuid4())
        with patch("onefoundry.task_conversation_store._provider_transport", return_value=fake):
            unknown = client.post(
                path + "/turns",
                json={
                    "conversation_id": conversation_id,
                    "client_turn_id": key,
                    "expected_sequence": 0,
                    "text": "Ask once",
                },
                headers=ORIGIN,
            )
        assert unknown.status_code == 503 and unknown.json()["turn_id"]
        saved = client.get(path).json()
        assert saved["status"] == "unknown" and saved["turns"][0]["status"] == "unknown"
        assert saved["turns"][0]["client_turn_id"] == key
        assert saved["turns"][0]["reason"] == "provider_outcome_unknown"
        assert (
            client.post(
                path + "/turns",
                json={
                    "conversation_id": conversation_id,
                    "client_turn_id": str(uuid4()),
                    "expected_sequence": 1,
                    "text": "Try again",
                },
                headers=ORIGIN,
            ).status_code
            == 409
        )
        assert len(fake.calls) == 1


@pytest.mark.parametrize(
    ("failure", "expected_status", "http_status", "diagnostic"),
    [
        (CodexStreamFailure("response_failed_usage_limit"), "failed", 502, "response_failed_usage_limit"),
        (
            CodexStreamIncomplete("stream_ended_without_completion"),
            "unknown",
            503,
            "stream_ended_without_completion",
        ),
    ],
)
def test_conversation_records_codex_terminal_diagnostic(
    tmp_path: Path, failure: Exception, expected_status: str, http_status: int, diagnostic: str
) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        _project_id, _profile_id, order_id, base = _setup(client)
        path = f"{base}/work-orders/{order_id}/conversation"
        conversation_id = client.post(path, json={}, headers=ORIGIN).json()["id"]
        fake = FakeTransport([failure])
        with patch("onefoundry.task_conversation_store._provider_transport", return_value=fake):
            response = client.post(
                path + "/turns",
                json={
                    "conversation_id": conversation_id,
                    "client_turn_id": str(uuid4()),
                    "expected_sequence": 0,
                    "text": "Ask once",
                },
                headers=ORIGIN,
            )
        assert response.status_code == http_status
        saved = client.get(path).json()["turns"][0]
        assert saved["status"] == expected_status
        assert saved["diagnostic_code"] == diagnostic
        assert len(fake.calls) == 1


def test_operator_can_acknowledge_unknown_and_continue_same_conversation(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        project_id, _profile_id, order_id, base = _setup(client)
        path = f"{base}/work-orders/{order_id}/conversation"
        conversation_id = client.post(path, json={}, headers=ORIGIN).json()["id"]
        fake = FakeTransport(
            [TransportError("lost response"), TextResult("A new answer.", "response-next", 5, 3, "completed")]
        )
        with patch("onefoundry.task_conversation_store._provider_transport", return_value=fake):
            first = client.post(
                path + "/turns",
                json={
                    "conversation_id": conversation_id,
                    "client_turn_id": str(uuid4()),
                    "expected_sequence": 0,
                    "text": "First question with uncertain result",
                },
                headers=ORIGIN,
            )
            assert first.status_code == 503
            unknown_id = first.json()["turn_id"]
            acknowledge = path + f"/turns/{unknown_id}/acknowledge-unknown"
            assert (
                client.post(path + f"/turns/{uuid4()}/acknowledge-unknown", headers=ORIGIN).status_code == 409
            )
            assert client.get(path).json()["status"] == "unknown"
            accepted = client.post(acknowledge, headers=ORIGIN)
            assert accepted.status_code == 200, accepted.text
            assert accepted.json()["id"] == conversation_id
            assert accepted.json()["status"] == "open"
            assert accepted.json()["turns"][0]["status"] == "unknown"
            assert client.get(f"{base}/work-orders/{order_id}/conversations").json()[0]["status"] == "open"
            assert client.post(acknowledge, headers=ORIGIN).status_code == 200
            later = client.post(
                path + "/turns",
                json={
                    "conversation_id": conversation_id,
                    "client_turn_id": str(uuid4()),
                    "expected_sequence": 1,
                    "text": "New question after acknowledging uncertainty",
                },
                headers=ORIGIN,
            )
        assert later.status_code == 201, later.text
        assert client.post(acknowledge, headers=ORIGIN).status_code == 200
        assert [turn["status"] for turn in client.get(path).json()["turns"]] == ["unknown", "done"]
        assert "First question with uncertain result" not in fake.calls[1].prompt
        assert "New question after acknowledging uncertainty" in fake.calls[1].prompt
        engine = create_engine(_settings(tmp_path).database_url)
        try:
            with engine.connect() as connection:
                count = connection.scalar(
                    text(
                        "SELECT count(*) FROM events WHERE project_id=:project_id "
                        "AND work_order_id=:order_id "
                        "AND event_type='task_conversation_unknown_acknowledged'"
                    ),
                    {"project_id": project_id, "order_id": order_id},
                )
            assert count == 1
        finally:
            engine.dispose()


def test_conversation_definitive_failure_allows_manual_followup(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _project_id, _profile_id, order_id, base = _setup(client)
        path = f"{base}/work-orders/{order_id}/conversation"
        assert client.post(path, json={}, headers=ORIGIN).status_code == 201
        conversation_id = client.get(path).json()["id"]
        fake = FakeTransport(
            [AuthenticationError("rejected"), TextResult("Now connected.", "response-ok", 5, 3, "completed")]
        )
        with patch("onefoundry.task_conversation_store._provider_transport", return_value=fake):
            failed = client.post(
                path + "/turns",
                json={
                    "conversation_id": conversation_id,
                    "client_turn_id": str(uuid4()),
                    "expected_sequence": 0,
                    "text": "First",
                },
                headers=ORIGIN,
            )
            assert failed.status_code == 502 and failed.json()["turn_id"]
            later = client.post(
                path + "/turns",
                json={
                    "conversation_id": conversation_id,
                    "client_turn_id": str(uuid4()),
                    "expected_sequence": 1,
                    "text": "Second",
                },
                headers=ORIGIN,
            )
        assert later.status_code == 201, later.text
        saved = client.get(path).json()
        assert [item["status"] for item in saved["turns"]] == ["failed", "done"]
        assert "First" not in fake.calls[1].prompt


def test_secret_looking_provider_response_id_is_not_saved(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _project_id, _profile_id, order_id, base = _setup(client)
        path = f"{base}/work-orders/{order_id}/conversation"
        conversation_id = client.post(path, json={}, headers=ORIGIN).json()["id"]
        secret_id = "sk-" + "A" * 24
        fake = FakeTransport([TextResult("Safe reply.", secret_id, 4, 2, "completed")])
        with patch("onefoundry.task_conversation_store._provider_transport", return_value=fake):
            response = client.post(
                path + "/turns",
                json={
                    "conversation_id": conversation_id,
                    "client_turn_id": str(uuid4()),
                    "expected_sequence": 0,
                    "text": "Ask",
                },
                headers=ORIGIN,
            )
        assert response.status_code == 503
        assert secret_id not in response.text
        saved = client.get(path)
        assert saved.status_code == 200
        assert secret_id not in saved.text
        assert saved.json()["turns"][0]["status"] == "unknown"


def test_conversation_can_follow_up_after_work_order_completion(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _project_id, _profile_id, order_id, base = _setup(client)
        path = f"{base}/work-orders/{order_id}/conversation"
        assert client.post(f"{base}/work-orders/{order_id}/ready", headers=ORIGIN).status_code == 200
        run = client.post(f"{base}/work-orders/{order_id}/runs", headers=ORIGIN)
        assert run.status_code == 201
        assert (
            client.post(
                f"{base}/runs/{run.json()['id']}/finish", json={"outcome": "done"}, headers=ORIGIN
            ).status_code
            == 200
        )
        assert client.post(path, json={}, headers=ORIGIN).status_code == 201
        conversation_id = client.get(path).json()["id"]
        fake = FakeTransport([TextResult("Follow-up answer.", "response-followup", 4, 3, "completed")])
        with patch("onefoundry.task_conversation_store._provider_transport", return_value=fake):
            response = client.post(
                path + "/turns",
                json={
                    "conversation_id": conversation_id,
                    "client_turn_id": str(uuid4()),
                    "expected_sequence": 0,
                    "text": "What next?",
                },
                headers=ORIGIN,
            )
        assert response.status_code == 201, response.text
        assert client.get(f"{base}/work-orders").json()[0]["status"] == "needs_review"


def test_conversation_can_discuss_blocked_and_unknown_work_orders(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _project_id, _profile_id, order_id, base = _setup(client)
        path = f"{base}/work-orders/{order_id}/conversation"
        order = next(item for item in client.get(f"{base}/work-orders").json() if item["id"] == order_id)
        blocked = client.post(
            f"{base}/work-orders/{order_id}/block",
            json={"reason": "Need operator guidance", "expected_version": order["version"]},
            headers=ORIGIN,
        )
        assert blocked.status_code == 200
        conversation_id = client.post(path, json={}, headers=ORIGIN).json()["id"]
        fake = FakeTransport([TextResult("Clarify the blocker.", "blocked-response", 4, 4, "completed")])
        with patch("onefoundry.task_conversation_store._provider_transport", return_value=fake):
            sent = client.post(
                path + "/turns",
                json={
                    "conversation_id": conversation_id,
                    "client_turn_id": str(uuid4()),
                    "expected_sequence": 0,
                    "text": "What is blocked?",
                },
                headers=ORIGIN,
            )
        assert sent.status_code == 201
        assert client.get(f"{base}/work-orders").json()[0]["status"] == "blocked"
        resumed = client.post(
            f"{base}/work-orders/{order_id}/unblock",
            json={"expected_version": blocked.json()["version"]},
            headers=ORIGIN,
        )
        assert resumed.status_code == 200
        assert client.post(f"{base}/work-orders/{order_id}/ready", headers=ORIGIN).status_code == 200
        run = client.post(f"{base}/work-orders/{order_id}/runs", headers=ORIGIN)
        assert run.status_code == 201
        finished = client.post(
            f"{base}/runs/{run.json()['id']}/finish", json={"outcome": "unknown"}, headers=ORIGIN
        )
        assert finished.status_code == 200
        fake = FakeTransport([TextResult("Inspect saved state.", "unknown-response", 5, 4, "completed")])
        with patch("onefoundry.task_conversation_store._provider_transport", return_value=fake):
            next_turn = client.post(
                path + "/turns",
                json={
                    "conversation_id": conversation_id,
                    "client_turn_id": str(uuid4()),
                    "expected_sequence": 1,
                    "text": "How to resolve unknown?",
                },
                headers=ORIGIN,
            )
        assert next_turn.status_code == 201, next_turn.text
        assert client.get(f"{base}/work-orders").json()[0]["status"] == "unknown"


def test_handoff_keeps_old_history_and_rejects_stale_or_reused_send(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _project_id, profile_id, order_id, base = _setup(client)
        path = f"{base}/work-orders/{order_id}/conversation"
        opened = client.post(path, json={}, headers=ORIGIN)
        conversation_id = opened.json()["id"]
        first_key = str(uuid4())
        fake = FakeTransport([TextResult("Old agent answer.", "old-response", 5, 4, "completed")])
        with patch("onefoundry.task_conversation_store._provider_transport", return_value=fake):
            first = client.post(
                path + "/turns",
                json={
                    "conversation_id": conversation_id,
                    "client_turn_id": first_key,
                    "expected_sequence": 0,
                    "text": "Question for A",
                },
                headers=ORIGIN,
            )
        assert first.status_code == 201, first.text
        profiles = client.get(f"{base}/profiles").json()
        account_id = next(item["model_account_id"] for item in profiles if item["id"] == profile_id)
        next_profile = client.post(
            f"{base}/profiles",
            json={
                "name": "Second advisor",
                "instructions": "Respond plainly.",
                "model": "gpt-test",
                "model_account_id": account_id,
            },
            headers=ORIGIN,
        )
        assert next_profile.status_code == 201
        order = next(item for item in client.get(f"{base}/work-orders").json() if item["id"] == order_id)
        reassigned = client.post(
            f"{base}/work-orders/{order_id}/assign",
            json={"profile_id": next_profile.json()["id"], "expected_version": order["version"]},
            headers=ORIGIN,
        )
        assert reassigned.status_code == 200, reassigned.text
        history = client.get(f"{base}/work-orders/{order_id}/conversations")
        assert history.status_code == 200 and history.json()[0]["profile_id"] == profile_id
        old = client.get(f"{base}/work-orders/{order_id}/conversations/{conversation_id}")
        assert old.status_code == 200 and old.json()["turns"][0]["assistant_text"] == "Old agent answer."
        lookup = client.get(path + f"/turns/by-client/{first_key}")
        assert lookup.status_code == 200 and lookup.json()["conversation_id"] == conversation_id
        assert client.get(path).status_code == 404
        assert (
            client.post(
                path + "s",
                json={"id": conversation_id, "title": "Main chat"},
                headers=ORIGIN,
            ).status_code
            == 409
        )
        new_conversation = client.post(path, json={}, headers=ORIGIN)
        assert new_conversation.status_code == 201
        new_id = new_conversation.json()["id"]
        assert new_id != conversation_id
        stale = client.post(
            path + "/turns",
            json={
                "conversation_id": conversation_id,
                "client_turn_id": str(uuid4()),
                "expected_sequence": 1,
                "text": "Stale A question",
            },
            headers=ORIGIN,
        )
        reused = client.post(
            path + "/turns",
            json={
                "conversation_id": new_id,
                "client_turn_id": first_key,
                "expected_sequence": 0,
                "text": "Question for A",
            },
            headers=ORIGIN,
        )
        assert stale.status_code == reused.status_code == 409
        assert len(fake.calls) == 1


def test_running_turn_blocks_archive_and_reassignment_until_saved(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        project_id, profile_id, order_id, base = _setup(client)
        path = f"{base}/work-orders/{order_id}/conversation"
        conversation_id = client.post(path, json={}, headers=ORIGIN).json()["id"]
        profile = client.post(
            f"{base}/profiles",
            json={
                "name": "Backup",
                "instructions": "Reply.",
                "model": "gpt-test",
                "model_account_id": next(
                    item["model_account_id"]
                    for item in client.get(f"{base}/profiles").json()
                    if item["id"] == profile_id
                ),
            },
            headers=ORIGIN,
        )
        assert profile.status_code == 201
        project = client.get(f"/api/projects/{project_id}").json()
        order = next(item for item in client.get(f"{base}/work-orders").json() if item["id"] == order_id)
        observed: list[int] = []

        class CheckingTransport:
            def generate(self, request: TextRequest) -> TextResult:
                archive = client.post(
                    f"{base}/archive", json={"expected_version": project["version"]}, headers=ORIGIN
                )
                assign = client.post(
                    f"{base}/work-orders/{order_id}/assign",
                    json={"profile_id": profile.json()["id"], "expected_version": order["version"]},
                    headers=ORIGIN,
                )
                observed.extend((archive.status_code, assign.status_code))
                return TextResult("Saved reply.", "saved-reply", 6, 3, "completed")

        with patch(
            "onefoundry.task_conversation_store._provider_transport", return_value=CheckingTransport()
        ):
            sent = client.post(
                path + "/turns",
                json={
                    "conversation_id": conversation_id,
                    "client_turn_id": str(uuid4()),
                    "expected_sequence": 0,
                    "text": "Please answer",
                },
                headers=ORIGIN,
            )
        assert sent.status_code == 201, sent.text
        assert observed == [409, 409]
        assert client.get(path).json()["turns"][0]["status"] == "done"
        archived = client.post(
            f"{base}/archive", json={"expected_version": project["version"]}, headers=ORIGIN
        )
        assert archived.status_code == 200
        assert client.post(path, json={}, headers=ORIGIN).status_code == 409
        assert client.get(path).status_code == 200
        trashed = client.post(
            f"{base}/trash",
            json={"expected_version": archived.json()["version"], "confirm_name": project["name"]},
            headers=ORIGIN,
        )
        assert trashed.status_code == 200
        assert (
            client.post(
                path + "/turns",
                json={
                    "conversation_id": conversation_id,
                    "client_turn_id": str(uuid4()),
                    "expected_sequence": 1,
                    "text": "After Trash",
                },
                headers=ORIGIN,
            ).status_code
            == 409
        )


def test_history_window_does_not_cap_turns_and_terminal_digest_cannot_mutate(tmp_path: Path) -> None:
    with _client(tmp_path, max_turns=1) as client:
        project_id, _profile_id, order_id, base = _setup(client)
        path = f"{base}/work-orders/{order_id}/conversation"
        conversation_id = client.post(path, json={}, headers=ORIGIN).json()["id"]
        fake = FakeTransport(
            [
                TextResult("Cap answer.", "cap-response", 3, 2, "completed"),
                TextResult("Next answer.", "next-response", 3, 2, "completed"),
            ]
        )
        with patch("onefoundry.task_conversation_store._provider_transport", return_value=fake):
            sent = client.post(
                path + "/turns",
                json={
                    "conversation_id": conversation_id,
                    "client_turn_id": str(uuid4()),
                    "expected_sequence": 0,
                    "text": "One",
                },
                headers=ORIGIN,
            )
        assert sent.status_code == 201, sent.text
        turn = sent.json()["turn"]
        assert turn["assistant_sha256"] == hashlib.sha256(b"Cap answer.").hexdigest()
        with patch("onefoundry.task_conversation_store._provider_transport", return_value=fake):
            capped = client.post(
                path + "/turns",
                json={
                    "conversation_id": conversation_id,
                    "client_turn_id": str(uuid4()),
                    "expected_sequence": 1,
                    "text": "Two",
                },
                headers=ORIGIN,
            )
        assert capped.status_code == 201, capped.text
        assert len(fake.calls) == 2
        assert capped.json()["conversation"]["turns"][-1]["sequence"] == 2
        assert capped.json()["conversation"]["turns_omitted"] == 1
        engine = create_engine(_settings(tmp_path, 1).database_url)
        try:
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "UPDATE task_conversation_turns SET assistant_text='changed' "
                            "WHERE project_id=:project_id AND id=:turn_id"
                        ),
                        {"project_id": project_id, "turn_id": turn["id"]},
                    )
            except SQLAlchemyError:
                pass
            else:
                raise AssertionError("completed turn was mutable")
        finally:
            engine.dispose()
        page = client.get(f"{base}/work-orders/{order_id}/conversations/{conversation_id}?before_sequence=2")
        assert page.status_code == 200, page.text
        assert page.json()["turns"][0]["assistant_text"] == "Cap answer."


def test_startup_reconciles_committed_pending_turn_without_provider_call(tmp_path: Path) -> None:
    from uuid import UUID

    from onefoundry.model_secrets import ModelSecretStore
    from onefoundry.task_conversation_schemas import TurnSend
    from onefoundry.task_conversation_store import TaskConversationStore
    from onefoundry.tool_gateway import ToolGateway

    settings = _settings(tmp_path)
    with _client(tmp_path) as client:
        project_id, _profile_id, order_id, base = _setup(client)
        path = f"{base}/work-orders/{order_id}/conversation"
        conversation_id = client.post(path, json={}, headers=ORIGIN).json()["id"]
        engine = create_engine(settings.database_url)
        try:
            gateway = ToolGateway(engine, settings.operator_actor_id, live_text_settings=settings)
            store = TaskConversationStore(gateway, settings, ModelSecretStore(settings.model_secret_root))
            prepared, _ = store._prepare(
                UUID(project_id),
                UUID(order_id),
                TurnSend(
                    conversation_id=UUID(conversation_id),
                    client_turn_id=uuid4(),
                    expected_sequence=0,
                    text="Pending before process stopped",
                ),
            )
            assert prepared is not None
        finally:
            engine.dispose()
    original_reconcile = ToolGateway.reconcile_task_conversations
    attempts = 0

    def fail_then_recover(gateway: ToolGateway) -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise SQLAlchemyError("temporary database failure")
        original_reconcile(gateway)

    with (
        patch.object(ToolGateway, "reconcile_task_conversations", fail_then_recover),
        patch(
            "onefoundry.task_conversation_store._provider_transport",
            side_effect=AssertionError("provider called during reconciliation"),
        ) as provider,
    ):
        with _client(tmp_path) as restarted:
            pending_open = restarted.post(path, json={}, headers=ORIGIN)
            pending_send = restarted.post(
                path + "/turns",
                json={
                    "conversation_id": conversation_id,
                    "client_turn_id": str(uuid4()),
                    "expected_sequence": 1,
                    "text": "Must wait for reconciliation",
                },
                headers=ORIGIN,
            )
            assert pending_open.status_code == pending_send.status_code == 503
            assert attempts == 1
            assert restarted.get("/health/ready").status_code == 503
            assert restarted.post(path, json={}, headers=ORIGIN).status_code == 503
            assert restarted.get("/health/ready").status_code == 200
            assert restarted.post(path, json={}, headers=ORIGIN).status_code == 200
            saved = restarted.get(path).json()
            assert attempts == 3
            assert saved["status"] == "unknown"
            assert saved["turns"][0]["reason"] == "process_interrupted"
            assert (
                restarted.post(
                    path + "/turns",
                    json={
                        "conversation_id": conversation_id,
                        "client_turn_id": str(uuid4()),
                        "expected_sequence": 1,
                        "text": "Retry?",
                    },
                    headers=ORIGIN,
                ).status_code
                == 409
            )
        provider.assert_not_called()


def test_named_chats_keep_separate_history_and_idempotent_creation(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        project_id, profile_id, order_id, base = _setup(client)
        path = f"{base}/work-orders/{order_id}/conversation"
        first = client.post(path, json={}, headers=ORIGIN)
        assert first.status_code == 201, first.text
        first_id = first.json()["id"]
        fake = FakeTransport(
            [
                TextResult("First answer", "first-reply", 8, 2, "completed"),
                TextResult("Second answer", "second-reply", 8, 2, "completed"),
            ]
        )
        with patch("onefoundry.task_conversation_store._provider_transport", return_value=fake):
            first_send = client.post(
                path + "/turns",
                json={
                    "conversation_id": first_id,
                    "client_turn_id": str(uuid4()),
                    "expected_sequence": 0,
                    "text": "First topic",
                },
                headers=ORIGIN,
            )
            assert first_send.status_code == 201, first_send.text
            chat_id = str(uuid4())
            chat_data = {"id": chat_id, "title": "Second topic"}
            second = client.post(path + "s", json=chat_data, headers=ORIGIN)
            assert second.status_code == 201, second.text
            assert second.json()["id"] == chat_id
            assert second.json()["is_active"] is True
            assert second.json()["turns"] == []
            again = client.post(path + "s", json=chat_data, headers=ORIGIN)
            assert again.status_code == 200 and again.json()["id"] == chat_id
            assert client.get(path).json()["id"] == chat_id
            old = client.get(path + f"s/{first_id}")
            assert old.status_code == 200 and old.json()["is_active"] is False
            assert old.json()["turns"][0]["operator_text"] == "First topic"
            assert (
                client.post(
                    path + "/turns",
                    json={
                        "conversation_id": first_id,
                        "client_turn_id": str(uuid4()),
                        "expected_sequence": 1,
                        "text": "Stale chat",
                    },
                    headers=ORIGIN,
                ).status_code
                == 409
            )
            second_send = client.post(
                path + "/turns",
                json={
                    "conversation_id": chat_id,
                    "client_turn_id": str(uuid4()),
                    "expected_sequence": 0,
                    "text": "Second topic question",
                },
                headers=ORIGIN,
            )
            assert second_send.status_code == 201, second_send.text
        assert "First topic" not in fake.calls[1].prompt
        assert "Second topic question" in fake.calls[1].prompt
        assert "briefly state your plan" in fake.calls[1].instructions
        assert "Do not reveal private reasoning" in fake.calls[1].instructions
        listed = client.get(path + "s").json()
        assert [(item["id"], item["is_active"]) for item in listed] == [(first_id, False), (chat_id, True)]
        assert all(item["profile_id"] == profile_id for item in listed)
        other = client.post("/api/projects", json={"name": "Other thread scope"}, headers=ORIGIN).json()
        assert (
            client.get(
                f"/api/projects/{other['id']}/work-orders/{order_id}/conversations/{chat_id}"
            ).status_code
            == 404
        )
        assert project_id != other["id"]


def test_default_legacy_chat_exceeds_100_turns_restart_exact_replay_and_unknown_recovery(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    first_request: dict[str, Any] = {}
    fake = FakeTransport(
        [TextResult(f"Saved checkpoint {n}.", f"checkpoint-{n}", 3, 2, "completed") for n in range(1, 106)]
    )
    with patch("onefoundry.task_conversation_store._provider_transport", return_value=fake):
        with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
            project, _, order, base = _setup(client)
            path = f"{base}/work-orders/{order}/conversation"
            assert client.get(f"{base}/work-orders/{order}/team-session").status_code == 404
            conversation = client.post(path, json={}, headers=ORIGIN).json()["id"]
            for sequence in range(55):
                data = {
                    "conversation_id": conversation,
                    "client_turn_id": str(uuid4()),
                    "expected_sequence": sequence,
                    "text": f"Checkpoint {sequence + 1}",
                }
                if sequence == 0:
                    first_request = data
                sent = client.post(path + "/turns", json=data, headers=ORIGIN)
                assert sent.status_code == 201, sent.text
        with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
            assert client.get(path).json()["latest_sequence"] == 55
            for sequence in range(55, 105):
                sent = client.post(
                    path + "/turns",
                    json={
                        "conversation_id": conversation,
                        "client_turn_id": str(uuid4()),
                        "expected_sequence": sequence,
                        "text": f"Checkpoint {sequence + 1}",
                    },
                    headers=ORIGIN,
                )
                assert sent.status_code == 201, sent.text
            current = client.get(path).json()
            assert current["latest_sequence"] == 105
            assert current["turns_omitted"] == 5 and len(current["turns"]) == 100
            assert current["turns"][0]["sequence"] == 6
            replay = client.post(path + "/turns", json=first_request, headers=ORIGIN)
            assert replay.status_code == 200, replay.text
            assert replay.json()["turn"]["sequence"] == 1 and len(fake.calls) == 105
            page_path = f"{base}/work-orders/{order}/conversations/{conversation}"
            page = client.get(page_path + "?before_sequence=6")
            assert page.status_code == 200, page.text
            assert [t["sequence"] for t in page.json()["turns"]] == [1, 2, 3, 4, 5]
            assert page.json()["latest_sequence"] == 105 and page.json()["turns_omitted"] == 0
            assert client.get(page_path + "?before_sequence=0").status_code == 422
            assert (
                client.get(
                    f"{base}/work-orders/{uuid4()}/conversations/{conversation}?before_sequence=6"
                ).status_code
                == 404
            )
            fake.results.extend(
                [
                    AuthenticationError("rejected"),
                    CodexStreamIncomplete("stream_ended_without_completion"),
                    TextResult("Recovered checkpoint.", "recovered", 3, 2, "completed"),
                ]
            )
            for sequence, expected in ((105, 502), (106, 503)):
                sent = client.post(
                    path + "/turns",
                    json={
                        "conversation_id": conversation,
                        "client_turn_id": str(uuid4()),
                        "expected_sequence": sequence,
                        "text": f"Recovery checkpoint {sequence + 1}",
                    },
                    headers=ORIGIN,
                )
                assert sent.status_code == expected, sent.text
            unknown = client.get(path).json()
            assert unknown["status"] == "unknown" and unknown["latest_sequence"] == 107
            assert (
                client.post(
                    path + "/turns",
                    json={
                        "conversation_id": conversation,
                        "client_turn_id": str(uuid4()),
                        "expected_sequence": 107,
                        "text": "Blocked until acknowledgement",
                    },
                    headers=ORIGIN,
                ).status_code
                == 409
            )
            assert len(fake.calls) == 107
        with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
            unknown = client.get(path).json()
            ack = client.post(
                path + f"/turns/{unknown['turns'][-1]['id']}/acknowledge-unknown", headers=ORIGIN
            )
            assert ack.status_code == 200 and ack.json()["status"] == "open"
            sent = client.post(
                path + "/turns",
                json={
                    "conversation_id": conversation,
                    "client_turn_id": str(uuid4()),
                    "expected_sequence": 107,
                    "text": "Continue from confirmed work",
                },
                headers=ORIGIN,
            )
            assert sent.status_code == 201 and sent.json()["turn"]["sequence"] == 108, sent.text
    assert len(fake.calls) == 108 and fake.results == []
    assert all(
        len((request.prompt + request.instructions).encode()) <= settings.live_text_max_prompt_bytes
        for request in fake.calls
    )


def test_legacy_history_beyond_1000_is_paged_and_next_send_uses_current_sequence(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, _, order, base = _setup(client)
        path = f"{base}/work-orders/{order}/conversation"
        conversation = client.post(path, json={}, headers=ORIGIN).json()["id"]
        fake = FakeTransport(
            [
                TextResult("Confirmed reply.", "initial", 3, 2, "completed"),
                TextResult("After 1000.", "later", 3, 2, "completed"),
            ]
        )
        with patch("onefoundry.task_conversation_store._provider_transport", return_value=fake):
            initial = client.post(
                path + "/turns",
                json={
                    "conversation_id": conversation,
                    "client_turn_id": str(uuid4()),
                    "expected_sequence": 0,
                    "text": "Initial",
                },
                headers=ORIGIN,
            )
            assert initial.status_code == 201, initial.text
            # Synthetic persisted transcript boundary; no provider call per seeded row.
            with create_engine(settings.database_url).begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO task_conversation_turns "
                        "(id,project_id,actor_id,conversation_id,client_turn_id,sequence,status,operator_text,"
                        "operator_sha256,prompt_sha256,assistant_text,assistant_sha256,assistant_actor_id,"
                        "profile_id,account_id,provider,model_id,credential_revision,"
                        "input_tokens,output_tokens,finished_at) "
                        "SELECT gen_random_uuid(),project_id,actor_id,conversation_id,"
                        "gen_random_uuid(),n,status,"
                        "operator_text,operator_sha256,prompt_sha256,assistant_text,assistant_sha256,assistant_actor_id,"
                        "profile_id,account_id,provider,model_id,credential_revision,"
                        "input_tokens,output_tokens,finished_at "
                        "FROM task_conversation_turns CROSS JOIN generate_series(2,1001) n WHERE id=:id"
                    ),
                    {"id": UUID(initial.json()["turn"]["id"])},
                )
            saved = client.get(path)
            assert saved.status_code == 200, saved.text
            assert saved.json()["latest_sequence"] == 1001 and saved.json()["turns_omitted"] == 901
            sent = client.post(
                path + "/turns",
                json={
                    "conversation_id": conversation,
                    "client_turn_id": str(uuid4()),
                    "expected_sequence": 1001,
                    "text": "Continue after 1000",
                },
                headers=ORIGIN,
            )
            assert sent.status_code == 201 and sent.json()["turn"]["sequence"] == 1002, sent.text
            page = client.get(f"{base}/work-orders/{order}/conversations/{conversation}?before_sequence=903")
            assert page.status_code == 200 and page.json()["turns"][-1]["sequence"] == 902
            assert len(page.json()["turns"]) == 100 and len(fake.calls) == 2


def test_discussion_send_cannot_apply_a_provider_action_and_replays_exactly(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        _pid, _profile, order_id, base = _setup(client)
        path = f"{base}/work-orders/{order_id}/conversation"
        cid = client.post(path, json={}, headers=ORIGIN).json()["id"]
        proposal = {
            "reply": "I will save a team plan.",
            "decision": "save_plan",
            "plan": "Unsafe claim that ordinary Send performs an action.",
        }
        fake = FakeTransport([TextResult(json.dumps(proposal), "discussion", 10, 7, "completed")])
        data = {
            "conversation_id": cid,
            "client_turn_id": str(uuid4()),
            "expected_sequence": 0,
            "text": "Can we discuss the workflow?",
            "discussion_only": True,
        }
        with patch("onefoundry.task_conversation_store._provider_transport", return_value=fake):
            response = client.post(path + "/turns", json=data, headers=ORIGIN)
            assert response.status_code == 201, response.text
            replay = client.post(path + "/turns", json=data, headers=ORIGIN)
            assert replay.status_code == 200, replay.text
            assert replay.json() == response.json()
            changed = client.post(path + "/turns", json={**data, "discussion_only": False}, headers=ORIGIN)
            assert changed.status_code == 409
        assert len(fake.calls) == 1
        assert "This is discussion only" in fake.calls[0].instructions
        assert response.json()["turn"]["discussion_only"] is True
        assert response.json()["turn"]["automation_decision"] is None
        assert len(client.get(f"{base}/work-orders").json()) == 1
        with create_engine(_settings(tmp_path).database_url).connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM events WHERE work_order_id=:o AND "
                        "event_type='task_automation_decision'"
                    ),
                    {"o": UUID(order_id)},
                )
                == 0
            )
        malformed = client.post(
            path + "/turns",
            json={**data, "client_turn_id": str(uuid4()), "expected_sequence": 1, "discussion_only": "false"},
            headers=ORIGIN,
        )
        assert malformed.status_code == 422
