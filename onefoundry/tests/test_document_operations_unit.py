"""Folder containment, bounded paging, exact ranges, and provider payloads."""

import json
from typing import Any
from urllib.request import Request
from uuid import uuid4

import pytest
from agents.strict_schema import ensure_strict_json_schema
from pydantic import ValidationError

from onefoundry.document_access import SHEETS, DocumentAccess
from onefoundry.document_action_schemas import EditCells, RangeRead, dimensions, parse_action
from onefoundry.document_api_sources import (
    DRIVE,
    DRIVE_UPLOAD,
    GRAPH,
    DocumentError,
    GoogleDriveFolder,
    OneDriveFolder,
)
from onefoundry.role_document_tools import DESCRIPTORS
from tests.fake_provider_http import FakeHTTP


@pytest.mark.parametrize("google", [False, True])
def test_search_is_direct_bounded_and_does_not_follow_foreign_urls(google: bool) -> None:
    http = FakeHTTP()
    client = GoogleDriveFolder(lambda: "test", http, 1) if google else OneDriveFolder(lambda: "test", http, 1)
    access = DocumentAccess(client, "Folder")
    prefix = f"{DRIVE}/files?" if google else f"{GRAPH}/me/drive/items/Folder/children"
    item = (
        {"id": "File", "name": "Budget.xlsx", "parents": ["Folder"], "version": "1"}
        if google
        else {
            "id": "File",
            "name": "Budget.xlsx",
            "parentReference": {"id": "Folder"},
            "file": {},
            "eTag": "1",
        }
    )
    foreign = {**item, "id": "Foreign", "parents": ["Elsewhere"], "parentReference": {"id": "Elsewhere"}}
    page = {
        "files" if google else "value": [item, foreign],
        "nextPageToken" if google else "@odata.nextLink": "next" if google else prefix + "?$skiptoken=2",
    }
    http.on("GET", prefix, (200, page))
    assert access.search("bud", max_pages=1, limit=10) == {
        "files": [{"file_id": "File", "name": "Budget.xlsx", "revision": "1"}],
        "truncated": True,
        "scope": "selected_folder_direct_files",
    }
    if not google:
        http.on("GET", prefix, (200, {**page, "@odata.nextLink": "https://attacker.example/collect"}))
        with pytest.raises(DocumentError, match="pagination_rejected"):
            access.search("no match", max_pages=2, limit=10)
        assert all("attacker" not in url for _, url, _, _ in http.calls)
    http.on("GET", prefix, (200, {"files" if google else "value": []}))
    assert access.search("empty", max_pages=1, limit=1)["files"] == []
    http.on("GET", prefix, (503, {}))
    with pytest.raises(DocumentError, match="search_failed"):
        access.search("fail", max_pages=1, limit=1)


@pytest.mark.parametrize("google", [False, True])
def test_ranges_validate_parent_and_write_literal_exact_values(google: bool) -> None:
    http = FakeHTTP()
    client = GoogleDriveFolder(lambda: "test", http, 1) if google else OneDriveFolder(lambda: "test", http, 1)
    access = DocumentAccess(client, "Folder")
    meta = (
        {
            "id": "File",
            "name": "Budget.xlsx",
            "parents": ["Folder"],
            "version": "1",
            "mimeType": "application/vnd.google-apps.spreadsheet",
        }
        if google
        else {
            "id": "File",
            "name": "Budget.xlsx",
            "parentReference": {"id": "Folder"},
            "file": {},
            "eTag": "1",
        }
    )
    http.on("GET", access.item_url("File") + "?", (200, meta))
    data = RangeRead(source_id=uuid4(), file_id="File", sheet="Summary", address="A1:B2")
    http.on("GET", access.range_url(data), (200, {"values": [[1]]}))
    assert access.range(data, 4) == [[1, ""], ["", ""]]
    with pytest.raises(DocumentError, match="range_too_large"):
        access.range(data, 3)
    bodies: list[object] = []

    def written(request: Request) -> tuple[int, object]:
        assert isinstance(request.data, bytes)
        bodies.append(json.loads(request.data or b"{}"))
        return 200, {}

    http.on("PUT" if google else "PATCH", access.range_url(data), written)
    access.edit(data, [[1, "literal"], [False, ""]])
    assert bodies == [
        {"values": [[1, "literal"], [False, ""]], **({"majorDimension": "ROWS"} if google else {})}
    ]
    if google:
        assert http.calls[-1][1].endswith("valueInputOption=RAW")
        assert http.calls[-1][1].startswith(SHEETS)
    http.on(
        "GET",
        access.item_url("File") + "?",
        (200, {**meta, "parents": ["Other"], "parentReference": {"id": "Other"}}),
    )
    with pytest.raises(DocumentError, match="outside_selected_folder"):
        access.range(data, 4)


@pytest.mark.parametrize("google", [False, True])
def test_replacement_and_sharing_use_exact_file_recipient_no_notification(google: bool) -> None:
    http = FakeHTTP()
    client = GoogleDriveFolder(lambda: "test", http, 1) if google else OneDriveFolder(lambda: "test", http, 1)
    access = DocumentAccess(client, "Folder")
    url = f"{DRIVE_UPLOAD}/files/File" if google else f"{GRAPH}/me/drive/items/File/content"
    http.on("PATCH" if google else "PUT", url, (200, {}))
    access.replace("File", b"new bytes", "text/plain")
    assert http.calls[-1][1].startswith(url)
    captured: list[dict[str, object]] = []

    def shared(request: Request) -> tuple[int, object]:
        assert isinstance(request.data, bytes)
        captured.append(json.loads(request.data or b"{}"))
        return 200, {}

    suffix = "/permissions" if google else "/invite"
    http.on("POST", access.item_url("File") + suffix, shared)
    access.share("File", "test@example.com", "read")
    if google:
        assert captured == [{"type": "user", "role": "reader", "emailAddress": "test@example.com"}]
        assert "sendNotificationEmail=false" in http.calls[-1][1]
        perms: dict[str, Any] = {
            "permissions": [{"type": "user", "role": "reader", "emailAddress": "test@example.com"}]
        }
    else:
        assert captured[0]["sendInvitation"] is False and captured[0]["requireSignIn"] is True
        assert captured[0]["retainInheritedPermissions"] is True
        perms = {"value": [{"roles": ["read"], "grantedToV2": {"user": {"email": "test@example.com"}}}]}
    http.on("GET", access.item_url("File") + "/permissions", (200, perms))
    assert access.shared("File", "test@example.com", "read")
    assert not access.shared("File", "other@example.com", "read")
    http.on("POST", access.item_url("File") + suffix, (503, {}))
    with pytest.raises(DocumentError, match="write_outcome_unknown"):
        access.share("File", "test@example.com", "read")


@pytest.mark.parametrize("address", ["A:A", "Sheet!A1:B2", "A0:B2", "B2:A1", "XFE1:XFE2", "A1:A1048577"])
def test_range_rejects_unbounded_named_and_outside_addresses(address: str) -> None:
    with pytest.raises(ValueError):
        dimensions(address)


def test_action_contract_rejects_formulas_extra_side_effects_and_has_sdk_schema() -> None:
    fields = {
        "source_id": str(uuid4()),
        "file_id": "File",
        "operation": "edit_cells",
        "sheet": "Summary",
        "address": "A1:A1",
        "values": [[""]],
    }
    assert isinstance(parse_action({**fields, "email": None}), EditCells)
    for value in ('=HYPERLINK("https://example.com")', "@SUM(A1)", " +1"):
        with pytest.raises(ValidationError):
            parse_action({**fields, "values": [[value]]})
    with pytest.raises(ValidationError):
        parse_action({**fields, "email": "extra@example.com"})
    for descriptor in DESCRIPTORS.values():
        schema = ensure_strict_json_schema(descriptor["parameters"])
        assert schema["type"] == "object" and schema["additionalProperties"] is False
