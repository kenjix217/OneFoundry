"""Real MCP transport and synthetic TLS mail flows, including denials and receipts."""

import json
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from typing import Any
from unittest.mock import MagicMock

import anyio
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.mail_imap import MailConfig, MailError
from onefoundry.mail_plugin_auth import MailTokenVerifier
from onefoundry.mail_plugin_contracts import MessageIds
from onefoundry.mail_plugin_imap import SearchMailbox, SearchResult
from onefoundry.mail_plugin_service import create_mail_plugin_app
from onefoundry.mail_plugin_settings import MailPluginSettings
from tests.fake_imap import message
from tests.fake_mail_plugin_imap import PluginMailbox, serve
from tests.mail_plugin_fixtures import keypair, mock_gateway, policy, token


def rpc(client: TestClient, method: str, params: Any = None, *, authorized: bool = True) -> Any:
    body = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    headers = {} if authorized else {"authorization": "Bearer not-an-access-token"}
    return client.post("/mcp", json=body, headers=headers)


def test_tunnel_transport_keeps_metadata_local_and_requires_external_token_audience() -> None:
    configured = policy(
        public_url="http://127.0.0.1:9876/mcp",
        oauth_audience="https://tunnel.example.test/v1/mcp/synthetic",
        oauth_auxiliary_audiences=("https://auth.example.test/userinfo",),
    )
    config = MailConfig("127.0.0.1", 993, configured.username, "synthetic-password", "INBOX", 3, 4096)
    factory = MagicMock()
    factory.return_value.search.return_value = SearchResult((), 1, 0, None, False)
    gateway, _, events = mock_gateway(configured, config, factory=factory)
    private, public = keypair()
    app = create_mail_plugin_app(configured, gateway, MailTokenVerifier(configured, public))
    bearer = token(
        private,
        configured,
        aud=[configured.oauth_audience, *configured.oauth_auxiliary_audiences],
        scope="openid email profile mail.read",
    )
    headers = {
        "authorization": f"Bearer {bearer}",
        "accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25",
    }
    with TestClient(app, base_url="http://127.0.0.1:9876", headers=headers) as client:
        metadata = client.get("/.well-known/oauth-protected-resource/mcp").json()
        assert metadata["resource"] == configured.public_url
        assert metadata["authorization_servers"] == [configured.oauth_issuer]
        challenge = rpc(client, "tools/list", authorized=False)
        assert challenge.status_code == 401
        assert (
            'resource_metadata="http://127.0.0.1:9876/.well-known/oauth-protected-resource/mcp"'
            in challenge.headers["www-authenticate"]
        )
        assert {item["name"] for item in rpc(client, "tools/list").json()["result"]["tools"]} == {
            "mail_search",
            "mail_read",
        }
        result = rpc(client, "tools/call", {"name": "mail_search", "arguments": {"folder": "INBOX"}})
        assert result.json()["result"]["structuredContent"]["messages"] == []
        denied = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"authorization": f"Bearer {token(private, configured, aud=configured.public_url)}"},
        )
        assert denied.status_code == 401
    assert [item["phase"] for item in events] == ["started", "finished"]


def test_mcp_discovery_search_read_auth_and_input_denials(tmp_path: Path) -> None:
    fixture = PluginMailbox(
        messages={101: message("Synthetic buyer", "buyer@example.test", "Quoted buyer text")}
    )
    with serve(fixture, tmp_path) as (port, context):
        configured = policy(imap_port=port)
        config = MailConfig("127.0.0.1", port, fixture.username, fixture.password, "INBOX", 3, 4096)

        def factory(cfg: MailConfig, pol: MailPluginSettings, ids: MessageIds) -> SearchMailbox:
            return SearchMailbox(cfg, pol, ids, context=context)

        gateway, store, events = mock_gateway(configured, config, factory=factory)
        store.read_only_config.side_effect = lambda *args, **kwargs: replace(config, folder=kwargs["folder"])
        private, public = keypair()
        app = create_mail_plugin_app(configured, gateway, MailTokenVerifier(configured, public))
        headers = {
            "authorization": f"Bearer {token(private, configured)}",
            "accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
        }
        with TestClient(app, base_url="https://mail-plugin.example.test", headers=headers) as client:
            assert rpc(client, "tools/list", authorized=False).status_code == 401
            assert (
                rpc(
                    client,
                    "initialize",
                    {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "synthetic-test", "version": "1"},
                    },
                ).status_code
                == 200
            )
            listing = rpc(client, "tools/list").json()["result"]["tools"]
            assert {tool["name"] for tool in listing} == {"mail_search", "mail_read"}
            for tool in listing:
                assert tool["annotations"]["readOnlyHint"] is True
                assert tool["inputSchema"]["additionalProperties"] is False
                assert tool["_meta"]["securitySchemes"] == [{"type": "oauth2", "scopes": ["mail.read"]}]
            search = rpc(
                client, "tools/call", {"name": "mail_search", "arguments": {"folder": "INBOX"}}
            ).json()
            identifier = search["result"]["structuredContent"]["messages"][0]["message_id"]
            read = rpc(
                client, "tools/call", {"name": "mail_read", "arguments": {"message_id": identifier}}
            ).json()
            assert read["result"]["structuredContent"]["text"] == "Quoted buyer text"
            assert json.loads(read["result"]["content"][0]["text"]) == read["result"]["structuredContent"]
            for args, expected in [
                ({"folder": "Trash"}, "folder_denied"),
                ({"folder": "INBOX", "password": "synthetic-rejected-secret"}, "invalid_input"),
                ({"folder": "INBOX", "limit": True}, "invalid_input"),
                ({"folder": "INBOX", "text": "x\r\nSTORE 1"}, "invalid_input"),
            ]:
                denied = rpc(client, "tools/call", {"name": "mail_search", "arguments": args}).json()[
                    "result"
                ]
                assert denied["isError"] and denied["content"][0]["text"] == expected
            write = rpc(client, "tools/call", {"name": "mail_send", "arguments": {}}).json()["result"]
            assert write["isError"] and write["content"][0]["text"] == "unsupported_tool"
            assert client.post("/mcp", headers={"host": "evil.example.test"}, json={}).status_code == 421
            assert (
                client.post("/mcp", headers={"origin": "https://evil.example.test"}, json={}).status_code
                == 403
            )
            assert client.post("/mcp", content=b"x" * (configured.max_request_bytes + 1)).status_code == 413
            metadata = client.get("/.well-known/oauth-protected-resource/mcp").json()
            assert metadata["resource"] == configured.public_url
            assert metadata["authorization_servers"] == [configured.oauth_issuer]
    receipts = json.dumps(events)
    assert len(events) == 12 and "finished" in receipts
    for excluded in [
        fixture.password,
        "Quoted buyer text",
        "Synthetic buyer",
        "buyer@example.test",
        identifier,
        "synthetic-rejected-secret",
        "STORE 1",
    ]:
        assert excluded not in receipts
    assert not any(" SELECT " in command or " STORE " in command for command in fixture.commands)


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (NotFound("synthetic owner failure"), "account_unavailable"),
        (Conflict("synthetic owner failure"), "account_binding_denied"),
        (Unavailable("synthetic owner failure"), "credential_unavailable"),
        (SQLAlchemyError("synthetic owner failure"), "database_unavailable"),
    ],
)
def test_owner_failure_is_distinct_and_audited(failure: Exception, code: str) -> None:
    configured = policy()
    config = MailConfig("127.0.0.1", 993, configured.username, "synthetic-password", "INBOX", 3, 4096)
    factory = MagicMock()
    gateway, store, events = mock_gateway(configured, config, factory=factory)
    store.read_only_config.side_effect = failure
    with pytest.raises(MailError, match=code):
        gateway.search({"folder": "INBOX"})
    factory.assert_not_called()
    assert events[-1]["outcome"] == "failed" and events[-1]["code"] == code


def test_audit_failure_prevents_read_and_result_release() -> None:
    configured = policy()
    config = MailConfig("127.0.0.1", 993, configured.username, "synthetic-password", "INBOX", 3, 4096)
    factory = MagicMock()
    gateway, store, _ = mock_gateway(configured, config, factory=factory)
    gateway.gateway._event.side_effect = SQLAlchemyError("synthetic audit unavailable")  # type: ignore[attr-defined]
    with pytest.raises(MailError, match="audit_unavailable"):
        gateway.search({"folder": "INBOX"})
    store.read_only_config.assert_not_called()
    factory.assert_not_called()


def test_concurrent_calls_are_denied_and_rate_limit_uses_injected_clock() -> None:
    configured = policy(max_calls_per_minute=1)
    config = MailConfig("127.0.0.1", 993, configured.username, "synthetic-password", "INBOX", 3, 4096)
    now = [0.0]
    entered, released = Event(), Event()
    failures: list[str] = []

    def blocked(*args: Any, **kwargs: Any) -> MailConfig:
        entered.set()
        assert released.wait(5)
        raise NotFound("synthetic failure")

    gateway, store, events = mock_gateway(configured, config, clock=lambda: now[0])
    store.read_only_config.side_effect = blocked

    def first_call() -> None:
        try:
            gateway.search({"folder": "INBOX"})
        except MailError as failure:
            failures.append(failure.code)

    thread = Thread(target=first_call)
    thread.start()
    try:
        assert entered.wait(5)
        with pytest.raises(MailError, match="busy"):
            gateway.search({"folder": "INBOX"})
    finally:
        released.set()
        thread.join(5)
    assert failures == ["account_unavailable"] and not thread.is_alive()
    with pytest.raises(MailError, match="rate_limited"):
        gateway.search({"folder": "INBOX"})
    now[0] = 60.0
    with pytest.raises(MailError, match="account_unavailable"):
        gateway.search({"folder": "INBOX"})
    assert any(event.get("code") == "busy" for event in events)
    assert any(event.get("code") == "rate_limited" for event in events)


def test_cancellation_closes_worker_and_records_interrupted_outcome() -> None:
    configured = policy()
    config = MailConfig("127.0.0.1", 993, configured.username, "synthetic-password", "INBOX", 3, 4096)
    entered = Event()
    release = Event()
    closed = Event()

    def interruptible(*args: Any, **kwargs: Any) -> MailConfig:
        entered.set()
        try:
            assert release.wait(5)
            anyio.from_thread.check_cancelled()
            raise AssertionError("worker should have been cancelled")
        finally:
            closed.set()

    gateway, store, events = mock_gateway(configured, config)
    store.read_only_config.side_effect = interruptible

    async def check() -> None:
        async with anyio.create_task_group() as group:
            group.start_soon(anyio.to_thread.run_sync, gateway.search, {"folder": "INBOX"})
            assert await anyio.to_thread.run_sync(entered.wait, 5)
            group.cancel_scope.cancel()
            release.set()

    anyio.run(check)
    assert closed.is_set() and events[-1]["code"] == "interrupted"
