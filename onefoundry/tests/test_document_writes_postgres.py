"""Approval-bound document writes to a watched Google Drive folder, with scripted provider responses."""

import hashlib
import os
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from onefoundry import document_writes
from onefoundry.connector_oauth import GOOGLE_TOKEN
from onefoundry.connector_store import ConnectorStore
from onefoundry.document_api_sources import DRIVE, DRIVE_UPLOAD
from onefoundry.document_store import DocumentStore
from onefoundry.gateway_errors import Conflict
from onefoundry.inference.contracts import TransportError
from onefoundry.main import create_app
from onefoundry.role_tool_admission import ToolDenied
from onefoundry.settings import Settings
from onefoundry.tool_gateway import ToolGateway
from tests.fake_provider_http import FakeHTTP

ORIGIN = {"origin": "http://localhost:8765"}
FOLDER = "1AbCdEfGhIjKlMnOp"
CONTENT = "Pilot pricing\n50 seats at the education rate.\n"


def _request(client: TestClient, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    response = client.request(method, path, json=body or {}, headers=ORIGIN)
    assert response.status_code in {200, 201}, response.text
    return response.json()


def _live_run(client: TestClient, settings: Settings, base: str, profile_id: str) -> UUID:
    order = _request(
        client,
        "POST",
        f"{base}/work-orders",
        {"title": "Write the brief", "instructions": "Draft it.", "profile_id": profile_id},
    )
    _request(client, "POST", f"{base}/work-orders/{order['id']}/ready")
    run = _request(client, "POST", f"{base}/work-orders/{order['id']}/runs")
    owner = create_engine(Settings(db_user="onefoundry", db_password=os.environ["DB_PASSWORD"]).database_url)
    with owner.begin() as connection:
        connection.execute(
            text("UPDATE runs SET mode='live_text',tokens_used=NULL,cost_usd=NULL WHERE id=:id"),
            {"id": UUID(run["id"])},
        )
    owner.dispose()
    return UUID(run["id"])


def test_document_write_is_approved_dispatched_once_and_verified(tmp_path: Path) -> None:
    root = tmp_path / "secrets"
    root.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(root),
        coordination_mode="draft_only",
    )
    engine = create_engine(settings.database_url)
    gateway = ToolGateway(engine, "operator", live_text_settings=settings)
    http = FakeHTTP()
    connectors = ConnectorStore(gateway, settings, http=http)
    documents = DocumentStore(gateway, settings, connectors, http=http)
    try:
        with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
            project = _request(client, "POST", "/api/projects", {"name": f"Writes {uuid4()}"})
            pid = UUID(project["id"])
            base = f"/api/projects/{pid}"
            profiles = {row["name"]: row["id"] for row in client.get(f"{base}/profiles").json()}
            _request(
                client,
                "PATCH",
                f"{base}/profiles/{profiles['GTM']}",
                {"allowed_tools": ["propose_document_write_v1"]},
            )

            def connect(label: str, features: list[str]) -> UUID:
                account = _request(
                    client,
                    "POST",
                    f"{base}/connectors",
                    {
                        "provider": "google",
                        "label": label,
                        "client_id": "client-123.apps",
                        "client_secret": "s",
                        "features": features,
                    },
                )
                started = connectors.start(pid, UUID(account["id"]))
                state = parse_qs(urlsplit(started["authorize_url"]).query)["state"][0]
                http.on(
                    "POST",
                    GOOGLE_TOKEN,
                    (200, {"access_token": "g-at", "refresh_token": "g-rt", "expires_in": 3600}),
                )
                assert connectors.callback(state, "auth-code")["status"] == "connected"
                return UUID(account["id"])

            def watch(label: str, account_id: UUID) -> UUID:
                source = _request(
                    client,
                    "POST",
                    f"{base}/documents/sources",
                    {
                        "label": label,
                        "provider": "gdrive",
                        "connector_account_id": str(account_id),
                        "folder_ref": FOLDER,
                    },
                )
                http.on(
                    "GET",
                    f"{DRIVE}/files/{FOLDER}?fields=id,name,mimeType",
                    (200, {"id": FOLDER, "name": "Briefs", "mimeType": "application/vnd.google-apps.folder"}),
                )
                http.on("GET", f"{DRIVE}/changes/startPageToken", (200, {"startPageToken": "10"}))
                assert documents.probe(pid, UUID(source["id"]))["status"] == "ready"
                return UUID(source["id"])

            read_only = watch("Read only", connect("Readonly Google", ["files"]))
            writable = watch("Briefs", connect("Writer Google", ["files", "files_write"]))
            run_id = _live_run(client, settings, base, profiles["GTM"])

            def propose(source_id: UUID, call: str, name: str = "pilot-pricing.md") -> dict[str, Any]:
                receipt = gateway.invoke_role_tool(
                    pid,
                    run_id,
                    UUID(profiles["GTM"]),
                    call,
                    "propose_document_write_v1",
                    {
                        "source_id": str(source_id),
                        "file_name": name,
                        "mime_type": "text/markdown",
                        "content": CONTENT,
                    },
                    tool_version=1,
                )
                result: dict[str, Any] = receipt["result"]
                return result

            with pytest.raises(ToolDenied, match="write access"):
                propose(read_only, "w-0")
            first = propose(writable, "w-1")
            approval = next(
                row for row in client.get(f"{base}/approvals").json() if row["id"] == first["approval_id"]
            )
            assert (
                approval["action_type"] == "publish"
                and "50 seats at the education rate." in approval["content"]
            )
            assert hashlib.sha256(CONTENT.encode()).hexdigest() in approval["content"]
            with pytest.raises(Conflict):
                document_writes.dispatch(gateway, documents, pid, UUID(first["write_id"]))
            _request(
                client, "POST", f"{base}/approvals/{first['approval_id']}/decision", {"decision": "approved"}
            )
            md5 = hashlib.md5(CONTENT.encode(), usedforsecurity=False).hexdigest()
            written = {
                "id": "new-file",
                "name": "pilot-pricing.md",
                "md5Checksum": md5,
                "size": str(len(CONTENT.encode())),
            }
            http.on("POST", f"{DRIVE_UPLOAD}/files?uploadType=multipart", (200, written))
            http.on("GET", f"{DRIVE}/files/new-file?fields=id,name,md5Checksum,size", (200, written))
            done = document_writes.dispatch(gateway, documents, pid, UUID(first["write_id"]))
            assert done["state"] == "done" and done["provider_file_id"] == "new-file"
            assert done["readback"]["verified"] is True
            upload = next(call for call in http.calls if call[1].startswith(DRIVE_UPLOAD))
            assert upload[0] == "POST"
            with pytest.raises(Conflict):
                document_writes.dispatch(gateway, documents, pid, UUID(first["write_id"]))
            listed = client.get(f"{base}/documents/writes").json()
            assert [row["state"] for row in listed if row["id"] == first["write_id"]] == ["done"]

            rejected = propose(writable, "w-2", "second.md")
            _request(
                client,
                "POST",
                f"{base}/approvals/{rejected['approval_id']}/decision",
                {"decision": "rejected"},
            )
            with pytest.raises(Conflict):
                document_writes.dispatch(gateway, documents, pid, UUID(rejected["write_id"]))

            mismatch = propose(writable, "w-3", "third.md")
            _request(
                client,
                "POST",
                f"{base}/approvals/{mismatch['approval_id']}/decision",
                {"decision": "approved"},
            )
            http.on(
                "GET",
                f"{DRIVE}/files/new-file?fields=id,name,md5Checksum,size",
                (200, {**written, "md5Checksum": "0" * 32}),
            )
            uncertain = document_writes.dispatch(gateway, documents, pid, UUID(mismatch["write_id"]))
            assert uncertain["state"] == "unknown" and uncertain["error_code"] == "readback_mismatch"

            dropped = propose(writable, "w-4", "fourth.md")
            _request(
                client,
                "POST",
                f"{base}/approvals/{dropped['approval_id']}/decision",
                {"decision": "approved"},
            )

            def lost(_request: object) -> tuple[int, object]:
                raise TransportError("connection reset")

            http.on("POST", f"{DRIVE_UPLOAD}/files?uploadType=multipart", lost)
            lost_write = document_writes.dispatch(gateway, documents, pid, UUID(dropped["write_id"]))
            assert lost_write["state"] == "unknown" and lost_write["error_code"] == "connection_failed"
            with pytest.raises(Conflict):
                document_writes.dispatch(gateway, documents, pid, UUID(dropped["write_id"]))
    finally:
        engine.dispose()
