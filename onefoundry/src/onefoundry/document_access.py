"""Provider reads and mutations scoped to one selected folder's direct files."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from onefoundry.document_action_schemas import RangeRead, dimensions
from onefoundry.document_api_sources import (
    DRIVE,
    DRIVE_UPLOAD,
    GRAPH,
    DocumentError,
    GoogleDriveFolder,
    OneDriveFolder,
)

SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


class DocumentAccess:
    def __init__(self, client: OneDriveFolder | GoogleDriveFolder, folder_id: str) -> None:
        self.client = client
        self.folder_id = folder_id
        self.google = isinstance(client, GoogleDriveFolder)

    def item_url(self, file_id: str) -> str:
        return (
            f"{DRIVE}/files/{quote(file_id, safe='')}"
            if self.google
            else f"{GRAPH}/me/drive/items/{quote(file_id, safe='')}"
        )

    def metadata(self, file_id: str) -> dict[str, Any]:
        query = (
            "?fields=id,name,mimeType,parents,version,size,trashed&supportsAllDrives=true"
            if self.google
            else "?$select=id,name,eTag,size,file,parentReference,deleted"
        )
        status, item = self.client.json(self.item_url(file_id) + query)
        if status != 200 or item.get("id") != file_id or item.get("trashed") or "deleted" in item:
            raise DocumentError("file_unavailable")
        parent_ok = self._direct_file(item)
        if not parent_ok:
            raise DocumentError("outside_selected_folder")
        revision = item.get("version") if self.google else item.get("eTag")
        name = item.get("name")
        if not isinstance(revision, str) or not revision or not isinstance(name, str):
            raise DocumentError("invalid_response")
        mime = item.get("mimeType") if self.google else (item.get("file") or {}).get("mimeType")
        return {
            "file_id": file_id,
            "name": name,
            "revision": revision,
            "mime_type": mime,
            "folder_id": self.folder_id,
        }

    def _direct_file(self, item: dict[str, Any]) -> bool:
        if self.google:
            parents = item.get("parents")
            return (
                isinstance(parents, list)
                and self.folder_id in parents
                and item.get("mimeType") != "application/vnd.google-apps.folder"
            )
        parent, file = item.get("parentReference"), item.get("file")
        return isinstance(parent, dict) and parent.get("id") == self.folder_id and isinstance(file, dict)

    def search(self, query: str, *, max_pages: int, limit: int) -> dict[str, Any]:
        if self.google:
            escaped = self.folder_id.replace("'", "\\'")
            url = (
                DRIVE
                + "/files?"
                + urlencode(
                    {
                        "q": f"'{escaped}' in parents and trashed = false",
                        "pageSize": limit,
                        "fields": "nextPageToken,files(id,name,mimeType,parents,version)",
                        "supportsAllDrives": "true",
                        "includeItemsFromAllDrives": "true",
                    }
                )
            )
        else:
            url = (
                f"{GRAPH}/me/drive/items/{quote(self.folder_id, safe='')}/children"
                f"?$select=id,name,file,folder,parentReference,eTag&$top={limit}"
            )
        root = url.split("?", 1)[0]
        found: list[dict[str, Any]] = []
        truncated = False
        for _ in range(max_pages):
            status, page = self.client.json(url)
            if status != 200:
                raise DocumentError("search_failed")
            items = page.get("files" if self.google else "value")
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise DocumentError("invalid_response")
            for item in items:
                parent_ok = self._direct_file(item)
                if (
                    parent_ok
                    and item.get("mimeType") != "application/vnd.google-apps.folder"
                    and query.casefold() in str(item.get("name", "")).casefold()
                ):
                    if (
                        not isinstance(item.get("id"), str)
                        or not isinstance(item.get("name"), str)
                        or not isinstance(item.get("version") if self.google else item.get("eTag"), str)
                    ):
                        raise DocumentError("invalid_response")
                    found.append(
                        {
                            "file_id": item.get("id"),
                            "name": item.get("name"),
                            "revision": item.get("version") if self.google else item.get("eTag"),
                        }
                    )
            follow = page.get("nextPageToken" if self.google else "@odata.nextLink")
            truncated = bool(follow) or len(found) > limit
            if not follow or len(found) >= limit:
                break
            if not isinstance(follow, str):
                raise DocumentError("invalid_response")
            if self.google:
                url = root + "?" + urlencode({**dict(parse_qsl(urlsplit(url).query)), "pageToken": follow})
            else:
                if (
                    urlsplit(follow).scheme != "https"
                    or urlsplit(follow).netloc != urlsplit(root).netloc
                    or urlsplit(follow).path != urlsplit(root).path
                ):
                    raise DocumentError("pagination_rejected")
                url = follow
        return {"files": found[:limit], "truncated": truncated, "scope": "selected_folder_direct_files"}

    def range_url(self, data: RangeRead) -> str:
        if self.google:
            a1 = "'" + data.sheet.replace("'", "''") + "'!" + data.address
            return f"{SHEETS}/{quote(data.file_id, safe='')}/values/{quote(a1, safe='')}"
        return (
            self.item_url(data.file_id)
            + f"/workbook/worksheets/{quote(data.sheet, safe='')}/range(address='{data.address}')"
        )

    def range(self, data: RangeRead, max_cells: int) -> list[list[Any]]:
        rows, columns = dimensions(data.address)
        if rows * columns > max_cells:
            raise DocumentError("range_too_large")
        item = self.metadata(data.file_id)
        if self.google and item["mime_type"] != "application/vnd.google-apps.spreadsheet":
            raise DocumentError("not_a_spreadsheet")
        if not self.google and not item["name"].lower().endswith(".xlsx"):
            raise DocumentError("not_a_spreadsheet")
        status, value = self.client.json(
            self.range_url(data) + ("?valueRenderOption=UNFORMATTED_VALUE" if self.google else "")
        )
        values = value.get("values", [])
        if status != 200:
            raise DocumentError("range_read_failed")
        if (
            not isinstance(values, list)
            or len(values) > rows
            or any(not isinstance(row, list) or len(row) > columns for row in values)
        ):
            raise DocumentError("invalid_response")
        if any(type(cell) not in {str, int, float, bool} for row in values for cell in row):
            raise DocumentError("invalid_response")
        return [
            [values[r][c] if r < len(values) and c < len(values[r]) else "" for c in range(columns)]
            for r in range(rows)
        ]

    def replace(self, file_id: str, content: bytes, mime: str) -> None:
        url = (
            f"{DRIVE_UPLOAD}/files/{quote(file_id, safe='')}?uploadType=media&supportsAllDrives=true"
            if self.google
            else self.item_url(file_id) + "/content"
        )
        status, _ = self.client.call(
            url, method="PATCH" if self.google else "PUT", body=content, content_type=mime
        )
        self._written(status)

    def edit(self, data: RangeRead, values: list[list[Any]]) -> None:
        body = {"values": values, **({"majorDimension": "ROWS"} if self.google else {})}
        url = self.range_url(data) + ("?valueInputOption=RAW" if self.google else "")
        status, _ = self.client.call(
            url,
            method="PUT" if self.google else "PATCH",
            body=json.dumps(body, allow_nan=False).encode(),
            content_type="application/json",
        )
        self._written(status)

    def share(self, file_id: str, email: str, access: str) -> None:
        body = (
            {"type": "user", "role": "reader" if access == "read" else "writer", "emailAddress": email}
            if self.google
            else {
                "recipients": [{"email": email}],
                "roles": [access],
                "requireSignIn": True,
                "sendInvitation": False,
                "retainInheritedPermissions": True,
            }
        )
        suffix = (
            "/permissions?sendNotificationEmail=false&supportsAllDrives=true" if self.google else "/invite"
        )
        status, _ = self.client.call(
            self.item_url(file_id) + suffix,
            method="POST",
            body=json.dumps(body).encode(),
            content_type="application/json",
        )
        self._written(status)

    @staticmethod
    def _written(status: int) -> None:
        if status not in {200, 201}:
            raise DocumentError("write_rejected" if 400 <= status < 500 else "write_outcome_unknown")

    def shared(self, file_id: str, email: str, access: str) -> bool:
        suffix = (
            "/permissions?fields=nextPageToken,permissions(type,emailAddress,role)&supportsAllDrives=true"
            if self.google
            else "/permissions"
        )
        status, result = self.client.json(self.item_url(file_id) + suffix)
        if status != 200 or result.get("nextPageToken") or result.get("@odata.nextLink"):
            raise DocumentError("readback_failed")
        items = result.get("permissions" if self.google else "value")
        if not isinstance(items, list):
            raise DocumentError("invalid_response")
        for permission in items:
            if not isinstance(permission, dict):
                raise DocumentError("invalid_response")
            if self.google:
                if (
                    permission.get("type") == "user"
                    and str(permission.get("emailAddress", "")).lower() == email
                    and permission.get("role") == ("reader" if access == "read" else "writer")
                ):
                    return True
            else:
                identities = permission.get("grantedToIdentitiesV2") or [permission.get("grantedToV2") or {}]
                if not isinstance(identities, list) or any(
                    not isinstance(identity, dict) or not isinstance(identity.get("user", {}), dict)
                    for identity in identities
                ):
                    raise DocumentError("invalid_response")
                addresses = [
                    str((identity.get("user") or {}).get("email", "")).lower()
                    for identity in identities
                    if isinstance(identity, dict)
                ]
                invitation = permission.get("invitation") or {}
                if not isinstance(invitation, dict):
                    raise DocumentError("invalid_response")
                addresses.append(str(invitation.get("email", "")).lower())
                if email in addresses and permission.get("roles") == [access] and not permission.get("link"):
                    return True
        return False
