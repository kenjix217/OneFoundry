"""API keys preserve the same server grants, lifecycle owner and MCP contracts."""

import hashlib
import json
import secrets
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import anyio
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from onefoundry.memory_plugin_api_key import MemoryApiKeyVerifier
from onefoundry.memory_plugin_gateway import MemoryPluginGateway
from onefoundry.memory_plugin_service import create_memory_plugin_app
from tests.memory_plugin_fixtures import policy


def configured_key(tmp_path: Path) -> tuple[Any, str, Path]:
    path = tmp_path / "records.json"
    configured = policy(
        auth_mode="api_key",
        api_key_records_path=path,
        oauth_issuer=None,
        oauth_public_key_path=None,
        public_url="http://127.0.0.1:9877/mcp",
    )
    key = secrets.token_urlsafe(32)
    path.write_text(
        json.dumps(
            [
                {
                    "sha256": hashlib.sha256(key.encode()).hexdigest(),
                    "subject": configured.grants[0].subject,
                    "client_id": configured.grants[0].client_id,
                    "expires_at": int(time.time()) + 3600,
                }
            ]
        )
    )
    path.chmod(0o600)
    return configured, key, path


def test_api_key_success_revocation_rotation_expiry_and_file_failures(tmp_path: Path) -> None:
    configured, key, path = configured_key(tmp_path)
    verifier = MemoryApiKeyVerifier(configured)
    accepted = anyio.run(verifier.verify_token, key)
    assert accepted is not None and accepted.scopes == ["memory.read"]
    assert anyio.run(verifier.verify_token, secrets.token_urlsafe(32)) is None
    assert anyio.run(verifier.verify_token, "malformed") is None
    row = json.loads(path.read_text())[0]
    row["expires_at"] = 1
    path.write_text(json.dumps([row]))
    assert anyio.run(verifier.verify_token, key) is None
    row["expires_at"] = int(time.time()) + 3600
    new_key = secrets.token_urlsafe(32)
    row["sha256"] = hashlib.sha256(new_key.encode()).hexdigest()
    path.write_text(json.dumps([row]))
    assert anyio.run(verifier.verify_token, key) is None
    assert anyio.run(verifier.verify_token, new_key) is not None
    path.write_text("[]")
    assert anyio.run(verifier.verify_token, new_key) is None
    path.write_text(json.dumps([row]))
    path.chmod(0o644)
    assert anyio.run(verifier.verify_token, new_key) is None
    with pytest.raises(ValueError):
        MemoryApiKeyVerifier(configured)
    path.chmod(0o600)
    path.unlink()
    assert anyio.run(verifier.verify_token, new_key) is None


@pytest.mark.parametrize("kind", ["symlink", "malformed", "unknown_actor", "duplicate", "oversized"])
def test_key_file_rejects_invalid_owner_contract(tmp_path: Path, kind: str) -> None:
    configured, _key, path = configured_key(tmp_path)
    row = json.loads(path.read_text())[0]
    if kind == "symlink":
        actual = tmp_path / "actual.json"
        path.rename(actual)
        path.symlink_to(actual)
    elif kind == "malformed":
        path.write_text('{"wrong": true}')
    elif kind == "unknown_actor":
        row["subject"] = "unapproved"
        path.write_text(json.dumps([row]))
    elif kind == "duplicate":
        path.write_text(json.dumps([row, row]))
    else:
        path.write_text(" " * 16385)
    with pytest.raises((OSError, ValueError)):
        MemoryApiKeyVerifier(configured)


def test_key_settings_fail_closed_and_override_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured, _key, _path = configured_key(tmp_path)
    for changes in (
        {"api_key_records_path": None},
        {"api_key_records_path": "relative.json"},
        {"oauth_issuer": "https://issuer.example.test/"},
        {"auth_mode": "none"},
        {"public_url": "http://127.0.0.1:9999/mcp"},
    ):
        with pytest.raises(ValidationError):
            policy(**{**configured.model_dump(), **changes})
    monkeypatch.setenv("MEMORY_PLUGIN_AUTH_MODE", "api_key")
    assert policy(auth_mode="oauth").auth_mode == "oauth"  # Explicit settings precede environment.


def test_real_mcp_key_auth_scopes_and_no_oauth_metadata(tmp_path: Path) -> None:
    configured, key, path = configured_key(tmp_path)
    grant = configured.grants[0]
    owner = MagicMock()
    owner.retrieve_memory.return_value = []
    app = create_memory_plugin_app(
        configured, MemoryPluginGateway(configured, {grant.actor_id: owner}), MemoryApiKeyVerifier(configured)
    )
    with TestClient(
        app, base_url="http://127.0.0.1:9877", headers={"accept": "application/json, text/event-stream"}
    ) as client:
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        assert client.post("/mcp", json=body).status_code == 401
        headers = {"authorization": "Bearer " + key}
        tools = client.post("/mcp", json=body, headers=headers).json()["result"]["tools"]
        assert {item["name"] for item in tools} == {"memory_search", "memory_read"}
        assert not any("securitySchemes" in item.get("_meta", {}) for item in tools)
        assert client.get("/.well-known/oauth-protected-resource/mcp", headers=headers).status_code == 404
        body.update(
            method="tools/call",
            params={"name": "memory_search", "arguments": {"project_id": str(grant.project_ids[0])}},
        )
        assert (
            client.post("/mcp", json=body, headers=headers).json()["result"]["structuredContent"]["count"]
            == 0
        )
        body["params"]["arguments"]["project_id"] = str(uuid4())
        denied = client.post("/mcp", json=body, headers=headers).json()["result"]
        assert denied["isError"] and denied["content"][0]["text"] == "authorization_denied"
        path.write_text("[]")
        assert client.post("/mcp", json=body, headers=headers).status_code == 401
        assert owner.retrieve_memory.call_count == 1
