"""Real MCP request parsing and OAuth/grant denials with synthetic storage owners."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import anyio
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from onefoundry.memory_plugin_auth import MemoryTokenVerifier, load_public_key
from onefoundry.memory_plugin_gateway import MemoryFacadeError, MemoryPluginGateway
from onefoundry.memory_plugin_service import create_memory_plugin_app
from onefoundry.memory_plugin_settings import MemoryPluginSettings
from tests.memory_plugin_fixtures import keypair, policy, token


def test_settings_missing_invalid_and_environment_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        MemoryPluginSettings(_env_file=None)  # type: ignore[call-arg]
    invalid: tuple[dict[str, Any], ...] = (
        {"listen_host": "0.0.0.0"},  # noqa: S104 - rejection fixture
        {"public_url": "http://localhost:9877/mcp"},
        {"public_url": "http://127.0.0.1:9877/mcp"},
        {"oauth_issuer": "http://issuer.example.test"},
        {"public_url": "https://memory.example.test/mcp?token=bad"},
        {"max_results": 21},
        {"oauth_public_key_path": "relative.pem"},
        {"grants": []},
        {"max_request_bytes": 200000},
        {"deadline_seconds": 0},
        {"max_in_flight": 0},
        {"max_token_seconds": 3601},
    )
    for changes in invalid:
        with pytest.raises(ValidationError):
            policy(**changes)
    configured = policy()
    for field, value in configured.model_dump(mode="json").items():
        if value is not None:
            monkeypatch.setenv(
                f"MEMORY_PLUGIN_{field.upper()}",
                json.dumps(value) if isinstance(value, (list, dict)) else str(value),
            )
    from_env = MemoryPluginSettings()  # type: ignore[call-arg]
    assert from_env == configured
    monkeypatch.setenv("MEMORY_PLUGIN_MAX_RESULTS", "2")
    assert MemoryPluginSettings().max_results == 2  # type: ignore[call-arg]
    assert MemoryPluginSettings(max_results=3).max_results == 3  # type: ignore[call-arg]
    monkeypatch.setenv("MEMORY_PLUGIN_MAX_RESULTS", "0")
    with pytest.raises(ValidationError):
        MemoryPluginSettings()  # type: ignore[call-arg]


def test_auth_requires_exact_issuer_resource_identity_expiry_and_scope(tmp_path: Path) -> None:
    configured = policy()
    private, public = keypair()
    verifier = MemoryTokenVerifier(configured, public)
    assert anyio.run(verifier.verify_token, token(private, configured)) is not None
    invalid: tuple[dict[str, Any], ...] = (
        {"sub": "another-operator"},
        {"client_id": "another-client"},
        {"scope": "memory.capture"},
        {"aud": "https://other.example.test/mcp"},
        {"iss": "https://other.example.test/"},
        {"exp": 1},
        {"iat": 1, "exp": 2000000000},
        {"exp": "2000000000"},
        {"aud": [configured.public_url, "https://other.example.test/"]},
    )
    for changes in invalid:
        assert anyio.run(verifier.verify_token, token(private, configured, **changes)) is None
    assert anyio.run(verifier.verify_token, "malformed") is None
    key_path = tmp_path / "public.pem"
    key_path.write_bytes(public)
    key_path.chmod(0o600)
    assert load_public_key(key_path) == public
    key_path.chmod(0o666)
    with pytest.raises(ValueError):
        load_public_key(key_path)
    key_path.chmod(0o600)
    alias = tmp_path / "alias.pem"
    alias.symlink_to(key_path)
    with pytest.raises(ValueError):
        load_public_key(alias)


def test_tunnel_resource_binding_and_no_unapproved_tools() -> None:
    configured = policy(
        public_url="http://127.0.0.1:9877/mcp",
        oauth_audience="https://tunnel.example.test/private-memory",
        oauth_auxiliary_audiences=("https://issuer.example.test/userinfo",),
    )
    private, public = keypair()
    owner = MagicMock()
    owner.retrieve_memory.return_value = []
    gateway = MemoryPluginGateway(configured, {configured.grants[0].actor_id: owner})
    headers = {
        "authorization": f"Bearer {token(private, configured)}",
        "accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25",
    }
    app = create_memory_plugin_app(configured, gateway, MemoryTokenVerifier(configured, public))
    project = str(configured.grants[0].project_ids[0])
    with TestClient(app, base_url="http://127.0.0.1:9877", headers=headers) as client:

        def rpc(method: str, params: Any = None, **extra: Any) -> Any:
            return client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": method, **({"params": params} if params else {})},
                **extra,
            )

        metadata = client.get("/.well-known/oauth-protected-resource/mcp").json()
        assert metadata["resource"] == configured.public_url
        assert metadata["authorization_servers"] == [configured.oauth_issuer]
        denied = rpc("tools/list", headers={"authorization": "Bearer invalid"})
        assert denied.status_code == 401 and "resource_metadata=" in denied.headers["www-authenticate"]
        listing = rpc("tools/list").json()["result"]["tools"]
        assert {item["name"] for item in listing} == {"memory_search", "memory_read"}
        assert all(item["inputSchema"]["additionalProperties"] is False for item in listing)
        assert rpc("tools/call", {"name": "memory_search", "arguments": {"project_id": project}}).json()[
            "result"
        ]["structuredContent"] == {"items": [], "count": 0, "current_only": True}
        owner.retrieve_memory.assert_called_once_with(configured.grants[0].project_ids[0], "", 10, "project")
        for args, code in (
            ({"project_id": str(uuid4())}, "authorization_denied"),
            ({"project_id": project, "scope": "company"}, "authorization_denied"),
            ({"project_id": project, "limit": "10"}, "invalid_input"),
            ({"project_id": project, "query": "界" * 50}, "invalid_input"),
            ({"project_id": project, "actor_id": "operator"}, "invalid_input"),
        ):
            result = rpc("tools/call", {"name": "memory_search", "arguments": args}).json()["result"]
            assert result["isError"] is True and result["content"][0]["text"] == code
        for name in ("memory_review", "memory_delete", "sql", "system_instructions"):
            result = rpc("tools/call", {"name": name, "arguments": {}}).json()["result"]
            assert result["content"][0]["text"] == "unsupported_tool"
        wrong = rpc(
            "tools/list",
            headers={"authorization": f"Bearer {token(private, configured, aud=configured.public_url)}"},
        )
        assert wrong.status_code == 401
    assert owner.retrieve_memory.call_count == 1


def test_rate_bound_and_dependency_identity() -> None:
    configured = policy(max_calls_per_minute=1)
    grant = configured.grants[0]
    owner = MagicMock()
    owner.retrieve_memory.return_value = []
    gateway = MemoryPluginGateway(configured, {grant.actor_id: owner})
    gateway.invoke(grant, "memory_search", {"project_id": str(grant.project_ids[0])})
    with pytest.raises(MemoryFacadeError, match="rate_limited"):
        gateway.invoke(grant, "memory_search", {"project_id": str(grant.project_ids[0])})
    configured = policy()
    grant = configured.grants[0]
    owner.retrieve_memory.side_effect = SQLAlchemyError("sensitive dependency diagnostic")
    gateway = MemoryPluginGateway(configured, {grant.actor_id: owner})
    with pytest.raises(MemoryFacadeError, match="dependency_unavailable"):
        gateway.invoke(grant, "memory_search", {"project_id": str(grant.project_ids[0])})
    payloads = [call.args[5] for call in owner._event.call_args_list]
    assert all("sensitive" not in json.dumps(payload) for payload in payloads)


def test_queued_deadline_does_not_launch_or_abandon_running_receipt() -> None:
    configured = policy(max_in_flight=1, deadline_seconds=2)
    grant = configured.grants[0]
    private, public = keypair()
    owner = MagicMock()
    started, release = Event(), Event()

    def blocked_read(*_args: object) -> list[dict[str, object]]:
        started.set()
        assert release.wait(timeout=6), "fixture failed to release bounded owner work"
        return []

    owner.retrieve_memory.side_effect = blocked_read
    gateway = MemoryPluginGateway(configured, {grant.actor_id: owner})
    app = create_memory_plugin_app(configured, gateway, MemoryTokenVerifier(configured, public))
    headers = {
        "authorization": f"Bearer {token(private, configured)}",
        "accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25",
    }
    with TestClient(app, base_url=configured.public_url.removesuffix("/mcp"), headers=headers) as client:

        def call() -> Any:
            return client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "memory_search",
                        "arguments": {"project_id": str(grant.project_ids[0])},
                    },
                },
            ).json()["result"]

        with ThreadPoolExecutor(max_workers=2) as executor:
            running = executor.submit(call)
            try:
                assert started.wait(timeout=2)
                queued = executor.submit(call).result(timeout=5)
                assert queued["isError"] is True
                assert queued["content"][0]["text"] == "deadline_exceeded"
                assert not running.done(), "cancellation must wait for the running owner receipt"
                assert owner.retrieve_memory.call_count == 1
            finally:
                release.set()
                running.result(timeout=3)
    phases = [call.args[5]["phase"] for call in owner._event.call_args_list]
    assert phases == ["started", "finished"]
