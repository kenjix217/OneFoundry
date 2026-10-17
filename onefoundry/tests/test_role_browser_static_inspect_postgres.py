"""Browser role calls bind one approved fixture to one durable job."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from onefoundry.browser_static_fixtures import StaticFixtureStore
from onefoundry.browser_static_jobs import BrowserStaticJobStore
from onefoundry.role_browser_static_inspect import TOOL_ID, _call_key, parse_arguments
from onefoundry.role_tool_admission import (
    LIVE_SDK_EXCLUDED_IDS,
    ToolDenied,
    ToolOutcomeUnknown,
)
from onefoundry.tool_gateway import ToolGateway
from onefoundry.workflow_schemas import ApprovalDecision
from runner.browser_static_job_spool import BrowserJobError, BrowserStaticJobSpool
from tests.test_browser_static_fixtures_postgres import proposal
from tests.test_role_tool_admission_postgres import _post, _run

pytest_plugins = ["tests.test_browser_static_fixtures_postgres"]

IMAGE = "sha256:" + "b" * 64


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"fixture_id": "not-a-uuid"},
        {"fixture_id": str(UUID(int=0))},
        {"fixture_id": str(uuid4()), "url": "http://example.invalid"},
        {"fixture_id": str(uuid4()), "html": "<main>unapproved</main>"},
        {"fixture_id": str(uuid4()), "css": "body{}"},
        {"fixture_id": str(uuid4()), "image_id": IMAGE},
    ],
)
def test_parse_rejects_missing_malformed_and_role_supplied_source(value: object) -> None:
    with pytest.raises(ValueError):
        parse_arguments(value)


def test_parse_accepts_only_fixture_uuid() -> None:
    fixture_id = uuid4()
    assert parse_arguments({"fixture_id": str(fixture_id)}) == {"fixture_id": str(fixture_id)}


def _owner(gateway: ToolGateway, store: StaticFixtureStore, tmp_path: Path) -> BrowserStaticJobStore:
    root = tmp_path / "browser-spool"
    root.mkdir(mode=0o700)
    jobs = BrowserStaticJobStore(gateway, store, BrowserStaticJobSpool(root), IMAGE, 30)
    settings = gateway.live_text_settings
    assert settings is not None
    gateway.live_text_settings = settings.model_copy(
        update={"browser_static_enabled": True, "browser_static_image_id": IMAGE}
    )
    gateway.browser_static_jobs = jobs
    return jobs


def _fixture(gateway: ToolGateway, store: StaticFixtureStore, project_id: UUID, run_id: UUID) -> UUID:
    fixture = store.create(project_id, run_id, proposal())
    gateway.decide_approval(project_id, fixture["approval_id"], ApprovalDecision(decision="approved"))
    return UUID(str(fixture["id"]))


def _call(
    gateway: ToolGateway,
    project_id: UUID,
    run_id: UUID,
    profile_id: UUID,
    call_id: str,
    fixture_id: UUID,
    *,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    arguments: dict[str, object] = {"fixture_id": str(fixture_id)}
    arguments.update(extra or {})
    return gateway.invoke_role_tool(
        project_id, run_id, profile_id, call_id, TOOL_ID, arguments, tool_version=1
    )


def test_researcher_queues_once_with_profile_actor_and_hash_only_events(
    client: TestClient, gateway: ToolGateway, store: StaticFixtureStore, tmp_path: Path
) -> None:
    project_id, run_id, profile_id = _run(client, gateway, tools=[TOOL_ID])
    assert TOOL_ID in LIVE_SDK_EXCLUDED_IDS
    assert gateway.available_role_tools(project_id, run_id, profile_id) == []
    jobs = _owner(gateway, store, tmp_path)
    fixture_id = _fixture(gateway, store, project_id, run_id)
    advertised = gateway.available_role_tools(project_id, run_id, profile_id)
    assert [item["id"] for item in advertised] == [TOOL_ID]
    assert advertised[0]["parameters"]["additionalProperties"] is False
    first = _call(gateway, project_id, run_id, profile_id, "browser-one", fixture_id)
    replay = _call(gateway, project_id, run_id, profile_id, "browser-one", fixture_id)
    assert first == replay
    assert first["status"] == "queued"
    row = jobs.by_idempotency_key(project_id, run_id, _call_key(project_id, run_id, "browser-one"))
    assert row is not None
    assert row["actor_id"] == f"profile:{profile_id}"
    assert row["fixture_id"] == fixture_id
    assert "inspection_text" not in jobs.get(project_id, run_id, row["id"])
    assert [item["id"] for item in jobs.list_for_run(project_id, run_id)] == [row["id"]]
    with gateway.engine.connect() as connection:
        events = (
            connection.execute(
                text(
                    "SELECT event_type,actor_id,payload FROM events WHERE project_id=:project_id "
                    "AND run_id=:run_id AND event_type IN "
                    "('role_tool_started','role_tool_finished','browser_static_job_queued') "
                    "ORDER BY created_at,id"
                ),
                {"project_id": project_id, "run_id": run_id},
            )
            .mappings()
            .all()
        )
    assert [event["event_type"] for event in events] == [
        "role_tool_started",
        "browser_static_job_queued",
        "role_tool_finished",
    ]
    assert all(event["actor_id"] == f"profile:{profile_id}" for event in events)
    event_text = json.dumps([event["payload"] for event in events])
    assert "Approved page" not in event_text and "<main>" not in event_text
    assert "fixture.html" not in event_text
    other_fixture = _fixture(gateway, store, project_id, run_id)
    with pytest.raises(ToolDenied, match="already bound"):
        _call(gateway, project_id, run_id, profile_id, "browser-one", other_fixture)
    with gateway.engine.begin() as connection:
        connection.execute(
            text("UPDATE profiles SET allowed_tools='[]'::jsonb WHERE project_id=:project_id AND id=:id"),
            {"project_id": project_id, "id": profile_id},
        )
    with pytest.raises(ToolDenied):
        _call(gateway, project_id, run_id, profile_id, "browser-one", fixture_id)


def test_disabled_browser_static_policy_does_not_advertise_tool(
    client: TestClient, gateway: ToolGateway, store: StaticFixtureStore, tmp_path: Path
) -> None:
    project_id, run_id, profile_id = _run(client, gateway, tools=[TOOL_ID])
    _owner(gateway, store, tmp_path)
    assert [tool["id"] for tool in gateway.available_role_tools(project_id, run_id, profile_id)] == [TOOL_ID]
    settings = gateway.live_text_settings
    assert settings is not None
    gateway.live_text_settings = settings.model_copy(update={"browser_static_enabled": False})
    assert gateway.available_role_tools(project_id, run_id, profile_id) == []


def test_malformed_denied_and_unapproved_fixture_never_launch(
    client: TestClient, gateway: ToolGateway, store: StaticFixtureStore, tmp_path: Path
) -> None:
    project_id, run_id, profile_id = _run(client, gateway, tools=[TOOL_ID])
    jobs = _owner(gateway, store, tmp_path)
    fixture_id = _fixture(gateway, store, project_id, run_id)
    with pytest.raises(ToolDenied):
        _call(gateway, project_id, run_id, profile_id, "extra", fixture_id, extra={"url": "http://evil"})
    with pytest.raises(ToolDenied):
        gateway.invoke_role_tool(
            project_id,
            run_id,
            profile_id,
            "malformed",
            TOOL_ID,
            {"fixture_id": "not-a-uuid"},
            tool_version=1,
        )
    with pytest.raises(ToolDenied):
        _call(gateway, project_id, run_id, profile_id, "unapproved", uuid4())
    assert jobs.by_idempotency_key(project_id, run_id, _call_key(project_id, run_id, "extra")) is None
    with gateway.engine.begin() as connection:
        connection.execute(
            text("UPDATE profiles SET allowed_tools='[]'::jsonb WHERE project_id=:project_id AND id=:id"),
            {"project_id": project_id, "id": profile_id},
        )
    assert gateway.available_role_tools(project_id, run_id, profile_id) == []
    with pytest.raises(ToolDenied):
        _call(gateway, project_id, run_id, profile_id, "revoked", fixture_id)


def test_replay_does_not_relaunch_unknown_or_cross_run_fixture(
    client: TestClient, gateway: ToolGateway, store: StaticFixtureStore, tmp_path: Path
) -> None:
    project_id, run_id, profile_id = _run(client, gateway, tools=[TOOL_ID])
    jobs = _owner(gateway, store, tmp_path)
    fixture_id = _fixture(gateway, store, project_id, run_id)
    with gateway.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO events (id,project_id,work_order_id,run_id,actor_id,event_type,payload) "
                "SELECT :id,:project_id,work_order_id,:run_id,:actor_id,'role_tool_started',"
                "jsonb_build_object('call_id','orphan','tool_id',CAST(:tool_id AS text),"
                "'arguments_sha256',CAST(:digest AS text)) "
                "FROM runs WHERE project_id=:project_id AND id=:run_id"
            ),
            {
                "id": uuid4(),
                "project_id": project_id,
                "run_id": run_id,
                "actor_id": f"profile:{profile_id}",
                "tool_id": TOOL_ID,
                "digest": hashlib.sha256(
                    json.dumps(
                        {"fixture_id": str(fixture_id)}, sort_keys=True, separators=(",", ":")
                    ).encode()
                ).hexdigest(),
            },
        )
    with pytest.raises(ToolOutcomeUnknown):
        _call(gateway, project_id, run_id, profile_id, "orphan", fixture_id)
    assert jobs.by_idempotency_key(project_id, run_id, _call_key(project_id, run_id, "orphan")) is None
    other_project, other_run, other_profile = _run(client, gateway, tools=[TOOL_ID])
    with pytest.raises(ToolDenied):
        _call(gateway, other_project, other_run, other_profile, "cross-run", fixture_id)
    assert (
        jobs.by_idempotency_key(other_project, other_run, _call_key(other_project, other_run, "cross-run"))
        is None
    )


def test_stage_failure_is_unknown_and_replay_never_stages_again(
    client: TestClient,
    gateway: ToolGateway,
    store: StaticFixtureStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, run_id, profile_id = _run(client, gateway, tools=[TOOL_ID])
    jobs = _owner(gateway, store, tmp_path)
    fixture_id = _fixture(gateway, store, project_id, run_id)
    stage_count = 0

    def failed_stage(*_args: object) -> None:
        nonlocal stage_count
        stage_count += 1
        raise BrowserJobError("simulated stage failure")

    monkeypatch.setattr(jobs.spool, "stage", failed_stage)
    with pytest.raises(ToolOutcomeUnknown):
        _call(gateway, project_id, run_id, profile_id, "stage-failed", fixture_id)
    replay = _call(gateway, project_id, run_id, profile_id, "stage-failed", fixture_id)
    assert replay["status"] == "unknown"
    assert stage_count == 1
    row = jobs.by_idempotency_key(project_id, run_id, _call_key(project_id, run_id, "stage-failed"))
    assert row is not None and row["actor_id"] == f"profile:{profile_id}"


def test_qa_is_admitted_only_as_builtin(
    client: TestClient, gateway: ToolGateway, store: StaticFixtureStore, tmp_path: Path
) -> None:
    project = _post(client, "/api/projects", {"name": f"Browser QA {uuid4()}"})
    project_id = UUID(str(project["id"]))
    profiles = client.get(f"/api/projects/{project_id}/profiles").json()
    qa = next(profile for profile in profiles if profile["name"] == "QA")
    profile_id = UUID(qa["id"])
    response = client.patch(
        f"/api/projects/{project_id}/profiles/{profile_id}",
        json={"allowed_tools": [TOOL_ID]},
        headers={"origin": "http://localhost:8765"},
    )
    assert response.status_code == 200
    order = _post(
        client,
        f"/api/projects/{project_id}/work-orders",
        {
            "title": "Inspect fixture",
            "instructions": "Inspect approved fixture",
            "profile_id": str(profile_id),
        },
    )
    order_id = UUID(str(order["id"]))
    _post(client, f"/api/projects/{project_id}/work-orders/{order_id}/ready")
    run_id = UUID(str(gateway.start_run(project_id, order_id)["id"]))
    jobs = _owner(gateway, store, tmp_path)
    fixture_id = _fixture(gateway, store, project_id, run_id)
    assigned = jobs.launch_for_assigned_role(project_id, run_id, fixture_id, uuid4())
    assert assigned["actor_id"] == "operator"
    assert assigned["fixture_id"] == fixture_id
    with pytest.raises(ToolDenied):
        gateway.available_role_tools(project_id, run_id, profile_id)
