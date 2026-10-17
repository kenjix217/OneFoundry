"""Microsoft and Google OAuth flows and read-only mail APIs against a scripted transport."""

import base64
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from pydantic import ValidationError

from onefoundry import connector_oauth
from onefoundry.connector_oauth import OAuthError
from onefoundry.connector_schemas import ConnectorCreate, SignOutEverywhere
from onefoundry.mail_api_sources import GRAPH, GmailMailbox, GraphMailbox
from onefoundry.mail_imap import MailError, parse_message
from tests.fake_imap import message
from tests.fake_provider_http import FakeHTTP

MS = "https://login.microsoftonline.com/common/oauth2/v2.0"


def test_signout_permission_is_explicit_microsoft_only_and_confirmation_is_required() -> None:
    assert "User.RevokeSessions.All" not in connector_oauth.scopes("microsoft", ["mail"])
    assert "User.RevokeSessions.All" in connector_oauth.scopes("microsoft", ["sign_out_everywhere"])
    for provider, tenant in [("google", "common"), ("microsoft", "consumers")]:
        with pytest.raises(ValidationError):
            ConnectorCreate.model_validate(
                {
                    "provider": provider,
                    "tenant": tenant,
                    "label": "Test",
                    "client_id": "client-test",
                    "features": ["sign_out_everywhere"],
                }
            )
    with pytest.raises(ValidationError):
        SignOutEverywhere.model_validate(
            {"request_id": uuid4(), "expected_version": 2, "confirmation": "yes"}
        )


def test_microsoft_device_flow_pending_then_tokens_and_declined() -> None:
    http = FakeHTTP()
    http.on(
        "POST",
        f"{MS}/devicecode",
        (
            200,
            {
                "device_code": "dev-1",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://microsoft.com/devicelogin",
                "interval": 5,
                "expires_in": 900,
            },
        ),
    )
    scopes = connector_oauth.scopes("microsoft", ["mail", "files"])
    assert scopes == ["offline_access", "User.Read", "Mail.Read", "Files.Read.All"]
    start = connector_oauth.microsoft_start("client-id-123", "common", scopes, http=http)
    assert start.user_code == "ABCD-EFGH" and start.interval == 5
    assert http.calls[0][2]["scope"] == [" ".join(scopes)]
    http.on("POST", f"{MS}/token", (400, {"error": "authorization_pending"}))
    assert connector_oauth.microsoft_poll("client-id-123", "common", "dev-1", http=http) is None
    http.on(
        "POST",
        f"{MS}/token",
        (200, {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600, "scope": "Mail.Read"}),
    )
    tokens = connector_oauth.microsoft_poll("client-id-123", "common", "dev-1", http=http)
    assert tokens is not None and tokens["refresh_token"] == "rt-1"
    http.on("POST", f"{MS}/token", (400, {"error": "authorization_declined"}))
    with pytest.raises(OAuthError) as declined:
        connector_oauth.microsoft_poll("client-id-123", "common", "dev-1", http=http)
    assert declined.value.code == "sign_in_declined"
    with pytest.raises(OAuthError):
        connector_oauth.microsoft_start("client-id-123", "bad/tenant", scopes, http=http)


def test_google_pkce_exchange_refresh_and_failures() -> None:
    verifier, challenge = connector_oauth.pkce_pair()
    url = connector_oauth.google_authorize_url(
        "client.apps.googleusercontent.com", "http://127.0.0.1:8765/", ["openid"], "acct.state", challenge
    )
    query = parse_qs(urlsplit(url).query)
    assert query["code_challenge_method"] == ["S256"] and query["redirect_uri"] == ["http://127.0.0.1:8765/"]
    assert query["access_type"] == ["offline"] and verifier not in url
    http = FakeHTTP()
    http.on(
        "POST",
        connector_oauth.GOOGLE_TOKEN,
        (200, {"access_token": "g-at", "refresh_token": "g-rt", "expires_in": 3599}),
    )
    tokens = connector_oauth.google_exchange(
        "cid", "secret", "code-1", verifier, "http://127.0.0.1:8765/", http=http
    )
    assert tokens["refresh_token"] == "g-rt"
    assert http.calls[-1][2]["code_verifier"] == [verifier]
    http.on("POST", connector_oauth.GOOGLE_TOKEN, (200, {"access_token": "g-at2", "expires_in": 3599}))
    refreshed = connector_oauth.refresh("google", "cid", "common", "secret", "g-rt", http=http)
    assert refreshed["access_token"] == "g-at2" and refreshed["refresh_token"] == "g-rt"
    http.on("POST", connector_oauth.GOOGLE_TOKEN, (400, {"error": "invalid_grant"}))
    with pytest.raises(OAuthError) as rejected:
        connector_oauth.refresh("google", "cid", "common", "secret", "g-rt", http=http)
    assert rejected.value.code == "refresh_rejected"
    with pytest.raises(OAuthError) as no_refresh:
        http.on("POST", connector_oauth.GOOGLE_TOKEN, (200, {"access_token": "x"}))
        connector_oauth.google_exchange(
            "cid", "secret", "code", verifier, "http://127.0.0.1:8765/", http=http
        )
    assert no_refresh.value.code == "no_refresh_token"


def _raw(subject: str) -> str:
    return base64.urlsafe_b64encode(message(subject, "buyer@acme.example", "Hello")).decode().rstrip("=")


def test_gmail_history_fetch_bounds_and_errors() -> None:
    http = FakeHTTP()
    gmail = "https://gmail.googleapis.com/gmail/v1/users/me"
    http.on("GET", f"{gmail}/profile", (200, {"historyId": "100"}))
    http.on(
        "GET",
        f"{gmail}/history?startHistoryId=100",
        (
            200,
            {
                "historyId": "105",
                "history": [{"messagesAdded": [{"message": {"id": "m1"}}, {"message": {"id": "m2"}}]}],
            },
        ),
    )
    http.on("GET", f"{gmail}/messages/m1", (200, {"raw": _raw("First")}))
    http.on("GET", f"{gmail}/messages/m2", (200, {"raw": _raw("Second")}))
    box = GmailMailbox(lambda: "token-1", http, 5, 65536)
    assert box.baseline() == "100"
    batch = box.fetch("100", 10)
    assert batch.cursor == "105" and batch.skipped == 0
    assert [parse_message(item.raw).subject for item in batch.messages] == ["First", "Second"]
    assert all(call[3] == "Bearer token-1" for call in http.calls)
    burst = box.fetch("100", 1)
    assert [item.message_id for item in burst.messages] == ["m2"] and burst.skipped == 1
    http.on("GET", f"{gmail}/history?startHistoryId=7", (404, {}))
    with pytest.raises(MailError) as expired:
        box.fetch("7", 10)
    assert expired.value.code == "cursor_expired"
    http.on("GET", f"{gmail}/profile", (401, {}))
    with pytest.raises(MailError) as denied:
        box.baseline()
    assert denied.value.code == "authentication_failed"


def test_graph_delta_pages_skip_removed_and_reject_foreign_cursors() -> None:
    http = FakeHTTP()
    delta = f"{GRAPH}/me/mailFolders/inbox/messages/delta?$deltatoken=t1"
    http.on(
        "GET",
        f"{GRAPH}/me/mailFolders/inbox/messages/delta?$select=id&$deltatoken=latest",
        (200, {"value": [], "@odata.deltaLink": delta}),
    )
    http.on(
        "GET",
        delta,
        (
            200,
            {
                "value": [{"id": "a"}, {"id": "gone", "@removed": {"reason": "deleted"}}],
                "@odata.nextLink": f"{GRAPH}/next-page",
            },
        ),
    )
    http.on(
        "GET", f"{GRAPH}/next-page", (200, {"value": [{"id": "b"}], "@odata.deltaLink": f"{GRAPH}/delta-2"})
    )
    http.on("GET", f"{GRAPH}/me/messages/a/$value", (200, message("Alpha", "x@acme.example", "A")))
    http.on("GET", f"{GRAPH}/me/messages/b/$value", (200, message("Beta", "x@acme.example", "B")))
    box = GraphMailbox(lambda: "ms-token", http, 5, 65536)
    assert box.baseline() == delta
    batch = box.fetch(delta, 10)
    assert [parse_message(item.raw).subject for item in batch.messages] == ["Alpha", "Beta"]
    assert batch.cursor == f"{GRAPH}/delta-2"
    limited = box.fetch(delta, 1)
    assert [item.message_id for item in limited.messages] == ["a"] and limited.cursor == f"{GRAPH}/next-page"
    with pytest.raises(MailError) as foreign:
        box.fetch("https://attacker.example/delta", 10)
    assert foreign.value.code == "cursor_expired"


def test_graph_baseline_walks_pages_when_latest_token_is_ignored() -> None:
    http = FakeHTTP()
    first = f"{GRAPH}/me/mailFolders/inbox/messages/delta?$select=id&$deltatoken=latest"
    http.on("GET", first, (200, {"value": [{"id": "old-1"}], "@odata.nextLink": f"{GRAPH}/page-2"}))
    http.on(
        "GET",
        f"{GRAPH}/page-2",
        (200, {"value": [{"id": "old-2"}], "@odata.deltaLink": f"{GRAPH}/delta-now"}),
    )
    box = GraphMailbox(lambda: "ms-token", http, 5, 65536)
    assert box.baseline() == f"{GRAPH}/delta-now"
    # Walking the pages never downloads the existing messages.
    assert not any("/$value" in call[1] for call in http.calls)
    http.on("GET", first, (200, {"value": [], "@odata.nextLink": "https://attacker.example/page"}))
    with pytest.raises(MailError) as foreign:
        box.baseline()
    assert foreign.value.code == "folder_unavailable"
