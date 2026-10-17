"""Agent findings and Chief of Staff triage against disposable PostgreSQL with a fake model."""

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from onefoundry import coordination_store, coordination_triage
from onefoundry.inference import TextRequest, TextResult
from onefoundry.main import create_app
from onefoundry.model_providers import DiscoveredModel
from onefoundry.role_tool_admission import ToolDenied
from onefoundry.settings import Settings
from onefoundry.tool_gateway import ToolGateway

ORIGIN = {"origin": "http://localhost:8765"}


class FakeTransport:
    def __init__(self, reply: str | Exception) -> None:
        self.reply = reply
        self.calls: list[TextRequest] = []

    def generate(self, request: TextRequest) -> TextResult:
        self.calls.append(request)
        if isinstance(self.reply, Exception):
            raise self.reply
        return TextResult(self.reply, "triage-reply", 40, 20, "completed")


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    root = tmp_path / f"secrets-{uuid4()}"
    root.mkdir(mode=0o700)
    values: dict[str, Any] = {"coordination_mode": "draft_only", **overrides}
    return Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(root),
        **values,
    )


@contextmanager
def _env(settings: Settings) -> Iterator[tuple[TestClient, ToolGateway, ToolGateway, ToolGateway]]:
    engine = create_engine(settings.database_url)
    operator = ToolGateway(engine, "operator", live_text_settings=settings)
    coordinator = ToolGateway(engine, settings.onefoundry_coordinator_actor_id, live_text_settings=settings)
    triage = ToolGateway(engine, settings.coordination_launcher_actor_id, live_text_settings=settings)
    triage.reconcile_task_conversations()
    try:
        with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
            yield client, operator, coordinator, triage
    finally:
        engine.dispose()


def _post(client: TestClient, path: str, body: dict[str, Any] | None = None) -> Any:
    response = client.post(path, json=body or {}, headers=ORIGIN)
    assert response.status_code in {200, 201}, response.text
    return response.json()


def _project(client: TestClient, *, chief_model: bool = True) -> tuple[UUID, dict[str, str]]:
    project = _post(client, "/api/projects", {"name": f"Triage {uuid4()}"})
    base = f"/api/projects/{project['id']}"
    profiles = {row["name"]: row["id"] for row in client.get(f"{base}/profiles").json()}
    if chief_model:
        account = _post(
            client,
            f"{base}/model/accounts",
            {"provider": "openai", "label": "Fake", "api_key": "test-only-api-key"},
        )
        with patch(
            "onefoundry.model_gateway.discover_models",
            return_value=[DiscoveredModel("gpt-test", "GPT Test", "text")],
        ):
            _post(client, f"{base}/model/accounts/{account['id']}/refresh")
        chief = client.patch(
            f"{base}/profiles/{profiles['Chief of Staff']}",
            json={
                "model": "gpt-test",
                "model_account_id": account["id"],
                "instructions": "Coordinate the team.",
            },
            headers=ORIGIN,
        )
        assert chief.status_code == 200, chief.text
    researcher = client.patch(
        f"{base}/profiles/{profiles['Researcher']}",
        json={"allowed_tools": ["report_signal_v1"]},
        headers=ORIGIN,
    )
    assert researcher.status_code == 200, researcher.text
    return UUID(project["id"]), profiles


def _report(client: TestClient, operator: ToolGateway, project_id: UUID, profiles: dict[str, str]) -> UUID:
    base = f"/api/projects/{project_id}"
    order = _post(
        client,
        f"{base}/work-orders",
        {"title": "Scan districts", "instructions": "Look for buyers.", "profile_id": profiles["Researcher"]},
    )
    _post(client, f"{base}/work-orders/{order['id']}/ready")
    run = operator.start_synthetic_text(project_id, UUID(order["id"]))
    receipt = operator.invoke_role_tool(
        project_id,
        UUID(str(run["run_id"])),
        UUID(profiles["Researcher"]),
        "finding-1",
        "report_signal_v1",
        {"topic": "lead", "summary": "Springfield district plans a pilot purchase in November."},
        tool_version=1,
    )
    assert receipt["status"] == "done"
    return UUID(receipt["result"]["signal_id"])


def _signal(client: TestClient, project_id: UUID, signal_id: UUID) -> dict[str, Any]:
    rows = client.get(f"/api/projects/{project_id}/coordination/signals").json()
    return next(row for row in rows if row["id"] == str(signal_id))


def _decision(profiles: dict[str, str]) -> str:
    return "Here is my decision:\n" + json.dumps(
        {
            "action": "create_work_order",
            "target_profile_id": profiles["Sales"],
            "title": "Prepare a pilot outreach draft",
            "instructions": "Draft an outreach note for the Springfield pilot.",
            "reason": "A concrete buying signal with a date.",
        }
    )


def test_unmatched_finding_is_triaged_into_one_draft(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with _env(settings) as (client, operator, coordinator, triage):
        project_id, profiles = _project(client)
        signal_id = _report(client, operator, project_id, profiles)
        assert coordination_store.run_once(coordinator, settings, project_id)
        queued = _signal(client, project_id, signal_id)
        assert queued["state"] == "triage"
        assert queued["fields"]["topic"] == "lead"
        assert queued["intents"][0]["action_kind"] == "chief_triage"
        fake = FakeTransport(_decision(profiles))
        with patch("onefoundry.task_conversation_store._provider_transport", return_value=fake):
            assert coordination_triage.step(triage, settings, project_id)
            assert not coordination_triage.step(triage, settings, project_id)
        assert len(fake.calls) == 1
        assert "Triage this automation event" in fake.calls[0].prompt
        assert "Operator rules that already exist (none matched this event):\n- none" in fake.calls[0].prompt
        assert "> summary: Springfield district plans a pilot purchase in November." in fake.calls[0].prompt
        done = _signal(client, project_id, signal_id)
        assert done["state"] == "matched"
        intent = done["intents"][0]
        assert intent["state"] == "done"
        assert intent["decision"]["action"] == "create_work_order"
        assert intent["work_order_title"] == "Prepare a pilot outreach draft"
        created = next(
            row
            for row in client.get(f"/api/projects/{project_id}/work-orders").json()
            if row["id"] == intent["work_order_id"]
        )
        assert created["status"] == "draft"
        assert created["profile_id"] == profiles["Sales"]
        assert created["chain_depth"] == 1
        assert "Created automatically by Chief of Staff triage" in created["instructions"]
        triage_orders = [
            row
            for row in client.get(f"/api/projects/{project_id}/work-orders").json()
            if row["title"] == "Automation triage"
        ]
        assert len(triage_orders) == 1 and triage_orders[0]["profile_id"] == profiles["Chief of Staff"]
        chats = client.get(
            f"/api/projects/{project_id}/work-orders/{triage_orders[0]['id']}/conversations"
        ).json()
        assert [chat["title"] for chat in chats] == ["Triage: agent finding"]


def test_rule_match_takes_precedence_over_triage(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with _env(settings) as (client, operator, coordinator, _triage):
        project_id, profiles = _project(client)
        _post(
            client,
            f"/api/projects/{project_id}/coordination/rules",
            {
                "name": "Leads go to Sales",
                "signal_type": "agent.finding",
                "filters": {"topic": "lead"},
                "target_profile_id": profiles["Sales"],
                "title": "Follow up the lead",
                "instructions": "Draft a follow-up.",
            },
        )
        signal_id = _report(client, operator, project_id, profiles)
        for _ in range(5):
            coordination_store.run_once(coordinator, settings, project_id)
        signal = _signal(client, project_id, signal_id)
        assert signal["state"] == "matched"
        assert [intent["action_kind"] for intent in signal["intents"]] == ["rule_work_order"]


def test_malformed_reply_asks_operator_and_unknown_is_not_retried(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with _env(settings) as (client, operator, coordinator, triage):
        project_id, profiles = _project(client)
        first = _report(client, operator, project_id, profiles)
        coordination_store.run_once(coordinator, settings, project_id)
        with patch(
            "onefoundry.task_conversation_store._provider_transport",
            return_value=FakeTransport("I think Sales should look at this."),
        ):
            assert coordination_triage.step(triage, settings, project_id)
        asked = _signal(client, project_id, first)
        assert asked["state"] == "blocked" and asked["reason"] == "ask_operator"
        assert asked["intents"][0]["decision"]["action"] == "ask_operator"

        base = f"/api/projects/{project_id}"
        order = _post(
            client,
            f"{base}/work-orders",
            {"title": "Second scan", "instructions": "Look again.", "profile_id": profiles["Researcher"]},
        )
        _post(client, f"{base}/work-orders/{order['id']}/ready")
        run = operator.start_synthetic_text(project_id, UUID(order["id"]))
        second = UUID(
            operator.invoke_role_tool(
                project_id,
                UUID(str(run["run_id"])),
                UUID(profiles["Researcher"]),
                "finding-2",
                "report_signal_v1",
                {"topic": "risk", "summary": "A competitor cut prices."},
                tool_version=1,
            )["result"]["signal_id"]
        )
        coordination_store.run_once(coordinator, settings, project_id)
        broken = FakeTransport(TimeoutError("connection dropped"))
        with patch("onefoundry.task_conversation_store._provider_transport", return_value=broken):
            assert coordination_triage.step(triage, settings, project_id)
            assert not coordination_triage.step(triage, settings, project_id)
        assert len(broken.calls) == 1
        unknown = _signal(client, project_id, second)
        assert unknown["intents"][0]["state"] == "unknown"
        assert unknown["intents"][0]["error_code"] == "triage_outcome_unknown"


def test_chain_limit_and_missing_chief_model_block_triage(tmp_path: Path) -> None:
    settings = _settings(tmp_path, coordination_max_chain_depth=1)
    with _env(settings) as (client, operator, coordinator, triage):
        project_id, profiles = _project(client, chief_model=False)
        with coordinator.engine.begin() as connection:
            deep = coordination_store.record_signal(
                coordinator,
                connection,
                project_id,
                source="agent_finding",
                signal_type="agent.finding",
                dedupe_key="deep",
                fields={"topic": "lead", "summary": "Deep chain"},
                chain_depth=1,
            )
        shallow = _report(client, operator, project_id, profiles)
        for _ in range(3):
            coordination_store.run_once(coordinator, settings, project_id)
        assert deep is not None
        blocked = _signal(client, project_id, deep)
        assert blocked["state"] == "blocked" and blocked["reason"] == "chain_depth_exceeded"
        fake = FakeTransport(_decision(profiles))
        with patch("onefoundry.task_conversation_store._provider_transport", return_value=fake):
            assert coordination_triage.step(triage, settings, project_id)
        assert fake.calls == []
        unavailable = _signal(client, project_id, shallow)
        assert unavailable["intents"][0]["error_code"] == "triage_unavailable"
        assert unavailable["state"] == "blocked"


def test_finding_tool_is_denied_when_automation_is_off(tmp_path: Path) -> None:
    settings = _settings(tmp_path, coordination_mode="off")
    with _env(settings) as (client, operator, _coordinator, _triage):
        project_id, profiles = _project(client, chief_model=False)
        with pytest.raises(ToolDenied, match="automation is off"):
            _report(client, operator, project_id, profiles)
        with operator.engine.connect() as connection:
            count = connection.scalar(
                text("SELECT count(*) FROM coordination_signals WHERE project_id=:p"), {"p": project_id}
            )
        assert count == 0
