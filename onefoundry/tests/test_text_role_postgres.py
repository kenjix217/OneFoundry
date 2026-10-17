"""Real database and SDK checks for the synthetic Researcher to GTM flow."""

import json
import os
from collections.abc import Iterator, Mapping
from typing import Any, cast
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, OperationalError

from onefoundry.main import create_app
from onefoundry.scripted_text_sdk import run_research_to_gtm
from onefoundry.settings import Settings
from onefoundry.tool_gateway import ToolGateway


@pytest.fixture
def settings() -> Settings:
    return Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"), db_password=os.environ["APP_DB_PASSWORD"]
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url="http://localhost:8765") as test_client:
        yield test_client


def post(client: TestClient, path: str, body: Mapping[str, object] | None = None) -> Response:
    return cast(Response, client.post(path, json=body, headers={"origin": "http://localhost:8765"}))


def project(client: TestClient, name: str) -> tuple[str, dict[str, dict[str, object]]]:
    project_id = post(client, "/api/projects", {"name": name}).json()["id"]
    profiles = client.get(f"/api/projects/{project_id}/profiles").json()
    return project_id, {row["name"]: cast(dict[str, object], row) for row in profiles}


def ready_order(client: TestClient, project_id: str, profile_id: object, title: str) -> str:
    path = f"/api/projects/{project_id}"
    order = post(
        client,
        f"{path}/work-orders",
        {
            "title": title,
            "instructions": "Prepare Launch text",
            "profile_id": profile_id,
        },
    ).json()
    assert post(client, f"{path}/work-orders/{order['id']}/ready").status_code == 200
    return cast(str, order["id"])


def publish_memory(
    client: TestClient,
    project_id: str,
    engineer_id: object,
    summary: str,
    scope: str,
) -> str:
    path = f"/api/projects/{project_id}"
    order_id = ready_order(client, project_id, engineer_id, "Launch source")
    run = post(client, f"{path}/work-orders/{order_id}/runs").json()
    assert post(client, f"{path}/runs/{run['id']}/finish", {"outcome": "done"}).status_code == 200
    result = post(
        client,
        f"{path}/runs/{run['id']}/results",
        {
            "summary": summary,
            "scope": scope,
        },
    ).json()
    candidate = post(client, f"{path}/results/{result['id']}/candidates", {}).json()
    assert (
        post(
            client,
            f"{path}/candidates/{candidate['id']}/review",
            {
                "decision": "approved",
            },
        ).status_code
        == 200
    )
    memories = client.get(f"{path}/memory?query=Launch").json()
    return cast(str, next(row["id"] for row in memories if row["text"] == summary))


def synthetic_run(client: TestClient, project_id: str, order_id: str) -> Response:
    return post(
        client,
        f"/api/projects/{project_id}/work-orders/{order_id}/synthetic-text-run",
        {"fixture": "research_to_gtm"},
    )


def test_sdk_handoff_persists_role_outputs_events_and_citations(
    client: TestClient, settings: Settings
) -> None:
    source_project, source_profiles = project(client, "Source company")
    company_id = publish_memory(
        client,
        source_project,
        source_profiles["Engineer"]["id"],
        "Launch company source",
        "company",
    )
    target_project, profiles = project(client, "Target company")
    order_id = ready_order(client, target_project, profiles["Researcher"]["id"], "Launch analysis")
    path = f"/api/projects/{target_project}"
    eligibility = client.get(f"{path}/work-orders/{order_id}/synthetic-text-eligibility")
    assert eligibility.status_code == 200 and eligibility.json()["eligible"] is True
    response = synthetic_run(client, target_project, order_id)
    assert response.status_code == 201, response.text
    data = response.json()
    run = data["run"]
    outputs = data["outputs"]
    assert run["status"] == "done" and run["mode"] == "synthetic_text"
    assert run["profile_id"] == profiles["GTM"]["id"]
    assert run["tokens_used"] == 0 and run["cost_usd"] == 0
    assert [row["role"] for row in outputs] == ["Researcher", "GTM"]
    assert all(row["source_kind"] == "synthetic_scripted_sdk" for row in outputs)
    assert all(row["tokens_used"] == 0 and row["cost_usd"] == 0 for row in outputs)
    assert all(row["source_memory_ids"] == [company_id] for row in outputs)
    assert all(row["source_memories"][0]["text"] == "Launch company source" for row in outputs)
    assert all(company_id in row["text"] for row in outputs)
    assert all(row["actor_id"] == f"profile:{row['profile_id']}" for row in outputs)
    assert client.get(f"{path}/runs/{run['id']}/text-outputs").json() == outputs
    detail = client.get(f"{path}/runs/{run['id']}").json()
    events = detail["events"]
    event_types = [event["event_type"] for event in events]
    assert event_types.count("synthetic_text_output_recorded") == 2
    assert "synthetic_text_handed_off" in event_types
    assert "synthetic_text_finished" in event_types
    assert all("Launch company source" not in json.dumps(event["payload"]) for event in events)
    role_events = [
        event
        for event in events
        if event["event_type"]
        in {"synthetic_text_output_recorded", "synthetic_text_handed_off", "synthetic_text_finished"}
    ]
    assert all(event["actor_id"].startswith("profile:") for event in role_events)
    assert synthetic_run(client, target_project, order_id).status_code == 409
    assert client.get(f"{path}/work-orders").json()[0]["status"] == "needs_review"
    engine = create_engine(settings.database_url)
    with engine.connect() as connection:
        db_count = connection.scalar(
            text("SELECT count(*) FROM text_outputs WHERE project_id = :id AND run_id = :run_id"),
            {"id": UUID(target_project), "run_id": UUID(run["id"])},
        )
    assert db_count == 2
    engine.dispose()


def test_synthetic_text_role_snapshots_survive_later_profile_edits(client: TestClient) -> None:
    project_id, profiles = project(client, "Synthetic text audit")
    root = f"/api/projects/{project_id}"
    for name in ("Researcher", "GTM"):
        response = client.patch(
            f"{root}/profiles/{profiles[name]['id']}",
            json={"model": f"{name.lower()}-before", "instructions": f"{name} before"},
            headers={"origin": "http://localhost:8765"},
        )
        assert response.status_code == 200, response.text
    order_id = ready_order(client, project_id, profiles["Researcher"]["id"], "Text audit")
    run_response = synthetic_run(client, project_id, order_id)
    assert run_response.status_code == 201, run_response.text
    run_id = run_response.json()["run"]["id"]
    for name in ("Researcher", "GTM"):
        response = client.patch(
            f"{root}/profiles/{profiles[name]['id']}",
            json={"model": f"{name.lower()}-after", "instructions": f"{name} after"},
            headers={"origin": "http://localhost:8765"},
        )
        assert response.status_code == 200, response.text
    detail = client.get(f"{root}/runs/{run_id}").json()
    assert [row["profile_name"] for row in detail["profile_snapshots"]] == ["Researcher", "GTM"]
    assert [row["profile_model"] for row in detail["profile_snapshots"]] == [
        "researcher-before",
        "gtm-before",
    ]
    assert [row["profile_instructions"] for row in detail["profile_snapshots"]] == [
        "Researcher before",
        "GTM before",
    ]
    assert detail["profile_model"] == "gtm-before"
    assert [row["configured_model"] for row in client.get(f"{root}/runs/{run_id}/text-outputs").json()] == [
        "researcher-before",
        "gtm-before",
    ]


def test_gtm_project_scope_excludes_company_memory_from_sdk_input(client: TestClient) -> None:
    source_project, source_profiles = project(client, "Company source")
    publish_memory(
        client,
        source_project,
        source_profiles["Engineer"]["id"],
        "Launch company-only marker",
        "company",
    )
    target_project, profiles = project(client, "Scoped target")
    project_id = publish_memory(
        client,
        target_project,
        profiles["Engineer"]["id"],
        "Launch project-only marker",
        "project",
    )
    patch_response = cast(
        Response,
        client.patch(
            f"/api/projects/{target_project}/profiles/{profiles['GTM']['id']}",
            json={"memory_scope": "project"},
            headers={"origin": "http://localhost:8765"},
        ),
    )
    assert patch_response.status_code == 200
    order_id = ready_order(client, target_project, profiles["Researcher"]["id"], "Launch scope")
    captured: list[dict[str, Any]] = []

    def run_with_capture(**kwargs: Any) -> Any:
        captured.extend(kwargs["source_memories"])
        return run_research_to_gtm(**kwargs)

    with patch("onefoundry.tool_gateway.run_research_to_gtm", side_effect=run_with_capture):
        response = synthetic_run(client, target_project, order_id)
    assert response.status_code == 201, response.text
    assert [str(item["id"]) for item in captured] == [project_id]
    outputs = response.json()["outputs"]
    assert all(row["source_memory_ids"] == [project_id] for row in outputs)
    assert all("company-only marker" not in row["text"] for row in outputs)


def test_ineligible_input_failure_and_unknown_reconciliation(client: TestClient) -> None:
    project_id, profiles = project(client, "Failure tests")
    path = f"/api/projects/{project_id}"
    wrong_order = ready_order(client, project_id, profiles["QA"]["id"], "Wrong role")
    eligible = client.get(f"{path}/work-orders/{wrong_order}/synthetic-text-eligibility").json()
    assert eligible["eligible"] is False
    assert synthetic_run(client, project_id, wrong_order).status_code == 409
    order_id = ready_order(client, project_id, profiles["Researcher"]["id"], "Launch failure")
    assert (
        post(
            client,
            f"{path}/work-orders/{order_id}/synthetic-text-run",
            {
                "fixture": "other",
            },
        ).status_code
        == 422
    )
    assert client.get(f"{path}/work-orders/{uuid4()}/synthetic-text-eligibility").status_code == 404
    assert (
        client.get(f"/api/projects/{uuid4()}/work-orders/{order_id}/synthetic-text-eligibility").status_code
        == 404
    )
    assert client.get(f"{path}/runs/{uuid4()}/text-outputs").status_code == 404
    assert (
        client.get(
            f"{path}/work-orders/{order_id}/synthetic-text-eligibility", headers={"host": "bad.example"}
        ).status_code
        == 400
    )
    assert (
        cast(
            Response,
            client.post(
                f"{path}/work-orders/{order_id}/synthetic-text-run",
                json={"fixture": "research_to_gtm"},
                headers={"origin": "http://evil.example"},
            ),
        ).status_code
        == 403
    )
    with patch(
        "onefoundry.tool_gateway.run_research_to_gtm", side_effect=RuntimeError("private failure detail")
    ):
        failed = synthetic_run(client, project_id, order_id)
    assert failed.status_code == 502 and "private failure detail" not in failed.text
    run = client.get(f"{path}/runs").json()[0]
    assert run["status"] == "failed" and run["mode"] == "synthetic_text"
    assert client.get(f"{path}/runs/{run['id']}/text-outputs").json() == []
    events = client.get(f"{path}/runs/{run['id']}").json()["events"]
    assert any(event["event_type"] == "synthetic_text_failed" for event in events)
    assert all("private failure detail" not in json.dumps(event["payload"]) for event in events)


def test_ambiguous_post_commit_error_reads_done_owner_state(client: TestClient) -> None:
    project_id, profiles = project(client, "Commit readback")
    order_id = ready_order(client, project_id, profiles["Researcher"]["id"], "Launch readback")
    original = ToolGateway.complete_synthetic_text

    def commit_then_raise(self: ToolGateway, *args: Any, **kwargs: Any) -> Any:
        original(self, *args, **kwargs)
        raise OperationalError("after commit", {}, Exception())

    with patch.object(ToolGateway, "complete_synthetic_text", commit_then_raise):
        response = synthetic_run(client, project_id, order_id)
    assert response.status_code == 201
    assert response.json()["run"]["status"] == "done"
    assert len(response.json()["outputs"]) == 2


def test_database_rejects_cross_project_private_citation(client: TestClient, settings: Settings) -> None:
    source_project, source_profiles = project(client, "Private citation source")
    private_id = publish_memory(
        client,
        source_project,
        source_profiles["Engineer"]["id"],
        "Launch private source",
        "project",
    )
    target_project, profiles = project(client, "Private citation target")
    order_id = ready_order(client, target_project, profiles["Researcher"]["id"], "Launch target")
    response = synthetic_run(client, target_project, order_id)
    assert response.status_code == 201
    output_id = response.json()["outputs"][0]["id"]
    engine = create_engine(settings.database_url)
    with pytest.raises(IntegrityError, match="not visible to project"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO text_output_sources "
                    "(id, project_id, actor_id, text_output_id, source_project_id, memory_item_id) "
                    "VALUES (:id, :project_id, 'operator', :output_id, :source_project_id, :memory_id)"
                ),
                {
                    "id": uuid4(),
                    "project_id": UUID(target_project),
                    "output_id": UUID(output_id),
                    "source_project_id": UUID(source_project),
                    "memory_id": UUID(private_id),
                },
            )
    engine.dispose()
