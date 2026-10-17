"""Auto-start rules launch one live text run per created work order, within caps, with a fake model."""

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from onefoundry import coordination_autostart, coordination_store
from onefoundry.coordination_schemas import RuleCreate
from onefoundry.inference import TextRequest, TextResult
from onefoundry.main import create_app
from onefoundry.model_providers import DiscoveredModel
from onefoundry.settings import Settings
from onefoundry.tool_gateway import ToolGateway

ORIGIN = {"origin": "http://localhost:8765"}


@pytest.fixture(autouse=True)
def manual_launch_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    # These tests explicitly claim and dispatch each intent. A second background
    # launcher can correctly claim it first, invalidating the manual step assertion.
    async def quiet(_self: object) -> None:
        return None

    monkeypatch.setattr("onefoundry.coordination_launcher.CoordinationLauncher.start", quiet)


class FakeTransport:
    def __init__(self, reply: str | Exception = "Research summary for the finished change.") -> None:
        self.reply = reply
        self.calls: list[TextRequest] = []

    def generate(self, request: TextRequest) -> TextResult:
        self.calls.append(request)
        if isinstance(self.reply, Exception):
            raise self.reply
        return TextResult(self.reply, "auto-reply", 30, 12, "completed")


def _settings(
    tmp_path: Path, mode: Literal["off", "draft_only", "auto"] = "auto", **overrides: Any
) -> Settings:
    root = tmp_path / f"secrets-{uuid4()}"
    root.mkdir(mode=0o700)
    return Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(root),
        coordination_mode=mode,
        **overrides,
    )


@contextmanager
def _env(settings: Settings) -> Iterator[tuple[TestClient, ToolGateway, ToolGateway]]:
    engine = create_engine(settings.database_url)
    coordinator = ToolGateway(engine, settings.onefoundry_coordinator_actor_id, live_text_settings=settings)
    launcher = ToolGateway(engine, settings.coordination_launcher_actor_id, live_text_settings=settings)
    try:
        with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
            yield client, coordinator, launcher
    finally:
        engine.dispose()


def _post(client: TestClient, path: str, body: dict[str, Any] | None = None) -> Any:
    response = client.post(path, json=body or {}, headers=ORIGIN)
    assert response.status_code in {200, 201}, response.text
    return response.json()


def _project(
    client: TestClient, *, researcher_model: bool = True, **rule_fields: Any
) -> tuple[str, dict[str, str]]:
    project = _post(client, "/api/projects", {"name": f"Auto {uuid4()}"})
    base = f"/api/projects/{project['id']}"
    profiles = {row["name"]: row["id"] for row in client.get(f"{base}/profiles").json()}
    if researcher_model:
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
        updated = client.patch(
            f"{base}/profiles/{profiles['Researcher']}",
            json={
                "model": "gpt-test",
                "model_account_id": account["id"],
                "instructions": "Research briefly.",
            },
            headers=ORIGIN,
        )
        assert updated.status_code == 200, updated.text
    _post(
        client,
        f"{base}/coordination/rules",
        {
            "name": "Research after engineering",
            "signal_type": "work_order.done",
            "filters": {"profile_name": "Engineer"},
            "target_profile_id": profiles["Researcher"],
            "title": "Research the shipped change",
            "instructions": "Summarize what changed for the team.",
            "autonomy": "auto_start",
            "max_per_day": 10,
            **rule_fields,
        },
    )
    return base, profiles


def test_rule_attempt_cap_serializes_claims_and_counts_unknown_starts(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with _env(settings) as (client, coordinator, launcher):
        base, profiles = _project(client, max_model_attempts=1)
        pid = UUID(base.rsplit("/", 1)[1])
        for title in ("First", "Second"):
            _complete_engineering(client, base, profiles, title)
        _drain(coordinator, settings, base)
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: coordination_autostart.claim(launcher, settings, pid), range(2)))
        assert sum(bool(item) for item in claims) == 1
        launches = _launches(client, base)
        assert sorted(item["state"] for item in launches) == ["blocked", "leased"]
        assert (
            next(item for item in launches if item["state"] == "blocked")["error_code"]
            == "rule_model_attempt_limit"
        )
        admitted = next(item for item in claims if item)
        coordination_autostart._finish(launcher, admitted, "unknown", "interrupted", None)
        _complete_engineering(client, base, profiles, "Third")
        _drain(coordinator, settings, base)
        assert coordination_autostart.claim(launcher, settings, pid) == {}
        assert [item["state"] for item in _launches(client, base)].count("blocked") == 2


def test_quiet_hours_defer_without_spending_an_attempt(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text

    settings = _settings(tmp_path)
    with _env(settings) as (client, coordinator, launcher):
        base, profiles = _project(client, quiet_hours={"start": "22:00", "end": "08:00"})
        pid = UUID(base.rsplit("/", 1)[1])
        _complete_engineering(client, base, profiles, "Queued overnight")
        _drain(coordinator, settings, base)
        due = datetime.now(UTC) + timedelta(hours=1)
        with patch("onefoundry.coordination_autostart.quiet_until", return_value=due):
            assert coordination_autostart.claim(launcher, settings, pid) == {}
        with launcher.engine.connect() as connection:
            saved = (
                connection.execute(
                    text(
                        "SELECT state,attempts,model_admitted_at,due_at FROM coordination_intents "
                        "WHERE project_id=:p AND action_kind='auto_start'"
                    ),
                    {"p": pid},
                )
                .mappings()
                .one()
            )
        assert saved["state"] == "pending" and saved["attempts"] == 0
        assert saved["model_admitted_at"] is None and saved["due_at"] == due
        assert coordination_autostart.claim(launcher, settings, pid) is None


def test_rule_revisions_keep_pending_limits_and_do_not_reset_admission_counts(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with _env(settings) as (client, coordinator, launcher):
        base, profiles = _project(client, max_model_attempts=1)
        pid = UUID(base.rsplit("/", 1)[1])
        for title in ("First", "Pinned"):
            _complete_engineering(client, base, profiles, title)
        _drain(coordinator, settings, base)
        first = coordination_autostart.claim(launcher, settings, pid)
        assert first
        coordination_autostart._finish(launcher, first, "done", "provider_failed", {"status": "failed"})
        saved = client.get(f"{base}/coordination/rules").json()[0]
        fields = {key: saved[key] for key in RuleCreate.model_fields if key in saved}
        revised = client.put(
            f"{base}/coordination/rules/{saved['id']}",
            headers=ORIGIN,
            json={**fields, "max_model_attempts": 2, "expected_version": saved["version"]},
        )
        assert revised.status_code == 200
        assert coordination_autostart.claim(launcher, settings, pid) == {}
        _complete_engineering(client, base, profiles, "After revision")
        _drain(coordinator, settings, base)
        assert coordination_autostart.claim(launcher, settings, pid)


def _complete_engineering(client: TestClient, base: str, profiles: dict[str, str], title: str) -> None:
    order = _post(
        client,
        f"{base}/work-orders",
        {"title": title, "instructions": "Do it.", "profile_id": profiles["Engineer"]},
    )
    _post(client, f"{base}/work-orders/{order['id']}/ready")
    run = _post(client, f"{base}/work-orders/{order['id']}/runs")
    _post(client, f"{base}/runs/{run['id']}/finish", {"outcome": "done", "reason": "Recorded."})
    current = next(row for row in client.get(f"{base}/work-orders").json() if row["id"] == order["id"])
    _post(
        client,
        f"{base}/work-orders/{order['id']}/accept",
        {"expected_version": current["version"], "reason": "Reviewed."},
    )


def _drain(coordinator: ToolGateway, settings: Settings, base: str) -> None:
    pid = UUID(base.rsplit("/", 1)[1])
    for _ in range(20):
        if not coordination_store.run_once(coordinator, settings, pid):
            return


def _launches(client: TestClient, base: str) -> list[dict[str, Any]]:
    return [
        intent
        for signal in client.get(f"{base}/coordination/signals").json()
        for intent in signal["intents"]
        if intent["action_kind"] == "auto_start"
    ]


def test_auto_mode_launches_one_run_and_completion_rules_still_apply(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with _env(settings) as (client, coordinator, launcher):
        base, profiles = _project(client)
        _complete_engineering(client, base, profiles, "Ship the change")
        _drain(coordinator, settings, base)
        created = [row for row in client.get(f"{base}/work-orders").json() if row["origin_signal_id"]]
        assert [(row["status"], row["profile_id"]) for row in created] == [("ready", profiles["Researcher"])]
        assert [launch["state"] for launch in _launches(client, base)] == ["pending"]
        fake = FakeTransport()
        pid = UUID(base.rsplit("/", 1)[1])
        with patch("onefoundry.live_text_store._provider_transport", return_value=fake):
            assert coordination_autostart.step(launcher, settings, pid)
            assert not coordination_autostart.step(launcher, settings, pid)
        assert len(fake.calls) == 1
        launch = _launches(client, base)[0]
        assert launch["state"] == "done" and launch["decision"]["run_id"]
        order = next(row for row in client.get(f"{base}/work-orders").json() if row["id"] == created[0]["id"])
        # A model reply alone does not complete operator-review work.
        assert order["status"] == "needs_review"


def test_draft_only_mode_never_queues_a_launch(tmp_path: Path) -> None:
    settings = _settings(tmp_path, "draft_only")
    with _env(settings) as (client, coordinator, launcher):
        base, profiles = _project(client)
        _complete_engineering(client, base, profiles, "Ship the change")
        _drain(coordinator, settings, base)
        created = [row for row in client.get(f"{base}/work-orders").json() if row["origin_signal_id"]]
        assert [row["status"] for row in created] == ["draft"]
        assert _launches(client, base) == []
        assert not coordination_autostart.step(launcher, settings, UUID(base.rsplit("/", 1)[1]))


def test_daily_cap_unknown_outcome_and_unavailable_model(tmp_path: Path) -> None:
    settings = _settings(tmp_path, coordination_max_auto_starts_per_day=1)
    with _env(settings) as (client, coordinator, launcher):
        base, profiles = _project(client)
        pid = UUID(base.rsplit("/", 1)[1])
        _complete_engineering(client, base, profiles, "First change")
        _complete_engineering(client, base, profiles, "Second change")
        _drain(coordinator, settings, base)
        broken = FakeTransport(TimeoutError("dropped"))
        with patch("onefoundry.live_text_store._provider_transport", return_value=broken):
            assert coordination_autostart.step(launcher, settings, pid)
            assert coordination_autostart.step(launcher, settings, pid)
            assert not coordination_autostart.step(launcher, settings, pid)
        assert len(broken.calls) == 1
        states = sorted((launch["state"], launch["error_code"]) for launch in _launches(client, base))
        assert states == [("blocked", "auto_start_daily_limit"), ("unknown", "auto_start_outcome_unknown")]
        orders = {
            row["status"] for row in client.get(f"{base}/work-orders").json() if row["origin_signal_id"]
        }
        # The capped order waits for the operator; the uncertain one is not restarted.
        assert "ready" in orders

    other = _settings(tmp_path)
    with _env(other) as (client, coordinator, launcher):
        base, profiles = _project(client, researcher_model=False)
        _complete_engineering(client, base, profiles, "Ship without a model")
        _drain(coordinator, other, base)
        assert coordination_autostart.step(launcher, other, UUID(base.rsplit("/", 1)[1]))
        [launch] = _launches(client, base)
        assert launch["state"] == "blocked" and launch["error_code"] == "auto_start_unavailable"
