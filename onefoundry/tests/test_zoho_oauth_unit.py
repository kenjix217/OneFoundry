"""Provider discovery, PKCE, boundaries, secret diagnostics and settings contracts."""

import json
import logging
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from onefoundry.settings import Settings
from onefoundry.zoho_mail_api import _HideOAuthCallback, make_zoho_mail_router
from onefoundry.zoho_mail_oauth import BoundedOAuthHTTP, ZohoOAuth, ZohoOAuthError

SERVER = "https://mail-fixture.zohomcp.com/mcp/" + "0" * 32 + "/message"
AUTHORITY = "https://mcp.zoho.com"


class OAuthFixture:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.reject = False
        self.pkce = True
        self.foreign = False
        self.foreign_resource = False

    def __call__(
        self, method: str, url: str, *, body: dict[str, Any] | None = None, form: bool = False
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        self.calls.append((method, url, body))
        if url == SERVER:
            origin = "https://foreign.test" if self.foreign_resource else "https://mail-fixture.zohomcp.com"
            return (
                401,
                {},
                {"www-authenticate": f'Bearer resource_metadata="{origin}/resource"'},
            )
        if url.endswith("/resource"):
            return 200, {"resource": SERVER, "authorization_servers": [AUTHORITY]}, {}
        if ".well-known" in url:
            return (
                200,
                {
                    "issuer": AUTHORITY,
                    "authorization_endpoint": AUTHORITY + "/authorize",
                    "token_endpoint": ("https://evil.test" if self.foreign else AUTHORITY) + "/token",
                    "registration_endpoint": AUTHORITY + "/register",
                    "scopes_supported": ["mail.read", "mail.create"],
                    "code_challenge_methods_supported": ["S256"] if self.pkce else ["plain"],
                    "token_endpoint_auth_methods_supported": ["none"],
                    "grant_types_supported": ["authorization_code", "refresh_token"],
                },
                {},
            )
        if url.endswith("/register"):
            assert body is not None
            return (
                201,
                {
                    "client_id": "fixture-client",
                    "redirect_uris": body["redirect_uris"],
                    "token_endpoint_auth_method": "none",
                },
                {},
            )
        if url.endswith("/token"):
            if self.reject:
                raise ZohoOAuthError("oauth_request_rejected")
            return (
                200,
                {
                    "access_token": "synthetic-access",
                    "refresh_token": "synthetic-refresh",
                    "token_type": "Bearer",
                    "expires_in": 60,
                },
                {},
            )
        raise AssertionError("Unexpected fixture request")


def oauth_fixture() -> tuple[ZohoOAuth, OAuthFixture]:
    fixture = OAuthFixture()
    return ZohoOAuth(fixture, ("mcp.zoho.com",)), fixture


def test_discovery_pins_resource_and_issuer_and_requires_pkce() -> None:
    oauth, fixture = oauth_fixture()
    fixture.foreign_resource = True
    with pytest.raises(ZohoOAuthError, match="oauth_resource_rejected"):
        oauth.discover(SERVER)
    fixture.foreign_resource = False
    metadata = oauth.discover(SERVER)
    assert metadata["resource"] == SERVER and metadata["refresh_supported"] is True
    client = oauth.register(SERVER, metadata, "http://127.0.0.1:8765/callback")
    pending = oauth.pending(metadata, client, 1000)
    params = parse_qs(urlsplit(pending["authorize_url"]).query)
    assert params["code_challenge_method"] == ["S256"] and params["resource"] == [SERVER]
    assert pending["verifier"] not in pending["authorize_url"] and len(pending["state"]) >= 40
    fixture.pkce = False
    with pytest.raises(ZohoOAuthError, match="oauth_pkce_required"):
        oauth.discover(SERVER)
    fixture.pkce = True
    fixture.foreign = True
    with pytest.raises(ZohoOAuthError, match="oauth_authority_rejected"):
        oauth.discover(SERVER)


def test_http_plain_401_challenge_and_private_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    real_client = httpx.Client

    def handle(request: httpx.Request) -> httpx.Response:
        logging.getLogger("httpcore.http11").debug("synthetic-private-url %s", SERVER)
        return httpx.Response(
            401,
            text="Authorization required",
            headers={"www-authenticate": 'Bearer resource_metadata="synthetic"'},
        )

    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs)
    )
    with caplog.at_level(logging.DEBUG):
        status, body, headers = BoundedOAuthHTTP(2, 4096)("POST", SERVER, body={"method": "initialize"})
    assert status == 401 and body == {} and "www-authenticate" in headers
    assert SERVER not in caplog.text and "synthetic-private-url" not in caplog.text
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        "%s",
        ("GET /api/projects/test/mail/zoho/connections/test/oauth/callback?code=synthetic",),
        None,
    )
    assert not _HideOAuthCallback().filter(record)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"Location": "https://evil.test"}),
        httpx.Response(200, content=b"x" * 4097),
    ],
)
def test_http_rejects_redirects_and_oversize(
    monkeypatch: pytest.MonkeyPatch, response: httpx.Response
) -> None:
    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(lambda _: response), **kwargs),
    )
    with pytest.raises(ZohoOAuthError):
        BoundedOAuthHTTP(2, 4096)("GET", AUTHORITY + "/resource")


def test_oauth_settings_defaults_invalid_values_and_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ZOHO_OAUTH_PENDING_TIMEOUT_SECONDS", "ZOHO_OAUTH_AUTHORITY_HOSTS"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(db_password="test-only")
    assert settings.zoho_oauth_pending_timeout_seconds == 900
    assert settings.zoho_oauth_authority_hosts == ("mcp.zoho.com", "accounts.zoho.com")
    monkeypatch.setenv("ZOHO_OAUTH_PENDING_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("ZOHO_OAUTH_AUTHORITY_HOSTS", json.dumps(["mcp.zoho.com"]))
    assert Settings(db_password="test-only").zoho_oauth_pending_timeout_seconds == 120
    assert Settings(db_password="test-only").zoho_oauth_authority_hosts == ("mcp.zoho.com",)
    explicit = Settings(
        db_password="test-only",
        zoho_oauth_pending_timeout_seconds=300,
        zoho_oauth_authority_hosts=("accounts.zoho.com",),
    )
    assert explicit.zoho_oauth_pending_timeout_seconds == 300 and explicit.zoho_oauth_authority_hosts == (
        "accounts.zoho.com",
    )
    for bad in ("0", "1801", "invalid"):
        monkeypatch.setenv("ZOHO_OAUTH_PENDING_TIMEOUT_SECONDS", bad)
        with pytest.raises(ValidationError):
            Settings(db_password="test-only")

    monkeypatch.setenv("ZOHO_OAUTH_PENDING_TIMEOUT_SECONDS", "120")
    for hosts in ([], ["127.0.0.1"], ["mcp.zoho.com", "mcp.zoho.com"], ["mcp.zoho.com.evil.test"]):
        monkeypatch.setenv("ZOHO_OAUTH_AUTHORITY_HOSTS", json.dumps(hosts))
        with pytest.raises(ValidationError):
            Settings(db_password="test-only")


def test_callback_cleans_private_query_and_marks_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class Callback:
        fails = False

        def callback(self, *args: Any) -> dict[str, str]:
            if self.fails:
                raise ZohoOAuthError("oauth_request_rejected")
            return {"status": "ready"}

    owner = Callback()
    monkeypatch.setattr("onefoundry.zoho_mail_api.ZohoSignInStore", lambda _: owner)
    app = FastAPI()
    app.include_router(make_zoho_mail_router(object()))  # type: ignore[arg-type]
    project, cid = "00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002"
    path = f"/api/projects/{project}/mail/zoho/connections/{cid}/oauth/callback"
    with TestClient(app) as client:
        reply = client.get(
            path, params={"code": "synthetic-private", "state": "private-state"}, follow_redirects=False
        )
        assert reply.status_code == 303
        assert reply.headers["location"] == f"/#project/{project}/connections?zoho=ready"
        assert reply.headers["cache-control"] == "no-store"
        assert reply.headers["referrer-policy"] == "no-referrer"
        assert "synthetic-private" not in str(reply.headers) + reply.text
        owner.fails = True
        failed = client.get(path, params={"code": "synthetic-private"}, follow_redirects=False)
        assert failed.headers["location"].endswith("?zoho=failed")


@pytest.mark.parametrize(
    "payload",
    [
        {"access_token": "bad\nheader", "token_type": "Bearer"},
        {"access_token": "synthetic", "token_type": "Basic"},
        {"access_token": "synthetic", "token_type": "Bearer", "expires_in": 0},
        {"access_token": "synthetic", "token_type": "Bearer", "scope": "unexpected.write"},
    ],
)
def test_token_contract_rejects_invalid_provider_response(payload: dict[str, Any]) -> None:
    def http(*args: Any, **kwargs: Any) -> tuple[int, dict[str, Any], dict[str, str]]:
        return 200, payload, {}

    oauth, _ = oauth_fixture()
    metadata = oauth.discover(SERVER)
    with pytest.raises(ZohoOAuthError):
        ZohoOAuth(http, ("mcp.zoho.com",)).tokens(
            metadata, {"client_id": "fixture"}, {"grant_type": "refresh_token"}
        )
