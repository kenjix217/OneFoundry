"""Source/account lifecycle on isolated migrated PostgreSQL and restricted app role."""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError

from onefoundry.gateway_errors import Unavailable
from onefoundry.main import create_app
from onefoundry.settings import Settings
from onefoundry.source_schemas import GitHubAccountCreate
from onefoundry.source_store import SourceStore

ORIGIN = {"origin": "http://localhost:8765"}


def test_github_source_binding_and_disconnect(tmp_path: Path) -> None:
    secret_root = tmp_path / "credentials"
    secret_root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(secret_root),
    )
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        first = client.post("/api/projects", json={"name": "Sources first"}, headers=ORIGIN).json()
        second = client.post("/api/projects", json={"name": "Sources second"}, headers=ORIGIN).json()
        path = f"/api/projects/{first['id']}"
        assert client.get("/api/source-roots").json() == [
            {"id": "workspace", "label": "Configured local repository root", "available": False}
        ]
        assert client.get("/api/source-roots/workspace/repositories").json() == {
            "available": False,
            "truncated": False,
            "repositories": [],
        }
        local = client.post(
            f"{path}/sources/local",
            json={"root_id": "workspace", "relative_path": "repo"},
            headers=ORIGIN,
        )
        assert local.status_code == 503
        account = client.post(
            f"{path}/github/accounts",
            json={"label": "Personal", "token": "test-only-token"},
            headers=ORIGIN,
        )
        assert account.status_code == 201, account.text
        assert account.json()["credential_present"] is True
        assert "test-only-token" not in account.text
        account_id = account.json()["id"]
        assert (secret_root / "github" / account_id).stat().st_mode & 0o777 == 0o600
        assert client.get(f"/api/projects/{second['id']}/github/accounts").json() == []
        assert client.get(f"{path}/github/accounts").json() == [account.json()]
        assert (
            client.get(f"/api/projects/{second['id']}/github/accounts/{account_id}/repos").status_code == 404
        )
        with patch(
            "onefoundry.source_store.list_repositories",
            return_value=[
                {
                    "full_name": "example/repo",
                    "default_branch": "main",
                    "private": True,
                    "html_url": "https://github.com/example/repo",
                }
            ],
        ) as listing:
            repos = client.get(f"{path}/github/accounts/{account_id}/repos")
        assert repos.status_code == 200 and repos.json()[0]["full_name"] == "example/repo"
        listing.assert_called_once()
        assert listing.call_args.args[0] == "test-only-token"
        revision = "a" * 40
        with patch("onefoundry.source_store.resolve_revision", return_value=("main", revision)) as resolve:
            source = client.post(
                f"{path}/sources/github",
                json={"account_id": account_id, "owner": "example", "repo": "repo", "branch": "main"},
                headers=ORIGIN,
            )
        assert source.status_code == 201, source.text
        resolve.assert_called_once()
        assert source.json()["revision"] == revision
        assert source.json()["availability"] == "verified_at_selection"
        source_id = source.json()["id"]
        assert client.get(f"{path}/sources").json() == [source.json()]
        assert client.get(f"/api/projects/{second['id']}/sources").json() == []
        assert client.delete(f"{path}/github/accounts/{account_id}", headers=ORIGIN).status_code == 409
        engine = create_engine(settings.database_url)
        with engine.connect() as connection:
            actor = connection.execute(
                text("SELECT project_id, actor_id FROM project_sources WHERE id=:id"),
                {"id": UUID(source_id)},
            ).one()
        engine.dispose()
        assert actor == (UUID(first["id"]), "operator")
        assert client.delete(f"{path}/sources/{source_id}", headers=ORIGIN).status_code == 204
        assert client.get(f"{path}/sources").json() == []
        assert client.delete(f"{path}/github/accounts/{account_id}", headers=ORIGIN).status_code == 204
        assert client.get(f"{path}/github/accounts").json() == []
        assert not (secret_root / "github" / account_id).exists()


def test_github_invalid_input_and_project_state(tmp_path: Path) -> None:
    secret_root = tmp_path / "credentials"
    secret_root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(secret_root),
    )
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project = client.post("/api/projects", json={"name": "Sources state"}, headers=ORIGIN).json()
        path = f"/api/projects/{project['id']}"
        for value in ("", "bad token\n", "bad token"):
            response = client.post(
                f"{path}/github/accounts", json={"label": "Invalid", "token": value}, headers=ORIGIN
            )
            assert response.status_code == 422
            assert value not in response.text or value == ""
        assert (
            client.post(
                f"{path}/sources/github",
                json={"account_id": str(uuid4()), "owner": "localhost:80", "repo": "repo"},
                headers=ORIGIN,
            ).status_code
            == 422
        )
        archived = client.post(
            f"{path}/archive", json={"expected_version": project["version"]}, headers=ORIGIN
        )
        assert archived.status_code == 200
        assert (
            client.post(
                f"{path}/github/accounts", json={"label": "Blocked", "token": "test-only"}, headers=ORIGIN
            ).status_code
            == 409
        )
        assert client.get(f"{path}/sources").status_code == 200


def test_account_commit_readback_preserves_credential(tmp_path: Path) -> None:
    secret_root = tmp_path / "credentials"
    secret_root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(secret_root),
    )
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id = UUID(
            client.post("/api/projects", json={"name": "Commit readback"}, headers=ORIGIN).json()["id"]
        )
    engine = create_engine(settings.database_url)
    store = SourceStore(engine, "operator", settings)
    real_begin = engine.begin

    @contextmanager
    def commit_then_lose_ack() -> Iterator[Connection]:
        with real_begin() as connection:
            yield connection
        raise OperationalError("COMMIT", {}, Exception("ack lost"))

    with patch.object(engine, "begin", side_effect=commit_then_lose_ack):
        saved = store.add_account(
            project_id, GitHubAccountCreate(label="Recovered", token=SecretStr("test-token-recovered"))
        )
    assert saved["credential_present"] is True
    assert store.list_accounts(project_id)[0]["id"] == saved["id"]
    assert store.secrets.read(saved["id"]) == "test-token-recovered"

    uncertain_id = uuid4()
    with (
        patch.object(engine, "begin", side_effect=commit_then_lose_ack),
        patch("onefoundry.source_store.uuid4", return_value=uncertain_id),
        patch.object(store, "_readback_account", side_effect=OperationalError("SELECT", {}, Exception())),
    ):
        with pytest.raises(Unavailable, match="outcome unknown"):
            store.add_account(
                project_id,
                GitHubAccountCreate(label="Uncertain", token=SecretStr("test-token-uncertain")),
            )
    assert store.secrets.read(uncertain_id) == "test-token-uncertain"
    assert {row["id"] for row in store.list_accounts(project_id)} == {saved["id"], uncertain_id}
    store.remove_account(project_id, saved["id"])
    store.remove_account(project_id, uncertain_id)
    engine.dispose()
