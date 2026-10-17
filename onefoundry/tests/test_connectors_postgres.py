"""Connected Microsoft and Google accounts and an API mailbox, with scripted provider responses."""

import base64
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, current_thread
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from onefoundry.connector_schemas import ConnectorCreate, SignOutEverywhere
from onefoundry.connector_signout import sign_out
from onefoundry.connector_store import ConnectorStore
from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.inference.contracts import TransportError
from onefoundry.mail_api_sources import GRAPH
from onefoundry.mail_store import MailStore
from onefoundry.main import create_app
from onefoundry.settings import Settings
from onefoundry.tool_gateway import ToolGateway
from tests.fake_imap import message
from tests.fake_provider_http import FakeHTTP

ORIGIN = {"origin": "http://localhost:8765"}
MS = "https://login.microsoftonline.com/common/oauth2/v2.0"


@pytest.mark.parametrize(
    ("response", "expected", "delayed_duplicate"),
    [
        ((200, {"value": True}), "completed", False),
        ((403, {}), "failed", False),
        ((503, {}), "unknown", False),
        ((200, {}), "unknown", False),
        (None, "unknown", False),
        ((200, {"value": True}), "completed", True),
    ],
)
def test_explicit_microsoft_signout_is_bound_and_never_replayed(
    tmp_path: Path, response: Any, expected: str, delayed_duplicate: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "secrets"
    root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(root),
    )
    engine = create_engine(settings.database_url)
    gateway = ToolGateway(engine, settings.operator_actor_id, live_text_settings=settings)
    http = FakeHTTP()
    store = ConnectorStore(gateway, settings, http=http)
    store.prepare_secrets()
    try:
        with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
            pid = UUID(_post(client, "/api/projects", {"name": f"Signout {uuid4()}"})["id"])
            other = UUID(_post(client, "/api/projects", {"name": f"Other {uuid4()}"})["id"])
            created = store.create(
                pid,
                ConnectorCreate(
                    provider="microsoft",
                    label="Work Microsoft",
                    client_id="test-client",
                    features=["mail", "sign_out_everywhere"],
                ),
            )
            aid = created["id"]
            store._connect(
                pid,
                aid,
                store._vault(aid),
                {
                    "access_token": "test-access",
                    "expires_at": int(time.time()) + 3600,
                    "scope": "User.Read User.RevokeSessions.All",
                },
            )
            data = SignOutEverywhere(
                expected_version=2, request_id=uuid4(), confirmation="sign_out_all_apps_and_browsers"
            )
            with pytest.raises(NotFound):
                sign_out(store, other, aid, data)
            with pytest.raises(Conflict):
                sign_out(store, pid, aid, data.model_copy(update={"expected_version": 1}))
            endpoint = GRAPH + "/me/revokeSignInSessions"

            def responder(request: Any) -> tuple[int, object]:
                assert request.data == b""
                with engine.connect() as connection:
                    assert (
                        connection.scalar(
                            text("SELECT state FROM connector_signouts WHERE id=:id"), {"id": data.request_id}
                        )
                        == "dispatching"
                    )
                if response is None:
                    raise TransportError("connection_failed", "test timeout")
                status, body = response
                return int(status), body

            http.on("POST", endpoint, responder)
            if delayed_duplicate:
                paused, released = Event(), Event()
                original_row = store._row

                def delayed_row(*args: Any, **kwargs: Any) -> Any:
                    if current_thread().name.startswith("delayed-duplicate") and not paused.is_set():
                        paused.set()
                        assert released.wait(timeout=10), "primary request did not complete"
                    return original_row(*args, **kwargs)

                monkeypatch.setattr(store, "_row", delayed_row)
                with ThreadPoolExecutor(max_workers=1, thread_name_prefix="delayed-duplicate") as pool:
                    duplicate = pool.submit(sign_out, store, pid, aid, data)
                    assert paused.wait(timeout=10), "duplicate did not reach account lookup"
                    try:
                        first = sign_out(store, pid, aid, data)
                    finally:
                        released.set()
                    results = [first, duplicate.result(timeout=10)]
            else:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(lambda _: sign_out(store, pid, aid, data), range(2)))
            assert all(item["state"] in {"dispatching", expected} for item in results)
            assert sign_out(store, pid, aid, data)["state"] == expected
            assert len(http.calls) == 1
            assert store.get(pid, aid)["status"] == "failed"
            with pytest.raises(Unavailable):
                store.access_token(pid, aid)
            with engine.connect() as connection:
                saved = str(
                    connection.execute(
                        text("SELECT event_type,payload FROM events WHERE project_id=:p"), {"p": pid}
                    ).all()
                )
            assert "test-access" not in saved
            assert "connector_signout_approved" in saved
    finally:
        engine.dispose()


def _post(client: TestClient, path: str, body: dict[str, Any]) -> Any:
    response = client.post(path, json=body, headers=ORIGIN)
    assert response.status_code in {200, 201}, response.text
    return response.json()


def test_microsoft_account_mailbox_refresh_and_google_rules(tmp_path: Path) -> None:
    root = tmp_path / "secrets"
    root.mkdir(mode=0o700)
    workspace = tmp_path / "artifacts"
    workspace.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(root),
        artifact_workspace_root=str(workspace),
        coordination_mode="draft_only",
    )
    engine = create_engine(settings.database_url)
    gateway = ToolGateway(engine, "operator", live_text_settings=settings)
    http = FakeHTTP()
    connectors = ConnectorStore(gateway, settings, http=http)
    mail = MailStore(gateway, settings, connectors=connectors, api_http=http)
    try:
        with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
            project = _post(client, "/api/projects", {"name": f"Connectors {uuid4()}"})
            pid = UUID(project["id"])
            base = f"/api/projects/{pid}"

            no_secret = client.post(
                f"{base}/connectors",
                json={
                    "provider": "google",
                    "label": "Google",
                    "client_id": "client-123.apps",
                    "features": ["mail"],
                },
                headers=ORIGIN,
            )
            assert no_secret.status_code == 409
            account = _post(
                client,
                f"{base}/connectors",
                {
                    "provider": "microsoft",
                    "label": "Work 365",
                    "client_id": "11111111-aaaa",
                    "features": ["mail"],
                },
            )
            assert account["status"] == "pending" and "client_secret" not in account
            aid = UUID(account["id"])

            http.on(
                "POST",
                f"{MS}/devicecode",
                (
                    200,
                    {
                        "device_code": "dev-9",
                        "user_code": "WXYZ-1234",
                        "verification_uri": "https://microsoft.com/devicelogin",
                    },
                ),
            )
            started = connectors.start(pid, aid)
            assert started["kind"] == "device" and started["user_code"] == "WXYZ-1234"
            http.on("POST", f"{MS}/token", (400, {"error": "authorization_pending"}))
            assert connectors.poll(pid, aid)["status"] == "pending"
            http.on(
                "POST",
                f"{MS}/token",
                (
                    200,
                    {
                        "access_token": "at-1",
                        "refresh_token": "rt-1",
                        "expires_in": 3600,
                        "scope": "Mail.Read User.Read",
                    },
                ),
            )
            connected = connectors.poll(pid, aid)
            assert connected["status"] == "connected" and "Mail.Read" in connected["granted_scopes"]
            listed = client.get(f"{base}/connectors").json()
            assert [row["status"] for row in listed] == ["connected"]
            assert "rt-1" not in json.dumps(listed)
            stored = json.loads(base64.urlsafe_b64decode((root / "connectors" / str(aid)).read_text()))
            assert stored["tokens"]["refresh_token"] == "rt-1"

            mailbox = _post(
                client,
                f"{base}/mail/accounts/api",
                {"label": "Work inbox", "protocol": "graph", "connector_account_id": str(aid)},
            )
            assert mailbox["protocol"] == "graph" and mailbox["host"] is None
            wrong = client.post(
                f"{base}/mail/accounts/api",
                json={"label": "Wrong", "protocol": "gmail", "connector_account_id": str(aid)},
                headers=ORIGIN,
            )
            assert wrong.status_code == 409
            delta = f"{GRAPH}/me/mailFolders/inbox/messages/delta?$deltatoken=t1"
            http.on(
                "GET",
                f"{GRAPH}/me/mailFolders/inbox/messages/delta?$select=id",
                (200, {"value": [], "@odata.deltaLink": delta}),
            )
            ready = mail.probe(pid, UUID(mailbox["id"]))
            assert ready["status"] == "ready"
            _post(
                client,
                f"{base}/mail/accounts/{mailbox['id']}/enabled",
                {"enabled": True, "expected_version": ready["version"]},
            )
            http.on("GET", delta, (200, {"value": [{"id": "msg-1"}], "@odata.deltaLink": f"{GRAPH}/delta-2"}))
            http.on(
                "GET",
                f"{GRAPH}/me/messages/msg-1/$value",
                (200, message("Renewal", "buyer@acme.example", "Renew?")),
            )
            assert mail.poll_next()
            with engine.connect() as connection:
                fields = connection.scalar(
                    text("SELECT fields FROM coordination_signals WHERE project_id=:p AND source='email'"),
                    {"p": pid},
                )
                cursor = connection.scalar(
                    text("SELECT cursor FROM mail_accounts WHERE id=:id"), {"id": UUID(mailbox["id"])}
                )
            assert fields["subject"] == "Renewal" and fields["from_domain"] == "acme.example"
            assert cursor == f"{GRAPH}/delta-2"
            assert any(call[3] == "Bearer at-1" for call in http.calls)

            # An expired access token is refreshed and saved before the next call.
            vault_path = root / "connectors" / str(aid)
            vault = json.loads(base64.urlsafe_b64decode(vault_path.read_text()))
            vault["tokens"]["expires_at"] = int(time.time()) - 5
            vault_path.write_text(base64.urlsafe_b64encode(json.dumps(vault).encode()).decode())
            http.on("POST", f"{MS}/token", (200, {"access_token": "at-2", "expires_in": 3600}))
            assert connectors.access_token(pid, aid) == "at-2"
            assert (
                json.loads(base64.urlsafe_b64decode(vault_path.read_text()))["tokens"]["refresh_token"]
                == "rt-1"
            )

            # Concurrent callers with an expired token refresh once and share the result.
            vault = json.loads(base64.urlsafe_b64decode(vault_path.read_text()))
            vault["tokens"]["expires_at"] = int(time.time()) - 5
            vault_path.write_text(base64.urlsafe_b64encode(json.dumps(vault).encode()).decode())
            refreshes = len([call for call in http.calls if call[1] == f"{MS}/token"])
            http.on("POST", f"{MS}/token", (200, {"access_token": "at-3", "expires_in": 3600}))
            with ThreadPoolExecutor(max_workers=4) as pool:
                tokens = list(pool.map(lambda _index: connectors.access_token(pid, aid), range(4)))
            assert tokens == ["at-3"] * 4
            assert len([call for call in http.calls if call[1] == f"{MS}/token"]) == refreshes + 1

            blocked = client.delete(
                f"{base}/connectors/{aid}?expected_version={connected['version']}", headers=ORIGIN
            )
            assert blocked.status_code == 409

            google = _post(
                client,
                f"{base}/connectors",
                {
                    "provider": "google",
                    "label": "Google",
                    "client_id": "client-123.apps",
                    "client_secret": "desktop-secret",
                    "features": ["mail", "files"],
                },
            )
            redirect = connectors.start(pid, UUID(google["id"]))
            assert redirect["kind"] == "redirect" and "code_challenge=" in redirect["authorize_url"]
            assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A8765%2F" in redirect["authorize_url"]
            stale = client.post(
                "/api/connectors/google/callback",
                json={"state": f"{google['id']}.forged-state-value", "code": "c"},
                headers=ORIGIN,
            )
            assert stale.status_code == 409
    finally:
        engine.dispose()
