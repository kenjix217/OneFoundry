"""Watched OneDrive folder into input files, document signals, and a work order with the file attached."""

import os
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from onefoundry import coordination_store
from onefoundry.connector_store import ConnectorStore
from onefoundry.document_api_sources import GRAPH
from onefoundry.document_store import DocumentStore
from onefoundry.main import create_app
from onefoundry.settings import Settings
from onefoundry.tool_gateway import ToolGateway
from tests.fake_provider_http import FakeHTTP

ORIGIN = {"origin": "http://localhost:8765"}
MS = "https://login.microsoftonline.com/common/oauth2/v2.0"


def _post(client: TestClient, path: str, body: dict[str, Any]) -> Any:
    response = client.post(path, json=body, headers=ORIGIN)
    assert response.status_code in {200, 201}, response.text
    return response.json()


def _item(item_id: str, name: str, etag: str, parent: str = "F1") -> dict[str, Any]:
    return {
        "id": item_id,
        "name": name,
        "eTag": etag,
        "size": 40,
        "file": {"mimeType": "text/plain"},
        "parentReference": {"id": parent},
    }


def test_watched_folder_ingests_new_and_changed_files(tmp_path: Path) -> None:
    directories = {name: tmp_path / name for name in ("secrets", "artifacts", "inputs")}
    for directory in directories.values():
        directory.mkdir(mode=0o700)
    settings = Settings(
        db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
        db_password=os.environ["APP_DB_PASSWORD"],
        model_secret_root=str(directories["secrets"]),
        artifact_workspace_root=str(directories["artifacts"]),
        input_file_root=str(directories["inputs"]),
        coordination_mode="draft_only",
    )
    engine = create_engine(settings.database_url)
    gateway = ToolGateway(engine, "operator", live_text_settings=settings)
    coordinator = ToolGateway(engine, settings.onefoundry_coordinator_actor_id, live_text_settings=settings)
    http = FakeHTTP()
    connectors = ConnectorStore(gateway, settings, http=http)
    documents = DocumentStore(gateway, settings, connectors, http=http)
    try:
        with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
            project = _post(client, "/api/projects", {"name": f"Docs {uuid4()}"})
            pid = UUID(project["id"])
            base = f"/api/projects/{pid}"
            profiles = {row["name"]: row["id"] for row in client.get(f"{base}/profiles").json()}
            mail_only = _post(
                client,
                f"{base}/connectors",
                {
                    "provider": "microsoft",
                    "label": "Mail only",
                    "client_id": "22222222-bbbb",
                    "features": ["mail"],
                },
            )
            refused = client.post(
                f"{base}/documents/sources",
                json={
                    "label": "x",
                    "provider": "onedrive",
                    "connector_account_id": mail_only["id"],
                    "folder_ref": "/x",
                },
                headers=ORIGIN,
            )
            assert refused.status_code == 409
            account = _post(
                client,
                f"{base}/connectors",
                {
                    "provider": "microsoft",
                    "label": "Files",
                    "client_id": "11111111-aaaa",
                    "features": ["files"],
                },
            )
            aid = UUID(account["id"])
            http.on(
                "POST",
                f"{MS}/devicecode",
                (
                    200,
                    {
                        "device_code": "d",
                        "user_code": "U",
                        "verification_uri": "https://microsoft.com/devicelogin",
                    },
                ),
            )
            connectors.start(pid, aid)
            http.on(
                "POST",
                f"{MS}/token",
                (200, {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}),
            )
            assert connectors.poll(pid, aid)["status"] == "connected"

            source = _post(
                client,
                f"{base}/documents/sources",
                {
                    "label": "Contracts",
                    "provider": "onedrive",
                    "connector_account_id": str(aid),
                    "folder_ref": "/Contracts",
                },
            )
            http.on(
                "GET",
                f"{GRAPH}/me/drive/root:/Contracts",
                (200, {"id": "F1", "name": "Contracts", "folder": {}}),
            )
            http.on(
                "GET",
                f"{GRAPH}/me/drive/root/delta?token=latest",
                (200, {"value": [], "@odata.deltaLink": f"{GRAPH}/d1"}),
            )
            ready = documents.probe(pid, UUID(source["id"]))
            assert ready["status"] == "ready" and ready["folder_name"] == "Contracts"
            _post(
                client,
                f"{base}/documents/sources/{source['id']}/enabled",
                {"enabled": True, "expected_version": ready["version"]},
            )
            _post(
                client,
                f"{base}/coordination/rules",
                {
                    "name": "Review new contracts",
                    "signal_type": "document.created",
                    "filters": {"source_label": "Contracts"},
                    "target_profile_id": profiles["Researcher"],
                    "title": "Summarize the new contract",
                    "instructions": "Summarize obligations and dates from the attached file.",
                },
            )
            http.on(
                "GET",
                f"{GRAPH}/d1",
                (
                    200,
                    {
                        "value": [
                            _item("a", "nda.txt", "e1"),
                            _item("z", "elsewhere.txt", "e9", parent="F2"),
                            _item("b", "tool.exe", "e3"),
                        ],
                        "@odata.deltaLink": f"{GRAPH}/d2",
                    },
                ),
            )
            http.on(
                "GET",
                f"{GRAPH}/me/drive/items/a?",
                (200, {"@microsoft.graph.downloadUrl": "https://tenant.sharepoint.com/a"}),
            )
            http.on("GET", "https://tenant.sharepoint.com/a", (200, b"The NDA renews on 1 March 2027."))
            http.on(
                "GET",
                f"{GRAPH}/me/drive/items/b?",
                (200, {"@microsoft.graph.downloadUrl": "https://tenant.sharepoint.com/b"}),
            )
            http.on("GET", "https://tenant.sharepoint.com/b", (200, b"MZ"))
            assert documents.poll_next()
            feed = client.get(f"{base}/coordination/signals").json()
            created = {row["fields"]["file_name"]: row for row in feed if row["source"] == "document"}
            assert set(created) == {"nda.txt", "tool.exe"}
            assert created["nda.txt"]["signal_type"] == "document.created"
            assert (
                created["nda.txt"]["fields"]["input_file_id"]
                and created["nda.txt"]["fields"]["ingest_error"] is None
            )
            assert created["tool.exe"]["fields"]["ingest_error"] == "unsupported_or_too_large"
            files = client.get(f"{base}/input-files").json()
            assert [row["display_name"] for row in files] == ["nda.txt"]

            for _ in range(6):
                coordination_store.run_once(coordinator, settings, pid)
            orders = [row for row in client.get(f"{base}/work-orders").json() if row["origin_signal_id"]]
            # Both new files match the rule; the unsupported one still creates work showing its error.
            assert sorted(row["origin_signal_id"] for row in orders) == sorted(
                row["id"] for row in created.values()
            )
            orders = [row for row in orders if row["origin_signal_id"] == created["nda.txt"]["id"]]
            with engine.connect() as connection:
                linked = connection.scalar(
                    text(
                        "SELECT file_id FROM input_file_links WHERE project_id=:p AND work_order_id=:o AND "
                        "scope='work_order'"
                    ),
                    {"p": pid, "o": UUID(orders[0]["id"])},
                )
            assert str(linked) == created["nda.txt"]["fields"]["input_file_id"]

            # The same revision again records nothing; a new revision is a change.
            with engine.begin() as connection:
                connection.execute(
                    text("UPDATE document_sources SET last_poll_at=NULL WHERE id=:id"),
                    {"id": UUID(source["id"])},
                )
            http.on(
                "GET",
                f"{GRAPH}/d2",
                (
                    200,
                    {
                        "value": [_item("a", "nda.txt", "e1"), _item("a", "nda.txt", "e2")],
                        "@odata.deltaLink": f"{GRAPH}/d3",
                    },
                ),
            )
            assert documents.poll_next()
            changes = [
                row
                for row in client.get(f"{base}/coordination/signals").json()
                if row["signal_type"] == "document.changed"
            ]
            assert len(changes) == 1 and changes[0]["fields"]["revision"] == "e2"
    finally:
        engine.dispose()
