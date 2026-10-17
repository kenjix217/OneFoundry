"""Explicit operator agent selection uses the existing SDK worker and scoped tools."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import pytest
from agents.testing import ModelStep, ScriptedModel, assistant_message
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from onefoundry.main import create_app
from onefoundry.task_session_attempts import TaskSessionAttempts
from onefoundry.task_session_context import TaskSessionContext
from onefoundry.task_session_runs import bind
from onefoundry.task_session_tools import TaskSessionTools
from onefoundry.tool_gateway import ToolGateway
from tests.test_team_workflow_postgres import ORIGIN, _scripted_worker, project_with_models, settings_for
from tests.test_team_workflow_postgres import quiet_worker as quiet_worker


def test_selected_agent_owns_one_replayable_sdk_conversation(tmp_path: Path, quiet_worker: None) -> None:
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, profiles = project_with_models(client)
        path = f"/api/projects/{project}/chief-workspace"
        request = {
            "client_message_id": str(uuid4()),
            "profile_id": profiles["Researcher"],
            "body": "Explain the saved product research.",
        }
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(
                pool.map(lambda _: client.post(path + "/messages", json=request, headers=ORIGIN), range(2))
            )
        assert sorted(response.status_code for response in responses) == [200, 202]
        receipt = responses[0].json()
        assert responses[1].json() == receipt
        assert client.get(path + "/messages/" + request["client_message_id"]).json() == receipt
        row = client.get(path).json()[0]
        assert row["profile_id"] == profiles["Researcher"] and not row["canonical_chief"]
        assert row["direct_agent"] and row["completion_policy"] == "operator_review"
        gateway = ToolGateway(
            create_engine(settings.database_url), "fixture-worker", live_text_settings=settings
        )
        model = ScriptedModel(
            [ModelStep(output=[assistant_message("The saved product brief is the starting point.")])]
        )
        result = _scripted_worker(gateway, settings, [model]).run_next(UUID(project))
        assert result is not None and result["outcome"] == "done", result
        view_path = f"/api/projects/{project}/work-orders/{receipt['order_id']}/team-session"
        view = client.get(view_path).json()
        assert view["session"]["profile_id"] == profiles["Researcher"]
        assert len(view["attempts"]) == 1 and view["attempts"][0]["status"] == "done"
        assert view["messages"][-1]["body"] == "The saved product brief is the starting point."
        changed = client.post(
            path + "/messages", json={**request, "profile_id": profiles["GTM"]}, headers=ORIGIN
        )
        assert changed.status_code == 409 and len(client.get(path).json()) == 1
        followup = client.post(
            path + "/messages",
            json={
                "client_message_id": str(uuid4()),
                "order_id": row["id"],
                "expected_version": client.get(path).json()[0]["version"],
                "expected_sequence": view["messages"][-1]["sequence"],
                "body": "Continue the explanation.",
            },
            headers=ORIGIN,
        )
        assert followup.status_code == 202, followup.text
        claimed = TaskSessionAttempts(gateway).claim_next(UUID(project))
        assert claimed is not None and str(claimed["profile_id"]) == profiles["Researcher"]
        bind(gateway, UUID(project), claimed)
        snapshot = TaskSessionContext(gateway, settings).prepare(UUID(project), claimed["id"])
        assert "Agent: Researcher" in snapshot.prompt and "Chief oversight scope:" not in snapshot.prompt
        tools = {
            item["name"]
            for namespace in TaskSessionTools(
                gateway, UUID(project), claimed["session_id"], claimed["id"]
            ).specifications()
            for item in namespace["tools"]
        }
        assert (
            not {
                "read_project_status",
                "read_work_order",
                "read_agent_session",
                "submit_completion",
                "delegate_agent",
            }
            & tools
        )
        gateway.engine.dispose()


@pytest.mark.parametrize("target", ["foreign", "missing", "malformed"])
def test_invalid_agent_selection_has_no_partial_writes(
    tmp_path: Path, quiet_worker: None, target: str
) -> None:
    with TestClient(create_app(settings_for(tmp_path)), base_url="http://localhost:8765") as client:
        project, _ = project_with_models(client)
        foreign = client.post("/api/projects", json={"name": "Other company"}, headers=ORIGIN).json()["id"]
        profile = (
            client.get(f"/api/projects/{foreign}/profiles").json()[0]["id"]
            if target == "foreign"
            else str(uuid4())
            if target == "missing"
            else "not-an-id"
        )
        response = client.post(
            f"/api/projects/{project}/chief-workspace/messages",
            json={"client_message_id": str(uuid4()), "profile_id": profile, "body": "A new question."},
            headers=ORIGIN,
        )
        assert response.status_code == (422 if target == "malformed" else 404), response.text
        assert client.get(f"/api/projects/{project}/chief-workspace").json() == []


@pytest.mark.parametrize("outcome", ["unknown", "done", "needs_review"])
def test_direct_unknown_and_accepted_work_only_allow_effect_free_discussion(
    tmp_path: Path, quiet_worker: None, outcome: str
) -> None:
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, profiles = project_with_models(client)
        path = f"/api/projects/{project}/chief-workspace"
        response = client.post(
            path + "/messages",
            json={
                "client_message_id": str(uuid4()),
                "profile_id": profiles["GTM"],
                "body": "Review the product brief.",
            },
            headers=ORIGIN,
        )
        assert response.status_code == 202, response.text
        gateway = ToolGateway(
            create_engine(settings.database_url), "fixture-worker", live_text_settings=settings
        )
        attempts = TaskSessionAttempts(gateway)
        claimed = attempts.claim_next(UUID(project))
        assert claimed is not None
        bind(gateway, UUID(project), claimed)
        attempt_outcome: Literal["unknown", "done"] = "unknown" if outcome == "unknown" else "done"
        attempts.settle(
            UUID(project),
            claimed["id"],
            attempt_outcome,
            reason="fixture_saved_outcome" if outcome == "unknown" else None,
            public_reply="Saved result for review." if outcome != "unknown" else None,
        )
        if outcome != "unknown":
            with gateway.engine.begin() as connection:
                connection.execute(
                    text("UPDATE work_orders SET status=:status WHERE project_id=:p AND id=:o"),
                    {"p": UUID(project), "o": claimed["root_work_order_id"], "status": outcome},
                )
        row = client.get(path).json()[0]
        discussion = client.post(
            path + "/messages",
            json={
                "client_message_id": str(uuid4()),
                "order_id": row["id"],
                "expected_version": row["version"],
                "expected_sequence": 1 if outcome == "unknown" else 2,
                "body": "Explain what is saved; do not repeat the action.",
            },
            headers=ORIGIN,
        )
        assert discussion.status_code == 202 and discussion.json()["disposition"] == "discussion", (
            discussion.text
        )
        next_attempt = attempts.claim_next(UUID(project))
        assert next_attempt is not None
        bind(gateway, UUID(project), next_attempt)
        specifications = TaskSessionTools(
            gateway, UUID(project), next_attempt["session_id"], next_attempt["id"]
        ).specifications()
        assert {item["name"] for namespace in specifications for item in namespace["tools"]} == {
            "publish_update"
        }
        snapshot = TaskSessionContext(gateway, settings).prepare(UUID(project), next_attempt["id"])
        assert "inspection-only discussion" in snapshot.prompt
        with gateway.engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT status FROM task_session_attempts WHERE project_id=:p AND id=:a"),
                    {"p": UUID(project), "a": claimed["id"]},
                )
                == attempt_outcome
            )
        gateway.engine.dispose()


def test_historical_specialist_root_is_not_admitted_as_a_direct_conversation(
    tmp_path: Path, quiet_worker: None
) -> None:
    with TestClient(create_app(settings_for(tmp_path)), base_url="http://localhost:8765") as client:
        project, profiles = project_with_models(client)
        order = client.post(
            f"/api/projects/{project}/work-orders",
            json={
                "title": "Historical specialist job",
                "instructions": "Keep the existing assignment owner.",
                "profile_id": profiles["Researcher"],
                "deliverable_kind": "text_reply",
            },
            headers=ORIGIN,
        ).json()
        path = f"/api/projects/{project}/chief-workspace"
        before = client.get(path).json()
        assert not before[0]["direct_agent"]
        response = client.post(
            path + "/messages",
            json={
                "client_message_id": str(uuid4()),
                "order_id": order["id"],
                "expected_version": order["version"],
                "body": "Continue the historical job.",
            },
            headers=ORIGIN,
        )
        assert response.status_code == 409, response.text
        assert client.get(path).json() == before


def test_unconfigured_direct_agent_saves_setup_state_without_queuing_a_turn(
    tmp_path: Path, quiet_worker: None
) -> None:
    with TestClient(
        create_app(settings_for(tmp_path, enabled=False)), base_url="http://localhost:8765"
    ) as client:
        project, profiles = project_with_models(client)
        path = f"/api/projects/{project}/chief-workspace"
        response = client.post(
            path + "/messages",
            json={
                "client_message_id": str(uuid4()),
                "profile_id": profiles["Engineer"],
                "body": "Discuss the architecture.",
            },
            headers=ORIGIN,
        )
        assert response.status_code == 202, response.text
        assert response.json()["attempt_id"] is None and response.json()["disposition"] == "awaiting_setup"
        assert client.get(path).json()[0]["state"] == "Setup needed"
