"""Project Chief conversation can create and discuss work without a channel order binding."""

import json
import os
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from onefoundry.inference import TextRequest, TextResult
from onefoundry.main import create_app
from onefoundry.model_providers import DiscoveredModel
from onefoundry.settings import Settings
from onefoundry.tool_gateway import Conflict

ORIGIN = {"origin": "http://localhost:8765"}


class FakeTransport:
    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.requests: list[TextRequest] = []

    def generate(self, request: TextRequest) -> TextResult:
        self.requests.append(request)
        return TextResult(self.replies.pop(0), f"reply-{len(self.requests)}", 12, 9, "completed")


def _setup(tmp_path: Path) -> tuple[TestClient, UUID, UUID, UUID]:
    secret_root = tmp_path / "model-secrets"
    secret_root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(secret_root),
        remote_channel_network_enabled=False,
    )
    client = TestClient(create_app(settings), base_url="http://localhost:8765")
    client.__enter__()
    project = client.post("/api/projects", json={"name": "Chief inbox test"}, headers=ORIGIN)
    assert project.status_code == 201, project.text
    project_id = UUID(project.json()["id"])
    base = f"/api/projects/{project_id}"
    account = client.post(
        f"{base}/model/accounts",
        json={"provider": "openai", "label": "Synthetic account", "api_key": "test-only-api-key"},
        headers=ORIGIN,
    )
    assert account.status_code == 201, account.text
    with patch(
        "onefoundry.model_gateway.discover_models",
        return_value=[DiscoveredModel("gpt-test", "GPT Test", "text")],
    ):
        refreshed = client.post(f"{base}/model/accounts/{account.json()['id']}/refresh", headers=ORIGIN)
    assert refreshed.status_code == 200, refreshed.text
    chief = next(row for row in client.get(f"{base}/profiles").json() if row["name"] == "Chief of Staff")
    updated = client.patch(
        f"{base}/profiles/{chief['id']}",
        json={"model": "gpt-test", "model_account_id": account.json()["id"]},
        headers=ORIGIN,
    )
    assert updated.status_code == 200, updated.text
    configured = client.put(
        f"{base}/channels/discord",
        json={
            "sender_id": "111",
            "chat_id": None,
            "profile_id": chief["id"],
            "reply_policy": "bound_dm",
            "enabled": False,
        },
        headers=ORIGIN,
    )
    assert configured.status_code == 200, configured.text
    channel_id = UUID(configured.json()["id"])
    credential = client.put(
        f"{base}/channels/{channel_id}/credential",
        json={"credential": "synthetic-discord-token"},
        headers=ORIGIN,
    )
    assert credential.status_code == 200, credential.text
    assert isinstance(client.app, FastAPI)
    core = client.app.state.remote_channels
    core.observe_identity(project_id, channel_id, "111", "222", True)
    confirmed = client.post(
        f"{base}/channels/{channel_id}/identity/confirm",
        json={"sender_id": "111", "chat_id": "222"},
        headers=ORIGIN,
    )
    assert confirmed.status_code == 200, confirmed.text
    enabled = client.put(
        f"{base}/channels/discord",
        json={
            "sender_id": "111",
            "chat_id": "222",
            "profile_id": chief["id"],
            "reply_policy": "bound_dm",
            "enabled": True,
        },
        headers=ORIGIN,
    )
    assert enabled.status_code == 200, enabled.text
    return client, project_id, UUID(chief["id"]), channel_id


def test_discord_project_inbox_creates_and_continues_work_without_selected_order(tmp_path: Path) -> None:
    client, project_id, chief_id, channel_id = _setup(tmp_path)
    try:
        assert isinstance(client.app, FastAPI)
        core = client.app.state.remote_channels
        with core.gateway.engine.connect() as db:
            assert (
                db.scalar(text("SELECT count(*) FROM work_orders WHERE project_id=:p"), {"p": project_id})
                == 0
            )
        create = json.dumps(
            {
                "reply": "I have enough detail to save the assignment.",
                "action": "create_work_order",
                "work_order": {
                    "title": "Prepare launch brief",
                    "instructions": "Draft a launch brief for the operator.",
                    "profile_id": str(chief_id),
                    "deliverable_kind": "document",
                    "acceptance_checks": ["A reviewable brief exists"],
                },
            }
        )
        fake = FakeTransport([create])
        with patch("onefoundry.project_chief_inbox._provider_transport", return_value=fake):
            received = core.receive(
                project_id, channel_id, "discord-1", "111", "222", "Start a launch brief task", True
            )
        with core.gateway.engine.connect() as db:
            state = [
                dict(row)
                for row in db.execute(
                    text("SELECT status,reason,action FROM project_chief_turns WHERE project_id=:p"),
                    {"p": project_id},
                ).mappings()
            ]
        assert received["status"] == "done" and received["outbound_id"], (received, state)
        assert len(fake.requests) == 1
        assert core.receive(
            project_id, channel_id, "discord-1", "111", "222", "Start a launch brief task", True
        )["duplicate"]
        assert len(fake.requests) == 1
        turns = client.get(f"/api/projects/{project_id}/chief-inbox/turns").json()
        assert len(turns) == 1 and turns[0]["source_kind"] == "discord"
        created_id = UUID(turns[0]["created_work_order_id"])
        with core.gateway.engine.connect() as db:
            order = (
                db.execute(
                    text(
                        "SELECT parent_work_order_id,origin_chief_turn_id,status FROM work_orders "
                        "WHERE project_id=:p AND id=:id"
                    ),
                    {"p": project_id, "id": created_id},
                )
                .mappings()
                .one()
            )
        assert order["parent_work_order_id"] is None
        assert order["origin_chief_turn_id"] == UUID(turns[0]["id"])
        assert order["status"] == "draft"
        focus = json.dumps(
            {
                "reply": "I found the launch brief assignment.",
                "action": "focus_work_order",
                "target_work_order_id": str(created_id),
            }
        )
        fake = FakeTransport([focus])
        with patch("onefoundry.project_chief_inbox._provider_transport", return_value=fake):
            continued = core.receive(
                project_id, channel_id, "discord-2", "111", "222", "Continue the launch brief", True
            )
        assert continued["status"] == "done"
        turns = client.get(f"/api/projects/{project_id}/chief-inbox/turns").json()
        assert turns[-1]["target_work_order_id"] == str(created_id)
        follow_up = json.dumps(
            {
                "reply": "I can add that section as follow-up work.",
                "action": "create_work_order",
                "parent_work_order_id": str(created_id),
                "work_order": {
                    "title": "Add competitor section",
                    "instructions": "Add a competitor section to the launch brief.",
                    "profile_id": str(chief_id),
                    "deliverable_kind": "document",
                    "acceptance_checks": ["Competitor section is reviewable"],
                },
            }
        )
        with patch(
            "onefoundry.project_chief_inbox._provider_transport", return_value=FakeTransport([follow_up])
        ):
            more = core.receive(
                project_id, channel_id, "discord-3", "111", "222", "Continue the brief with competitors", True
            )
        assert more["status"] == "done"
        turns = client.get(f"/api/projects/{project_id}/chief-inbox/turns").json()
        child_id = UUID(turns[-1]["created_work_order_id"])
        with core.gateway.engine.connect() as db:
            assert (
                db.scalar(
                    text("SELECT parent_work_order_id FROM work_orders WHERE project_id=:p AND id=:id"),
                    {"p": project_id, "id": child_id},
                )
                == created_id
            )
        with pytest.raises(Conflict):
            core.receive(project_id, channel_id, "discord-4", "999", "222", "Wrong account", True)
    finally:
        client.__exit__(None, None, None)


def test_app_project_inbox_accepts_message_without_channel_or_work_order(tmp_path: Path) -> None:
    client, _, _, _ = _setup(tmp_path)
    try:
        project = client.post("/api/projects", json={"name": "In-app only"}, headers=ORIGIN)
        assert project.status_code == 201, project.text
        project_id = UUID(project.json()["id"])
        assert isinstance(client.app, FastAPI)
        with client.app.state.remote_channels.gateway.engine.connect() as db:
            assert (
                db.scalar(text("SELECT count(*) FROM work_orders WHERE project_id=:p"), {"p": project_id})
                == 0
            )
            assert (
                db.scalar(
                    text(
                        "SELECT count(*) FROM remote_channels WHERE project_id=:p "
                        "AND (enabled OR sender_id IS NOT NULL)"
                    ),
                    {"p": project_id},
                )
                == 0
            )
        response = client.post(
            f"/api/projects/{project_id}/chief-inbox/turns",
            json={"client_turn_id": "eb9caa65-2c65-4cf4-a7c0-a083627f8259", "text": "Plan the first task"},
            headers=ORIGIN,
        )
        assert response.status_code == 202, response.text
        assert response.json()["status"] in {"pending", "blocked"}
        assert response.json()["source_kind"] == "operator_ui"
        assert "remote_inbound_id" not in response.json()
        replay = client.post(
            f"/api/projects/{project_id}/chief-inbox/turns",
            json={"client_turn_id": "eb9caa65-2c65-4cf4-a7c0-a083627f8259", "text": "Plan the first task"},
            headers=ORIGIN,
        )
        assert replay.status_code == 202 and replay.json()["id"] == response.json()["id"]
        changed = client.post(
            f"/api/projects/{project_id}/chief-inbox/turns",
            json={"client_turn_id": "eb9caa65-2c65-4cf4-a7c0-a083627f8259", "text": "Different task"},
            headers=ORIGIN,
        )
        assert changed.status_code == 409
        listed = client.get(f"/api/projects/{project_id}/chief-inbox/turns")
        assert listed.status_code == 200 and listed.json()[0]["id"] == response.json()["id"]
        client.app.state.remote_channels.process_one_project_message()
        settled = client.get(f"/api/projects/{project_id}/chief-inbox/turns").json()
        assert settled[0]["status"] == "blocked"
    finally:
        client.__exit__(None, None, None)
