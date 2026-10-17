"""A run-bound role can create one same-project draft without starting it."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from onefoundry.main import create_app
from onefoundry.role_draft_work_order import TOOL_ID, parse_arguments
from onefoundry.role_tool_admission import ToolDenied, ToolInputError
from onefoundry.settings import Settings
from onefoundry.tool_gateway import ToolGateway
from tests.test_role_tool_admission_postgres import _post, _run, _tool_events


@pytest.fixture
def settings() -> Settings:
    return Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        app_public_port=8765,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url="http://localhost:8765") as test_client:
        yield test_client


@pytest.fixture
def gateway(settings: Settings) -> Iterator[ToolGateway]:
    engine = create_engine(settings.database_url)
    try:
        yield ToolGateway(engine, "operator", live_text_settings=settings)
    finally:
        engine.dispose()


def _profile(client: TestClient, project_id: UUID, name: str) -> UUID:
    profiles = client.get(f"/api/projects/{project_id}/profiles").json()
    return UUID(next(row["id"] for row in profiles if row["name"] == name))


def _invoke(
    gateway: ToolGateway,
    project_id: UUID,
    run_id: UUID,
    source_profile_id: UUID,
    target_profile_id: UUID,
    call_id: str,
    *,
    title: object = "Prepare launch brief",
    instructions: object = "Draft a launch brief for review.",
) -> dict[str, object]:
    return gateway.invoke_role_tool(
        project_id,
        run_id,
        source_profile_id,
        call_id,
        TOOL_ID,
        {
            "target_profile_id": str(target_profile_id),
            "title": title,
            "instructions": instructions,
        },
        tool_version=1,
    )


def test_creates_draft_for_same_project_profile_with_hash_only_events(
    client: TestClient, gateway: ToolGateway
) -> None:
    project_id, run_id, source_profile_id = _run(client, gateway, tools=[TOOL_ID])
    assert TOOL_ID in {
        entry["id"] for entry in gateway.available_role_tools(project_id, run_id, source_profile_id)
    }
    target_profile_id = _profile(client, project_id, "GTM")
    receipt = _invoke(gateway, project_id, run_id, source_profile_id, target_profile_id, "draft-one")
    assert receipt["status"] == "done"
    result = receipt["result"]
    assert isinstance(result, dict)
    assert result["status"] == "draft"
    assert result["profile_id"] == str(target_profile_id)
    order_id = UUID(result["work_order_id"])
    with gateway.engine.connect() as connection:
        order = (
            connection.execute(
                text(
                    "SELECT actor_id,title,instructions,profile_id,status,deliverable_kind,"
                    "acceptance_checks,parent_work_order_id,source_run_id FROM work_orders "
                    "WHERE project_id=:project_id AND id=:id"
                ),
                {"project_id": project_id, "id": order_id},
            )
            .mappings()
            .one()
        )
        assert (
            connection.scalar(text("SELECT count(*) FROM runs WHERE work_order_id=:id"), {"id": order_id})
            == 0
        )
        created = (
            connection.execute(
                text(
                    "SELECT actor_id,payload FROM events WHERE work_order_id=:id "
                    "AND event_type='work_order_created'"
                ),
                {"id": order_id},
            )
            .mappings()
            .one()
        )
        parent_id = connection.scalar(
            text("SELECT work_order_id FROM runs WHERE project_id=:project_id AND id=:run_id"),
            {"project_id": project_id, "run_id": run_id},
        )
    assert dict(order) == {
        "actor_id": f"profile:{source_profile_id}",
        "title": "Prepare launch brief",
        "instructions": "Draft a launch brief for review.",
        "profile_id": target_profile_id,
        "status": "draft",
        "deliverable_kind": "operator_review",
        "acceptance_checks": [],
        "parent_work_order_id": parent_id,
        "source_run_id": run_id,
    }
    assert created["actor_id"] == f"profile:{source_profile_id}"
    events = _tool_events(gateway, project_id, run_id)
    assert [event["event_type"] for event in events] == ["role_tool_started", "role_tool_finished"]
    assert "Prepare launch brief" not in json.dumps(events) + json.dumps(created["payload"])
    assert "Draft a launch brief" not in json.dumps(events) + json.dumps(created["payload"])
    assert events[1]["payload"]["result_sha256"] == receipt["result_sha256"]
    assert result["parent_work_order_id"] == str(parent_id)
    with pytest.raises(ToolDenied, match="already attempted"):
        _invoke(gateway, project_id, run_id, source_profile_id, target_profile_id, "draft-one")


@pytest.mark.parametrize(
    "title,instructions",
    [
        (" ", "Safe"),
        ("Safe", " "),
        ("x" * 161, "Safe"),
        ("Safe", "x" * 16001),
        (42, "Safe"),
        ("Safe", "password=veryprivate"),
        ("Safe", "database_url=postgres://operator:example123@db.local/app"),
        ("Safe", "auth_token=example123456789012345"),
        ("Safe", "client_secret=example123456789012345"),
    ],
)
def test_rejects_invalid_or_sensitive_arguments(
    client: TestClient, gateway: ToolGateway, title: object, instructions: object
) -> None:
    project_id, run_id, source_profile_id = _run(client, gateway, tools=[TOOL_ID])
    with pytest.raises(ToolInputError):
        _invoke(
            gateway,
            project_id,
            run_id,
            source_profile_id,
            source_profile_id,
            "bad-args",
            title=title,
            instructions=instructions,
        )
    assert _tool_events(gateway, project_id, run_id) == []


def test_denies_cross_project_target_and_revoked_source(client: TestClient, gateway: ToolGateway) -> None:
    project_id, run_id, source_profile_id = _run(client, gateway, tools=[TOOL_ID])
    other_project = _post(client, "/api/projects", {"name": f"Other {uuid4()}"})
    foreign_profile = _profile(client, UUID(str(other_project["id"])), "GTM")
    with pytest.raises(ToolDenied, match="target profile"):
        _invoke(gateway, project_id, run_id, source_profile_id, foreign_profile, "foreign-target")
    response = client.patch(
        f"/api/projects/{project_id}/profiles/{source_profile_id}",
        json={"allowed_tools": []},
        headers={"origin": "http://localhost:8765"},
    )
    assert response.status_code == 200, response.text
    with pytest.raises(ToolDenied, match="not admitted"):
        _invoke(gateway, project_id, run_id, source_profile_id, source_profile_id, "revoked")
    with gateway.engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM work_orders WHERE project_id=:id AND actor_id=:actor"),
                {"id": project_id, "actor": f"profile:{source_profile_id}"},
            )
            == 0
        )


def test_denies_call_when_snapshot_lacks_tool(client: TestClient, gateway: ToolGateway) -> None:
    project_id, run_id, source_profile_id = _run(client, gateway, tools=[])
    with pytest.raises(ToolDenied, match="not admitted"):
        _invoke(gateway, project_id, run_id, source_profile_id, source_profile_id, "not-in-snapshot")


@pytest.mark.parametrize(
    "arguments",
    [
        {"target_profile_id": "not-a-uuid", "title": "Safe", "instructions": "Safe"},
        {"target_profile_id": str(UUID(int=0)), "title": "Safe", "instructions": "Safe"},
        {"target_profile_id": str(uuid4()), "title": "Safe", "instructions": "Safe", "extra": 1},
    ],
)
def test_rejects_malformed_target_and_extra_fields(arguments: object) -> None:
    with pytest.raises(ToolInputError):
        parse_arguments(arguments)


def test_same_call_id_creates_one_draft_under_concurrency(client: TestClient, gateway: ToolGateway) -> None:
    project_id, run_id, source_profile_id = _run(client, gateway, tools=[TOOL_ID])
    barrier = Barrier(2)

    def attempt() -> object:
        barrier.wait()
        try:
            return _invoke(gateway, project_id, run_id, source_profile_id, source_profile_id, "same-call")
        except ToolDenied as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: attempt(), range(2)))
    assert sum(isinstance(outcome, dict) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, ToolDenied) for outcome in outcomes) == 1
    with gateway.engine.connect() as connection:
        children = (
            connection.execute(
                text(
                    "SELECT source_run_id,parent_work_order_id FROM work_orders "
                    "WHERE project_id=:id AND actor_id=:actor"
                ),
                {"id": project_id, "actor": f"profile:{source_profile_id}"},
            )
            .mappings()
            .all()
        )
        parent_id = connection.scalar(
            text("SELECT work_order_id FROM runs WHERE project_id=:project_id AND id=:run_id"),
            {"project_id": project_id, "run_id": run_id},
        )
    assert len(children) == 1
    assert children[0]["source_run_id"] == run_id
    assert children[0]["parent_work_order_id"] == parent_id


def test_failure_to_save_receipt_rolls_back_draft(
    client: TestClient, gateway: ToolGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id, run_id, source_profile_id = _run(client, gateway, tools=[TOOL_ID])
    original = gateway._event

    def fail_finish(*args: object, **kwargs: object) -> None:
        if args[4] == "role_tool_finished":
            raise RuntimeError("synthetic event failure")
        original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(gateway, "_event", fail_finish)
    with pytest.raises(RuntimeError, match="synthetic event failure"):
        _invoke(gateway, project_id, run_id, source_profile_id, source_profile_id, "rollback")
    with gateway.engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM work_orders WHERE project_id=:id AND actor_id=:actor"),
                {"id": project_id, "actor": f"profile:{source_profile_id}"},
            )
            == 0
        )
    assert _tool_events(gateway, project_id, run_id) == []
