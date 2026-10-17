"""Bounded, non-executing extension ZIP build and inspection."""

from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import ValidationError

from .models import ExtensionManifest

MANIFEST_NAME = "extension.json"
MAX_ARCHIVE_BYTES = 4 * 1024 * 1024
MAX_EXPANDED_BYTES = 8 * 1024 * 1024
MAX_MEMBER_BYTES = 2 * 1024 * 1024
MAX_MEMBERS = 64
MEMBER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./-]{0,239}$")
SECRET_VALUE = re.compile(
    rb"(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{16,})"
)
SECRET_ASSIGNMENT = re.compile(
    rb"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|private[_-]?key|secret)\b"
    rb"\s*[:=]\s*['\"]?[^\s'\"$]{8,}"
)


class PackageError(ValueError):
    """A package failed an exact, local validation contract."""


@dataclass(frozen=True)
class PackageInspection:
    manifest: ExtensionManifest
    package_sha256: str
    manifest_json: bytes
    file_sha256: dict[str, str]
    total_expanded_bytes: int


def _valid_name(name: str) -> None:
    if (
        not MEMBER_NAME.fullmatch(name)
        or name.startswith("/")
        or "//" in name
        or any(part in {"", ".", ".."} for part in name.split("/"))
        or name.endswith("/")
    ):
        raise PackageError("unsafe_member_name")
    if name != MANIFEST_NAME and not (
        name in {"README.md", "LICENSE", "NOTICE"}
        or (name.startswith("src/") and name.endswith(".py"))
        or (name.startswith("skills/") and name.endswith("/SKILL.md"))
        or (name.startswith("tests/") and name.endswith(".json"))
        or (name.startswith("resources/") and name.endswith((".md", ".txt", ".json")))
    ):
        raise PackageError("unsupported_package_file")


def _check_member(name: str, contents: bytes) -> None:
    _valid_name(name)
    if len(contents) > MAX_MEMBER_BYTES:
        raise PackageError("package_member_too_large")
    if SECRET_VALUE.search(contents) or SECRET_ASSIGNMENT.search(contents):
        raise PackageError("secret_like_value_in_package")
    if name.endswith((".json", ".md", ".py", ".txt")):
        try:
            contents.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PackageError("package_text_not_utf8") from exc


def canonical_manifest_json(manifest: ExtensionManifest) -> bytes:
    return (
        json.dumps(
            manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        + "\n"
    ).encode("utf-8")


def build_package(manifest: ExtensionManifest, files: Mapping[str, bytes] | None = None) -> bytes:
    """Create a reproducible ZIP. No source is imported or run."""
    entries = dict(files or {})
    if MANIFEST_NAME in entries:
        raise PackageError("manifest_is_generated")
    entries[MANIFEST_NAME] = canonical_manifest_json(manifest)
    if len(entries) > MAX_MEMBERS:
        raise PackageError("too_many_package_members")
    total = 0
    out = io.BytesIO()
    with zipfile.ZipFile(
        out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9, strict_timestamps=True
    ) as archive:
        for name, contents in sorted(entries.items()):
            if not isinstance(contents, bytes):
                raise PackageError("package_file_must_be_bytes")
            _check_member(name, contents)
            total += len(contents)
            if total > MAX_EXPANDED_BYTES:
                raise PackageError("package_expanded_too_large")
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(info, contents, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    result = out.getvalue()
    if len(result) > MAX_ARCHIVE_BYTES:
        raise PackageError("package_archive_too_large")
    return result


def inspect_package(data: bytes) -> PackageInspection:
    """Validate all ZIP members and return exact hashes without importing payload code."""
    if len(data) > MAX_ARCHIVE_BYTES:
        raise PackageError("package_archive_too_large")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data), "r")
    except (ValueError, zipfile.BadZipFile) as exc:
        raise PackageError("invalid_package_zip") from exc
    with archive:
        members = archive.infolist()
        if not members or len(members) > MAX_MEMBERS:
            raise PackageError("invalid_package_member_count")
        names: set[str] = set()
        hashes: dict[str, str] = {}
        total = 0
        manifest_bytes: bytes | None = None
        for member in members:
            name = member.filename
            _valid_name(name)
            if name in names:
                raise PackageError("duplicate_package_member")
            names.add(name)
            mode = member.external_attr >> 16
            if member.is_dir() or (stat.S_IFMT(mode) not in {0, stat.S_IFREG}):
                raise PackageError("non_regular_package_member")
            if member.flag_bits & 0x1:
                raise PackageError("encrypted_package_member")
            if member.file_size > MAX_MEMBER_BYTES:
                raise PackageError("package_member_too_large")
            total += member.file_size
            if total > MAX_EXPANDED_BYTES:
                raise PackageError("package_expanded_too_large")
            try:
                with archive.open(member) as stream:
                    contents = stream.read(MAX_MEMBER_BYTES + 1)
            except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
                raise PackageError("package_member_unreadable") from exc
            _check_member(name, contents)
            hashes[name] = hashlib.sha256(contents).hexdigest()
            if name == MANIFEST_NAME:
                manifest_bytes = contents
        if manifest_bytes is None:
            raise PackageError("extension_manifest_missing")
    try:
        manifest_raw = json.loads(manifest_bytes)
        manifest = ExtensionManifest.model_validate(manifest_raw)
    except (ValueError, ValidationError, TypeError) as exc:
        raise PackageError("extension_manifest_invalid") from exc
    _validate_sources_present(manifest, names)
    return PackageInspection(
        manifest=manifest,
        package_sha256=hashlib.sha256(data).hexdigest(),
        manifest_json=canonical_manifest_json(manifest),
        file_sha256=hashes,
        total_expanded_bytes=total,
    )


def _validate_sources_present(manifest: ExtensionManifest, names: set[str]) -> None:
    for tool in manifest.tools:
        if tool.runtime != "python":
            continue
        assert tool.entrypoint is not None
        module = tool.entrypoint.split(":", 1)[0]
        path = "src/" + module.replace(".", "/") + ".py"
        package_path = "src/" + module.replace(".", "/") + "/__init__.py"
        if path not in names and package_path not in names:
            raise PackageError("python_entrypoint_source_missing")
