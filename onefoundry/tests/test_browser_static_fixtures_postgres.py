"""Durable static fixture approval is inbox-bound, private, and project scoped."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from onefoundry.browser_static_fixture_api import make_browser_static_fixture_router
from onefoundry.browser_static_fixtures import StaticFixtureInput, StaticFixturePolicy, StaticFixtureStore
from onefoundry.browser_target import BrowserTargetLimits
from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.main import create_app
from onefoundry.settings import Settings
from onefoundry.tool_gateway import ToolGateway
from onefoundry.workflow_schemas import ApprovalDecision
from tests.test_role_tool_admission_postgres import _run


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    secrets = tmp_path / "secrets"
    secrets.mkdir(mode=0o700)
    return Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        app_public_port=8765,
        model_secret_root=str(secrets),
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url="http://localhost:8765") as result:
        yield result


@pytest.fixture
def gateway(settings: Settings) -> Iterator[ToolGateway]:
    engine = create_engine(settings.database_url)
    try:
        yield ToolGateway(engine, "operator", live_text_settings=settings)
    finally:
        engine.dispose()


@pytest.fixture
def store(gateway: ToolGateway) -> StaticFixtureStore:
    return StaticFixtureStore(
        gateway,
        StaticFixturePolicy(BrowserTargetLimits(2048, 1920, 1200, 262_144, 30, 300), 262_144),
    )


def proposal(**changes: object) -> StaticFixtureInput:
    values: dict[str, object] = {
        "idempotency_key": uuid4(),
        "approved_url": "http://127.0.0.1:8765/fixture.html",
        "html": "<!doctype html><title>Fixture</title><main>Approved page</main>",
        "css": "main { color: navy; }",
        "viewport_width": 800,
        "viewport_height": 600,
        "max_page_bytes": 262_144,
        "timeout_seconds": 10,
        "lifetime_seconds": 120,
    }
    values.update(changes)
    return StaticFixtureInput.model_validate(values)


@pytest.mark.parametrize("css", ["</style><script>alert(1)</script>", "MAIN { color: red } </StYlE  >"])
def test_css_close_tag_rejected_before_approval(
    client: TestClient, gateway: ToolGateway, store: StaticFixtureStore, css: str
) -> None:
    project_id, run_id, _ = _run(client, gateway, tools=[])
    with pytest.raises(Conflict, match="source is invalid"):
        store.create(project_id, run_id, proposal(css=css))
    with gateway.engine.connect() as connection:
        count = connection.scalar(
            text("SELECT count(*) FROM approval_requests WHERE project_id=:id AND run_id=:run"),
            {"id": project_id, "run": run_id},
        )
    assert count == 0


def test_inbox_approval_and_private_owner_readback(
    client: TestClient, gateway: ToolGateway, store: StaticFixtureStore
) -> None:
    project_id, run_id, _ = _run(client, gateway, tools=[])
    data = proposal()
    pending = store.create(project_id, run_id, data)
    assert pending["status"] == "pending"
    assert "html" not in pending and "css" not in pending
    assert store.create(project_id, run_id, data)["id"] == pending["id"]
    with pytest.raises(NotFound):
        store.approved(project_id, run_id, pending["id"])
    inbox = gateway.list_approvals(project_id, "pending")
    assert any(row["id"] == pending["approval_id"] for row in inbox)
    gateway.decide_approval(project_id, pending["approval_id"], ApprovalDecision(decision="approved"))
    approved = store.get(project_id, run_id, pending["id"])
    assert approved["status"] == "approved"
    owner = store.approved(project_id, run_id, pending["id"], now=approved["decided_at"])
    assert owner.html == data.html.encode()
    assert owner.css == data.css.encode()
    assert owner.target.approved_url == data.approved_url
    assert owner.target.project_id == project_id and owner.target.run_id == run_id
    with pytest.raises(Conflict, match="expired"):
        store.approved(
            project_id,
            run_id,
            pending["id"],
            now=approved["decided_at"] + timedelta(seconds=120),
        )
    with pytest.raises(Conflict, match="already decided"):
        gateway.decide_approval(project_id, pending["approval_id"], ApprovalDecision(decision="rejected"))
    with gateway.engine.connect() as connection:
        events = (
            connection.execute(
                text(
                    "SELECT event_type,payload FROM events WHERE project_id=:project_id AND run_id=:run_id "
                    "AND event_type LIKE 'browser_static_fixture_%' ORDER BY created_at"
                ),
                {"project_id": project_id, "run_id": run_id},
            )
            .mappings()
            .all()
        )
    assert [row["event_type"] for row in events] == ["browser_static_fixture_proposed"]
    assert "Approved page" not in str(events)


def test_reject_isolation_changed_proposal_and_invalid_source(
    client: TestClient, gateway: ToolGateway, store: StaticFixtureStore
) -> None:
    project_id, run_id, _ = _run(client, gateway, tools=[])
    other_project, other_run, _ = _run(client, gateway, tools=[])
    data = proposal()
    pending = store.create(project_id, run_id, data)
    with pytest.raises(NotFound):
        store.get(other_project, other_run, pending["id"])
    with pytest.raises(Conflict, match="already bound"):
        store.create(project_id, run_id, proposal(idempotency_key=data.idempotency_key, html="changed"))
    gateway.decide_approval(project_id, pending["approval_id"], ApprovalDecision(decision="rejected"))
    with pytest.raises(NotFound):
        store.approved(project_id, run_id, pending["id"])
    for invalid in (
        proposal(approved_url="http://example.invalid:8765/fixture.html"),
        proposal(html="password=secret123"),
        proposal(max_page_bytes=5),
    ):
        with pytest.raises(Conflict):
            store.create(project_id, run_id, invalid)


def test_operator_routes_return_metadata_only(
    client: TestClient, gateway: ToolGateway, store: StaticFixtureStore
) -> None:
    project_id, run_id, _ = _run(client, gateway, tools=[])

    class DelegatingGateway:
        def create_browser_static_fixture(
            self, project_id: UUID, run_id: UUID, data: StaticFixtureInput
        ) -> dict[str, object]:
            return store.create(project_id, run_id, data)

        def get_browser_static_fixture(
            self, project_id: UUID, run_id: UUID, fixture_id: UUID
        ) -> dict[str, object]:
            return store.get(project_id, run_id, fixture_id)

    app = FastAPI()
    app.include_router(make_browser_static_fixture_router(DelegatingGateway()))
    with TestClient(app) as operator:
        path = f"/api/projects/{project_id}/runs/{run_id}/browser-static-fixtures"
        response = operator.post(path, json=proposal().model_dump(mode="json"))
        assert response.status_code == 201, response.text
        pending = response.json()
        assert "html" not in pending and "css" not in pending
        assert "Approved page" not in response.text
        gateway.decide_approval(
            project_id, UUID(pending["approval_id"]), ApprovalDecision(decision="approved")
        )
        assert operator.get(f"{path}/{pending['id']}").json()["status"] == "approved"
        assert (
            operator.get(
                f"/api/projects/{UUID(int=0)}/runs/{run_id}/browser-static-fixtures/{pending['id']}"
            ).status_code
            == 404
        )


def test_immutable_source_and_exact_approval_content(
    client: TestClient, gateway: ToolGateway, store: StaticFixtureStore, settings: Settings
) -> None:
    project_id, run_id, _ = _run(client, gateway, tools=[])
    pending = store.create(project_id, run_id, proposal())
    with pytest.raises(DBAPIError):
        with gateway.engine.begin() as connection:
            connection.execute(
                text("UPDATE browser_static_fixtures SET html=:html WHERE project_id=:project_id AND id=:id"),
                {"html": b"tampered", "project_id": project_id, "id": pending["id"]},
            )
    gateway.decide_approval(project_id, pending["approval_id"], ApprovalDecision(decision="approved"))
    bootstrap = create_engine(
        Settings(
            db_host=settings.db_host,
            db_port=settings.db_port,
            db_name=settings.db_name,
            db_user=os.environ["DB_USER"],
            db_password=os.environ["DB_PASSWORD"],
        ).database_url
    )
    try:
        with bootstrap.begin() as connection:
            connection.execute(
                text("UPDATE approval_requests SET content=:content WHERE project_id=:project_id AND id=:id"),
                {"content": "{}", "project_id": project_id, "id": pending["approval_id"]},
            )
    finally:
        bootstrap.dispose()
    with pytest.raises(Conflict, match="binding changed"):
        store.approved(project_id, run_id, pending["id"])


def test_completed_run_cannot_stage_or_retrieve_fixture(
    client: TestClient, gateway: ToolGateway, store: StaticFixtureStore
) -> None:
    project_id, run_id, _ = _run(client, gateway, tools=[])
    pending = store.create(project_id, run_id, proposal())
    gateway.decide_approval(project_id, pending["approval_id"], ApprovalDecision(decision="approved"))
    with gateway.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE runs SET status='done',finished_at=clock_timestamp() "
                "WHERE project_id=:project_id AND id=:run_id"
            ),
            {"project_id": project_id, "run_id": run_id},
        )
    with pytest.raises(Conflict, match="not active"):
        store.create(project_id, run_id, proposal())
    with pytest.raises(NotFound):
        store.approved(project_id, run_id, pending["id"])
