"""Real narrow database roles and selective capture/review/indexing lifecycle."""

import secrets
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from onefoundry.company_memory_schemas import CaptureCreate
from onefoundry.db_roles import RuntimeRoleSettings
from onefoundry.main import create_app
from onefoundry.memory_embedding_index import index_project
from onefoundry.memory_plugin_capture_db_role import provision_memory_capture_role
from onefoundry.memory_plugin_capture_gateway import capture_gateways
from onefoundry.memory_plugin_db_role import provision_memory_read_role
from onefoundry.memory_plugin_gateway import MemoryFacadeError, MemoryPluginGateway
from onefoundry.memory_plugin_settings import MemoryGrant
from onefoundry.memory_schemas import CandidateReview
from onefoundry.settings import Settings
from onefoundry.tool_gateway import ToolGateway
from tests.memory_plugin_fixtures import policy
from tests.test_company_memory_postgres import owner
from tests.test_memory_capture_api_key import selection
from tests.test_memory_embeddings import configured
from tests.test_memory_embeddings_postgres import project, vectors
from tests.test_task_session_postgres import _settings


def role(bootstrap: Settings, kind: str) -> RuntimeRoleSettings:
    return RuntimeRoleSettings(
        app_db_user=kind + "_" + uuid4().hex, app_db_password=secrets.token_urlsafe(32)
    )


def make_facade(
    tmp_path: Path, project_id: UUID, monkeypatch: pytest.MonkeyPatch
) -> tuple[Any, Any, Any, Any]:
    bootstrap = Settings()
    reader_role, writer_role = role(bootstrap, "capture_reader"), role(bootstrap, "capture_writer")
    provision_memory_read_role(bootstrap, reader_role, deadline_seconds=10)
    provision_memory_capture_role(bootstrap, writer_role, deadline_seconds=10)
    monkeypatch.setenv("MEMORY_CAPTURE_DB_USER", writer_role.app_db_user)
    monkeypatch.setenv("MEMORY_CAPTURE_DB_PASSWORD", writer_role.app_db_password)
    grant = MemoryGrant(
        subject="fixture-capture",
        client_id="selected-capture",
        actor_id="external-memory:fixture-capture",
        project_ids=(project_id,),
        capture=True,
    )
    read_settings = configured(
        db_host=bootstrap.db_host,
        db_port=bootstrap.db_port,
        db_name=bootstrap.db_name,
        db_user=reader_role.app_db_user,
        db_password=reader_role.app_db_password,
    )
    read = ToolGateway(
        create_engine(read_settings.database_url, hide_parameters=True),
        grant.actor_id,
        live_text_settings=read_settings,
    )
    configured_policy = policy(
        auth_mode="api_key",
        oauth_issuer=None,
        oauth_public_key_path=None,
        api_key_records_path=tmp_path / "unread-key-file.json",
        public_url="http://127.0.0.1:9877/mcp",
        grants=[grant],
        max_calls_per_minute=120,
    )
    writers = capture_gateways(configured_policy, read_settings)
    facade = MemoryPluginGateway(configured_policy, {grant.actor_id: read}, capture_gateways=writers)
    return facade, grant, writers[grant.actor_id], read_settings


def propose(facade: MemoryPluginGateway, grant: MemoryGrant, data: dict[str, Any]) -> dict[str, Any]:
    preview = facade.invoke(grant, "memory_capture_preview", data)
    return facade.invoke(grant, "memory_capture", {**data, "preview_sha256": preview["preview_sha256"]})


def test_narrow_capture_does_not_publish_until_review_and_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = owner()
    with TestClient(create_app(_settings(tmp_path)), base_url="http://localhost:8765") as client:
        own, foreign = project(client), project(client)
        facade, grant, writer, _settings_read = make_facade(tmp_path, own, monkeypatch)
        chosen = selection(str(own), summary="Copper customers renew annually")
        with gateway.engine.connect() as connection:
            before = connection.scalar(
                text("SELECT count(*) FROM memory_captures WHERE project_id=:p"), {"p": own}
            )
        preview = facade.invoke(grant, "memory_capture_preview", chosen)
        with gateway.engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM memory_captures WHERE project_id=:p"), {"p": own}
                )
                == before
            )
        result = facade.invoke(
            grant, "memory_capture", {**chosen, "preview_sha256": preview["preview_sha256"]}
        )
        saved = result["capture"]
        assert result["review_required"] and result["selection_sha256"] == preview["preview_sha256"]
        assert saved["candidate"]["status"] == "pending" and saved["candidate"]["actor_id"] == grant.actor_id
        archive = gateway.read_memory_source(own, UUID(saved["source_id"]))
        assert archive["actor_id"] == grant.actor_id
        assert archive["messages"] == preview["selection"]["messages"]
        assert (
            facade.invoke(
                grant, "memory_search", {"project_id": str(own), "query": "subscription extension"}
            )["items"]
            == []
        )
        gateway.review_candidate(own, UUID(saved["candidate"]["id"]), CandidateReview(decision="rejected"))
        assert gateway.read_memory_source(own, UUID(saved["source_id"])) == archive
        assert facade.invoke(grant, "memory_search", {"project_id": str(own)})["items"] == []
        next_result = propose(facade, grant, selection(str(own), summary="Copper customers renew annually"))
        candidate = next_result["capture"]["candidate"]
        gateway.review_candidate(own, UUID(candidate["id"]), CandidateReview(decision="approved"))
        with pytest.raises(MemoryFacadeError, match="dependency_unavailable"):
            facade.invoke(grant, "memory_search", {"project_id": str(own), "query": "subscription extension"})
        with patch("onefoundry.memory_embeddings.LocalEmbeddings.embed", side_effect=vectors):
            assert index_project(gateway, configured(), own, limit=16)["indexed"] == 1
            found = facade.invoke(
                grant, "memory_search", {"project_id": str(own), "query": "subscription extension"}
            )
            assert len(found["items"]) == 1 and found["items"][0]["score"] == 0
        for data in (selection(str(foreign)), selection(str(own), scope="company")):
            with pytest.raises(MemoryFacadeError, match="authorization_denied"):
                facade.invoke(grant, "memory_capture_preview", data)
        with writer.engine.connect() as connection:
            for query in (
                "UPDATE dreamcycle_candidates SET status='approved' WHERE false",
                "INSERT INTO dreamcycle_candidates (status) VALUES ('approved')",
                "UPDATE memory_items SET state='withdrawn' WHERE false",
                "INSERT INTO memory_items (id) VALUES (gen_random_uuid())",
                "DELETE FROM memory_captures WHERE false",
                "SELECT messages FROM memory_captures LIMIT 0",
                "SELECT instructions FROM profiles LIMIT 0",
                "SELECT payload FROM events LIMIT 0",
                "UPDATE projects SET status='trashed' WHERE false",
            ):
                with pytest.raises(DBAPIError) as failure:
                    connection.exec_driver_sql(query)
                assert getattr(failure.value.orig, "sqlstate", None) == "42501"
                connection.rollback()


def test_capture_duplicates_race_and_changed_id_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = owner()
    with TestClient(create_app(_settings(tmp_path)), base_url="http://localhost:8765") as client:
        own = project(client)
        facade, grant, writer, _settings_read = make_facade(tmp_path, own, monkeypatch)
        chosen = selection(str(own))
        preview = facade.invoke(grant, "memory_capture_preview", chosen)
        committed = {**chosen, "preview_sha256": preview["preview_sha256"]}
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: facade.invoke(grant, "memory_capture", committed), range(2)))
        assert responses[0] == responses[1]
        with gateway.engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM memory_captures WHERE project_id=:p"), {"p": own}
                )
                == 1
            )
        with pytest.raises(MemoryFacadeError, match="capture_preview_required"):
            facade.invoke(grant, "memory_capture", {**committed, "summary": "Different selected fact"})
        altered = {**chosen, "summary": "Different selected fact"}
        preview = facade.invoke(grant, "memory_capture_preview", altered)
        with pytest.raises(MemoryFacadeError, match="conflict"):
            facade.invoke(grant, "memory_capture", {**altered, "preview_sha256": preview["preview_sha256"]})
        archive = gateway.read_memory_source(own, UUID(responses[0]["capture"]["source_id"]))
        assert archive["messages"] == preview["selection"]["messages"]


def test_draft_database_owner_supports_capture_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(create_app(_settings(tmp_path)), base_url="http://localhost:8765") as client:
        own = project(client)
        _facade, grant, writer, _settings_read = make_facade(tmp_path, own, monkeypatch)
        chosen = selection(str(own))
        chosen.pop("project_id")
        result = writer.capture_company_memory(own, CaptureCreate.model_validate(chosen))
        assert result["candidate"]["status"] == "pending"
        assert result["candidate"]["actor_id"] == grant.actor_id


def test_capture_startup_rejects_widened_archive_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bootstrap = create_engine(Settings().database_url, hide_parameters=True)
    with TestClient(create_app(_settings(tmp_path)), base_url="http://localhost:8765") as client:
        own = project(client)
        facade, _grant, writer, read_settings = make_facade(tmp_path, own, monkeypatch)
        # RuntimeRoleSettings admits only a SQL-safe lowercase role identifier.
        with bootstrap.begin() as connection:
            connection.exec_driver_sql(
                f'GRANT SELECT (messages) ON memory_captures TO "{writer.engine.url.username}"'
            )
            assert connection.scalar(
                text("SELECT has_column_privilege(:role,'memory_captures','messages','SELECT')"),
                {"role": writer.engine.url.username},
            )
        with pytest.raises(ValueError, match="draft-only privileges"):
            capture_gateways(facade.policy, read_settings)
    bootstrap.dispose()
