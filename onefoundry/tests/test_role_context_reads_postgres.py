"""Scoped project-context tools use owner records without exposing raw evidence."""

import json
import os
from collections.abc import Iterator, Sequence
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.dialects.postgresql import JSONB

from onefoundry.main import create_app
from onefoundry.role_tool_admission import ToolDenied, ToolOutputRejected
from onefoundry.settings import Settings
from onefoundry.tool_gateway import ToolGateway

TOOLS = ["project_brief_v1", "recent_project_work_v1", "run_evidence_v1", "approved_memory_v1"]


@pytest.fixture
def settings() -> Settings:
    return Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        app_public_port=8765,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url="http://localhost:8765") as value:
        yield value


@pytest.fixture
def gateway(settings: Settings) -> Iterator[ToolGateway]:
    engine = create_engine(settings.database_url)
    try:
        yield ToolGateway(engine, "operator", live_text_settings=settings)
    finally:
        engine.dispose()


def _post(client: TestClient, path: str, body: dict[str, object] | None = None) -> dict[str, object]:
    response = client.post(path, json=body or {}, headers={"origin": "http://localhost:8765"})
    assert response.status_code in (200, 201), response.text
    return response.json()  # type: ignore[no-any-return]


def _patch(client: TestClient, path: str, body: dict[str, object]) -> dict[str, object]:
    response = client.patch(path, json=body, headers={"origin": "http://localhost:8765"})
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


def _role_run(
    client: TestClient,
    gateway: ToolGateway,
    *,
    project_id: UUID | None = None,
    memory_scope: str = "company",
) -> tuple[UUID, UUID, UUID]:
    if project_id is None:
        project_id = UUID(str(_post(client, "/api/projects", {"name": f"Context {uuid4()}"})["id"]))
    profiles = client.get(f"/api/projects/{project_id}/profiles").json()
    profile_id = UUID(next(item for item in profiles if item["name"] == "Researcher")["id"])
    _patch(
        client,
        f"/api/projects/{project_id}/profiles/{profile_id}",
        {"allowed_tools": TOOLS, "memory_scope": memory_scope},
    )
    order = _post(
        client,
        f"/api/projects/{project_id}/work-orders",
        {
            "title": "Research active work",
            "instructions": "Use admitted project reads",
            "profile_id": str(profile_id),
        },
    )
    order_id = UUID(str(order["id"]))
    _post(client, f"/api/projects/{project_id}/work-orders/{order_id}/ready")
    prepared = gateway.start_synthetic_text(project_id, order_id)
    return project_id, UUID(str(prepared["run_id"])), profile_id


def _manual_result(
    client: TestClient, project_id: UUID, *, summary: str, scope: str
) -> tuple[UUID, UUID, UUID]:
    profiles = client.get(f"/api/projects/{project_id}/profiles").json()
    profile_id = profiles[0]["id"]
    root = f"/api/projects/{project_id}"
    order = _post(
        client,
        f"{root}/work-orders",
        {"title": "Completed evidence", "instructions": "Save an operator result", "profile_id": profile_id},
    )
    _post(client, f"{root}/work-orders/{order['id']}/ready")
    run = _post(client, f"{root}/work-orders/{order['id']}/runs")
    _post(client, f"{root}/runs/{run['id']}/finish", {"outcome": "done"})
    result = _post(client, f"{root}/runs/{run['id']}/results", {"summary": summary, "scope": scope})
    candidate = _post(client, f"{root}/results/{result['id']}/candidates")
    return UUID(str(run["id"])), UUID(str(result["id"])), UUID(str(candidate["id"]))


def _call(
    gateway: ToolGateway,
    project_id: UUID,
    run_id: UUID,
    profile_id: UUID,
    tool_id: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    return gateway.invoke_role_tool(
        project_id, run_id, profile_id, uuid4().hex, tool_id, arguments, tool_version=1
    )


def test_project_brief_and_recent_work_are_bounded_and_project_scoped(
    client: TestClient, gateway: ToolGateway, settings: Settings
) -> None:
    project_id, run_id, profile_id = _role_run(client, gateway)
    _patch(
        client,
        f"/api/projects/{project_id}/brief",
        {"goal": "Improve handoffs", "desired_outcome": "Evidence ready", "expected_version": 1},
    )
    other_id = UUID(str(_post(client, "/api/projects", {"name": "Other project"})["id"]))
    other_profile = client.get(f"/api/projects/{other_id}/profiles").json()[0]["id"]
    _post(
        client,
        f"/api/projects/{other_id}/work-orders",
        {"title": "Other private task", "instructions": "Private", "profile_id": other_profile},
    )
    advertised = gateway.available_role_tools(project_id, run_id, profile_id)
    assert {item["id"] for item in advertised} == set(TOOLS)
    brief = _call(gateway, project_id, run_id, profile_id, "project_brief_v1", {})["result"]
    assert brief == {
        "project_id": str(project_id),
        "name": client.get(f"/api/projects/{project_id}").json()["name"],
        "goal": "Improve handoffs",
        "desired_outcome": "Evidence ready",
    }
    limited = ToolGateway(
        gateway.engine,
        "operator",
        live_text_settings=settings.model_copy(update={"role_context_max_items": 1}),
    )
    first = _call(limited, project_id, run_id, profile_id, "recent_project_work_v1", {})["result"]
    assert first["items"][0]["title"] == "Research active work"  # type: ignore[index]
    assert first["has_more"] is False  # type: ignore[index]
    _post(
        client,
        f"/api/projects/{project_id}/work-orders",
        {"title": "New draft", "instructions": "Do not return this text", "profile_id": str(profile_id)},
    )
    recent = _call(limited, project_id, run_id, profile_id, "recent_project_work_v1", {})["result"]
    assert recent["has_more"] is True  # type: ignore[index]
    assert len(recent["items"]) == 1  # type: ignore[index]
    encoded = json.dumps(recent)
    assert "Other private task" not in encoded
    assert "Do not return this text" not in encoded
    assert "New draft" in encoded


def test_run_evidence_exposes_result_and_metadata_without_paths_or_payloads(
    client: TestClient, gateway: ToolGateway
) -> None:
    project_id, run_id, profile_id = _role_run(client, gateway)
    historical_run, result_id, _ = _manual_result(
        client, project_id, summary="Reviewed the submitted change", scope="project"
    )
    job_id = uuid4()
    artifact_id = uuid4()
    with gateway.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO runner_jobs (id,project_id,actor_id,run_id,stage,status) "
                "VALUES (:id,:project_id,'operator',:run_id,'engineer','queued')"
            ),
            {"id": job_id, "project_id": project_id, "run_id": historical_run},
        )
        connection.execute(
            text(
                "INSERT INTO runner_artifacts "
                "(id,project_id,actor_id,run_id,job_id,kind,path,sha256,byte_size) "
                "VALUES (:id,:project_id,'operator',:run_id,:job_id,'diff',:path,:sha256,12)"
            ),
            {
                "id": artifact_id,
                "project_id": project_id,
                "run_id": historical_run,
                "job_id": job_id,
                "path": "private/fixture-sentinel-path",
                "sha256": "a" * 64,
            },
        )
        connection.execute(
            text(
                "INSERT INTO events (id,project_id,run_id,work_order_id,actor_id,event_type,payload) "
                "SELECT :id,:project_id,:run_id,work_order_id,'operator','fixture_event',:payload "
                "FROM runs WHERE project_id=:project_id AND id=:run_id"
            ).bindparams(bindparam("payload", type_=JSONB)),
            {
                "id": uuid4(),
                "project_id": project_id,
                "run_id": historical_run,
                "payload": {"token": "fixture-sentinel-payload"},
            },
        )
    evidence = _call(
        gateway, project_id, run_id, profile_id, "run_evidence_v1", {"run_id": str(historical_run)}
    )["result"]
    assert evidence["found"] is True  # type: ignore[index]
    assert evidence["result"]["result_id"] == str(result_id)  # type: ignore[index]
    assert evidence["result"]["summary"] == "Reviewed the submitted change"  # type: ignore[index]
    assert evidence["artifacts"][0] == {  # type: ignore[index]
        "artifact_id": str(artifact_id),
        "source": "synthetic",
        "kind": "diff",
        "sha256": "a" * 64,
        "byte_size": 12,
    }
    assert any(event["event_type"] == "fixture_event" for event in evidence["events"])  # type: ignore[index]
    encoded = json.dumps(evidence)
    assert "fixture-sentinel-path" not in encoded
    assert "fixture-sentinel-payload" not in encoded
    other_project = UUID(str(_post(client, "/api/projects", {"name": "Other evidence"})["id"]))
    other_run, _, _ = _manual_result(client, other_project, summary="Other result", scope="project")
    hidden = _call(gateway, project_id, run_id, profile_id, "run_evidence_v1", {"run_id": str(other_run)})[
        "result"
    ]
    assert hidden["found"] is False  # type: ignore[index]
    assert hidden["result"] is None  # type: ignore[index]
    assert hidden["artifacts"] == []  # type: ignore[index]


def test_approved_memory_uses_publication_and_both_profile_scopes(
    client: TestClient, gateway: ToolGateway
) -> None:
    project_id, run_id, profile_id = _role_run(client, gateway)
    other_project = UUID(str(_post(client, "/api/projects", {"name": "Other memory"})["id"]))
    _, _, pending_id = _manual_result(
        client, other_project, summary="Unreviewed comet record", scope="company"
    )
    root = f"/api/projects/{other_project}"
    before = _call(gateway, project_id, run_id, profile_id, "approved_memory_v1", {"query": "comet"})
    assert before["result"]["items"] == []  # type: ignore[index]
    _post(client, f"{root}/candidates/{pending_id}/review", {"decision": "approved"})
    company = _call(gateway, project_id, run_id, profile_id, "approved_memory_v1", {"query": "comet"})
    assert [item["text"] for item in company["result"]["items"]] == [  # type: ignore[index]
        "Unreviewed comet record"
    ]
    _, _, local_id = _manual_result(client, project_id, summary="Local comet record", scope="project")
    _post(client, f"/api/projects/{project_id}/candidates/{local_id}/review", {"decision": "approved"})
    _patch(
        client,
        f"/api/projects/{project_id}/profiles/{profile_id}",
        {"memory_scope": "project"},
    )
    restricted = _call(gateway, project_id, run_id, profile_id, "approved_memory_v1", {"query": "comet"})
    assert [item["text"] for item in restricted["result"]["items"]] == [  # type: ignore[index]
        "Local comet record"
    ]
    _, project_run, project_profile = _role_run(
        client, gateway, project_id=project_id, memory_scope="project"
    )
    _patch(
        client,
        f"/api/projects/{project_id}/profiles/{project_profile}",
        {"memory_scope": "company"},
    )
    snapshot_restricted = _call(
        gateway, project_id, project_run, project_profile, "approved_memory_v1", {"query": "comet"}
    )
    assert [item["text"] for item in snapshot_restricted["result"]["items"]] == [  # type: ignore[index]
        "Local comet record"
    ]


def test_typed_arguments_fail_closed_and_do_not_leak_source_text(
    client: TestClient, gateway: ToolGateway
) -> None:
    project_id, run_id, profile_id = _role_run(client, gateway)
    invalid_calls: tuple[tuple[str, dict[str, object]], ...] = (
        ("project_brief_v1", {"other_project": str(uuid4())}),
        ("run_evidence_v1", {}),
        ("run_evidence_v1", {"run_id": "bad-uuid"}),
        ("run_evidence_v1", {"run_id": str(uuid4()), "extra": True}),
        ("approved_memory_v1", {"query": "x" * 121}),
        ("approved_memory_v1", {"query": "api_key=fixture-secret"}),
        ("approved_memory_v1", {"query": 42}),
    )
    for tool_id, arguments in invalid_calls:
        with pytest.raises(ToolDenied):
            _call(gateway, project_id, run_id, profile_id, tool_id, arguments)
    with gateway.engine.connect() as connection:
        rows: Sequence[dict[str, object]] = (
            connection.execute(
                text(
                    "SELECT payload FROM events WHERE project_id=:project_id AND run_id=:run_id "
                    "AND event_type='role_tool_denied'"
                ),
                {"project_id": project_id, "run_id": run_id},
            )
            .scalars()
            .all()
        )
    assert len(rows) == 7
    assert all(row["reason"] == "invalid_arguments" for row in rows)
    assert "fixture-secret" not in json.dumps(rows)
    limited = ToolGateway(
        gateway.engine,
        "operator",
        live_text_settings=gateway.live_text_settings.model_copy(  # type: ignore[union-attr]
            update={"live_text_max_output_bytes": 1024}
        ),
    )
    _patch(
        client,
        f"/api/projects/{project_id}/brief",
        {"goal": "g" * 2000, "desired_outcome": "Useful", "expected_version": 1},
    )
    with pytest.raises(ToolOutputRejected):
        _call(limited, project_id, run_id, profile_id, "project_brief_v1", {})
