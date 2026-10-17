"""OneDrive/SharePoint (Microsoft Graph) and Google Drive folders: resolve, watch, fetch, write.

Only files directly inside the selected folder are reported or fetched. Google
Docs, Sheets, and Slides are exported to DOCX, XLSX, and PPTX so the existing
input-file worker can extract them. Graph download links are accepted only on
Microsoft content hosts. Writes are used only after an exact operator approval.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit
from urllib.request import Request

from onefoundry.inference.contracts import TransportError
from onefoundry.inference.http import HTTPTransport, send_http

GRAPH = "https://graph.microsoft.com/v1.0"
DRIVE = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD = "https://www.googleapis.com/upload/drive/v3"
GRAPH_DOWNLOAD_HOSTS = (".sharepoint.com", ".1drv.com", ".onedrive.com", ".svc.ms", ".live.net")
GOOGLE_EXPORTS = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "docx",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "pptx",
    ),
}
FOLDER_URL = re.compile(r"/folders/([A-Za-z0-9_-]{10,200})")
TokenSource = Callable[[], str]


class DocumentError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Folder:
    folder_id: str
    name: str


@dataclass(frozen=True, slots=True)
class Change:
    file_id: str
    name: str
    revision: str
    size: int
    mime_type: str
    modified_at: str


@dataclass(frozen=True, slots=True)
class ChangeBatch:
    changes: tuple[Change, ...]
    cursor: str


@dataclass(frozen=True, slots=True)
class Written:
    file_id: str
    name: str
    revision: str
    size: int


class _Client:
    def __init__(self, token: TokenSource, http: HTTPTransport, timeout: float) -> None:
        self.token = token
        self.http = http
        self.timeout = timeout

    def call(
        self,
        url: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        content_type: str | None = None,
        auth: bool = True,
    ) -> tuple[int, bytes]:
        headers = {"Accept": "*/*"}
        if auth:
            headers["Authorization"] = f"Bearer {self.token()}"
        if content_type:
            headers["Content-Type"] = content_type
        request = Request(url, data=body, headers=headers, method=method)  # noqa: S310 - fixed provider origins
        try:
            result = self.http(request, timeout=self.timeout)
        except TransportError:
            raise DocumentError("connection_failed") from None
        if result.status in {401, 403}:
            raise DocumentError("authentication_failed")
        return result.status, result.body

    def json(self, url: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        status, body = self.call(url, **kwargs)
        if status >= 300:
            return status, {}
        try:
            value = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise DocumentError("invalid_response") from None
        if not isinstance(value, dict):
            raise DocumentError("invalid_response")
        return status, value


class OneDriveFolder(_Client):
    provider = "onedrive"

    def resolve(self, ref: str) -> Folder:
        ref = ref.strip()
        url = (
            f"{GRAPH}/me/drive/root:{quote(ref.rstrip('/') or '/', safe='/')}"
            if ref.startswith("/")
            else f"{GRAPH}/me/drive/items/{quote(ref, safe='!')}"
        )
        status, data = self.json(url + ("?$select=id,name,folder" if "?" not in url else ""))
        if status != 200 or "folder" not in data or not isinstance(data.get("id"), str):
            raise DocumentError("folder_unavailable")
        return Folder(str(data["id"]), str(data.get("name", ""))[:200])

    def baseline(self) -> str:
        status, data = self.json(f"{GRAPH}/me/drive/root/delta?token=latest")
        link = data.get("@odata.deltaLink")
        if status != 200 or not isinstance(link, str) or not link.startswith(GRAPH + "/"):
            raise DocumentError("folder_unavailable")
        return link

    def changes(self, cursor: str, folder_id: str, limit: int) -> ChangeBatch:
        if not cursor.startswith(GRAPH + "/"):
            raise DocumentError("cursor_expired")
        found: list[Change] = []
        url, next_cursor = cursor, cursor
        while True:
            status, data = self.json(url)
            if status in {404, 410}:
                raise DocumentError("cursor_expired")
            if status != 200:
                raise DocumentError("fetch_failed")
            for item in data.get("value", []) or []:
                parent = (item.get("parentReference") or {}).get("id")
                if parent != folder_id or "file" not in item or "deleted" in item:
                    continue
                found.append(
                    Change(
                        str(item["id"]),
                        str(item.get("name", ""))[:255],
                        str(item.get("eTag") or item.get("lastModifiedDateTime") or "")[:300] or "unknown",
                        int(item.get("size") or 0),
                        str((item.get("file") or {}).get("mimeType", "")),
                        str(item.get("lastModifiedDateTime", "")),
                    )
                )
            follow, done = data.get("@odata.nextLink"), data.get("@odata.deltaLink")
            if isinstance(follow, str) and follow.startswith(GRAPH + "/") and len(found) < limit:
                url = follow
                continue
            if isinstance(follow, str) and follow.startswith(GRAPH + "/"):
                next_cursor = follow
            elif isinstance(done, str) and done.startswith(GRAPH + "/"):
                next_cursor = done
            break
        return ChangeBatch(tuple(found[:limit]), next_cursor)

    def download(self, change: Change, max_bytes: int) -> tuple[str, bytes]:
        if change.size > max_bytes:
            raise DocumentError("file_too_large")
        status, data = self.json(
            f"{GRAPH}/me/drive/items/{quote(change.file_id, safe='!')}"
            "?$select=id,name,size,@microsoft.graph.downloadUrl"
        )
        link = data.get("@microsoft.graph.downloadUrl")
        host = urlsplit(link).hostname if isinstance(link, str) else None
        if status != 200 or not isinstance(link, str) or not link.startswith("https://") or not host:
            raise DocumentError("fetch_failed")
        if not host.endswith(GRAPH_DOWNLOAD_HOSTS):
            raise DocumentError("download_host_rejected")
        status, body = self.call(link, auth=False)
        if status != 200 or len(body) > max_bytes:
            raise DocumentError("fetch_failed" if status != 200 else "file_too_large")
        return change.name, body

    def upload(self, folder_id: str, name: str, data: bytes, mime_type: str) -> Written:
        url = (
            f"{GRAPH}/me/drive/items/{quote(folder_id, safe='!')}:/{quote(name)}:/content"
            "?@microsoft.graph.conflictBehavior=rename"
        )
        status, raw = self.call(url, method="PUT", body=data, content_type=mime_type)
        if status not in {200, 201}:
            raise DocumentError("write_failed")
        try:
            item = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise DocumentError("write_outcome_unknown") from None
        return Written(
            str(item.get("id")), str(item.get("name")), str(item.get("eTag", "")), int(item.get("size") or 0)
        )

    def read_back(self, file_id: str) -> Written:
        status, item = self.json(
            f"{GRAPH}/me/drive/items/{quote(file_id, safe='!')}?$select=id,name,eTag,size"
        )
        if status != 200:
            raise DocumentError("readback_failed")
        return Written(
            str(item.get("id")), str(item.get("name")), str(item.get("eTag", "")), int(item.get("size") or 0)
        )


class GoogleDriveFolder(_Client):
    provider = "gdrive"
    FIELDS = "id,name,mimeType,parents,modifiedTime,size,trashed,md5Checksum,version"

    def resolve(self, ref: str) -> Folder:
        match = FOLDER_URL.search(ref)
        folder_id = match.group(1) if match else ref.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,200}", folder_id):
            raise DocumentError("folder_unavailable")
        status, data = self.json(f"{DRIVE}/files/{folder_id}?fields=id,name,mimeType&supportsAllDrives=true")
        if status != 200 or data.get("mimeType") != "application/vnd.google-apps.folder":
            raise DocumentError("folder_unavailable")
        return Folder(str(data["id"]), str(data.get("name", ""))[:200])

    def baseline(self) -> str:
        status, data = self.json(f"{DRIVE}/changes/startPageToken?supportsAllDrives=true")
        token = data.get("startPageToken")
        if status != 200 or not isinstance(token, str) or not token:
            raise DocumentError("folder_unavailable")
        return token

    def changes(self, cursor: str, folder_id: str, limit: int) -> ChangeBatch:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", cursor):
            raise DocumentError("cursor_expired")
        found: list[Change] = []
        token, next_cursor = cursor, cursor
        while True:
            status, data = self.json(
                f"{DRIVE}/changes?pageToken={quote(token)}&pageSize=100&supportsAllDrives=true"
                f"&includeItemsFromAllDrives=true&fields=nextPageToken,newStartPageToken,"
                f"changes(fileId,removed,file({self.FIELDS}))"
            )
            if status in {400, 404}:
                raise DocumentError("cursor_expired")
            if status != 200:
                raise DocumentError("fetch_failed")
            for change in data.get("changes", []) or []:
                item = change.get("file") or {}
                if (
                    change.get("removed")
                    or item.get("trashed")
                    or folder_id not in (item.get("parents") or [])
                ):
                    continue
                if item.get("mimeType") == "application/vnd.google-apps.folder":
                    continue
                found.append(
                    Change(
                        str(item["id"]),
                        str(item.get("name", ""))[:255],
                        str(
                            item.get("md5Checksum")
                            or item.get("version")
                            or item.get("modifiedTime")
                            or "unknown"
                        ),
                        int(item.get("size") or 0),
                        str(item.get("mimeType", "")),
                        str(item.get("modifiedTime", "")),
                    )
                )
            page, start = data.get("nextPageToken"), data.get("newStartPageToken")
            if isinstance(page, str) and page and len(found) < limit:
                token = page
                continue
            next_cursor = page if isinstance(page, str) and page else str(start or token)
            break
        return ChangeBatch(tuple(found[:limit]), next_cursor)

    def download(self, change: Change, max_bytes: int) -> tuple[str, bytes]:
        export = GOOGLE_EXPORTS.get(change.mime_type)
        if export:
            name = f"{change.name}.{export[1]}"
            url = f"{DRIVE}/files/{change.file_id}/export?mimeType={quote(export[0], safe='')}"
        else:
            if change.size > max_bytes:
                raise DocumentError("file_too_large")
            name, url = change.name, f"{DRIVE}/files/{change.file_id}?alt=media&supportsAllDrives=true"
        status, body = self.call(url)
        if status != 200:
            raise DocumentError("fetch_failed")
        if len(body) > max_bytes:
            raise DocumentError("file_too_large")
        return name, body

    def upload(self, folder_id: str, name: str, data: bytes, mime_type: str) -> Written:
        boundary = f"onefoundry-{uuid.uuid4().hex}"
        metadata = json.dumps({"name": name, "parents": [folder_id]}).encode()
        body = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
            + metadata
            + f"\r\n--{boundary}\r\nContent-Type: {mime_type}\r\n\r\n".encode()
            + data
            + f"\r\n--{boundary}--\r\n".encode()
        )
        status, raw = self.call(
            f"{DRIVE_UPLOAD}/files?uploadType=multipart&supportsAllDrives=true&fields=id,name,md5Checksum,size",
            method="POST",
            body=body,
            content_type=f"multipart/related; boundary={boundary}",
        )
        if status not in {200, 201}:
            raise DocumentError("write_failed")
        try:
            item = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise DocumentError("write_outcome_unknown") from None
        return Written(
            str(item.get("id")),
            str(item.get("name")),
            str(item.get("md5Checksum", "")),
            int(item.get("size") or 0),
        )

    def read_back(self, file_id: str) -> Written:
        status, item = self.json(
            f"{DRIVE}/files/{file_id}?fields=id,name,md5Checksum,size&supportsAllDrives=true"
        )
        if status != 200:
            raise DocumentError("readback_failed")
        return Written(
            str(item.get("id")),
            str(item.get("name")),
            str(item.get("md5Checksum", "")),
            int(item.get("size") or 0),
        )


def folder_client(
    provider: str, token: TokenSource, *, http: HTTPTransport = send_http, timeout: float
) -> OneDriveFolder | GoogleDriveFolder:
    if provider == "onedrive":
        return OneDriveFolder(token, http, timeout)
    if provider == "gdrive":
        return GoogleDriveFolder(token, http, timeout)
    raise DocumentError("provider_unsupported")
