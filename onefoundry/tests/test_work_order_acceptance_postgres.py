"""A saved run is not a completed job without declared evidence or operator review."""

import os
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient

from onefoundry.inference import TextRequest, TextResult
from onefoundry.main import create_app
from onefoundry.model_providers import DiscoveredModel
from onefoundry.settings import Settings

ORIGIN = {"origin": "http://localhost:8765"}


class FakeTransport:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[TextRequest] = []

    def generate(self, request: TextRequest) -> TextResult:
        self.calls.append(request)
        return TextResult(self.reply, "offline-reply", 12, 6, "completed")


def _client(tmp_path: Path) -> TestClient:
    root = tmp_path / "secrets"
    root.mkdir(mode=0o700)
    return TestClient(
        create_app(
            Settings(
                db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
                db_password=os.environ["APP_DB_PASSWORD"],
                model_secret_root=str(root),
            )
        ),
        base_url="http://localhost:8765",
    )


def _setup(client: TestClient) -> tuple[str, str]:
    project = client.post("/api/projects", json={"name": f"Acceptance {uuid4()}"}, headers=ORIGIN)
    assert project.status_code == 201, project.text
    base = f"/api/projects/{project.json()['id']}"
    account = client.post(
        f"{base}/model/accounts",
        json={"provider": "openai", "label": "Offline fake", "api_key": "test-only-api-key"},
        headers=ORIGIN,
    )
    assert account.status_code == 201, account.text
    with patch(
        "onefoundry.model_gateway.discover_models",
        return_value=[DiscoveredModel("gpt-test", "GPT Test", "text")],
    ):
        assert (
            client.post(f"{base}/model/accounts/{account.json()['id']}/refresh", headers=ORIGIN).status_code
            == 200
        )
    profile = client.post(
        f"{base}/profiles",
        json={
            "name": "Chief",
            "instructions": "State saved facts.",
            "model": "gpt-test",
            "model_account_id": account.json()["id"],
        },
        headers=ORIGIN,
    )
    assert profile.status_code == 201, profile.text
    return base, profile.json()["id"]


def _ready(client: TestClient, base: str, profile_id: str, kind: str = "operator_review") -> dict[str, Any]:
    created = client.post(
        f"{base}/work-orders",
        json={
            "title": "Finish the requested job",
            "instructions": "Deliver the finished result",
            "profile_id": profile_id,
            "deliverable_kind": kind,
        },
        headers=ORIGIN,
    )
    assert created.status_code == 201, created.text
    order = cast(dict[str, Any], created.json())
    assert client.post(f"{base}/work-orders/{order['id']}/ready", headers=ORIGIN).status_code == 200
    return order


def test_plan_waits_review_and_next_chat_knows_what_is_missing(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        base, profile_id = _setup(client)
        order = _ready(client, base, profile_id)
        fake = FakeTransport("Excellent plan. I propose assignments; none have been created.")
        with patch("onefoundry.live_text_store._provider_transport", return_value=fake):
            response = client.post(f"{base}/work-orders/{order['id']}/live-text-run", headers=ORIGIN)
        assert response.status_code == 201, response.text
        assert response.json()["run"]["status"] == "done"
        saved = next(row for row in client.get(f"{base}/work-orders").json() if row["id"] == order["id"])
        assert saved["status"] == "needs_review"
        conversation = client.post(f"{base}/work-orders/{order['id']}/conversation", json={}, headers=ORIGIN)
        assert conversation.status_code == 201, conversation.text
        chat = FakeTransport("No deliverable or assignments are recorded.")
        with patch("onefoundry.task_conversation_store._provider_transport", return_value=chat):
            turn = client.post(
                f"{base}/work-orders/{order['id']}/conversation/turns",
                json={
                    "conversation_id": conversation.json()["id"],
                    "client_turn_id": str(uuid4()),
                    "expected_sequence": 0,
                    "text": "Where is the deliverable?",
                    "attachment_ids": [],
                },
                headers=ORIGIN,
            )
        assert turn.status_code == 201, turn.text
        assert len(chat.calls) == 1
        assert "Status: needs_review" in chat.calls[0].prompt
        assert "Child assignments: none recorded" in chat.calls[0].prompt
        assert "Artifacts (saved path and SHA-256): none recorded" in chat.calls[0].prompt
        assert "Deliverable evidence: none recorded" in chat.calls[0].prompt


def test_review_actions_have_reason_version_and_state_guards(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        base, profile_id = _setup(client)
        order = _ready(client, base, profile_id)
        run = client.post(f"{base}/work-orders/{order['id']}/runs", headers=ORIGIN).json()
        finished = client.post(f"{base}/runs/{run['id']}/finish", json={"outcome": "done"}, headers=ORIGIN)
        assert finished.status_code == 200, finished.text
        path = f"{base}/work-orders/{order['id']}"
        pending = next(item for item in client.get(f"{base}/work-orders").json() if item["id"] == order["id"])
        assert pending["status"] == "needs_review"
        stale = {"reason": "Needs a result", "expected_version": order["version"]}
        assert client.post(f"{path}/accept", json=stale, headers=ORIGIN).status_code == 409
        returned = client.post(
            f"{path}/return",
            json={"reason": "Deliverable missing", "expected_version": pending["version"]},
            headers=ORIGIN,
        )
        assert returned.status_code == 200 and returned.json()["status"] == "ready"
        assert (
            client.post(
                f"{path}/return",
                json={"reason": "again", "expected_version": returned.json()["version"]},
                headers=ORIGIN,
            ).status_code
            == 409
        )
        second_run = client.post(f"{path}/runs", headers=ORIGIN).json()
        assert (
            client.post(
                f"{base}/runs/{second_run['id']}/finish", json={"outcome": "done"}, headers=ORIGIN
            ).status_code
            == 200
        )
        pending = next(item for item in client.get(f"{base}/work-orders").json() if item["id"] == order["id"])
        accepted = client.post(
            f"{path}/accept",
            json={"reason": "I accept the saved plan", "expected_version": pending["version"]},
            headers=ORIGIN,
        )
        assert accepted.status_code == 200 and accepted.json()["status"] == "done"
        assert (
            client.post(
                f"{path}/accept",
                json={"reason": "again", "expected_version": accepted.json()["version"]},
                headers=ORIGIN,
            ).status_code
            == 409
        )
        reopened = client.post(
            f"{path}/reopen",
            json={"reason": "Finished artifact absent", "expected_version": accepted.json()["version"]},
            headers=ORIGIN,
        )
        assert reopened.status_code == 200 and reopened.json()["status"] == "ready"
        assert (
            client.post(
                f"{path}/reopen",
                json={"reason": "again", "expected_version": reopened.json()["version"]},
                headers=ORIGIN,
            ).status_code
            == 409
        )
        events = client.get(f"{path}/activity").json()
        decisions = [(event["event_type"], event["payload"].get("reason")) for event in events]
        assert ("work_order_returned", "Deliverable missing") in decisions
        assert ("work_order_accepted", "I accept the saved plan") in decisions
        assert ("work_order_reopened", "Finished artifact absent") in decisions


def test_text_reply_is_opt_in_and_artifact_without_file_waits(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        base, profile_id = _setup(client)
        text_order = _ready(client, base, profile_id, "text_reply")
        fake = FakeTransport("Confirmed answer.")
        with patch("onefoundry.live_text_store._provider_transport", return_value=fake):
            response = client.post(f"{base}/work-orders/{text_order['id']}/live-text-run", headers=ORIGIN)
        assert response.status_code == 201, response.text
        saved = {row["id"]: row for row in client.get(f"{base}/work-orders").json()}
        assert saved[text_order["id"]]["status"] == "done"
        artifact_order = _ready(client, base, profile_id, "artifact")
        run = client.post(f"{base}/work-orders/{artifact_order['id']}/runs", headers=ORIGIN).json()
        assert (
            client.post(
                f"{base}/runs/{run['id']}/finish", json={"outcome": "done"}, headers=ORIGIN
            ).status_code
            == 200
        )
        saved = {row["id"]: row for row in client.get(f"{base}/work-orders").json()}
        assert saved[artifact_order["id"]]["status"] == "needs_review"


def test_unavailable_tools_block_before_provider_call(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        base, profile_id = _setup(client)
        order = _ready(client, base, profile_id)
        fake = FakeTransport("Should never be returned")
        with (
            patch(
                "onefoundry.live_text_store.live_tool_readiness",
                return_value=(False, "requested role tools are unavailable"),
            ),
            patch("onefoundry.live_text_store._provider_transport", return_value=fake),
        ):
            response = client.post(f"{base}/work-orders/{order['id']}/live-text-run", headers=ORIGIN)
        assert response.status_code == 409
        assert fake.calls == []
        saved = next(row for row in client.get(f"{base}/work-orders").json() if row["id"] == order["id"])
        assert saved["status"] == "blocked"
        assert saved["block_reason"] == "requested role tools are unavailable"
        assert client.get(f"{base}/runs").json() == []
