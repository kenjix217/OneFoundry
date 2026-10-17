"""Gateway-owned inert package import and supported-format export."""

from __future__ import annotations

import base64
import io
import json
import stat
import tempfile
import zipfile
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from onefoundry_sdk.formats import (
    export_plugin,
    inspect_foreign_folder,
    read_foreign_provenance,
    read_foreign_resources,
)
from onefoundry_sdk.package import (
    MANIFEST_NAME,
    MAX_ARCHIVE_BYTES,
    MAX_EXPANDED_BYTES,
    MAX_MEMBER_BYTES,
    MAX_MEMBERS,
    PackageError,
    inspect_package,
)
from pydantic import ValidationError
from sqlalchemy import text

from onefoundry.gateway_errors import NotFound
from onefoundry.gateway_extension_schemas import PackageCreate

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway

PackageFormat = Literal["onefoundry", "codex-portable", "codex-legacy", "claude"]


class ArchiveRejected(ValueError):
    """The uploaded archive does not satisfy the import contract."""


def _foreign_files(data: bytes) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if not members or len(members) > MAX_MEMBERS:
                raise PackageError("invalid_package_member_count")
            for member in members:
                name = member.filename
                path = PurePosixPath(name)
                if (
                    not name
                    or len(name) > 240
                    or path.is_absolute()
                    or "\\" in name
                    or any(part in {"", ".", ".."} for part in name.split("/"))
                    or any(ord(char) < 32 for char in name)
                    or name in files
                    or stat.S_IFMT(member.external_attr >> 16) not in {0, stat.S_IFREG}
                    or member.flag_bits & 1
                    or member.file_size > MAX_MEMBER_BYTES
                ):
                    raise PackageError("unsafe_plugin_member")
                total += member.file_size
                if total > MAX_EXPANDED_BYTES:
                    raise PackageError("package_expanded_too_large")
                with archive.open(member) as source:
                    body = source.read(MAX_MEMBER_BYTES + 1)
                if len(body) > MAX_MEMBER_BYTES:
                    raise PackageError("package_member_too_large")
                files[name] = body
    except (OSError, RuntimeError, zipfile.BadZipFile, NotImplementedError):
        raise PackageError("invalid_plugin_archive") from None
    return files


def import_archive(
    gateway: ToolGateway,
    project_id: UUID,
    format: PackageFormat,
    archive_base64: str,
) -> dict[str, Any]:
    try:
        if len(archive_base64) > ((MAX_ARCHIVE_BYTES + 2) // 3) * 4:
            raise PackageError("package_archive_too_large")
        data = base64.b64decode(archive_base64, validate=True)
        if len(data) > MAX_ARCHIVE_BYTES:
            raise PackageError("package_archive_too_large")
        if format == "onefoundry":
            manifest = inspect_package(data).manifest
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                files = {
                    name: archive.read(name).decode("utf-8")
                    for name in archive.namelist()
                    if name != MANIFEST_NAME
                }
            report: dict[str, Any] = {
                "format": format,
                "components": [
                    {"component": "package", "status": "supported", "reason": "Imported as an inactive draft"}
                ],
            }
        else:
            source = _foreign_files(data)
            with tempfile.TemporaryDirectory(prefix="onefoundry-plugin-import-") as temporary:
                root = Path(temporary)
                for name, contents in source.items():
                    target = root / name
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    target.write_bytes(contents)
                manifest, portability = inspect_foreign_folder(root, format)
                resource_files, _ = read_foreign_resources(root)
                files = {name: body.decode("utf-8") for name, body in resource_files.items()}
                provenance_files, _ = read_foreign_provenance(root)
                files.update({name: body.decode("utf-8") for name, body in provenance_files.items()})
            report = asdict(portability)
            report["components"] = list(report["components"])
        package = (
            gateway._extensions().create_package_archive(project_id, data)
            if format == "onefoundry"
            else gateway._extensions().create_package(
                project_id, PackageCreate(manifest=manifest, files=files)
            )
        )
    except (PackageError, ValueError, UnicodeError, ValidationError):
        raise ArchiveRejected("Package import rejected: invalid, unsafe or unsupported archive") from None
    return {"package": package, "report": report}


def export_archive(
    gateway: ToolGateway,
    project_id: UUID,
    package_id: UUID,
    format: PackageFormat,
) -> tuple[bytes, str]:
    with gateway.engine.connect() as connection:
        gateway._require_project(connection, project_id)
        saved = (
            connection.execute(
                text(
                    "SELECT archive,package_key,package_version FROM extension_packages "
                    "WHERE project_id=:p AND id=:id"
                ),
                {"p": project_id, "id": package_id},
            )
            .mappings()
            .one_or_none()
        )
        if saved is None:
            raise NotFound("extension package not found")
    archive = bytes(saved["archive"])
    inspection = inspect_package(archive)
    if format != "onefoundry":
        with zipfile.ZipFile(io.BytesIO(archive)) as source:
            resources = {
                name: source.read(name) for name in source.namelist() if name.startswith("resources/")
            }
            provenance = {
                name: source.read(name) for name in ("LICENSE", "NOTICE") if name in source.namelist()
            }
        files, report = export_plugin(inspection.manifest, format, resources=resources, provenance=provenance)
        files["portability-report.json"] = json.dumps(asdict(report), sort_keys=True, indent=2).encode()
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as exported:
            for name, content in sorted(files.items()):
                exported.writestr(name, content)
        archive = output.getvalue()
    return archive, f"{saved['package_key']}-{saved['package_version']}-{format}.zip"
