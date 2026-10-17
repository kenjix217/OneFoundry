"""Project owner paths for verified DM routing and one-shot remote replies."""

import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from onefoundry.inference import TextRequest, TextResult, TransportError
from onefoundry.main import create_app
from onefoundry.model_providers import DiscoveredModel
from onefoundry.remote_channel_core import RemoteChannelCore
from onefoundry.settings import Settings
from onefoundry.tool_gateway import Conflict, NotFound

ORIGIN = {"origin": "http://localhost:8765"}


@pytest.fixture(autouse=True)
def _disable_remote_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REMOTE_CHANNEL_NETWORK_ENABLED", "false")


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


def _client(tmp_path: Path) -> TestClient:
    root = tmp_path / "model-secrets"
    root.mkdir(mode=0o700, exist_ok=True)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(root),
        remote_channel_network_enabled=False,
    )
    return TestClient(create_app(settings), base_url="http://localhost:8765")


def _core(client: TestClient) -> RemoteChannelCore:
    app = client.app
    assert isinstance(app, FastAPI)
    return cast(RemoteChannelCore, app.state.remote_channels)


def _setup(client: TestClient) -> tuple[str, str, str, str]:
    project = client.post("/api/projects", json={"name": "Remote channel"}, headers=ORIGIN)
    assert project.status_code == 201, project.text
    project_id = project.json()["id"]
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
        refreshed = client.post(f"{base}/model/accounts/{account_id}/refresh", headers=ORIGIN)
    assert refreshed.status_code == 200, refreshed.text
    profile = client.post(
        f"{base}/profiles",
        json={
            "name": "Advisor",
            "instructions": "Answer plainly.",
            "model": "gpt-test",
            "model_account_id": account_id,
        },
        headers=ORIGIN,
    )
    assert profile.status_code == 201, profile.text
    order = client.post(
        f"{base}/work-orders",
        json={"title": "Advisor chat", "instructions": "Help", "profile_id": profile.json()["id"]},
        headers=ORIGIN,
    )
    assert order.status_code == 201, order.text
    return project_id, profile.json()["id"], order.json()["id"], base


def _chief(client: TestClient, project_id: str, advisor_id: str) -> tuple[str, str]:
    profiles = client.get(f"/api/projects/{project_id}/profiles").json()
    chief = next(item for item in profiles if item["name"] == "Chief of Staff")
    advisor = next(item for item in profiles if item["id"] == advisor_id)
    patched = client.patch(
        f"/api/projects/{project_id}/profiles/{chief['id']}",
        json={"model": advisor["model"], "model_account_id": advisor["model_account_id"]},
        headers=ORIGIN,
    )
    assert patched.status_code == 200, patched.text
    order = client.post(
        f"/api/projects/{project_id}/work-orders",
        json={
            "title": "Remote Chief conversation",
            "instructions": "Assist the operator",
            "profile_id": chief["id"],
        },
        headers=ORIGIN,
    )
    assert order.status_code == 201, order.text
    return chief["id"], order.json()["id"]


def _discord(
    client: TestClient, project_id: str, chief_id: str, order_id: str
) -> tuple[UUID, RemoteChannelCore]:
    base = f"/api/projects/{project_id}/channels"
    listed = client.get(base)
    assert listed.status_code == 200, listed.text
    discord = next(item for item in listed.json() if item["platform"] == "discord")
    assert not discord["configured"] and not discord["enabled"]
    configured = client.put(
        f"{base}/discord",
        json={
            "sender_id": "111",
            "chat_id": None,
            "work_order_id": order_id,
            "profile_id": chief_id,
            "reply_policy": "bound_dm",
            "enabled": False,
        },
        headers=ORIGIN,
    )
    assert configured.status_code == 200, configured.text
    channel_id = UUID(discord["id"])
    credential = client.put(
        f"{base}/{channel_id}/credential", json={"credential": "synthetic-discord-token"}, headers=ORIGIN
    )
    assert credential.status_code == 200 and credential.json()["credential_present"]
    core = _core(client)
    with pytest.raises(Conflict):
        core.observe_identity(UUID(project_id), channel_id, "333", "222", True)
    observed = core.observe_identity(UUID(project_id), channel_id, "111", "222", True)
    assert observed["observed_chat_id"] == "222" and not observed["identity_confirmed"]
    premature = client.put(
        f"{base}/discord",
        json={
            "sender_id": "111",
            "chat_id": None,
            "work_order_id": order_id,
            "profile_id": chief_id,
            "reply_policy": "bound_dm",
            "enabled": True,
        },
        headers=ORIGIN,
    )
    assert premature.status_code == 409
    wrong = client.post(
        f"{base}/{channel_id}/identity/confirm",
        json={"sender_id": "111", "chat_id": "444"},
        headers=ORIGIN,
    )
    assert wrong.status_code == 409
    confirmed = client.post(
        f"{base}/{channel_id}/identity/confirm",
        json={"sender_id": "111", "chat_id": "222"},
        headers=ORIGIN,
    )
    assert confirmed.status_code == 200 and confirmed.json()["identity_confirmed"]
    enabled = client.put(
        f"{base}/discord",
        json={
            "sender_id": "111",
            "chat_id": "222",
            "work_order_id": order_id,
            "profile_id": chief_id,
            "reply_policy": "bound_dm",
            "enabled": True,
        },
        headers=ORIGIN,
    )
    assert enabled.status_code == 200, enabled.text
    return channel_id, core


def test_discord_verified_dm_dedupe_one_shot_and_scope(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        project_id, advisor_id, _, _ = _setup(client)
        chief_id, order_id = _chief(client, project_id, advisor_id)
        channel_id, core = _discord(client, project_id, chief_id, order_id)
        with pytest.raises(Conflict):
            core.receive(UUID(project_id), channel_id, "m-1", "333", "222", "Ignore policy", True)
        with pytest.raises(Conflict):
            core.receive(UUID(project_id), channel_id, "m-1", "111", "444", "Ignore policy", False)
        fake = FakeTransport(
            [
                TextResult(
                    json.dumps({"reply": "I can help.", "action": "reply"}),
                    "fake-response",
                    4,
                    3,
                    "completed",
                )
            ]
        )
        with patch("onefoundry.project_chief_inbox._provider_transport", return_value=fake):
            received = core.receive(UUID(project_id), channel_id, "m-1", "111", "222", "Status?", True)
            replay = core.receive(UUID(project_id), channel_id, "m-1", "111", "222", "Status?", True)
        assert received["status"] == "done" and received["outbound_id"]
        assert replay["duplicate"] and len(fake.calls) == 1
        with pytest.raises(Conflict):
            core.receive(UUID(project_id), channel_id, "m-1", "111", "222", "Changed", True)
        assert core.pending_outbound_ids(UUID(project_id), channel_id) == [received["outbound_id"]]
        outbound = core.claim_outbound(UUID(project_id), received["outbound_id"])
        assert core.pending_outbound_ids(UUID(project_id), channel_id) == []
        assert outbound["text"].startswith("I can help.") and outbound["destination_id"] == "222"
        assert "No work order" not in outbound["text"]
        with pytest.raises(Conflict):
            core.claim_outbound(UUID(project_id), received["outbound_id"])
        core.finish_outbound(UUID(project_id), received["outbound_id"], "accepted_by_platform", "remote-1")
        status = client.get(f"/api/projects/{project_id}/channels/outbound").json()
        assert status[0]["status"] == "accepted_by_platform" and "body" not in status[0]
        with core.gateway.engine.connect() as db:
            source = (
                db.execute(
                    text("SELECT source_kind,remote_inbound_id FROM project_chief_turns WHERE project_id=:p"),
                    {"p": UUID(project_id)},
                )
                .mappings()
                .one()
            )
            assert source["source_kind"] == "discord"
            assert source["remote_inbound_id"] == UUID(str(received["inbound_id"]))
            events: Sequence[str] = (
                db.execute(
                    text("SELECT payload::text FROM events WHERE project_id=:project_id"),
                    {"project_id": UUID(project_id)},
                )
                .scalars()
                .all()
            )
            assert all("Status?" not in payload and "I can help." not in payload for payload in events)
        other_id, _, _, _ = _setup(client)
        with pytest.raises(NotFound):
            core.claim_outbound(UUID(other_id), received["outbound_id"])


def test_discord_server_binding_requires_exact_observed_operator_and_route(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        project_id, advisor_id, _, base = _setup(client)
        chief_id, _ = _chief(client, project_id, advisor_id)
        endpoint = f"{base}/channels/discord"
        binding = {
            "sender_id": "111",
            "chat_id": "222",
            "bot_id": "333",
            "guild_id": "444",
            "profile_id": chief_id,
            "reply_policy": "bound_dm",
            "enabled": False,
        }
        for invalid in (
            {**binding, "guild_id": "bad"},
            {**binding, "bot_id": None},
            {**binding, "chat_id": None},
        ):
            assert client.put(endpoint, json=invalid, headers=ORIGIN).status_code == 422
        saved = client.put(endpoint, json=binding, headers=ORIGIN)
        assert saved.status_code == 200, saved.text
        assert saved.json()["guild_id"] == "444" and saved.json()["bot_id"] == "333"
        channel_id = UUID(saved.json()["id"])
        core = _core(client)
        assert (
            client.put(
                f"{base}/channels/{channel_id}/credential",
                json={"credential": "synthetic-token"},
                headers=ORIGIN,
            ).status_code
            == 200
        )
        for sender, chat, is_dm, guild in (
            ("111", "222", True, None),
            ("111", "222", False, "445"),
            ("111", "223", False, "444"),
            ("112", "222", False, "444"),
        ):
            with pytest.raises(Conflict):
                core.observe_identity(UUID(project_id), channel_id, sender, chat, is_dm, guild)
        observed = core.observe_identity(UUID(project_id), channel_id, "111", "222", False, "444")
        assert observed["observed_chat_id"] == "222"
        assert (
            client.post(
                f"{base}/channels/{channel_id}/identity/confirm",
                json={"sender_id": "111", "chat_id": "222"},
                headers=ORIGIN,
            ).status_code
            == 200
        )
        enabled = client.put(endpoint, json={**binding, "enabled": True}, headers=ORIGIN)
        assert enabled.status_code == 200, enabled.text
        for is_dm, guild in ((True, None), (False, "445")):
            with pytest.raises(Conflict):
                core.receive(
                    UUID(project_id), channel_id, "m-server", "111", "222", "Status?", is_dm, guild_id=guild
                )
        fake = FakeTransport(
            [
                TextResult(
                    json.dumps({"reply": "Ready.", "action": "reply"}), "fake-response", 4, 3, "completed"
                )
            ]
        )
        with patch("onefoundry.project_chief_inbox._provider_transport", return_value=fake):
            received = core.receive(
                UUID(project_id), channel_id, "m-server", "111", "222", "Status?", False, guild_id="444"
            )
        assert received["outbound_id"] and len(fake.calls) == 1
        assert core.claim_outbound(UUID(project_id), received["outbound_id"])["destination_id"] == "222"
        changed = client.put(endpoint, json={**binding, "guild_id": "446"}, headers=ORIGIN)
        assert changed.status_code == 200 and not changed.json()["enabled"]
        assert not changed.json()["identity_confirmed"]


def test_confirmed_discord_message_can_save_one_agent_rule(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        project_id, advisor_id, _, _ = _setup(client)
        chief_id, order_id = _chief(client, project_id, advisor_id)
        channel_id, core = _discord(client, project_id, chief_id, order_id)
        response = {
            "reply": "I will set up the daily brief.",
            "action": "create_rule",
            "rule": {
                "name": "Daily project brief",
                "signal_type": "schedule.fired",
                "schedule": {"kind": "daily", "at": "09:00"},
                "target_profile_id": advisor_id,
                "title": "Write the daily brief",
                "instructions": "Summarize project work for the operator.",
            },
        }
        fake = FakeTransport([TextResult(json.dumps(response), "rule-dm", 12, 24, "completed")])
        with patch("onefoundry.project_chief_inbox._provider_transport", return_value=fake):
            received = core.receive(
                UUID(project_id),
                channel_id,
                "rule-message-1",
                "111",
                "222",
                "Give me a daily project brief at 9 AM UTC.",
                True,
            )
            replay = core.receive(
                UUID(project_id),
                channel_id,
                "rule-message-1",
                "111",
                "222",
                "Give me a daily project brief at 9 AM UTC.",
                True,
            )
        assert received["status"] == "done" and replay["duplicate"]
        assert len(fake.calls) == 1
        rules = client.get(f"/api/projects/{project_id}/coordination/rules").json()
        assert len(rules) == 1 and rules[0]["name"] == "Daily project brief"
        outbound = core.claim_outbound(UUID(project_id), received["outbound_id"])
        assert "Automation rule created" in outbound["text"]
        with core.gateway.engine.connect() as db:
            saved = (
                db.execute(
                    text(
                        "SELECT t.status,t.source_kind,t.remote_inbound_id,t.created_rule_id "
                        "FROM project_chief_turns t WHERE t.project_id=:p"
                    ),
                    {"p": UUID(project_id)},
                )
                .mappings()
                .one()
            )
            assert saved["status"] == "done" and saved["source_kind"] == "discord"
            assert saved["remote_inbound_id"] == UUID(str(received["inbound_id"]))
            assert saved["created_rule_id"] == UUID(rules[0]["id"])


def test_discord_rule_write_rejects_pairing_change_during_model_reply(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        project_id, advisor_id, _, _ = _setup(client)
        chief_id, order_id = _chief(client, project_id, advisor_id)
        channel_id, core = _discord(client, project_id, chief_id, order_id)
        response = TextResult(
            json.dumps(
                {
                    "reply": "I will set up the brief.",
                    "action": "create_rule",
                    "rule": {
                        "name": "Daily brief after change",
                        "signal_type": "schedule.fired",
                        "schedule": {"kind": "daily", "at": "09:00"},
                        "target_profile_id": advisor_id,
                        "title": "Write the brief",
                        "instructions": "Summarize project work.",
                    },
                }
            ),
            "changed-pairing",
            12,
            24,
            "completed",
        )

        class ChangingTransport(FakeTransport):
            def generate(self, request: TextRequest) -> TextResult:
                with core.gateway.engine.begin() as db:
                    db.execute(
                        text("UPDATE remote_channels SET enabled=false WHERE project_id=:p AND id=:c"),
                        {"p": UUID(project_id), "c": channel_id},
                    )
                return super().generate(request)

        fake = ChangingTransport([response])
        with patch("onefoundry.project_chief_inbox._provider_transport", return_value=fake):
            received = core.receive(
                UUID(project_id),
                channel_id,
                "changed-pairing-1",
                "111",
                "222",
                "Give me a daily project brief at 9 AM UTC.",
                True,
            )
        assert received["status"] == "unknown"
        assert client.get(f"/api/projects/{project_id}/coordination/rules").json() == []
        with core.gateway.engine.connect() as db:
            assert (
                db.scalar(
                    text("SELECT status FROM project_chief_turns WHERE project_id=:p"),
                    {"p": UUID(project_id)},
                )
                == "unknown"
            )


def test_unknown_and_approval_each_fail_closed(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        project_id, advisor_id, _, _ = _setup(client)
        chief_id, order_id = _chief(client, project_id, advisor_id)
        channel_id, core = _discord(client, project_id, chief_id, order_id)
        fake = FakeTransport([TransportError("synthetic ambiguous network result")])
        with patch("onefoundry.project_chief_inbox._provider_transport", return_value=fake):
            result = core.receive(UUID(project_id), channel_id, "m-unknown", "111", "222", "Status?", True)
        assert result["status"] == "unknown" and result["outbound_id"]
        assert core.receive(UUID(project_id), channel_id, "m-unknown", "111", "222", "Status?", True)[
            "duplicate"
        ]
        disabled = client.put(
            f"/api/projects/{project_id}/channels/discord",
            json={
                "sender_id": "111",
                "chat_id": "222",
                "work_order_id": order_id,
                "profile_id": chief_id,
                "reply_policy": "approval_each",
                "enabled": True,
            },
            headers=ORIGIN,
        )
        assert disabled.status_code == 409


def test_credential_rotation_failure_disables_pending_reply(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        project_id, advisor_id, _, _ = _setup(client)
        chief_id, order_id = _chief(client, project_id, advisor_id)
        channel_id, core = _discord(client, project_id, chief_id, order_id)
        fake = FakeTransport(
            [
                TextResult(
                    json.dumps({"reply": "Pending reply.", "action": "reply"}),
                    "rotation-response",
                    4,
                    3,
                    "completed",
                )
            ]
        )
        with patch("onefoundry.project_chief_inbox._provider_transport", return_value=fake):
            received = core.receive(UUID(project_id), channel_id, "rotation-1", "111", "222", "Status?", True)
        assert core.pending_outbound_ids(UUID(project_id), channel_id) == [received["outbound_id"]]
        original_event = core.gateway._event

        def fail_final_event(
            connection: Any,
            event_project_id: UUID,
            work_order_id: UUID | None,
            run_id: UUID | None,
            event_type: str,
            payload: dict[str, Any],
            actor_id: str | None = None,
        ) -> None:
            if event_type == "remote_channel_credential_changed":
                raise RuntimeError("synthetic commit failure after secret replacement")
            original_event(connection, event_project_id, work_order_id, run_id, event_type, payload, actor_id)

        with patch.object(core.gateway, "_event", side_effect=fail_final_event):
            with pytest.raises(RuntimeError, match="synthetic commit failure"):
                core.put_credential(UUID(project_id), channel_id, "new-synthetic-token")
        status = core.status(UUID(project_id), channel_id)
        assert isinstance(status, dict)
        assert not status["enabled"] and not status["identity_confirmed"]
        assert not status["credential_present"]
        assert core.secrets.read(channel_id) == "new-synthetic-token"
        assert core.outbound_status(UUID(project_id))[0]["status"] == "failed"
        with pytest.raises(Conflict):
            core.claim_outbound(UUID(project_id), received["outbound_id"])


def test_archive_blocks_enabled_or_pending_remote_and_claim(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        project_id, advisor_id, _, _ = _setup(client)
        chief_id, order_id = _chief(client, project_id, advisor_id)
        channel_id, core = _discord(client, project_id, chief_id, order_id)
        base = f"/api/projects/{project_id}"
        assert client.post(f"{base}/archive", json={"expected_version": 1}, headers=ORIGIN).status_code == 409
        fake = FakeTransport(
            [
                TextResult(
                    json.dumps({"reply": "Pending.", "action": "reply"}),
                    "archive-response",
                    4,
                    3,
                    "completed",
                )
            ]
        )
        with patch("onefoundry.project_chief_inbox._provider_transport", return_value=fake):
            received = core.receive(UUID(project_id), channel_id, "archive-1", "111", "222", "Status?", True)
        disabled = client.put(
            f"{base}/channels/discord",
            json={
                "sender_id": "111",
                "chat_id": "222",
                "work_order_id": order_id,
                "profile_id": chief_id,
                "reply_policy": "bound_dm",
                "enabled": False,
            },
            headers=ORIGIN,
        )
        assert disabled.status_code == 200 and not disabled.json()["enabled"]
        assert core.outbound_status(UUID(project_id))[0]["status"] == "failed"
        archived = client.post(f"{base}/archive", json={"expected_version": 1}, headers=ORIGIN)
        assert archived.status_code == 200, archived.text
        with pytest.raises(Conflict):
            core.claim_outbound(UUID(project_id), received["outbound_id"])


def test_reject_pending_reply_is_one_shot_without_send(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        project_id, advisor_id, _, _ = _setup(client)
        chief_id, order_id = _chief(client, project_id, advisor_id)
        channel_id, core = _discord(client, project_id, chief_id, order_id)
        fake = FakeTransport(
            [
                TextResult(
                    json.dumps({"reply": "Reply draft.", "action": "reply"}),
                    "reject-response",
                    4,
                    3,
                    "completed",
                )
            ]
        )
        with patch("onefoundry.project_chief_inbox._provider_transport", return_value=fake):
            received = core.receive(UUID(project_id), channel_id, "reject-1", "111", "222", "Status?", True)
        outbound_id = received["outbound_id"]
        assert core.reject_pending_outbound(UUID(project_id), outbound_id, "binding_changed")
        assert not core.reject_pending_outbound(UUID(project_id), outbound_id, "binding_changed")
        assert core.pending_outbound_ids(UUID(project_id), channel_id) == []
        with pytest.raises(Conflict):
            core.claim_outbound(UUID(project_id), outbound_id)


def test_wechat_confirmed_pairing_and_cursor_batch(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        project_id, advisor_id, _, _ = _setup(client)
        chief_id, order_id = _chief(client, project_id, advisor_id)
        base = f"/api/projects/{project_id}/channels"
        created = client.put(
            f"{base}/wechat",
            json={
                "sender_id": "person@im.wechat",
                "chat_id": "person@im.wechat",
                "bot_id": "bot-1",
                "work_order_id": order_id,
                "profile_id": chief_id,
                "reply_policy": "bound_dm",
                "enabled": False,
            },
            headers=ORIGIN,
        )
        assert created.status_code == 200, created.text
        channel_id = UUID(created.json()["id"])
        core = _core(client)
        challenge_id = core.stage_pairing(
            UUID(project_id),
            channel_id,
            "person@im.wechat",
            "bot-1",
            "synthetic-wechat-token",
            datetime.now(UTC) + timedelta(minutes=5),
        )
        with pytest.raises(Conflict):
            core.confirm_pairing(UUID(project_id), channel_id, challenge_id, "wrong@im.wechat", "bot-1")
        confirmed = client.post(
            f"{base}/{channel_id}/pairing/{challenge_id}/confirm",
            json={"scanner_id": "person@im.wechat", "bot_id": "bot-1"},
            headers=ORIGIN,
        )
        assert confirmed.status_code == 200 and confirmed.json()["identity_confirmed"]
        assert not confirmed.json()["enabled"]
        assert core.secrets.present(channel_id)
        orphan_id = uuid4()
        core.secrets.write(orphan_id, "stale-synthetic-token")
        expiring_id = core.stage_pairing(
            UUID(project_id),
            channel_id,
            "person@im.wechat",
            "bot-1",
            "unused-synthetic-token",
            datetime.now(UTC) + timedelta(minutes=5),
        )
        core.expire_pairing(UUID(project_id), channel_id, expiring_id)
        core.cleanup_orphan_secrets()
        assert core.secrets.present(channel_id)
        assert not core.secrets.present(orphan_id)
        assert not core.secrets.present(expiring_id)
        enabled = client.put(
            f"{base}/wechat",
            json={
                "sender_id": "person@im.wechat",
                "chat_id": "person@im.wechat",
                "bot_id": "bot-1",
                "work_order_id": order_id,
                "profile_id": chief_id,
                "reply_policy": "bound_dm",
                "enabled": True,
            },
            headers=ORIGIN,
        )
        assert enabled.status_code == 200 and enabled.json()["enabled"]
        with pytest.raises(Conflict):
            core.save_cursor(UUID(project_id), channel_id, "", "cursor-1", ["unseen"])
        fake = FakeTransport(
            [
                TextResult(
                    json.dumps({"reply": "WeChat answer.", "action": "reply"}),
                    "wechat-response",
                    4,
                    3,
                    "completed",
                )
            ]
        )
        with patch("onefoundry.project_chief_inbox._provider_transport", return_value=fake):
            received = core.receive(
                UUID(project_id),
                channel_id,
                "w-1",
                "person@im.wechat",
                "person@im.wechat",
                "Question?",
                True,
                context_token="context-token",
            )
        with core.gateway.engine.connect() as db:
            assert (
                db.scalar(
                    text("SELECT source_kind FROM project_chief_turns WHERE project_id=:p"),
                    {"p": UUID(project_id)},
                )
                == "wechat"
            )
        core.cleanup_orphan_secrets()
        assert core.secrets.present(received["inbound_id"])
        core.save_cursor(UUID(project_id), channel_id, "", "cursor-1", ["w-1"])
        assert core.load_cursor(UUID(project_id), channel_id) == "cursor-1"
        claimed = core.claim_outbound(UUID(project_id), received["outbound_id"])
        assert claimed["context_token"] == "context-token"
        core.finish_outbound(UUID(project_id), received["outbound_id"], "accepted_by_platform", "w-sent")
        assert not core.secrets.present(received["inbound_id"])
        with pytest.raises(Conflict):
            core.save_cursor(UUID(project_id), channel_id, "", "cursor-2", [])
