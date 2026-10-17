"""Request boundary and readiness behavior without a database service."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import StaticPool

from onefoundry.main import create_app
from onefoundry.model_secrets import SecretStoreError
from onefoundry.settings import Settings


@pytest.fixture
def runtime_settings(tmp_path: Path) -> Settings:
    secret_root = tmp_path / "model-secrets"
    secret_root.mkdir(mode=0o700)
    return Settings(db_password="test-only", model_secret_root=str(secret_root))


def test_live_and_ready_with_database(tmp_path: Path) -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE projects (id TEXT, status TEXT, version INTEGER, trash_from_status TEXT)")
        )
        connection.execute(text("CREATE TABLE events (id TEXT)"))
        connection.execute(text("CREATE TABLE profiles (id TEXT, model_account_id TEXT)"))
        connection.execute(text("CREATE TABLE model_accounts (id TEXT)"))
        connection.execute(text("CREATE TABLE model_catalog (id TEXT)"))
        connection.execute(text("CREATE TABLE live_text_run_bindings (id TEXT)"))
        connection.execute(text("CREATE TABLE skills (id TEXT)"))
        connection.execute(text("CREATE TABLE skill_versions (id TEXT)"))
        connection.execute(text("CREATE TABLE profile_skill_bindings (id TEXT)"))
        connection.execute(text("CREATE TABLE run_skill_snapshots (id TEXT)"))
        connection.execute(text("CREATE TABLE live_text_outputs (id TEXT)"))
        connection.execute(text("CREATE TABLE live_text_output_sources (id TEXT)"))
        connection.execute(text("CREATE TABLE task_conversations (id TEXT)"))
        connection.execute(text("CREATE TABLE task_conversation_turns (id TEXT)"))
        connection.execute(text("CREATE TABLE remote_channels (id TEXT, credential_revision INTEGER)"))
        connection.execute(text("CREATE TABLE remote_inbound (id TEXT, status TEXT)"))
        connection.execute(text("CREATE TABLE remote_outbound (id TEXT, status TEXT)"))
        connection.execute(
            text(
                "CREATE TABLE work_orders (id TEXT, version INTEGER, "
                "block_reason TEXT, blocked_from_status TEXT)"
            )
        )
        connection.execute(
            text("CREATE TABLE runs (id TEXT, task_title_snapshot TEXT, task_instructions_snapshot TEXT)")
        )
        connection.execute(text("CREATE TABLE run_profile_snapshots (id TEXT)"))
        connection.execute(text("CREATE TABLE approval_requests (id TEXT)"))
        connection.execute(
            text(
                "CREATE TABLE run_results (id TEXT, source_sales_draft_id TEXT, "
                "source_runner_job_id TEXT, source_sha256 TEXT)"
            )
        )
        connection.execute(text("CREATE TABLE dreamcycle_candidates (id TEXT)"))
        connection.execute(text("CREATE TABLE memory_items (id TEXT)"))
        connection.execute(text("CREATE TABLE text_outputs (id TEXT)"))
        connection.execute(text("CREATE TABLE text_output_sources (id TEXT)"))
        connection.execute(text("CREATE TABLE runner_jobs (id TEXT)"))
        connection.execute(text("CREATE TABLE runner_artifacts (id TEXT)"))
        connection.execute(
            text(
                "CREATE TABLE external_code_attempts (id TEXT, source_id TEXT, source_root_id TEXT, "
                "source_relative_path TEXT, source_branch TEXT, source_revision TEXT)"
            )
        )
        connection.execute(text("CREATE TABLE external_code_artifacts (id TEXT)"))
        connection.execute(text("CREATE TABLE workspace_artifacts (id TEXT, status TEXT)"))
        connection.execute(text("CREATE TABLE github_accounts (id TEXT)"))
        connection.execute(text("CREATE TABLE project_sources (id TEXT)"))
        connection.execute(text("CREATE TABLE synthetic_merge_bindings (id TEXT)"))
        connection.execute(text("CREATE TABLE sales_drafts (id TEXT)"))
        connection.execute(text("CREATE TABLE coordination_intents (id TEXT, action_kind TEXT)"))
        connection.execute(text("CREATE TABLE coordination_signals (id TEXT)"))
        connection.execute(text("CREATE TABLE coordination_rules (id TEXT)"))
        connection.execute(text("CREATE TABLE coordination_rule_versions (id TEXT)"))
        connection.execute(text("CREATE TABLE coordination_controls (project_id TEXT)"))
        connection.execute(text("CREATE TABLE mail_accounts (id TEXT)"))
        connection.execute(text("CREATE TABLE smtp_accounts (id TEXT)"))
        connection.execute(text("CREATE TABLE zoho_mail_connections (id TEXT,credential_revision INTEGER)"))
        connection.execute(text("CREATE TABLE zoho_mail_actions (id TEXT)"))
        connection.execute(text("CREATE TABLE outbound_emails (id TEXT)"))
        connection.execute(text("CREATE TABLE coordination_profile_proposals (id TEXT)"))
        connection.execute(text("CREATE TABLE connector_accounts (id TEXT)"))
        connection.execute(text("CREATE TABLE document_sources (id TEXT)"))
        connection.execute(text("CREATE TABLE document_files (id TEXT)"))
        connection.execute(text("CREATE TABLE document_writes (id TEXT)"))
    secret_root = tmp_path / "model-secrets"
    secret_root.mkdir(mode=0o700)
    artifact_root = tmp_path / "artifact-workspace"
    artifact_root.mkdir(mode=0o700)
    # SQLite does not support the PostgreSQL row lock used by reconciliation.
    # The empty stub tables represent a fully reconciled conversation schema.
    with (
        patch("onefoundry.task_conversation_store.TaskConversationStore.reconcile_interrupted"),
        patch("onefoundry.remote_channel_core.RemoteChannelCore.reconcile_interrupted"),
        patch("onefoundry.remote_channel_runtime.RemoteChannelRuntime.start", new_callable=AsyncMock),
        patch("onefoundry.remote_channel_runtime.RemoteChannelRuntime.stop", new_callable=AsyncMock),
    ):
        app = create_app(
            Settings(
                db_password="test-only",
                model_secret_root=str(secret_root),
                artifact_workspace_root=str(artifact_root),
            ),
            engine,
        )
        app.state.remote_channels.gateway._task_conversations_reconciled = True
        app.state.remote_channel_runtime.reconciled = True
        with TestClient(
            app,
            base_url="http://localhost:8765",
        ) as client:
            assert client.get("/health/live").json() == {"status": "live"}
            response = client.get("/health/ready")
            artifact_root.rmdir()
            missing_artifact_response = client.get("/health/ready")
    assert app.openapi()["info"]["title"] == "OneFoundry"
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "database": "ready"}
    assert missing_artifact_response.status_code == 503
    assert missing_artifact_response.json() == {"detail": "artifact workspace unavailable"}
    missing_root = tmp_path / "missing-model-secrets"
    with pytest.raises(SecretStoreError, match="directory unavailable"):
        with TestClient(
            create_app(Settings(db_password="test-only", model_secret_root=str(missing_root)), engine),
            base_url="http://localhost:8765",
        ):
            pass
    engine.dispose()


def test_ready_reports_database_failure(runtime_settings: Settings) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with patch.object(engine, "connect", side_effect=OperationalError("SELECT 1", {}, Exception())):
        with TestClient(create_app(runtime_settings, engine), base_url="http://localhost:8765") as client:
            response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}
    engine.dispose()


def test_api_reports_database_failure(runtime_settings: Settings) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with patch.object(engine, "connect", side_effect=OperationalError("SELECT", {}, Exception())):
        with TestClient(create_app(runtime_settings, engine), base_url="http://localhost:8765") as client:
            response = client.get("/api/projects")
    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}
    engine.dispose()


def test_ready_rejects_missing_schema(runtime_settings: Settings) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with TestClient(create_app(runtime_settings, engine), base_url="http://localhost:8765") as client:
        response = client.get("/health/ready")
    assert response.status_code == 503
    engine.dispose()


def test_wrong_host_is_rejected(runtime_settings: Settings) -> None:
    with TestClient(
        create_app(runtime_settings, create_engine("sqlite+pysqlite:///:memory:")),
        base_url="http://localhost:8765",
    ) as client:
        response = client.get("/health/live", headers={"host": "example.com"})
    assert response.status_code == 400


def test_write_requires_exact_origin(runtime_settings: Settings) -> None:
    with TestClient(
        create_app(runtime_settings, create_engine("sqlite+pysqlite:///:memory:")),
        base_url="http://localhost:8765",
    ) as client:
        for origin in (None, "https://evil.example", "http://localhost:9999", "null"):
            headers = {} if origin is None else {"origin": origin}
            assert client.post("/missing", headers=headers).status_code == 403
        assert client.post("/missing", headers={"origin": "http://testserver"}).status_code == 403
        assert (
            client.post(
                "/api/projects",
                json={"name": "Test"},
                headers={"host": "localhost:9999", "origin": "http://localhost:9999"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/projects",
                json={"name": ""},
                headers={"origin": "http://localhost:8765"},
            ).status_code
            == 422
        )
