"""Explicit starts preserve the existing Gateway state, scope and replay fences."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.main import create_app
from onefoundry.settings import Settings
from onefoundry.task_session_start import TaskSessionStart, TeamStart
from onefoundry.tool_gateway import ToolGateway
from tests.test_team_workflow_postgres import ORIGIN, project_with_models, settings_for
from tests.test_team_workflow_postgres import quiet_worker as quiet_worker


def draft(client: TestClient) -> tuple[str, str, dict[str, Any], str]:
    project, profiles = project_with_models(client)
    created = client.post(
        f"/api/projects/{project}/work-orders",
        json={
            "title": "Start the saved research",
            "instructions": "Research then verify a saved result.",
            "profile_id": profiles["Chief of Staff"],
            "deliverable_kind": "operator_review",
        },
        headers=ORIGIN,
    )
    assert created.status_code == 201
    order: dict[str, Any] = created.json()
    return project, order["id"], order, f"/api/projects/{project}/work-orders/{order['id']}/team-session"


def counts(settings: Settings, project: str) -> tuple[int, int, int]:
    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            params = {"p": UUID(project)}
            sessions = connection.scalar(
                text("SELECT count(*) FROM task_sessions WHERE project_id=:p"), params
            )
            messages = connection.scalar(
                text("SELECT count(*) FROM task_session_messages WHERE project_id=:p"), params
            )
            attempts = connection.scalar(
                text("SELECT count(*) FROM task_session_attempts WHERE project_id=:p"), params
            )
            return int(sessions or 0), int(messages or 0), int(attempts or 0)
    finally:
        engine.dispose()


def test_maintenance_target_is_not_limited_by_displayed_sources_or_other_approvals(tmp_path: Path) -> None:
    from onefoundry.task_execution_capabilities import execution_capabilities
    from tests.test_repository_code import job_at

    seed, root, _spool = job_at(tmp_path)
    settings = settings_for(tmp_path).model_copy(
        update={"role_context_max_items": 1, "local_source_root": str(root)}
    )
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, order, order_row, _base = draft(client)
        other, _other_order, _row, _base = draft(client)
        engine = create_engine(Settings().database_url)
        try:
            with engine.begin() as connection:
                run_id = uuid4()
                connection.execute(
                    text(
                        "INSERT INTO runs(id,project_id,actor_id,work_order_id,profile_id,status,mode) "
                        "VALUES (:id,:p,'operator',:order,:profile,'done','manual_planning')"
                    ),
                    {
                        "id": run_id,
                        "p": UUID(project),
                        "order": UUID(order),
                        "profile": UUID(order_row["profile_id"]),
                    },
                )
                for position, (source_id, relative) in enumerate(
                    ((uuid4(), "first"), (seed.target.source_id, "repo"))
                ):
                    connection.execute(
                        text(
                            "INSERT INTO project_sources(id,project_id,actor_id,kind,display_name,"
                            "root_id,relative_path,branch,revision,created_at) "
                            "VALUES (:id,:p,'operator','local',"
                            ":path,'workspace',:path,'main',:revision,now()+:position*interval '1 second')"
                        ),
                        {
                            "id": source_id,
                            "p": UUID(project),
                            "path": relative,
                            "position": position,
                            "revision": seed.target.revision,
                        },
                    )
                for content in (seed.target.model_dump_json(), "Unrelated approved action"):
                    connection.execute(
                        text(
                            "INSERT INTO approval_requests(id,project_id,actor_id,work_order_id,run_id,"
                            "action_type,target,content,proposal_digest,status,decided_at) VALUES "
                            "(:id,:p,'operator',:order,:run,'other','Fixture',:content,:digest,'approved',now())"
                        ),
                        {
                            "id": uuid4(),
                            "p": UUID(project),
                            "order": UUID(order),
                            "content": content,
                            "digest": "a" * 64,
                            "run": run_id,
                        },
                    )
            with engine.connect() as connection:
                for project_id, expected in ((project, "ready"), (other, "configuration_missing")):
                    value = execution_capabilities(connection, UUID(project_id), settings)
                    check = next(
                        c for c in value["readiness"]["checks"] if c["operation"] == "maintenance_target"
                    )
                    assert check["status"] == expected
                assert (
                    execution_capabilities(connection, UUID(project), settings)["sources"][0]["display_name"]
                    == "first"
                )
        finally:
            engine.dispose()


@pytest.mark.parametrize("blocker", ["disabled", "paused", "stale", "specialist", "code_change"])
def test_blocked_start_leaves_draft_and_all_execution_counts_unchanged(tmp_path: Path, blocker: str) -> None:
    settings = settings_for(tmp_path, enabled=blocker != "disabled")
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, oid, order, base = draft(client)
        if blocker == "paused":
            controls = client.get(f"/api/projects/{project}/coordination").json()
            paused = client.post(
                f"/api/projects/{project}/coordination/pause",
                json={"expected_version": controls["version"]},
                headers=ORIGIN,
            )
            assert paused.status_code == 200, paused.text
        bootstrap = create_engine(Settings().database_url)
        with bootstrap.begin() as connection:
            if blocker == "specialist":
                connection.execute(
                    text(
                        "UPDATE work_orders SET profile_id=(SELECT id FROM profiles "
                        "WHERE project_id=:p AND name='Researcher') WHERE id=:o"
                    ),
                    {"p": UUID(project), "o": UUID(oid)},
                )
            elif blocker == "code_change":
                connection.execute(
                    text("UPDATE work_orders SET deliverable_kind='code_change' WHERE id=:o"),
                    {"o": UUID(oid)},
                )
        bootstrap.dispose()
        order = client.get(f"/api/projects/{project}/work-orders").json()[0]
        before = counts(settings, project)
        response = client.post(
            f"{base}/start",
            json={
                "client_message_id": str(uuid4()),
                "expected_version": order["version"] + (1 if blocker == "stale" else 0),
                "body": "Start this work.",
            },
            headers=ORIGIN,
        )
        assert response.status_code == 409, response.text
        expected = {
            "disabled": "disabled",
            "paused": "paused",
            "stale": "changed",
            "specialist": "Chief",
            "code_change": "separate",
        }
        assert expected[blocker].lower() in response.text.lower()
        assert counts(settings, project) == before == (0, 0, 0)
        assert client.get(f"/api/projects/{project}/work-orders").json()[0]["status"] == "draft"


def test_start_rollback_on_message_owner_failure(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, _oid, order, base = draft(client)
        with patch(
            "onefoundry.task_session_store.TaskSessionStore.send_in_transaction",
            side_effect=RuntimeError("fixture message owner unavailable"),
        ):
            with pytest.raises(RuntimeError, match="message owner unavailable"):
                client.post(
                    f"{base}/start",
                    json={
                        "client_message_id": str(uuid4()),
                        "expected_version": order["version"],
                        "body": "Start this work.",
                    },
                    headers=ORIGIN,
                )
        assert counts(settings, project) == (0, 0, 0)
        assert client.get(f"/api/projects/{project}/work-orders").json()[0]["status"] == "draft"
        with create_engine(settings.database_url).connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM events WHERE project_id=:p AND event_type='work_order_ready'"),
                    {"p": UUID(project)},
                )
                == 0
            )


def test_concurrent_same_start_has_one_attempt_and_preserves_unknown_outcome(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, oid, order, base = draft(client)
        gateway = ToolGateway(
            create_engine(settings.database_url), settings.operator_actor_id, live_text_settings=settings
        )
        data = TeamStart(client_message_id=uuid4(), expected_version=order["version"], body="Start once.")
        starter = TaskSessionStart(gateway)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(starter.start, UUID(project), UUID(oid), data) for _ in range(2)]
            results = [future.result() for future in futures]
        assert sum(created for _, created in results) == 1
        assert results[0][0] == results[1][0]
        assert counts(settings, project) == (1, 1, 1)
        receipt = results[0][0]
        bootstrap = create_engine(Settings().database_url)
        with bootstrap.begin() as connection:
            connection.execute(
                text(
                    "UPDATE task_session_attempts SET status='unknown',"
                    "reason='fixture_unknown' WHERE project_id=:p AND id=:a"
                ),
                {"p": UUID(project), "a": receipt["attempt_id"]},
            )
        bootstrap.dispose()
        replay = client.post(f"{base}/start", json=data.model_dump(mode="json"), headers=ORIGIN)
        assert replay.status_code == 200
        assert replay.json() == client.get(f"{base}/start/{data.client_message_id}").json()
        assert counts(settings, project) == (1, 1, 1)
        view = client.get(base).json()
        assert view["attempts"][0]["status"] == "unknown"
        with pytest.raises(Conflict, match="different message"):
            starter.start(UUID(project), UUID(oid), data.model_copy(update={"body": "Different work."}))
        other, other_order, _row, _path = draft(client)
        assert (
            client.get(
                f"/api/projects/{other}/work-orders/{other_order}/team-session/start/{data.client_message_id}"
            ).status_code
            == 404
        )
        gateway.engine.dispose()


@pytest.mark.parametrize("body", ["", "password=fixture-do-not-store"])
def test_invalid_start_input_has_no_effect(tmp_path: Path, body: str) -> None:
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, _oid, order, base = draft(client)
        response = client.post(
            f"{base}/start",
            json={"client_message_id": str(uuid4()), "expected_version": order["version"], "body": body},
            headers=ORIGIN,
        )
        assert response.status_code == 422
        assert counts(settings, project) == (0, 0, 0)


@pytest.mark.parametrize("external_enabled", [True, False])
def test_capability_metadata_is_project_scoped_and_does_not_grant_execution(
    tmp_path: Path, external_enabled: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    from onefoundry.task_execution_capabilities import execution_capabilities

    # This metadata contract uses an available source owner without contacting GitHub.
    monkeypatch.setattr("onefoundry.source_github_file.inspect", lambda *_args: None)
    settings = settings_for(tmp_path).model_copy(update={"external_code_enabled": external_enabled})
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, _oid, _row, base = draft(client)
        account = client.post(
            f"/api/projects/{project}/github/accounts",
            json={"label": "Fixture", "token": "fixture-only-token"},
            headers=ORIGIN,
        ).json()
        with patch("onefoundry.source_store.resolve_revision", return_value=("main", "a" * 40)):
            source = client.post(
                f"/api/projects/{project}/sources/github",
                json={"account_id": account["id"], "owner": "fixture", "repo": "sample", "branch": "main"},
                headers=ORIGIN,
            )
        assert source.status_code == 201, source.text
        readiness = client.get(f"{base}/readiness").json()
        assert readiness["ready_to_start"] is True
        report = readiness["capability_readiness"]
        assert report["project_id"] == project
        checks = {check["operation"]: check for check in report["checks"]}
        repository = checks["repository_read"]
        assert repository["source_id"] == source.json()["id"]
        assert repository["status"] == "permission_required"
        assert repository["next_action"] == "grant_access"
        assert {item["code"] for item in repository["blockers"]} == {
            "repository_permission",
        }
        assert checks["project_code_change"]["next_action"] == "configure"
        assert any("read permission" in note for note in readiness["capability_notes"])
        assert (
            any("coding is disabled" in note for note in readiness["capability_notes"])
            is not external_enabled
        )
        other, _order, _row, _base = draft(client)
        engine = create_engine(settings.database_url)
        with engine.connect() as connection:
            metadata = execution_capabilities(connection, UUID(project), settings)
            assert metadata["sources"][0]["id"] == source.json()["id"]
            assert metadata["external_coding_enabled"] is external_enabled
            assert metadata["repository_read_configured"] is False
            assert execution_capabilities(connection, UUID(other), settings)["sources"] == []
            with pytest.raises(NotFound, match="profile"):
                execution_capabilities(
                    connection, UUID(other), settings, profile_id=UUID(report["profile_id"])
                )
        engine.dispose()
        bootstrap = create_engine(Settings().database_url)
        with bootstrap.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO project_sources(id,project_id,actor_id,kind,display_name,"
                    "account_id,owner,repo,branch,revision) "
                    "SELECT :new_id,project_id,actor_id,kind,'token=fixture-private',"
                    "account_id,owner,repo,branch,revision "
                    "FROM project_sources WHERE id=:id"
                ),
                {"id": UUID(source.json()["id"]), "new_id": uuid4()},
            )
        bootstrap.dispose()
        engine = create_engine(settings.database_url)
        with engine.connect() as connection:
            assert len(execution_capabilities(connection, UUID(project), settings)["sources"]) == 1
        engine.dispose()
        assert counts(settings, project) == (0, 0, 0)


@pytest.mark.parametrize("state,created", [("coalesced", True), ("queued", False)])
def test_start_requires_a_newly_queued_owner_receipt(tmp_path: Path, state: str, created: bool) -> None:
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, _oid, order, base = draft(client)
        owner_receipt = {
            "session_id": uuid4(),
            "message_id": uuid4(),
            "sequence": 1,
            "attempt_id": uuid4() if state == "queued" else None,
            "state": state,
        }
        with patch(
            "onefoundry.task_session_store.TaskSessionStore.send_in_transaction",
            return_value=(owner_receipt, created),
        ):
            response = client.post(
                f"{base}/start",
                json={
                    "client_message_id": str(uuid4()),
                    "expected_version": order["version"],
                    "body": "Start once.",
                },
                headers=ORIGIN,
            )
        assert response.status_code == 409, response.text
        assert counts(settings, project) == (0, 0, 0)
        assert client.get(f"/api/projects/{project}/work-orders").json()[0]["status"] == "draft"
