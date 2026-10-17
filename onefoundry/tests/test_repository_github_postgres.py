"""GitHub file reads retain project/account ownership and committed citations."""

from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from onefoundry.main import create_app
from onefoundry.role_repository_file import RepositoryReadError
from tests.test_repository_github import DATA, REVISION, responses
from tests.test_role_repository_file_unit import _context, _read
from tests.test_team_workflow_postgres import ORIGIN, project_with_models, settings_for


@pytest.mark.parametrize("changed", [None, "revoked", "foreign", "source"])
def test_saved_github_read_and_revocation(tmp_path: Path, changed: str | None) -> None:
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, _ = project_with_models(client)
        base = f"/api/projects/{project}"
        with patch("onefoundry.source_store.resolve_revision", return_value=("main", REVISION)):
            account = client.post(
                base + "/github/accounts", json={"label": "Fixture", "token": "fixture-only"}, headers=ORIGIN
            ).json()
            source = client.post(
                base + "/sources/github",
                json={"account_id": account["id"], "owner": "example", "repo": "repo", "branch": "main"},
                headers=ORIGIN,
            ).json()
        engine = create_engine(settings.database_url)
        values = iter(responses())
        count = 0

        def response(*args: object, **kwargs: object) -> object:
            nonlocal count
            count += 1
            if count == 3 and changed in {"revoked", "source"}:
                if changed == "source":
                    reply = client.delete(base + "/sources/" + source["id"], headers=ORIGIN)
                    assert reply.status_code == 204, reply.text
                else:
                    from onefoundry.source_store import SourceStore

                    SourceStore(engine, settings.operator_actor_id, settings).secrets.remove(
                        UUID(account["id"])
                    )
            return next(values)

        with patch("onefoundry.repository_github._get", side_effect=response):
            context = _context(uuid4() if changed == "foreign" else UUID(project))
            if changed in {"revoked", "source"}:
                with pytest.raises(RepositoryReadError):
                    _read(engine, settings, context, UUID(source["id"]), "ARCHITECTURE.md")
            else:
                result = _read(engine, settings, context, UUID(source["id"]), "ARCHITECTURE.md")
                if changed == "foreign":
                    assert result["found"] is False and count == 0
                else:
                    assert result["text"] == DATA.decode() and result["revision"] == REVISION
                    assert count == 3
        engine.dispose()
