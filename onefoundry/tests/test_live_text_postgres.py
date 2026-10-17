"""One-shot live text behavior on migrated PostgreSQL with injected transport."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from onefoundry import coordination_store
from onefoundry.inference import (
    AuthenticationError,
    IncompleteResponseError,
    InvalidRequestError,
    ProviderError,
    RateLimitError,
    TextRequest,
    TextResult,
    TransportError,
)
from onefoundry.inference.http import HTTPResult
from onefoundry.inference.native_cloud import GeminiGenerateContentTransport
from onefoundry.inference.subscription_codex import CodexStreamFailure, CodexStreamIncomplete
from onefoundry.input_file_bytes import InputFileBytes
from onefoundry.input_file_worker import process_one
from onefoundry.live_text_store import LiveTextStore
from onefoundry.main import create_app
from onefoundry.model_providers import DiscoveredModel
from onefoundry.model_secrets import ModelSecretStore
from onefoundry.role_tool_admission import ToolDenied
from onefoundry.settings import Settings
from onefoundry.tool_gateway import ToolGateway

ORIGIN = {"origin": "http://localhost:8765"}


def _settings(tmp_path: Path) -> Settings:
    root = tmp_path / "model-secrets"
    root.mkdir(mode=0o700)
    input_root = tmp_path / "input-files"
    input_root.mkdir(mode=0o700)
    return Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(root),
        input_file_root=str(input_root),
    )


def _ready_order(client: TestClient, project_id: str, profile_id: str, title: str) -> str:
    path = f"/api/projects/{project_id}/work-orders"
    created = client.post(
        path,
        json={"title": title, "instructions": "Draft a concise response", "profile_id": profile_id},
        headers=ORIGIN,
    )
    assert created.status_code == 201
    order_id = created.json()["id"]
    assert client.post(f"{path}/{order_id}/ready", headers=ORIGIN).status_code == 200
    return str(order_id)


def _configured(client: TestClient) -> tuple[str, str, str]:
    project_id = client.post("/api/projects", json={"name": "Live text"}, headers=ORIGIN).json()["id"]
    model_path = f"/api/projects/{project_id}/model"
    account = client.post(
        f"{model_path}/accounts",
        json={"provider": "openai", "label": "Primary", "api_key": "test-only-api-key"},
        headers=ORIGIN,
    )
    assert account.status_code == 201
    account_id = account.json()["id"]
    with patch(
        "onefoundry.model_gateway.discover_models",
        return_value=[DiscoveredModel("gpt-test", "GPT Test", "text")],
    ):
        assert client.post(f"{model_path}/accounts/{account_id}/refresh", headers=ORIGIN).status_code == 200
    profile = client.post(
        f"/api/projects/{project_id}/profiles",
        json={
            "name": "Writer",
            "instructions": "Write plain language.",
            "model": "gpt-test",
            "model_account_id": account_id,
        },
        headers=ORIGIN,
    )
    assert profile.status_code == 201
    return project_id, profile.json()["id"], account_id


def _approved_memory(client: TestClient, project_id: str, summary: str, scope: str) -> str:
    base = f"/api/projects/{project_id}"
    profile_id = client.get(f"{base}/profiles").json()[0]["id"]
    order_id = _ready_order(client, project_id, profile_id, f"Record {summary}")
    run_id = client.post(f"{base}/work-orders/{order_id}/runs", headers=ORIGIN).json()["id"]
    assert (
        client.post(f"{base}/runs/{run_id}/finish", json={"outcome": "done"}, headers=ORIGIN).status_code
        == 200
    )
    result = client.post(
        f"{base}/runs/{run_id}/results", json={"summary": summary, "scope": scope}, headers=ORIGIN
    )
    assert result.status_code == 201
    candidate = client.post(f"{base}/results/{result.json()['id']}/candidates", json={}, headers=ORIGIN)
    assert candidate.status_code == 201
    review = client.post(
        f"{base}/candidates/{candidate.json()['id']}/review", json={"decision": "approved"}, headers=ORIGIN
    )
    assert review.status_code == 200
    memory = client.get(f"{base}/memory?query=Launch").json()
    return str(next(item["id"] for item in memory if item["text"] == summary))


class FakeTransport:
    def __init__(self, result: TextResult | Exception) -> None:
        self.result = result
        self.calls: list[TextRequest] = []

    def generate(self, request: TextRequest) -> TextResult:
        self.calls.append(request)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_assigned_agent_run_can_create_automation_rule(tmp_path: Path) -> None:
    settings = _settings(tmp_path).model_copy(update={"coordination_mode": "draft_only"})
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, profile_id, _account_id = _configured(client)
        order_id = _ready_order(client, project_id, profile_id, "Maintain an hourly competitor brief")
        rule = {
            "name": "Hourly competitor brief",
            "signal_type": "schedule.fired",
            "schedule": {"kind": "interval", "every_hours": 1},
            "target_profile_id": profile_id,
            "title": "Write competitor brief",
            "instructions": "Summarize competitor developments for the operator.",
        }
        fake = FakeTransport(
            TextResult(
                json.dumps(
                    {"reply": "I will keep this brief current.", "decision": "create_rule", "create": rule}
                ),
                "agent-run-rule",
                12,
                8,
                "completed",
            )
        )
        with patch("onefoundry.live_text_store._provider_transport", return_value=fake):
            response = client.post(
                f"/api/projects/{project_id}/work-orders/{order_id}/live-text-run", headers=ORIGIN
            )
        assert response.status_code == 201, response.text
        output = response.json()["output"]
        assert output["automation_decision"]["status"] == "saved"
        assert output["automation_decision"]["rule_version"] == 1
        assert "Automation rule created" in output["text"]
        assert "Project state (data)" in fake.calls[0].prompt
        listed = client.get(f"/api/projects/{project_id}/coordination/rules").json()
        assert len(listed) == 1
        assert listed[0]["last_agent_action"]["source_kind"] == "agent_run"
        assert listed[0]["last_agent_action"]["run_id"] == response.json()["run"]["id"]
        engine = create_engine(settings.database_url)
        with engine.connect() as connection:
            count = connection.scalar(
                text("SELECT count(*) FROM task_automation_decisions WHERE project_id=:p AND run_id=:r"),
                {"p": project_id, "r": response.json()["run"]["id"]},
            )
            created = connection.scalar(
                text("SELECT created_at FROM coordination_rule_versions WHERE rule_id=:r"),
                {"r": listed[0]["id"]},
            )
        assert count == 1
        coordinator = ToolGateway(
            engine, settings.onefoundry_coordinator_actor_id, live_text_settings=settings
        )
        assert coordination_store.fire_due_schedules(
            coordinator, settings, UUID(project_id), now=created + timedelta(hours=1, minutes=5)
        )
        for _ in range(10):
            if not coordination_store.run_once(coordinator, settings, UUID(project_id)):
                break
        generated = [
            item
            for item in client.get(f"/api/projects/{project_id}/work-orders").json()
            if item["origin_signal_id"] is not None
        ]
        assert len(generated) == 1
        assert generated[0]["title"] == "Write competitor brief"
        assert generated[0]["status"] == "draft"
        engine.dispose()


def test_agent_run_rule_switch_rejects_write_without_failing_result(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, profile_id, _account_id = _configured(client)
        base = f"/api/projects/{project_id}"
        control = client.get(f"{base}/coordination").json()
        changed = client.put(
            f"{base}/coordination/agent-rule-access",
            json={"enabled": False, "expected_version": control["version"]},
            headers=ORIGIN,
        )
        assert changed.status_code == 200
        order_id = _ready_order(client, project_id, profile_id, "Maintain a daily status")
        fake = FakeTransport(
            TextResult(
                json.dumps(
                    {
                        "reply": "I will set that up.",
                        "decision": "create_rule",
                        "create": {
                            "name": "Daily status",
                            "signal_type": "schedule.fired",
                            "schedule": {"kind": "daily", "at": "09:00"},
                            "target_profile_id": profile_id,
                            "title": "Write daily status",
                            "instructions": "Report progress.",
                        },
                    }
                ),
                "agent-run-switch",
                12,
                8,
                "completed",
            )
        )
        with patch("onefoundry.live_text_store._provider_transport", return_value=fake):
            response = client.post(f"{base}/work-orders/{order_id}/live-text-run", headers=ORIGIN)
        assert response.status_code == 201, response.text
        output = response.json()["output"]
        assert output["automation_decision"]["status"] == "rejected"
        assert "No project or Automation change was saved" in output["text"]
        assert client.get(f"{base}/coordination/rules").json() == []


def test_long_profile_instructions_reach_text_provider_intact(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, profile_id, _account_id = _configured(client)
        instructions = "Read every step.\n" * 2_800 + "Final instruction marker."
        assert 10_000 < len(instructions) < 50_000
        updated = client.patch(
            f"/api/projects/{project_id}/profiles/{profile_id}",
            json={"instructions": instructions},
            headers=ORIGIN,
        )
        assert updated.status_code == 200, updated.text
        order_id = _ready_order(client, project_id, profile_id, "Use the full profile")
        fake = FakeTransport(TextResult("Complete.", "long-profile", 10, 2, "completed"))
        with patch("onefoundry.live_text_store._provider_transport", return_value=fake):
            response = client.post(
                f"/api/projects/{project_id}/work-orders/{order_id}/live-text-run", headers=ORIGIN
            )
        assert response.status_code == 201, response.text
        assert len(fake.calls) == 1
        assert fake.calls[0].instructions.startswith(instructions + "\n\n")
        assert "durable Automation rule" in fake.calls[0].instructions
        detail = client.get(f"/api/projects/{project_id}/runs/{response.json()['run']['id']}").json()
        assert detail["profile_snapshots"][0]["profile_instructions"] == instructions


def test_live_text_work_order_file_is_used_and_recorded(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, profile_id, _account_id = _configured(client)
        order_id = _ready_order(client, project_id, profile_id, "Draft market plan")
        uploaded = client.post(
            f"/api/projects/{project_id}/input-files?scope=work_order&work_order_id={order_id}",
            content=b"Focus on mid-sized districts.",
            headers={**ORIGIN, "X-File-Name": "market.md", "Content-Type": "application/octet-stream"},
        )
        assert uploaded.status_code == 201, uploaded.text
        engine = create_engine(settings.database_url)
        assert process_one(engine, InputFileBytes(settings.input_file_root), settings)
        fake = FakeTransport(TextResult("Plan drafted.", "file-run", 8, 2, "completed"))
        with patch("onefoundry.live_text_store._provider_transport", return_value=fake):
            response = client.post(
                f"/api/projects/{project_id}/work-orders/{order_id}/live-text-run", headers=ORIGIN
            )
        assert response.status_code == 201, response.text
        assert "Focus on mid-sized districts." in fake.calls[0].prompt
        with engine.connect() as connection:
            uses = connection.scalar(
                text(
                    "SELECT count(*) FROM input_file_context_uses "
                    "WHERE project_id=:project_id AND run_id=:run_id"
                ),
                {"project_id": project_id, "run_id": response.json()["run"]["id"]},
            )
        assert uses == 1
        engine.dispose()


def test_live_success_persists_snapshots_tokens_unknown_cost_and_scope(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, profile_id, account_id = _configured(client)
        other_id = client.post("/api/projects", json={"name": "Other project"}, headers=ORIGIN).json()["id"]
        company_memory_id = _approved_memory(client, other_id, "Launch company evidence", "company")
        private_memory_id = _approved_memory(client, other_id, "Launch private evidence", "project")
        order_id = _ready_order(client, project_id, profile_id, "Summarize launch")
        fake = FakeTransport(TextResult("A concise draft.", "resp-test", 13, 7, "completed"))
        with patch("onefoundry.live_text_store._provider_transport", return_value=fake):
            response = client.post(
                f"/api/projects/{project_id}/work-orders/{order_id}/live-text-run", headers=ORIGIN
            )
        assert response.status_code == 201, response.text
        body = response.json()
        run = body["run"]
        output = body["output"]
        assert run["mode"] == "live_text" and run["status"] == "done"
        assert run["cost_usd"] is None and run["tokens_used"] == 20
        assert output["cost_usd"] is None and output["cost_usd_ticks"] is None
        assert output["text"] == "A concise draft."
        assert output["project_id"] == project_id and output["profile_id"] == profile_id
        assert output["account_id"] == account_id and output["actor_id"] == f"profile:{profile_id}"
        assert len(fake.calls) == 1 and fake.calls[0].model == "gpt-test"
        assert "Launch company evidence" in fake.calls[0].prompt
        assert "Launch private evidence" not in fake.calls[0].prompt
        assert company_memory_id in output["source_memory_ids"]
        assert private_memory_id not in output["source_memory_ids"]
        assert client.get(f"/api/projects/{project_id}/runs/{run['id']}/live-text-output").json() == output
        assert client.get(f"/api/projects/{other_id}/runs/{run['id']}/live-text-output").status_code == 404
        detail = client.get(f"/api/projects/{project_id}/runs/{run['id']}").json()
        assert detail["profile_snapshots"][0]["profile_model"] == "gpt-test"
        assert [event["event_type"] for event in detail["events"]][-4:] == [
            "live_text_started",
            "task_automation_decision",
            "work_order_completion_evaluated",
            "live_text_finished",
        ]
        assert detail["events"][-4]["actor_id"] == "operator"
        engine = create_engine(settings.database_url)
        with engine.connect() as connection:
            binding = (
                connection.execute(
                    text(
                        "SELECT actor_id,model_id,account_id,credential_revision,prompt_sha256 "
                        "FROM live_text_run_bindings WHERE project_id=:project_id AND run_id=:run_id"
                    ),
                    {"project_id": UUID(project_id), "run_id": UUID(run["id"])},
                )
                .mappings()
                .one()
            )
            assert binding["model_id"] == "gpt-test" and binding["account_id"] == UUID(account_id)
            assert binding["actor_id"] == "operator"
            assert binding["credential_revision"] == 0 and len(binding["prompt_sha256"]) == 64
            assert (
                connection.scalar(text("SELECT cost_usd FROM runs WHERE id=:id"), {"id": UUID(run["id"])})
                is None
            )
        engine.dispose()


def test_openai_api_key_with_admitted_tool_uses_sdk_continuation(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, profile_id, _account_id = _configured(client)
        update = client.patch(
            f"/api/projects/{project_id}/profiles/{profile_id}",
            json={"allowed_tools": ["current_work_order"]},
            headers=ORIGIN,
        )
        assert update.status_code == 200, update.text
        order_id = _ready_order(client, project_id, profile_id, "Use saved work context")
        with (
            patch("onefoundry.live_text_store._provider_transport") as one_shot,
            patch("onefoundry.live_tool_sdk.run_live_tool_sdk") as sdk,
        ):
            sdk.return_value = TextResult("Tool-backed draft.", "resp-sdk", 12, 6, "completed")
            response = client.post(
                f"/api/projects/{project_id}/work-orders/{order_id}/live-text-run",
                headers=ORIGIN,
            )
        assert response.status_code == 201, response.text
        one_shot.assert_not_called()
        sdk.assert_called_once()
        assert sdk.call_args.args[2].tools_active is True
        assert response.json()["output"]["text"] == "Tool-backed draft."
        detail = client.get(f"/api/projects/{project_id}/runs/{response.json()['run']['id']}").json()
        started = next(event for event in detail["events"] if event["event_type"] == "live_text_started")
        assert started["payload"]["tools_active"] is True


def test_admitted_tool_denial_is_recorded_as_failed_run(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, profile_id, _account_id = _configured(client)
        update = client.patch(
            f"/api/projects/{project_id}/profiles/{profile_id}",
            json={"allowed_tools": ["current_work_order"]},
            headers=ORIGIN,
        )
        assert update.status_code == 200
        order_id = _ready_order(client, project_id, profile_id, "Denied context read")
        with patch("onefoundry.live_tool_sdk.run_live_tool_sdk", side_effect=ToolDenied("revoked")):
            response = client.post(
                f"/api/projects/{project_id}/work-orders/{order_id}/live-text-run",
                headers=ORIGIN,
            )
        assert response.status_code == 502, response.text
        detail = client.get(f"/api/projects/{project_id}/runs/{response.json()['run_id']}").json()
        assert detail["status"] == "failed"
        assert detail["outcome_reason"] == "tool_rejected"


@pytest.mark.parametrize(
    "tool_id", ["repository_file_v1", "coding_artifact_text_v1", "github_pr_read_v1", "memory_search"]
)
def test_unavailable_tool_permissions_fail_before_provider_egress(tmp_path: Path, tool_id: str) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, profile_id, _account_id = _configured(client)
        update = client.patch(
            f"/api/projects/{project_id}/profiles/{profile_id}",
            json={"allowed_tools": [tool_id]},
            headers=ORIGIN,
        )
        assert update.status_code == 200, update.text
        order_id = _ready_order(client, project_id, profile_id, "Stay on the one-shot route")
        with (
            patch("onefoundry.live_text_store._provider_transport") as one_shot,
            patch("onefoundry.live_tool_sdk.run_live_tool_sdk") as sdk,
        ):
            response = client.post(
                f"/api/projects/{project_id}/work-orders/{order_id}/live-text-run", headers=ORIGIN
            )
        assert response.status_code == 409, response.text
        assert "tool" in response.json()["detail"].lower()
        one_shot.assert_not_called()
        sdk.assert_not_called()
        assert client.get(f"/api/projects/{project_id}/runs").json() == []
        assert client.get(f"/api/projects/{project_id}/work-orders").json()[0]["status"] == "blocked"


def test_live_failure_and_unknown_are_terminal_without_retry(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, profile_id, _account_id = _configured(client)
        for title, failure, expected_status, expected_code, diagnostic_code in (
            ("Auth reject", AuthenticationError("rejected"), "failed", 502, "authentication_rejected"),
            ("Rate limit", RateLimitError("rejected"), "failed", 502, "rate_limited"),
            ("Invalid model", InvalidRequestError("rejected"), "failed", 502, "invalid_request"),
            (
                "Provider failed terminally",
                CodexStreamFailure("response_failed_usage_limit"),
                "failed",
                502,
                "response_failed_usage_limit",
            ),
            (
                "Provider response incomplete",
                CodexStreamFailure("response_incomplete"),
                "failed",
                502,
                "response_incomplete",
            ),
            (
                "Stream ended without terminal event",
                CodexStreamIncomplete("stream_ended_without_completion"),
                "unknown",
                503,
                "stream_ended_without_completion",
            ),
            ("Network uncertain", TransportError("uncertain"), "unknown", 503, "transport_error"),
            (
                "Provider HTTP",
                ProviderError("Codex account request failed with HTTP 404"),
                "unknown",
                503,
                "provider_http_404",
            ),
            (
                "Incomplete stream",
                IncompleteResponseError("incomplete"),
                "unknown",
                503,
                "incomplete_response",
            ),
            (
                "Unexpected dispatch",
                RuntimeError("secret that must not be recorded"),
                "unknown",
                503,
                "unexpected_error",
            ),
        ):
            order_id = _ready_order(client, project_id, profile_id, title)
            fake = FakeTransport(failure)
            with patch("onefoundry.live_text_store._provider_transport", return_value=fake):
                response = client.post(
                    f"/api/projects/{project_id}/work-orders/{order_id}/live-text-run", headers=ORIGIN
                )
            assert response.status_code == expected_code
            run_id = response.json()["run_id"]
            detail = client.get(f"/api/projects/{project_id}/runs/{run_id}").json()
            assert detail["status"] == expected_status and detail["cost_usd"] is None
            finished = next(
                event for event in detail["events"] if event["event_type"] == "live_text_finished"
            )
            assert finished["payload"].get("diagnostic_code") == diagnostic_code
            assert "secret that must not be recorded" not in json.dumps(detail)
            assert detail["tokens_used"] is None
            assert client.get(f"/api/projects/{project_id}/runs/{run_id}/live-text-output").status_code == 404
            assert (
                client.post(
                    f"/api/projects/{project_id}/work-orders/{order_id}/live-text-run", headers=ORIGIN
                ).status_code
                == 409
            )
            assert len(fake.calls) == 1


def test_unknown_live_run_can_be_edited_and_explicitly_restarted(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, profile_id, _account_id = _configured(client)
        base = f"/api/projects/{project_id}"
        order_id = _ready_order(client, project_id, profile_id, "Original request")
        path = f"{base}/work-orders/{order_id}"
        uncertain = FakeTransport(TransportError("uncertain"))
        with patch("onefoundry.live_text_store._provider_transport", return_value=uncertain):
            first = client.post(f"{path}/live-text-run", headers=ORIGIN)
        assert first.status_code == 503 and len(uncertain.calls) == 1
        first_id = first.json()["run_id"]
        original = client.get(f"{base}/runs/{first_id}").json()
        order = next(row for row in client.get(f"{base}/work-orders").json() if row["id"] == order_id)
        assert order["status"] == "unknown" and original["status"] == "unknown"

        edited = client.put(
            path,
            json={
                "title": "Revised request",
                "instructions": "Write a shorter answer",
                "expected_version": order["version"],
            },
            headers=ORIGIN,
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["status"] == "unknown" and edited.json()["version"] > order["version"]
        assert client.get(f"{base}/runs/{first_id}").json()["work_order_title"] == "Original request"
        assert (
            client.put(
                path,
                json={"title": "Stale", "instructions": "Stale", "expected_version": order["version"]},
                headers=ORIGIN,
            ).status_code
            == 409
        )
        assert (
            client.put(
                path,
                json={
                    "title": "Unsafe",
                    "instructions": "password: secret-value",
                    "expected_version": edited.json()["version"],
                },
                headers=ORIGIN,
            ).status_code
            == 422
        )

        restart = {
            "prior_run_id": first_id,
            "expected_version": edited.json()["version"],
            "acknowledge_unknown": True,
        }
        assert client.post(
            f"{path}/live-text-restart", json={**restart, "acknowledge_unknown": False}, headers=ORIGIN
        ).status_code in {409, 422}
        assert (
            client.post(
                f"{path}/live-text-restart",
                json={**restart, "prior_run_id": str(UUID(int=1))},
                headers=ORIGIN,
            ).status_code
            == 409
        )
        assert len(uncertain.calls) == 1
        success = FakeTransport(TextResult("Revised answer", "response-restart", 7, 5, "completed"))
        with patch("onefoundry.live_text_store._provider_transport", return_value=success):
            second = client.post(f"{path}/live-text-restart", json=restart, headers=ORIGIN)
        assert second.status_code == 201, second.text
        second_id = second.json()["run"]["id"]
        assert second_id != first_id and len(success.calls) == 1
        assert second.json()["output"]["text"] == "Revised answer"
        assert "Revised request" in success.calls[0].prompt
        saved = [run for run in client.get(f"{base}/runs").json() if run["work_order_id"] == order_id]
        assert {(run["id"], run["status"]) for run in saved} == {(first_id, "unknown"), (second_id, "done")}
        assert client.get(f"{base}/runs/{first_id}").json()["work_order_title"] == "Original request"
        first_detail = client.get(f"{base}/runs/{first_id}").json()
        second_detail = client.get(f"{base}/runs/{second_id}").json()
        assert first_detail["work_order_title"] == "Original request"
        assert second_detail["work_order_title"] == "Revised request"
        assert all(event["run_id"] != second_id for event in first_detail["events"])
        assert all(event["run_id"] != first_id for event in second_detail["events"])
        assert any(event["event_type"] == "live_text_restart_requested" for event in second_detail["events"])
        assert client.post(f"{path}/live-text-restart", json=restart, headers=ORIGIN).status_code == 409
        assert len(success.calls) == 1
        other_project = client.post("/api/projects", json={"name": "Other project"}, headers=ORIGIN).json()[
            "id"
        ]
        assert (
            client.put(
                f"/api/projects/{other_project}/work-orders/{order_id}",
                json={
                    "title": "Other",
                    "instructions": "Other",
                    "expected_version": edited.json()["version"],
                },
                headers=ORIGIN,
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"/api/projects/{other_project}/work-orders/{order_id}/live-text-restart",
                json=restart,
                headers=ORIGIN,
            ).status_code
            == 404
        )


def test_concurrent_unknown_restart_dispatches_once(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, profile_id, _account_id = _configured(client)
        order_id = _ready_order(client, project_id, profile_id, "Concurrent restart")
        path = f"/api/projects/{project_id}/work-orders/{order_id}"
        uncertain = FakeTransport(TransportError("uncertain"))
        with patch("onefoundry.live_text_store._provider_transport", return_value=uncertain):
            first = client.post(f"{path}/live-text-run", headers=ORIGIN)
        assert first.status_code == 503
        order = next(
            row
            for row in client.get(f"/api/projects/{project_id}/work-orders").json()
            if row["id"] == order_id
        )
        restart = {
            "prior_run_id": first.json()["run_id"],
            "expected_version": order["version"],
            "acknowledge_unknown": True,
        }
        success = FakeTransport(TextResult("Only one answer", "response-once", 2, 3, "completed"))
        with patch("onefoundry.live_text_store._provider_transport", return_value=success):
            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(
                    pool.map(
                        lambda _: client.post(f"{path}/live-text-restart", json=restart, headers=ORIGIN),
                        range(2),
                    )
                )
        assert sorted(response.status_code for response in responses) == [201, 409]
        assert len(success.calls) == 1
        runs = [
            row
            for row in client.get(f"/api/projects/{project_id}/runs").json()
            if row["work_order_id"] == order_id
        ]
        assert len(runs) == 2


def test_legacy_secret_in_profile_instructions_blocks_egress(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, profile_id, _account_id = _configured(client)
        order_id = _ready_order(client, project_id, profile_id, "Unsafe legacy profile")
        engine = create_engine(settings.database_url)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE profiles SET instructions=:instructions WHERE project_id=:project_id AND id=:id"
                ),
                {
                    "project_id": UUID(project_id),
                    "id": UUID(profile_id),
                    "instructions": "Use password: legacy_sensitive_value",
                },
            )
        engine.dispose()
        fake = FakeTransport(TextResult("should not happen", "resp-test", 1, 1, "completed"))
        with patch("onefoundry.live_text_store._provider_transport", return_value=fake):
            response = client.post(
                f"/api/projects/{project_id}/work-orders/{order_id}/live-text-run", headers=ORIGIN
            )
        assert response.status_code == 409
        assert "legacy_sensitive_value" not in response.text
        assert fake.calls == []


def test_legacy_secret_in_work_order_blocks_egress(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, profile_id, _account_id = _configured(client)
        order_id = _ready_order(client, project_id, profile_id, "Unsafe legacy order")
        admin_settings = settings.model_copy(
            update={"db_user": "onefoundry", "db_password": os.environ["DB_PASSWORD"]}
        )
        engine = create_engine(admin_settings.database_url)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE work_orders SET instructions=:instructions "
                    "WHERE project_id=:project_id AND id=:id"
                ),
                {
                    "project_id": UUID(project_id),
                    "id": UUID(order_id),
                    "instructions": "Use password: legacy_sensitive_value",
                },
            )
        engine.dispose()
        fake = FakeTransport(TextResult("should not happen", "resp-test", 1, 1, "completed"))
        with patch("onefoundry.live_text_store._provider_transport", return_value=fake):
            response = client.post(
                f"/api/projects/{project_id}/work-orders/{order_id}/live-text-run", headers=ORIGIN
            )
        assert response.status_code == 409
        assert "legacy_sensitive_value" not in response.text
        assert fake.calls == []


def test_native_cloud_and_keyless_ollama_dispatch_with_selected_account(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id = client.post("/api/projects", json={"name": "Other providers"}, headers=ORIGIN).json()[
            "id"
        ]
        base = f"/api/projects/{project_id}"
        for provider, model_id, account_body, constructor_name in (
            ("grok", "grok-test", {"api_key": "test-only-grok-key"}, "XAIResponsesTransport"),
            (
                "anthropic",
                "claude-test",
                {"api_key": "test-only-anthropic-key"},
                "AnthropicMessagesTransport",
            ),
            (
                "gemini",
                "models/gemini-test",
                {"api_key": "test-only-gemini-key"},
                "GeminiGenerateContentTransport",
            ),
            ("ollama", "llama-test", {"endpoint": "http://ollama:11434"}, "OllamaTextTransport"),
        ):
            account = client.post(
                f"{base}/model/accounts",
                json={"provider": provider, "label": provider, **account_body},
                headers=ORIGIN,
            )
            assert account.status_code == 201, account.text
            account_id = account.json()["id"]
            with patch(
                "onefoundry.model_gateway.discover_models",
                return_value=[DiscoveredModel(model_id, model_id, "text")],
            ):
                refreshed = client.post(f"{base}/model/accounts/{account_id}/refresh", headers=ORIGIN)
            assert refreshed.status_code == 200, refreshed.text
            profile = client.post(
                f"{base}/profiles",
                json={
                    "name": f"{provider} writer",
                    "instructions": "Draft plainly.",
                    "model": model_id,
                    "model_account_id": account_id,
                },
                headers=ORIGIN,
            )
            assert profile.status_code == 201, profile.text
            order_id = _ready_order(client, project_id, profile.json()["id"], f"{provider} draft")
            ticks = 100_000_001 if provider == "grok" else None
            fake = FakeTransport(TextResult("A local draft.", "resp-test", 4, 3, "completed", ticks))
            with patch(f"onefoundry.live_text_store.{constructor_name}", return_value=fake) as constructor:
                response = client.post(f"{base}/work-orders/{order_id}/live-text-run", headers=ORIGIN)
            assert response.status_code == 201, response.text
            assert response.json()["output"]["provider"] == provider
            assert response.json()["output"]["account_id"] == account_id
            assert response.json()["output"]["cost_usd_ticks"] == ticks
            if provider == "grok":
                assert response.json()["run"]["cost_usd"] == 0.0100000001
                assert response.json()["output"]["cost_usd"] == 0.0100000001
                event = client.get(f"{base}/runs/{response.json()['run']['id']}").json()["events"][-1]
                assert event["payload"]["cost_source"] == "provider_reported"
                assert event["payload"]["cost_usd_ticks"] == ticks
                engine = create_engine(settings.database_url)
                with engine.connect() as connection:
                    saved = connection.scalar(
                        text("SELECT cost_usd FROM runs WHERE id=:id"),
                        {"id": UUID(response.json()["run"]["id"])},
                    )
                    assert saved == Decimal("0.0100000001")
                engine.dispose()
            else:
                assert response.json()["run"]["cost_usd"] is None
            assert len(fake.calls) == 1 and fake.calls[0].model == model_id
            if provider == "ollama":
                assert constructor.call_args.kwargs == {"endpoint": "http://ollama:11434"}
            else:
                assert constructor.call_args.kwargs["api_key"] == account_body["api_key"]


def test_gemini_thinking_tokens_reach_saved_run_total(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id = client.post("/api/projects", json={"name": "Thinking usage"}, headers=ORIGIN).json()[
            "id"
        ]
        base = f"/api/projects/{project_id}"
        account = client.post(
            f"{base}/model/accounts",
            json={"provider": "gemini", "label": "Gemini", "api_key": "test-only-key"},
            headers=ORIGIN,
        )
        assert account.status_code == 201, account.text
        account_id = account.json()["id"]
        with patch(
            "onefoundry.model_gateway.discover_models",
            return_value=[DiscoveredModel("models/gemini-test", "Gemini Test", "text")],
        ):
            assert (
                client.post(f"{base}/model/accounts/{account_id}/refresh", headers=ORIGIN).status_code == 200
            )
        profile = client.post(
            f"{base}/profiles",
            json={
                "name": "Thinking writer",
                "instructions": "Write plainly.",
                "model": "models/gemini-test",
                "model_account_id": account_id,
            },
            headers=ORIGIN,
        )
        assert profile.status_code == 201, profile.text
        order_id = _ready_order(client, project_id, profile.json()["id"], "Thinking draft")

        def fake_http(outbound: Request, *, timeout: float) -> HTTPResult:
            assert timeout == settings.live_text_timeout_seconds
            assert outbound.full_url.endswith("/models/gemini-test:generateContent")
            return HTTPResult(
                200,
                json.dumps(
                    {
                        "responseId": "resp-test",
                        "candidates": [
                            {"finishReason": "STOP", "content": {"parts": [{"text": "A draft."}]}}
                        ],
                        "usageMetadata": {
                            "promptTokenCount": 4,
                            "candidatesTokenCount": 2,
                            "thoughtsTokenCount": 8,
                            "totalTokenCount": 14,
                        },
                    }
                ).encode(),
            )

        def transport(*, endpoint: str, api_key: str) -> GeminiGenerateContentTransport:
            return GeminiGenerateContentTransport(endpoint=endpoint, api_key=api_key, http=fake_http)

        with patch("onefoundry.live_text_store.GeminiGenerateContentTransport", side_effect=transport):
            run = client.post(f"{base}/work-orders/{order_id}/live-text-run", headers=ORIGIN)
        assert run.status_code == 201, run.text
        assert run.json()["run"]["tokens_used"] == 14
        assert run.json()["output"]["input_tokens"] == 4
        assert run.json()["output"]["output_tokens"] == 10
        assert run.json()["run"]["cost_usd"] is None


def test_interrupted_live_run_reconciles_unknown_on_startup(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id, profile_id, _account_id = _configured(client)
        order_id = _ready_order(client, project_id, profile_id, "Interrupted")
        gateway = ToolGateway(create_engine(settings.database_url), settings.operator_actor_id)
        store = LiveTextStore(gateway, settings, ModelSecretStore(settings.model_secret_root))
        prepared = store._prepare(UUID(project_id), UUID(order_id))
        gateway.engine.dispose()
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        detail = client.get(f"/api/projects/{project_id}/runs/{prepared.run_id}").json()
        assert detail["status"] == "unknown" and detail["cost_usd"] is None
        assert detail["tokens_used"] is None
        assert detail["events"][-1]["payload"]["reason"] == "process_interrupted"
