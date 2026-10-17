"""OneDrive and Google Drive folder adapters against a scripted transport."""

import pytest

from onefoundry.document_api_sources import (
    DRIVE,
    GRAPH,
    Change,
    DocumentError,
    GoogleDriveFolder,
    OneDriveFolder,
)
from tests.fake_provider_http import FakeHTTP


def test_onedrive_resolves_watches_direct_children_and_checks_download_hosts() -> None:
    http = FakeHTTP()
    http.on(
        "GET", f"{GRAPH}/me/drive/root:/Contracts", (200, {"id": "F1", "name": "Contracts", "folder": {}})
    )
    http.on(
        "GET",
        f"{GRAPH}/me/drive/root/delta?token=latest",
        (200, {"value": [], "@odata.deltaLink": f"{GRAPH}/d1"}),
    )
    http.on(
        "GET",
        f"{GRAPH}/d1",
        (
            200,
            {
                "value": [
                    {
                        "id": "a",
                        "name": "nda.docx",
                        "eTag": "e1",
                        "size": 10,
                        "file": {"mimeType": "x"},
                        "parentReference": {"id": "F1"},
                    },
                    {
                        "id": "b",
                        "name": "other.docx",
                        "eTag": "e2",
                        "size": 10,
                        "file": {},
                        "parentReference": {"id": "F2"},
                    },
                    {"id": "c", "name": "Sub", "folder": {}, "parentReference": {"id": "F1"}},
                    {"id": "d", "deleted": {}, "file": {}, "parentReference": {"id": "F1"}},
                ],
                "@odata.deltaLink": f"{GRAPH}/d2",
            },
        ),
    )
    folder = OneDriveFolder(lambda: "token", http, 5)
    assert folder.resolve("/Contracts").folder_id == "F1"
    assert folder.baseline() == f"{GRAPH}/d1"
    batch = folder.changes(f"{GRAPH}/d1", "F1", 10)
    assert [change.file_id for change in batch.changes] == ["a"] and batch.cursor == f"{GRAPH}/d2"
    change = batch.changes[0]
    http.on(
        "GET",
        f"{GRAPH}/me/drive/items/a?",
        (200, {"@microsoft.graph.downloadUrl": "https://tenant.sharepoint.com/dl/a"}),
    )
    http.on("GET", "https://tenant.sharepoint.com/dl/a", (200, b"DOCX-BYTES"))
    assert folder.download(change, 1000) == ("nda.docx", b"DOCX-BYTES")
    assert http.calls[-1][3] == ""  # the pre-authenticated link gets no bearer token
    http.on(
        "GET",
        f"{GRAPH}/me/drive/items/a?",
        (200, {"@microsoft.graph.downloadUrl": "https://attacker.example/x"}),
    )
    with pytest.raises(DocumentError) as rejected:
        folder.download(change, 1000)
    assert rejected.value.code == "download_host_rejected"
    with pytest.raises(DocumentError) as large:
        folder.download(Change("a", "big.pdf", "e", 5000, "", ""), 1000)
    assert large.value.code == "file_too_large"
    with pytest.raises(DocumentError):
        folder.changes("https://attacker.example/delta", "F1", 10)


def test_google_drive_resolves_url_filters_parent_and_exports_native_files() -> None:
    http = FakeHTTP()
    folder_id = "1AbCdEfGhIjKlMnOp"
    http.on(
        "GET",
        f"{DRIVE}/files/{folder_id}?fields=id,name,mimeType",
        (200, {"id": folder_id, "name": "Pipeline", "mimeType": "application/vnd.google-apps.folder"}),
    )
    http.on("GET", f"{DRIVE}/changes/startPageToken", (200, {"startPageToken": "50"}))
    http.on(
        "GET",
        f"{DRIVE}/changes?pageToken=50",
        (
            200,
            {
                "newStartPageToken": "60",
                "changes": [
                    {
                        "fileId": "s1",
                        "file": {
                            "id": "s1",
                            "name": "Pipeline",
                            "mimeType": "application/vnd.google-apps.spreadsheet",
                            "parents": [folder_id],
                            "version": "7",
                        },
                    },
                    {
                        "fileId": "x",
                        "file": {
                            "id": "x",
                            "name": "Elsewhere.pdf",
                            "mimeType": "application/pdf",
                            "parents": ["other"],
                        },
                    },
                    {
                        "fileId": "t",
                        "file": {
                            "id": "t",
                            "name": "Old",
                            "mimeType": "text/plain",
                            "parents": [folder_id],
                            "trashed": True,
                        },
                    },
                    {"fileId": "r", "removed": True},
                ],
            },
        ),
    )
    drive = GoogleDriveFolder(lambda: "g-token", http, 5)
    assert drive.resolve(f"https://drive.google.com/drive/folders/{folder_id}?usp=sharing").name == "Pipeline"
    assert drive.baseline() == "50"
    batch = drive.changes("50", folder_id, 10)
    assert [change.file_id for change in batch.changes] == ["s1"] and batch.cursor == "60"
    http.on("GET", f"{DRIVE}/files/s1/export", (200, b"XLSX"))
    name, data = drive.download(batch.changes[0], 1000)
    assert name == "Pipeline.xlsx" and data == b"XLSX"
    assert "mimeType=application%2Fvnd.openxmlformats-officedocument.spreadsheetml.sheet" in http.calls[-1][1]
    with pytest.raises(DocumentError):
        drive.resolve("not a folder id!")
