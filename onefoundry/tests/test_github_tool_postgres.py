"""Built-in operator GitHub snapshot is scoped, read-only, and event-safe."""

import os
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from onefoundry.main import create_app
from onefoundry.settings import Settings
from onefoundry.source_github import GitHubUnavailable

ORIGIN = {"origin": "http://localhost:8765"}
TOKEN = "test-only-github-credential"
SAVED = "a" * 40
CURRENT = "b" * 40


def test_github_snapshot_scope_failure_and_secret_safe_event(tmp_path: Path) -> None:
    secret_root = tmp_path / "credentials"
    secret_root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(secret_root),
    )
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project = client.post("/api/projects", json={"name": "GitHub tool"}, headers=ORIGIN).json()
        other = client.post("/api/projects", json={"name": "Other GitHub tool"}, headers=ORIGIN).json()
        path = f"/api/projects/{project['id']}"
        account = client.post(
            f"{path}/github/accounts", json={"label": "Work", "token": TOKEN}, headers=ORIGIN
        )
        assert account.status_code == 201, account.text
        with patch("onefoundry.source_store.resolve_revision", return_value=("main", SAVED)):
            created = client.post(
                f"{path}/sources/github",
                json={
                    "account_id": account.json()["id"],
                    "owner": "example",
                    "repo": "repo",
                    "branch": "main",
                },
                headers=ORIGIN,
            )
        assert created.status_code == 201, created.text
        source_id = created.json()["id"]
        action = f"{path}/tools/github/source-snapshot"
        with patch("onefoundry.source_store.resolve_revision", return_value=("main", CURRENT)) as resolve:
            result = client.post(action, json={"source_id": source_id}, headers=ORIGIN)
        assert result.status_code == 200, result.text
        assert result.json() == {
            "project_id": project["id"],
            "source_id": source_id,
            "owner": "example",
            "repo": "repo",
            "branch": "main",
            "saved_revision": SAVED,
            "current_revision": CURRENT,
            "matches_saved_revision": False,
            "checked_at": result.json()["checked_at"],
        }
        resolve.assert_called_once()
        assert resolve.call_args.args[0] == TOKEN
        assert TOKEN not in result.text
        assert client.get(f"{path}/sources").json()[0]["revision"] == SAVED

        assert client.post(action, json={"source_id": "bad"}, headers=ORIGIN).status_code == 422
        foreign = client.post(
            f"/api/projects/{other['id']}/tools/github/source-snapshot",
            json={"source_id": source_id},
            headers=ORIGIN,
        )
        assert foreign.status_code == 404
        missing = client.post(action, json={"source_id": str(uuid4())}, headers=ORIGIN)
        assert missing.status_code == 404
        local_id = uuid4()
        engine = create_engine(settings.database_url)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO project_sources "
                    "(id,project_id,actor_id,kind,display_name,root_id,relative_path,branch,revision) "
                    "VALUES (:id,:project_id,'operator','local','Local','workspace','repo','main',:revision)"
                ),
                {"id": local_id, "project_id": UUID(project["id"]), "revision": SAVED},
            )
        engine.dispose()
        with patch("onefoundry.source_store.resolve_revision") as no_egress:
            wrong_kind = client.post(action, json={"source_id": str(local_id)}, headers=ORIGIN)
        assert wrong_kind.status_code == 409
        no_egress.assert_not_called()
        with patch(
            "onefoundry.source_store.resolve_revision", side_effect=GitHubUnavailable("secret " + TOKEN)
        ):
            failure = client.post(action, json={"source_id": source_id}, headers=ORIGIN)
        assert failure.status_code == 503
        assert TOKEN not in failure.text
        assert "saved revision was not changed" in failure.text
        assert client.get(f"{path}/sources").json()[0]["revision"] == SAVED
        with patch(
            "onefoundry.source_store.resolve_revision", side_effect=ValueError("invalid saved metadata")
        ):
            invalid_saved = client.post(action, json={"source_id": source_id}, headers=ORIGIN)
        assert invalid_saved.status_code == 409
        assert "invalid saved metadata" not in invalid_saved.text
        assert client.get(f"{path}/sources").json()[0]["revision"] == SAVED

        with patch("onefoundry.source_store.resolve_revision", return_value=("main", SAVED)):
            doomed = client.post(
                f"{path}/sources/github",
                json={
                    "account_id": account.json()["id"],
                    "owner": "example",
                    "repo": "doomed",
                    "branch": "main",
                },
                headers=ORIGIN,
            )
        assert doomed.status_code == 201

        def delete_during_remote(*_args: object) -> tuple[str, str]:
            race_engine = create_engine(settings.database_url)
            with race_engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM project_sources WHERE project_id=:project_id AND id=:source_id"),
                    {"project_id": UUID(project["id"]), "source_id": UUID(doomed.json()["id"])},
                )
            race_engine.dispose()
            return "main", CURRENT

        with patch("onefoundry.source_store.resolve_revision", side_effect=delete_during_remote):
            deleted_during_check = client.post(
                action, json={"source_id": doomed.json()["id"]}, headers=ORIGIN
            )
        assert deleted_during_check.status_code == 409
        assert "no current result was recorded" in deleted_during_check.text

        def archive_during_remote(*_args: object) -> tuple[str, str]:
            race_engine = create_engine(settings.database_url)
            with race_engine.begin() as connection:
                connection.execute(
                    text("UPDATE projects SET status='archived',version=version+1 WHERE id=:project_id"),
                    {"project_id": UUID(project["id"])},
                )
            race_engine.dispose()
            return "main", CURRENT

        with patch("onefoundry.source_store.resolve_revision", side_effect=archive_during_remote):
            archived_during_check = client.post(action, json={"source_id": source_id}, headers=ORIGIN)
        assert archived_during_check.status_code == 409
        assert "no current result was recorded" in archived_during_check.text
        with patch("onefoundry.source_store.resolve_revision") as no_egress:
            blocked = client.post(action, json={"source_id": source_id}, headers=ORIGIN)
        assert blocked.status_code == 409
        no_egress.assert_not_called()

    engine = create_engine(settings.database_url)
    with engine.connect() as connection:
        events = connection.execute(
            text(
                "SELECT project_id,actor_id,event_type,payload FROM events WHERE "
                "event_type='github_source_snapshot_attempt' AND project_id=:id ORDER BY created_at"
            ),
            {"id": UUID(project["id"])},
        ).all()
        other_events = connection.execute(
            text(
                "SELECT payload FROM events WHERE event_type='github_source_snapshot_attempt' "
                "AND project_id=:id"
            ),
            {"id": UUID(other["id"])},
        ).all()
    engine.dispose()
    assert [row[3]["status"] for row in events] == [
        "checked",
        "failed",
        "failed",
        "unknown",
        "failed",
        "unknown",
        "unknown",
    ]
    assert events[0][3]["current_revision"] == CURRENT
    assert events[1][3]["reason"] == "source_not_found"
    assert events[2][3]["reason"] == "source_check_conflict"
    assert events[3][3]["reason"] == "github_check_unavailable"
    assert events[4][3]["reason"] == "invalid_saved_source"
    assert events[5][3] == {
        "source_id": doomed.json()["id"],
        "status": "unknown",
        "reason": "source_changed_during_check",
    }
    assert events[6][3] == {
        "source_id": source_id,
        "status": "unknown",
        "reason": "project_changed_during_check",
    }
    assert all(row[0] == UUID(project["id"]) and row[1] == "operator" for row in events)
    assert other_events[0][0]["reason"] == "source_not_found"
    assert TOKEN not in repr(events) and TOKEN not in repr(other_events)
