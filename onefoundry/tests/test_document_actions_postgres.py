"""Proposal admission, project boundaries, exact approvals, and one-shot cloud writes."""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Any
from urllib.request import Request
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from onefoundry import document_action_store, document_operations, role_tool_admission
from onefoundry.connector_store import ConnectorStore
from onefoundry.document_access import DocumentAccess
from onefoundry.document_api_sources import DRIVE, DRIVE_UPLOAD, GRAPH, GoogleDriveFolder, OneDriveFolder
from onefoundry.document_schemas import DocumentSourceCreate
from onefoundry.document_store import DocumentStore
from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.inference.contracts import TransportError
from onefoundry.input_file_store import InputFileStore
from onefoundry.main import create_app
from onefoundry.role_tool_admission import ToolDenied, ToolInputError
from onefoundry.settings import Settings
from onefoundry.tool_gateway import ToolGateway
from tests.fake_provider_http import FakeHTTP
from tests.test_document_writes_postgres import _live_run, _request


@pytest.mark.parametrize("google", [False, True])
def test_document_actions_scope_approval_revision_verification_and_unknown(
    tmp_path: Path, google: bool
) -> None:
    for name in ("secrets", "inputs"):
        (tmp_path / name).mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(tmp_path / "secrets"),
        input_file_root=str(tmp_path / "inputs"),
        coordination_mode="off",
    )
    engine = create_engine(settings.database_url)
    gateway = ToolGateway(engine, "operator", live_text_settings=settings)
    gateway.input_file_store = InputFileStore(gateway, settings)
    http = FakeHTTP()
    connectors = ConnectorStore(gateway, settings, http=http)
    documents = DocumentStore(gateway, settings, connectors, http=http)
    provider, folder = ("google", "Folder12345678") if google else ("microsoft", "Folder")
    try:
        with TestClient(create_app(settings), base_url="http://localhost:8765") as browser:
            pid = UUID(_request(browser, "POST", "/api/projects", {"name": f"Actions {uuid4()}"})["id"])
            base = f"/api/projects/{pid}"
            profile = next(row for row in browser.get(f"{base}/profiles").json() if row["name"] == "GTM")
            tools = ["document_search_v1", "document_range_v1", "propose_document_action_v1"]
            _request(browser, "PATCH", f"{base}/profiles/{profile['id']}", {"allowed_tools": tools})
            profile_id = UUID(profile["id"])
            run_id = _live_run(browser, settings, base, profile["id"])
            account_fields: dict[str, Any] = {
                "provider": provider,
                "label": "Disposable",
                "client_id": "test.apps",
                "features": ["files", "files_write"],
            }
            if google:
                account_fields["client_secret"] = "test"
            account = _request(browser, "POST", f"{base}/connectors", account_fields)
            aid = UUID(account["id"])
            connectors._connect(
                pid,
                aid,
                connectors._vault(aid),
                {
                    "access_token": "fake-access",
                    "refresh_token": "fake-refresh",
                    "expires_at": int(time.time()) + 3600,
                },
            )
            source = documents.create(
                pid,
                DocumentSourceCreate(
                    label="Budget",
                    provider="gdrive" if google else "onedrive",
                    connector_account_id=aid,
                    folder_ref=folder,
                ),
            )
            sid = source["id"]
            if google:
                http.on(
                    "GET",
                    f"{DRIVE}/files/{folder}?",
                    (200, {"id": folder, "name": "Budget", "mimeType": "application/vnd.google-apps.folder"}),
                )
                http.on("GET", f"{DRIVE}/changes/startPageToken", (200, {"startPageToken": "1"}))
            else:
                http.on(
                    "GET",
                    f"{GRAPH}/me/drive/items/{folder}?",
                    (200, {"id": folder, "name": "Budget", "folder": {}}),
                )
                http.on("GET", f"{GRAPH}/me/drive/root/delta", (200, {"@odata.deltaLink": f"{GRAPH}/delta"}))
            assert documents.probe(pid, sid)["status"] == "ready"
            access = DocumentAccess(
                GoogleDriveFolder(lambda: "fake-access", http, 2)
                if google
                else OneDriveFolder(lambda: "fake-access", http, 2),
                folder,
            )
            revision = "1"
            values: list[list[Any]] = [[1, "Old"]]
            mime = (
                "application/vnd.google-apps.spreadsheet"
                if google
                else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )

            def metadata(_: Request) -> tuple[int, object]:
                return 200, {
                    "id": "File",
                    "name": "Budget.xlsx",
                    "version": revision,
                    "eTag": revision,
                    "mimeType": mime,
                    "parents": [folder],
                    "parentReference": {"id": folder},
                    "file": {"mimeType": mime},
                }

            http.on("GET", access.item_url("File") + "?", metadata)
            args = {
                "source_id": str(sid),
                "file_id": "File",
                "operation": "edit_cells",
                "sheet": "Summary",
                "address": "A1:B1",
                "values": [[2, "New"]],
            }
            from onefoundry.document_action_schemas import RangeRead

            rectangle = RangeRead(source_id=sid, file_id="File", sheet="Summary", address="A1:B1")
            http.on("GET", access.range_url(rectangle), lambda _: (200, {"values": values}))

            def propose(call: str, body: dict[str, Any]) -> dict[str, Any]:
                result = role_tool_admission.invoke_tool(
                    gateway, pid, run_id, profile_id, call, "propose_document_action_v1", body, tool_version=1
                )["result"]
                assert isinstance(result, dict)
                return result

            discovered = role_tool_admission.available_tools(gateway, pid, run_id, profile_id)
            assert all(tool in {item["id"] for item in discovered} for tool in tools)
            with pytest.raises(ToolInputError):
                propose("bad-range", {**args, "address": "A:A"})
            with pytest.raises(NotFound):
                document_operations.prepare(documents, uuid4(), uuid4(), args)
            proposal = propose("edit", args)
            action_id = UUID(proposal["action_id"])
            assert not [call for call in http.calls if call[0] in {"PATCH", "PUT", "POST"}]
            with pytest.raises(Conflict):
                document_operations.dispatch(documents, pid, action_id)
            _request(
                browser,
                "POST",
                f"{base}/approvals/{proposal['approval_id']}/decision",
                {"decision": "approved"},
            )

            def edit(_: Request) -> tuple[int, object]:
                nonlocal values
                values = [[2, "New"]]
                return 200, {}

            http.on("PUT" if google else "PATCH", access.range_url(rectangle), edit)
            assert document_operations.dispatch(documents, pid, action_id)["state"] == "done"
            assert document_action_store.list_actions(gateway, uuid4()) == []
            with pytest.raises(Conflict):
                document_operations.dispatch(documents, pid, action_id)
            with pytest.raises(ToolDenied):
                propose("edit", args)
            stale = propose("stale", args)
            _request(
                browser, "POST", f"{base}/approvals/{stale['approval_id']}/decision", {"decision": "approved"}
            )
            revision = "2"
            writes_before = len([call for call in http.calls if call[0] in {"PUT", "PATCH"}])
            assert document_operations.dispatch(documents, pid, UUID(stale["action_id"]))["state"] == "failed"
            assert len([call for call in http.calls if call[0] in {"PUT", "PATCH"}]) == writes_before
            # Existing binary files use the saved original-byte owner; no model sees those bytes.
            text_url = access.item_url("Text")
            http.on(
                "GET",
                text_url + "?",
                (
                    200,
                    {
                        "id": "Text",
                        "name": "brief.txt",
                        "version": "1",
                        "eTag": "1",
                        "mimeType": "text/plain",
                        "parents": [folder],
                        "parentReference": {"id": folder},
                        "file": {"mimeType": "text/plain"},
                    },
                ),
            )
            content = b"Updated brief\n"
            assert gateway.input_file_store is not None
            uploaded = gateway.input_file_store.ingest(pid, "brief.txt", content)
            replacement = propose(
                "replace",
                {
                    "source_id": str(sid),
                    "file_id": "Text",
                    "operation": "replace_file",
                    "input_file_id": str(uploaded["id"]),
                },
            )
            _request(
                browser,
                "POST",
                f"{base}/approvals/{replacement['approval_id']}/decision",
                {"decision": "approved"},
            )
            http.on(
                "PATCH" if google else "PUT",
                f"{DRIVE_UPLOAD}/files/Text" if google else text_url + "/content",
                (200, {}),
            )
            if google:
                http.on("GET", text_url + "?alt=media", (200, content))
            else:
                http.on(
                    "GET",
                    text_url + "?$select=id,name,size",
                    (200, {"@microsoft.graph.downloadUrl": "https://tenant.sharepoint.com/brief"}),
                )
                http.on("GET", "https://tenant.sharepoint.com/brief", (200, content))
            assert (
                document_operations.dispatch(documents, pid, UUID(replacement["action_id"]))["state"]
                == "done"
            )
            permission = propose(
                "permission",
                {
                    "source_id": str(sid),
                    "file_id": "Text",
                    "operation": "share_file",
                    "email": "review@example.com",
                    "access": "read",
                },
            )
            _request(
                browser,
                "POST",
                f"{base}/approvals/{permission['approval_id']}/decision",
                {"decision": "approved"},
            )
            http.on("POST", text_url + ("/permissions" if google else "/invite"), (200, {}))
            http.on(
                "GET",
                text_url + "/permissions",
                (
                    200,
                    {
                        "permissions": [
                            {"type": "user", "role": "reader", "emailAddress": "review@example.com"}
                        ]
                    }
                    if google
                    else {"value": [{"roles": ["read"], "invitation": {"email": "review@example.com"}}]},
                ),
            )
            assert (
                document_operations.dispatch(documents, pid, UUID(permission["action_id"]))["state"] == "done"
            )
            # Sharing is durable before network I/O. A concurrent dispatch is refused.
            shared = propose(
                "share",
                {
                    "source_id": str(sid),
                    "file_id": "File",
                    "operation": "share_file",
                    "email": "review@example.com",
                    "access": "read",
                },
            )
            _request(
                browser,
                "POST",
                f"{base}/approvals/{shared['approval_id']}/decision",
                {"decision": "approved"},
            )
            entered, release = Event(), Event()

            def uncertain(_: Request) -> tuple[int, object]:
                entered.set()
                assert release.wait(5)
                raise TransportError("test timeout")

            http.on("POST", access.item_url("File") + ("/permissions" if google else "/invite"), uncertain)
            share_id = UUID(shared["action_id"])
            with ThreadPoolExecutor(max_workers=2) as pool:
                future = pool.submit(document_operations.dispatch, documents, pid, share_id)
                assert entered.wait(5)
                with pytest.raises(Conflict):
                    document_operations.dispatch(documents, pid, share_id)
                release.set()
                assert future.result()["state"] == "unknown"
            assert len([call for call in http.calls if call[0] == "POST"]) == 2
            following = propose("after-unknown", args)
            _request(
                browser,
                "POST",
                f"{base}/approvals/{following['approval_id']}/decision",
                {"decision": "approved"},
            )
            with pytest.raises(Conflict, match="unknown outcome"):
                document_operations.dispatch(documents, pid, UUID(following["action_id"]))
            _request(browser, "PATCH", f"{base}/profiles/{profile['id']}", {"allowed_tools": []})
            with pytest.raises(ToolDenied):
                propose("no-permission", args)
            with engine.connect() as connection:
                assert (
                    connection.scalar(
                        text(
                            "SELECT count(*) FROM events WHERE project_id=:p "
                            "AND event_type='document_action_unknown'"
                        ),
                        {"p": pid},
                    )
                    == 1
                )
    finally:
        engine.dispose()
