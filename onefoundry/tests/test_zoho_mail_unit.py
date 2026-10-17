"""Hosted MCP, write intent, private diagnostics and credential boundary tests."""

import json
import logging
from typing import Any, cast
from uuid import uuid4

import anyio
import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from onefoundry import zoho_mail_mcp
from onefoundry.zoho_mail_api import make_zoho_mail_router
from onefoundry.zoho_mail_contracts import ZohoCredentials, ZohoRead, ZohoWrite, endpoint
from onefoundry.zoho_mail_dispatch import write_arguments
from onefoundry.zoho_mail_mcp import HostedZohoClient, ZohoMcpError, provider_data
from onefoundry.zoho_mail_store import ZohoMailStore


def private_url() -> str:
    return f"https://mail-fixture.zohomcp.com/mcp/{uuid4().hex}/message"


@pytest.mark.parametrize(
    "url",
    [
        "http://mail-fixture.zohomcp.com/mcp/" + "0" * 32 + "/message",
        "https://127.0.0.1/mcp/" + "0" * 32 + "/message",
        "https://mail-fixture.zohomcp.com.evil.test/mcp/" + "0" * 32 + "/message",
        "https://user@mail-fixture.zohomcp.com/mcp/" + "0" * 32 + "/message",
        "https://mail-fixture.zohomcp.com:443/mcp/" + "0" * 32 + "/message",
        "https://mail-fixture.zohomcp.com/mcp/" + "0" * 32 + "/message?next=elsewhere",
    ],
)
def test_private_endpoint_rejects_foreign_hosts_and_url_ambiguity(url: str) -> None:
    with pytest.raises(ValueError):
        endpoint(url)


def test_credentials_repr_is_private_and_invalid_input_response_does_not_echo() -> None:
    url = private_url()
    data = ZohoCredentials(
        expected_version=1, server_url=SecretStr(url), access_token=SecretStr("synthetic-access")
    )
    assert endpoint(url) == url and url not in repr(data) and "synthetic-access" not in repr(data)
    with pytest.raises(ValidationError):
        ZohoCredentials(expected_version=1, server_url=SecretStr(url), access_token=SecretStr("bad token"))
    app = FastAPI()
    app.include_router(make_zoho_mail_router(cast(ZohoMailStore, object())))
    with TestClient(app) as client:
        path = f"/api/projects/{uuid4()}/mail/zoho/connections/{uuid4()}/credentials"
        response = client.post(
            path,
            json={
                "expected_version": 1,
                "server_url": url,
                "access_token": "bad token",
                "unexpected": "synthetic-secret",
            },
        )
        assert response.status_code == 422
        assert (
            url not in response.text
            and "synthetic-secret" not in response.text
            and "bad token" not in response.text
        )
        assert client.post(path, content=b"x" * 8193).status_code == 413


@pytest.mark.parametrize("operation", ["draft", "send", "reply_draft", "reply_send"])
def test_draft_never_degrades_to_send_and_replies_are_explicit(operation: str) -> None:
    raw = {
        "connection_id": str(uuid4()),
        "operation": operation,
        "to_address": "buyer@example.test",
        "subject": "Hello",
        "body": "A draft",
        "message_id": "123" if operation.startswith("reply_") else None,
    }
    data = ZohoWrite.model_validate(raw)
    tool, arguments = write_arguments(
        {
            "message": data.model_dump(mode="json"),
            "from_address": "owner@example.test",
            "mail_account_id": "321",
        }
    )
    assert arguments["body"]["isSchedule"] is False
    assert arguments["body"].get("mode") == ("draft" if "draft" in operation else None)
    assert tool == ("sendReplyEmail" if operation.startswith("reply_") else "sendEmail")
    assert arguments["path_variables"]["accountId"] == "321"


def test_invalid_mail_arguments_and_provider_failure_are_not_success() -> None:
    with pytest.raises(ValidationError):
        ZohoRead(connection_id=uuid4(), operation="content")
    with pytest.raises(ValidationError):
        ZohoRead.model_validate({"connection_id": uuid4(), "operation": "send", "accountId": "other"})
    with pytest.raises(ValidationError):
        ZohoWrite(
            connection_id=uuid4(),
            operation="send",
            to_address="buyer@example.test",
            subject="Hello\nBcc: another@example.test",
            body="Hello",
        )
    for result in [{}, {"status": "failure"}, {"data": {"status": {"code": 401}, "data": []}}]:
        with pytest.raises(ZohoMcpError):
            provider_data(cast(dict[str, Any], result))
    assert provider_data({"status": {"code": 200}, "data": []}) == []


def test_sdk_discovery_and_call_hide_credentials_and_allow_only_known_tools(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    url, token, requests = private_url(), "synthetic-oauth-value", []

    def respond(request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) == url and request.headers["Authorization"] == f"Bearer {token}"
        body = json.loads(request.content)
        requests.append(body)
        if "id" not in body:
            return httpx2.Response(202)
        if body["method"] == "initialize":
            result: Any = {
                "protocolVersion": body["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fixture", "version": "1"},
            }
        elif body["method"] == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "ZohoMail_sendEmail",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "body": {
                                    "type": "object",
                                    "properties": {"mode": {"enum": ["draft", "template"]}},
                                }
                            },
                        },
                    }
                ]
            }
        else:
            assert body["method"] == "tools/call"
            result = {
                "content": [],
                "structuredContent": {
                    "status": "success",
                    "data": {"status": {"code": 200}, "data": {"messageId": "123"}},
                },
            }
        return httpx2.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    monkeypatch.setattr(
        zoho_mail_mcp,
        "create_mcp_http_client",
        lambda **kwargs: httpx2.AsyncClient(
            transport=httpx2.MockTransport(respond), headers=kwargs["headers"], timeout=kwargs["timeout"]
        ),
    )
    caplog.set_level(logging.DEBUG)
    client = HostedZohoClient(url, token, 2, 16384)
    receipt = client.call("sendEmail", {"body": {"mode": "draft"}})
    assert provider_data(receipt) == {"messageId": "123"}
    calls = [request for request in requests if request["method"] == "tools/call"]
    assert len(calls) == 1 and calls[0]["params"]["arguments"]["body"]["mode"] == "draft"
    assert url not in caplog.text and token not in caplog.text
    with pytest.raises(ZohoMcpError):
        client.call("deleteEmail", {})
    assert len([request for request in requests if request["method"] == "tools/call"]) == 1


def test_sdk_timeout_uses_safe_error_and_no_application_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = []

    async def wait(request: httpx2.Request) -> httpx2.Response:
        attempts.append(request)
        await anyio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(
        zoho_mail_mcp,
        "create_mcp_http_client",
        lambda **kwargs: httpx2.AsyncClient(transport=httpx2.MockTransport(wait), headers=kwargs["headers"]),
    )
    with pytest.raises(ZohoMcpError, match="request_timeout"):
        HostedZohoClient(private_url(), "synthetic-value", 0.02, 16384).call("getMailAccounts", {})
    assert len(attempts) == 1


def test_authentication_and_redirect_failures_do_not_leak_private_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for status, expected in [(401, "authentication_failed"), (307, "redirect_rejected")]:
        url = private_url()
        monkeypatch.setattr(
            zoho_mail_mcp,
            "create_mcp_http_client",
            lambda status=status, **kwargs: httpx2.AsyncClient(
                transport=httpx2.MockTransport(lambda request: httpx2.Response(status)),
                headers=kwargs["headers"],
            ),
        )
        with pytest.raises(ZohoMcpError) as failed:
            HostedZohoClient(url, "synthetic-token", 2, 16384).call("getMailAccounts", {})
        assert failed.value.code == expected and url not in str(failed.value)
