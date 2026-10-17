"""Recoverable removal of old conversations preserves records and execution holds."""

from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from onefoundry.main import create_app
from onefoundry.task_session_attempts import TaskSessionAttempts
from onefoundry.tool_gateway import ToolGateway
from tests.test_chief_workspace_postgres import command
from tests.test_team_workflow_postgres import ORIGIN, project_with_models, settings_for
from tests.test_team_workflow_postgres import quiet_worker as quiet_worker


def test_trash_and_recovery_preserve_history_and_hold_execution(tmp_path: Path, quiet_worker: None) -> None:
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, _ = project_with_models(client)
        saved = command(client, project, "Prepare a brief.")
        base = f"/api/projects/{project}/chief-workspace"
        order = saved["order_id"]
        session_path = f"/api/projects/{project}/work-orders/{order}/team-session"
        before = client.get(session_path).json()

        def action(name: str) -> dict[str, object]:
            row = client.get(base).json()[0]
            data = {
                "client_action_id": str(uuid4()),
                "expected_version": row["version"],
                "expected_controls_version": row["controls_version"],
                "action": name,
            }
            response = client.post(f"{base}/{order}/controls", json=data, headers=ORIGIN)
            assert response.status_code == 200, response.text
            assert (
                client.post(f"{base}/{order}/controls", json=data, headers=ORIGIN).json() == response.json()
            )
            return data

        action("archive")
        archived = client.get(base).json()[0]
        assert (
            client.post(
                f"{base}/messages",
                json={
                    "client_message_id": str(uuid4()),
                    "order_id": order,
                    "expected_version": archived["version"],
                    "expected_sequence": 1,
                    "body": "Continue",
                },
                headers=ORIGIN,
            ).status_code
            == 409
        )
        assert (
            client.post(
                f"{base}/{order}/controls",
                json={
                    "client_action_id": str(uuid4()),
                    "expected_version": archived["version"],
                    "expected_controls_version": archived["controls_version"],
                    "action": "resume",
                },
                headers=ORIGIN,
            ).status_code
            == 409
        )
        gateway = ToolGateway(
            create_engine(settings.database_url), "fixture-worker", live_text_settings=settings
        )
        assert TaskSessionAttempts(gateway).claim_next(UUID(project)) is None
        removed = action("trash")
        row = client.get(base).json()[0]
        assert row["trashed"] and row["archived"] and row["paused"]
        assert client.get(f"{base}/resolve/{order}").status_code == 404
        rejected = client.post(
            f"{base}/messages",
            json={
                "client_message_id": str(uuid4()),
                "order_id": order,
                "expected_version": row["version"],
                "expected_sequence": 1,
                "body": "Continue",
            },
            headers=ORIGIN,
        )
        assert rejected.status_code == 409
        assert "Trash" in rejected.json()["detail"]
        assert client.get(session_path).json() == before
        assert (
            client.post(
                f"{base}/{order}/controls", json={**removed, "action": "resume"}, headers=ORIGIN
            ).status_code
            == 409
        )
        action("restore")
        restored = client.get(base).json()[0]
        assert not restored["trashed"] and restored["archived"] and restored["paused"]
        assert client.get(f"{base}/resolve/{order}").status_code == 200
        assert TaskSessionAttempts(gateway).claim_next(UUID(project)) is None
        with gateway.engine.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM events WHERE project_id=:p "
                        "AND event_type='chief_conversation_control'"
                    ),
                    {"p": UUID(project)},
                )
                == 3
            )
        gateway.engine.dispose()


def test_trash_requires_archived_idle_root_and_current_versions(tmp_path: Path, quiet_worker: None) -> None:
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, _ = project_with_models(client)
        saved = command(client, project, "Prepare a brief.")
        base = f"/api/projects/{project}/chief-workspace"
        order = saved["order_id"]
        row = client.get(base).json()[0]
        data = {
            "client_action_id": str(uuid4()),
            "expected_version": row["version"],
            "expected_controls_version": row["controls_version"],
            "action": "trash",
        }
        assert client.post(f"{base}/{order}/controls", json=data, headers=ORIGIN).status_code == 409
        other = client.post("/api/projects", json={"name": "Other company"}, headers=ORIGIN).json()["id"]
        assert (
            client.post(
                f"/api/projects/{other}/chief-workspace/{order}/controls", json=data, headers=ORIGIN
            ).status_code
            == 404
        )
        gateway = ToolGateway(
            create_engine(settings.database_url), "fixture-worker", live_text_settings=settings
        )
        assert TaskSessionAttempts(gateway).claim_next(UUID(project)) is not None
        data["expected_version"] = client.get(base).json()[0]["version"]
        assert (
            client.post(
                f"{base}/{order}/controls", json={**data, "action": "archive"}, headers=ORIGIN
            ).status_code
            == 200
        )
        row = client.get(base).json()[0]
        rejected = client.post(
            f"{base}/{order}/controls",
            json={
                **data,
                "client_action_id": str(uuid4()),
                "expected_controls_version": row["controls_version"],
            },
            headers=ORIGIN,
        )
        assert rejected.status_code == 409
        assert "current turn must finish" in rejected.json()["detail"]
        assert not client.get(base).json()[0]["trashed"]
        gateway.engine.dispose()
