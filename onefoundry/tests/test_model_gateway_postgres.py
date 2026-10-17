"""Project isolation and redacted provider catalog on migrated PostgreSQL."""

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from onefoundry.inference import TransportError
from onefoundry.main import create_app
from onefoundry.model_providers import DiscoveredModel, DiscoveryError
from onefoundry.model_secrets import SecretStoreError
from onefoundry.settings import Settings

ORIGIN = {"origin": "http://localhost:8765"}


def test_grok_and_keyless_ollama_catalog_and_profile_binding(tmp_path: Path) -> None:
    root = tmp_path / "model-secrets"
    root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(root),
    )
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project = client.post("/api/projects", json={"name": "Transports"}, headers=ORIGIN).json()["id"]
        path = f"/api/projects/{project}/model"
        descriptors = client.get(f"{path}/providers").json()
        assert len(descriptors) == 61
        assert {"openai", "anthropic", "gemini", "grok", "ollama", "openrouter"}.issubset(
            {row["id"] for row in descriptors}
        )
        assert {"openai", "grok", "ollama", "openrouter"}.issubset(
            {row["id"] for row in descriptors if row["text_runtime_available"]}
        )
        assert not next(row for row in descriptors if row["id"] == "bedrock")["connectable"]
        assert (
            client.post(
                f"{path}/accounts",
                json={"provider": "bedrock", "label": "No adapter", "api_key": "test-key"},
                headers=ORIGIN,
            ).status_code
            == 422
        )
        invalid = client.post(
            f"{path}/accounts",
            json={"provider": "ollama", "label": "Invalid", "endpoint": "http://example.com:11434"},
            headers=ORIGIN,
        )
        assert invalid.status_code == 422
        local = client.post(
            f"{path}/accounts",
            json={"provider": "ollama", "label": "Local", "endpoint": "http://ollama:11434"},
            headers=ORIGIN,
        )
        assert local.status_code == 201, local.text
        local_id = local.json()["id"]
        assert local.json()["auth_kind"] == "none"
        assert local.json()["credential_present"] is False
        assert local.json()["endpoint"] == "http://ollama:11434"
        assert not (root / local_id).exists()
        assert (
            client.put(
                f"{path}/accounts/{local_id}/credential", json={"api_key": "test-value"}, headers=ORIGIN
            ).status_code
            == 409
        )
        with patch(
            "onefoundry.model_gateway.discover_models",
            return_value=[DiscoveredModel("qwen-test:latest", "Qwen test", "text")],
        ) as discover:
            refreshed = client.post(f"{path}/accounts/{local_id}/refresh", headers=ORIGIN)
        assert refreshed.status_code == 200, refreshed.text
        discover.assert_called_once_with("ollama", "", settings, "http://ollama:11434")
        assert refreshed.json()["models"][0]["available"] is True
        bound = client.post(
            f"/api/projects/{project}/profiles",
            json={
                "name": "Local role",
                "instructions": "Draft",
                "model": "qwen-test:latest",
                "model_account_id": local_id,
            },
            headers=ORIGIN,
        )
        assert bound.status_code == 201, bound.text
        cloud = client.post(
            f"{path}/accounts",
            json={"provider": "grok", "label": "xAI", "api_key": "test-xai-key"},
            headers=ORIGIN,
        )
        assert cloud.status_code == 201, cloud.text
        assert cloud.json()["auth_kind"] == "api_key" and cloud.json()["endpoint"] is None
        assert (root / cloud.json()["id"]).exists()
        compatible = client.post(
            f"{path}/accounts",
            json={"provider": "openrouter", "label": "Compatible", "api_key": "test-compatible-key"},
            headers=ORIGIN,
        )
        assert compatible.status_code == 201, compatible.text
        with patch(
            "onefoundry.model_gateway.discover_models",
            return_value=[DiscoveredModel("example-chat", "Example chat", "text")],
        ) as discover_compatible:
            refreshed_compatible = client.post(
                f"{path}/accounts/{compatible.json()['id']}/refresh", headers=ORIGIN
            )
        assert refreshed_compatible.status_code == 200, refreshed_compatible.text
        discover_compatible.assert_called_once_with("openrouter", "test-compatible-key", settings, None)
        assert refreshed_compatible.json()["models"][0]["available"] is True


def test_accounts_catalog_binding_and_secret_boundary(tmp_path: Path) -> None:
    root = tmp_path / "model-secrets"
    root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(root),
    )
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        absent_project = str(UUID(int=0))
        assert client.get(f"/api/projects/{absent_project}/model/accounts").status_code == 404
        first = client.post("/api/projects", json={"name": "Model first"}, headers=ORIGIN).json()["id"]
        second = client.post("/api/projects", json={"name": "Model second"}, headers=ORIGIN).json()["id"]
        path = f"/api/projects/{first}/model"
        key = "sensitive-test-value"
        invalid_key = "sensitive invalid key"
        invalid = client.post(
            f"{path}/accounts",
            json={"provider": "openai", "label": "Bad", "api_key": invalid_key},
            headers=ORIGIN,
        )
        assert invalid.status_code == 422 and invalid_key not in invalid.text
        nested = client.post(
            f"{path}/accounts",
            json={"provider": "openai", "label": "Bad", "api_key": {"nested": key}},
            headers=ORIGIN,
        )
        assert nested.status_code == 422 and key not in nested.text
        blank = client.post(
            f"{path}/accounts", json={"provider": "openai", "label": "   ", "api_key": key}, headers=ORIGIN
        )
        assert blank.status_code == 422 and key not in blank.text
        created = client.post(
            f"{path}/accounts",
            json={"provider": "openai", "label": "Primary", "api_key": key},
            headers=ORIGIN,
        )
        assert created.status_code == 201
        account = created.json()
        assert account["credential_present"] and account["catalog_valid"] is False
        assert key not in created.text
        assert key not in client.get(f"{path}/accounts").text
        assert client.get(f"/api/projects/{second}/model/accounts").json() == []
        assert (
            client.post(
                f"/api/projects/{second}/model/accounts/{account['id']}/refresh", headers=ORIGIN
            ).status_code
            == 404
        )
        assert client.get(f"{path}/catalog").json() == []
        with patch(
            "onefoundry.model_gateway.discover_models",
            return_value=[DiscoveredModel("gpt-test", "GPT Test", "text")],
        ):
            refreshed = client.post(f"{path}/accounts/{account['id']}/refresh", headers=ORIGIN)
        assert refreshed.status_code == 200
        assert refreshed.json()["models"][0]["model_id"] == "gpt-test"
        assert refreshed.json()["models"][0]["available"] is True
        profiles = f"/api/projects/{first}/profiles"
        bound = client.post(
            profiles,
            json={
                "name": "Model role",
                "instructions": "Draft",
                "model": "gpt-test",
                "model_account_id": account["id"],
            },
            headers=ORIGIN,
        )
        assert bound.status_code == 201
        assert bound.json()["model_account_id"] == account["id"]
        assert (
            client.post(
                f"/api/projects/{second}/profiles",
                json={
                    "name": "Wrong project",
                    "instructions": "Draft",
                    "model": "gpt-test",
                    "model_account_id": account["id"],
                },
                headers=ORIGIN,
            ).status_code
            == 422
        )
        assert (
            client.post(
                profiles,
                json={
                    "name": "Unknown",
                    "instructions": "Draft",
                    "model": "not-listed",
                    "model_account_id": account["id"],
                },
                headers=ORIGIN,
            ).status_code
            == 422
        )
        assert client.delete(f"{path}/accounts/{account['id']}", headers=ORIGIN).status_code == 409
        rotated = client.put(
            f"{path}/accounts/{account['id']}/credential", json={"api_key": "new-test-value"}, headers=ORIGIN
        )
        assert rotated.status_code == 200 and rotated.json()["catalog_valid"] is False
        assert client.get(f"{path}/catalog").json()[0]["available"] is False
        assert (
            client.patch(
                f"{profiles}/{bound.json()['id']}", json={"instructions": "Review"}, headers=ORIGIN
            ).status_code
            == 200
        )
        engine = create_engine(settings.database_url)
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM model_accounts WHERE project_id=:id"), {"id": UUID(first)}
                )
                == 1
            )
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM model_catalog WHERE project_id=:id"), {"id": UUID(first)}
                )
                == 1
            )
            for query in (
                "SELECT * FROM model_accounts WHERE project_id=:id",
                "SELECT * FROM model_catalog WHERE project_id=:id",
                "SELECT * FROM profiles WHERE project_id=:id",
            ):
                values = connection.execute(text(query), {"id": UUID(first)}).mappings().all()
                assert key not in str(values)
        engine.dispose()
        assert (root / account["id"]).stat().st_mode & 0o777 == 0o600


def test_failed_refresh_keeps_catalog_but_marks_unavailable(tmp_path: Path) -> None:
    root = tmp_path / "model-secrets"
    root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(root),
    )
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id = client.post("/api/projects", json={"name": "Refresh failure"}, headers=ORIGIN).json()[
            "id"
        ]
        path = f"/api/projects/{project_id}/model"
        account_id = client.post(
            f"{path}/accounts",
            json={"provider": "gemini", "label": "Primary", "api_key": "sensitive-test-value"},
            headers=ORIGIN,
        ).json()["id"]
        with patch(
            "onefoundry.model_gateway.discover_models",
            return_value=[DiscoveredModel("models/gemini-test", "Gemini Test", "text")],
        ):
            assert client.post(f"{path}/accounts/{account_id}/refresh", headers=ORIGIN).status_code == 200
        with patch("onefoundry.model_gateway.discover_models", side_effect=DiscoveryError("bad list")):
            assert client.post(f"{path}/accounts/{account_id}/refresh", headers=ORIGIN).status_code == 502
        catalog = client.get(f"{path}/catalog").json()
        assert len(catalog) == 1 and catalog[0]["available"] is False
        assert client.get(f"{path}/accounts").json()[0]["catalog_valid"] is False
        (root / account_id).unlink()
        assert client.post(f"{path}/accounts/{account_id}/refresh", headers=ORIGIN).status_code == 409
        (root / account_id).write_bytes(b"corrupt\x00value")
        (root / account_id).chmod(0o600)
        assert client.post(f"{path}/accounts/{account_id}/refresh", headers=ORIGIN).status_code == 409
        assert client.get(f"{path}/catalog").json()[0]["available"] is False


def test_delete_failure_orphan_reconciles_on_startup(tmp_path: Path) -> None:
    root = tmp_path / "model-secrets"
    root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(root),
    )
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project_id = client.post("/api/projects", json={"name": "Orphan recovery"}, headers=ORIGIN).json()[
            "id"
        ]
        path = f"/api/projects/{project_id}/model"
        account_id = client.post(
            f"{path}/accounts",
            json={"provider": "openai", "label": "Primary", "api_key": "sensitive-test-value"},
            headers=ORIGIN,
        ).json()["id"]
        with patch(
            "onefoundry.model_secrets.ModelSecretStore.remove", side_effect=SecretStoreError("cleanup failed")
        ):
            assert client.delete(f"{path}/accounts/{account_id}", headers=ORIGIN).status_code == 503
        assert client.get(f"{path}/accounts").json() == []
        assert (root / account_id).exists()
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        assert client.get("/health/ready").status_code == 200
    assert not (root / account_id).exists()


def test_older_transport_failure_cannot_invalidate_newer_catalog(tmp_path: Path) -> None:
    root = tmp_path / "secrets"
    root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(root),
    )
    first_started = threading.Event()
    release_first = threading.Event()
    counter_lock = threading.Lock()
    calls = 0

    def discovery(provider: str, key: str, config: Settings, endpoint: str | None) -> list[DiscoveredModel]:
        nonlocal calls
        with counter_lock:
            calls += 1
            attempt = calls
        if attempt == 1:
            first_started.set()
            assert release_first.wait(10), "newer discovery never completed"
            raise TransportError("private dependency detail")
        return [DiscoveredModel("newer-model", "Newer model", "text")]

    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project = client.post("/api/projects", json={"name": "Discovery race"}, headers=ORIGIN).json()["id"]
        base = f"/api/projects/{project}/model"
        account_id = client.post(
            f"{base}/accounts",
            json={"provider": "openai", "label": "API", "api_key": "private-key"},
            headers=ORIGIN,
        ).json()["id"]
        path = f"{base}/accounts/{account_id}/refresh"
        with (
            patch("onefoundry.model_gateway.discover_models", side_effect=discovery),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            older = pool.submit(client.post, path, headers=ORIGIN)
            try:
                assert first_started.wait(10), "first discovery never started"
                newer = client.post(path, headers=ORIGIN)
                assert newer.status_code == 200, newer.text
            finally:
                release_first.set()
            failure = older.result(timeout=10)
            assert failure.status_code == 502 and "transport failed" in failure.text
            assert "private" not in failure.text
        catalog = client.get(f"{base}/catalog").json()
        assert catalog[0]["model_id"] == "newer-model" and catalog[0]["available"] is True
        assert client.get(f"{base}/accounts").json()[0]["catalog_valid"] is True
        assert calls == 2
