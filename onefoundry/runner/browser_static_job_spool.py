"""Private one-claim browser job transport for a separate host runner.

FastAPI may stage an approved fixture here, but only the host runner calls
`process_job`. A claim without a receipt is unknown and is never retried.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import struct
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from onefoundry.browser_static_fixtures import ApprovedStaticFixture
from onefoundry.browser_target import BrowserTarget, BrowserTargetLimits
from runner.browser_static_supervisor import StaticBrowserReceipt, run_static_browser_inspection

MAX_SOURCE = 262_144
MAX_MANIFEST = 8192
MAX_RECEIPT = 8192
_IMAGE = re.compile(r"sha256:[0-9a-f]{64}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_TIMEOUT_FENCE = b"timeout:"


class BrowserJobError(RuntimeError):
    """A browser job failed its private transport contract."""


class BrowserJobTransient(BrowserJobError):
    """A final name is visible while its private staging link is being removed."""


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise BrowserJobError("browser_job_duplicate_key")
        value[key] = item
    return value


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise BrowserJobError("browser_job_time_invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise BrowserJobError("browser_job_time_invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BrowserJobError("browser_job_time_invalid")
    return parsed.astimezone(UTC)


class BrowserStaticJobSpool:
    """Exact immutable input files and one terminal text receipt per job ID."""

    def __init__(self, root: Path) -> None:
        if not root.is_absolute() or root.is_symlink() or not root.is_dir():
            raise BrowserJobError("browser_job_root_invalid")
        info = root.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise BrowserJobError("browser_job_root_not_private")
        self.root = root
        for name in ("jobs", "sources", "claims", "receipts"):
            path = root / name
            path.mkdir(mode=0o700, exist_ok=True)
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise BrowserJobError("browser_job_directory_not_private")

    def _path(self, kind: str, job_id: UUID, suffix: str) -> Path:
        if kind not in {"jobs", "sources", "claims", "receipts"} or not isinstance(job_id, UUID):
            raise BrowserJobError("browser_job_path_invalid")
        return self.root / kind / f"{job_id}.{suffix}"

    @staticmethod
    def _new(path: Path, content: bytes) -> None:
        temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.link(temporary, path, follow_symlinks=False)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _read(path: Path, maximum: int) -> bytes:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or not 1 <= info.st_size <= maximum
            ):
                raise BrowserJobError("browser_job_file_invalid")
            if info.st_nlink == 2:
                raise BrowserJobTransient("browser_job_stage_in_progress")
            if info.st_nlink != 1:
                raise BrowserJobError("browser_job_file_invalid")
            value = os.read(descriptor, maximum + 1)
            if len(value) != info.st_size:
                raise BrowserJobError("browser_job_file_changed")
            return value
        finally:
            os.close(descriptor)

    def plan(self, job_id: UUID, fixture: ApprovedStaticFixture, image_id: str) -> dict[str, Any]:
        """Calculate the exact request digest before committing a DB job intent."""
        if not isinstance(image_id, str) or _IMAGE.fullmatch(image_id) is None:
            raise BrowserJobError("browser_job_image_invalid")
        target = fixture.target
        html, css = fixture.html, fixture.css
        if not html or len(html) + len(css) + 15 > min(MAX_SOURCE, target.max_page_bytes):
            raise BrowserJobError("browser_job_source_invalid")
        if _digest(html + b"\x00" + css) != fixture.source_sha256:
            raise BrowserJobError("browser_job_source_changed")
        unsigned: dict[str, Any] = {
            "schema_version": 1,
            "job_id": str(job_id),
            "fixture_id": str(fixture.fixture_id),
            "project_id": str(target.project_id),
            "run_id": str(target.run_id),
            "actor_id": target.actor_id,
            "approval_digest": fixture.proposal_digest,
            "source_sha256": fixture.source_sha256,
            "binding_digest": target.binding_digest,
            "approved_url": target.approved_url,
            "issued_at": target.issued_at.isoformat(),
            "expires_at": target.expires_at.isoformat(),
            "idempotency_key": str(target.idempotency_key),
            "viewport_width": target.viewport_width,
            "viewport_height": target.viewport_height,
            "max_page_bytes": target.max_page_bytes,
            "timeout_seconds": target.timeout_seconds,
            "limits": asdict(target.limits),
            "image_id": image_id,
        }
        manifest = {**unsigned, "request_sha256": _digest(_canonical(unsigned))}
        encoded = _canonical(manifest)
        if len(encoded) > MAX_MANIFEST:
            raise BrowserJobError("browser_job_manifest_too_large")
        return manifest

    def stage(self, job_id: UUID, fixture: ApprovedStaticFixture, image_id: str) -> dict[str, Any]:
        """Stage only Gateway-approved bytes; a manifest appears last."""
        manifest = self.plan(job_id, fixture, image_id)
        html, css = fixture.html, fixture.css
        encoded = _canonical(manifest)
        source = struct.pack(">I", len(html)) + html + css
        if len(source) > MAX_SOURCE + 4:
            raise BrowserJobError("browser_job_source_too_large")
        try:
            self._new(self._path("sources", job_id, "bin"), source)
            self._new(self._path("jobs", job_id, "json"), encoded)
        except (OSError, FileExistsError) as error:
            raise BrowserJobError("browser_job_stage_unknown") from error
        return manifest

    def claimed(self, job_id: UUID, request_sha256: str) -> bool:
        snapshot = self._claim_snapshot(job_id)
        if snapshot is None:
            return False
        state = self._claim_state(snapshot[0], request_sha256)
        if state == "fence":
            return False
        if state != "host":
            raise BrowserJobError("browser_job_claim_incomplete")
        return True

    def _claim_snapshot(self, job_id: UUID) -> tuple[bytes, float] | None:
        try:
            descriptor = os.open(
                self._path("claims", job_id, "claim"), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
            )
        except FileNotFoundError:
            return None
        try:
            before = os.fstat(descriptor)
            raw = os.read(descriptor, 73)
            after = os.fstat(descriptor)
            if (
                any(
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or info.st_nlink != 1
                    or info.st_mode & 0o077
                    or info.st_size > 72
                    for info in (before, after)
                )
                or len(raw) > 72
            ):
                raise BrowserJobError("browser_job_file_invalid")
            return raw, max(0.0, time.time() - after.st_mtime)
        finally:
            os.close(descriptor)

    @staticmethod
    def _claim_state(raw: bytes, request_sha256: str) -> str:
        if not isinstance(request_sha256, str) or _DIGEST.fullmatch(request_sha256) is None:
            raise BrowserJobError("browser_job_claim_invalid")
        host = request_sha256.encode("ascii")
        fence = _TIMEOUT_FENCE + host
        if raw == host:
            return "host"
        if raw == fence:
            return "fence"
        if (len(raw) < len(host) and host.startswith(raw)) or (
            len(raw) < len(fence) and fence.startswith(raw)
        ):
            return "partial"
        raise BrowserJobError("browser_job_claim_mismatch")

    def fence_unclaimed(self, job_id: UUID, request_sha256: str) -> bool:
        """Reserve the host claim path before an unclaimed timeout becomes terminal."""
        if not isinstance(request_sha256, str) or _DIGEST.fullmatch(request_sha256) is None:
            raise BrowserJobError("browser_job_claim_invalid")
        path = self._path("claims", job_id, "claim")
        marker = _TIMEOUT_FENCE + request_sha256.encode("ascii")
        try:
            self._new(path, marker)
            return True
        except FileExistsError as error:
            snapshot = self._claim_snapshot(job_id)
            if snapshot is None:
                raise BrowserJobError("browser_job_claim_changed") from error
            return self._claim_state(snapshot[0], request_sha256) == "fence"

    def claim_age_seconds(self, job_id: UUID, request_sha256: str) -> float | None:
        snapshot = self._claim_snapshot(job_id)
        if snapshot is None or self._claim_state(snapshot[0], request_sha256) == "fence":
            return None
        return snapshot[1]

    def purge_source(self, job_id: UUID) -> None:
        """Remove the transient source copy after a terminal owner readback."""
        path = self._path("sources", job_id, "bin")
        try:
            self._read(path, MAX_SOURCE + 4)
        except FileNotFoundError:
            pass
        except BrowserJobTransient:
            final_info = path.lstat()
            if (
                not stat.S_ISREG(final_info.st_mode)
                or final_info.st_uid != os.getuid()
                or final_info.st_mode & 0o077
                or final_info.st_nlink != 2
                or not 1 <= final_info.st_size <= MAX_SOURCE + 4
            ):
                raise
            matching: list[Path] = []
            for temporary in path.parent.glob(f".{path.name}.*.tmp"):
                nonce = temporary.name.removeprefix(f".{path.name}.").removesuffix(".tmp")
                if re.fullmatch(r"[0-9a-f]{32}", nonce) is None:
                    continue
                info = temporary.lstat()
                if (info.st_dev, info.st_ino) == (final_info.st_dev, final_info.st_ino):
                    matching.append(temporary)
            if len(matching) != 1:
                if path.lstat().st_nlink != 1:
                    raise BrowserJobTransient("browser_job_source_link_unrecognized") from None
                self._read(path, MAX_SOURCE + 4)
            path.unlink()
            if matching:
                matching[0].unlink(missing_ok=True)
        else:
            path.unlink()
        for temporary in path.parent.glob(f".{path.name}.*.tmp"):
            nonce = temporary.name.removeprefix(f".{path.name}.").removesuffix(".tmp")
            if re.fullmatch(r"[0-9a-f]{32}", nonce) is None:
                continue
            info = temporary.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or info.st_mode & 0o077
            ):
                raise BrowserJobError("browser_job_source_temp_invalid")
            temporary.unlink()
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _load_manifest(self, job_id: UUID) -> tuple[dict[str, Any], BrowserTarget]:
        try:
            raw = self._read(self._path("jobs", job_id, "json"), MAX_MANIFEST)
            manifest = json.loads(raw, object_pairs_hook=_unique_keys)
            if not isinstance(manifest, dict) or set(manifest) != {
                "schema_version",
                "job_id",
                "fixture_id",
                "project_id",
                "run_id",
                "actor_id",
                "approval_digest",
                "source_sha256",
                "binding_digest",
                "approved_url",
                "issued_at",
                "expires_at",
                "idempotency_key",
                "viewport_width",
                "viewport_height",
                "max_page_bytes",
                "timeout_seconds",
                "limits",
                "image_id",
                "request_sha256",
            }:
                raise BrowserJobError("browser_job_manifest_invalid")
            unsigned = {key: value for key, value in manifest.items() if key != "request_sha256"}
            if (
                manifest["schema_version"] != 1
                or manifest["job_id"] != str(job_id)
                or manifest["request_sha256"] != _digest(_canonical(unsigned))
                or not isinstance(manifest["image_id"], str)
                or _IMAGE.fullmatch(manifest["image_id"]) is None
                or not isinstance(manifest["limits"], dict)
            ):
                raise BrowserJobError("browser_job_manifest_invalid")
            target = BrowserTarget(
                project_id=UUID(manifest["project_id"]),
                run_id=UUID(manifest["run_id"]),
                actor_id=manifest["actor_id"],
                approved_url=manifest["approved_url"],
                expires_at=_parse_time(manifest["expires_at"]),
                idempotency_key=UUID(manifest["idempotency_key"]),
                viewport_width=manifest["viewport_width"],
                viewport_height=manifest["viewport_height"],
                max_page_bytes=manifest["max_page_bytes"],
                timeout_seconds=manifest["timeout_seconds"],
                limits=BrowserTargetLimits(**manifest["limits"]),
                issued_at=_parse_time(manifest["issued_at"]),
            )
            if target.binding_digest != manifest["binding_digest"]:
                raise BrowserJobError("browser_job_binding_changed")
            return manifest, target
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise BrowserJobError("browser_job_input_invalid") from error

    def _load(self, job_id: UUID) -> tuple[dict[str, Any], BrowserTarget, bytes, bytes]:
        manifest, target = self._load_manifest(job_id)
        try:
            source = self._read(self._path("sources", job_id, "bin"), MAX_SOURCE + 4)
            if len(source) < 5:
                raise BrowserJobError("browser_job_source_invalid")
            html_size = struct.unpack(">I", source[:4])[0]
            html, css = source[4 : 4 + html_size], source[4 + html_size :]
            if (
                not html
                or len(html) + len(css) + 15 > target.max_page_bytes
                or _digest(html + b"\x00" + css) != manifest["source_sha256"]
            ):
                raise BrowserJobError("browser_job_source_changed")
            return manifest, target, html, css
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise BrowserJobError("browser_job_input_invalid") from error

    def process_job(
        self,
        job_id: UUID,
        *,
        inspect: Callable[..., StaticBrowserReceipt] = run_static_browser_inspection,
        preclaimed: bool = False,
    ) -> dict[str, Any] | None:
        """Host runner only: claim once, inspect, and save a bounded receipt."""
        receipt_path = self._path("receipts", job_id, "json")
        if receipt_path.exists():
            return None
        if not preclaimed:
            try:
                self._path("claims", job_id, "claim").lstat()
                return None
            except FileNotFoundError:
                pass
        manifest, target, html, css = self._load(job_id)
        if preclaimed:
            if not self.claimed(job_id, manifest["request_sha256"]):
                raise BrowserJobError("browser_job_host_claim_missing")
        else:
            try:
                self._new(self._path("claims", job_id, "claim"), manifest["request_sha256"].encode())
            except FileExistsError:
                return None
        status, reason = "unknown", "runner_failure_after_claim"
        result: StaticBrowserReceipt | None = None
        try:
            target.check_navigation(target.approved_url, now=datetime.now(UTC))
            result = inspect(target, html=html, css=css, image=manifest["image_id"])
            if (
                result.binding_digest != manifest["binding_digest"]
                or result.source_sha256 != manifest["source_sha256"]
                or result.final_url != target.approved_url
                or result.image_id != manifest["image_id"]
            ):
                raise BrowserJobError("browser_job_receipt_mismatch")
            status, reason = "done", "inspection_complete"
        except Exception:  # noqa: BLE001 - fail closed after durable claim
            status, reason = "unknown", "runner_failure_after_claim"
        receipt: dict[str, Any] = {
            "schema_version": 1,
            "job_id": str(job_id),
            "project_id": manifest["project_id"],
            "run_id": manifest["run_id"],
            "fixture_id": manifest["fixture_id"],
            "request_sha256": manifest["request_sha256"],
            "binding_digest": manifest["binding_digest"],
            "source_sha256": manifest["source_sha256"],
            "image_id": manifest["image_id"],
            "status": status,
            "reason_code": reason,
            "title": result.title if status == "done" and result is not None else None,
            "text": result.text if status == "done" and result is not None else None,
            "blocked_request_count": result.blocked_request_count
            if status == "done" and result is not None
            else None,
            "finished_at": datetime.now(UTC).isoformat(),
        }
        encoded = _canonical(receipt)
        if len(encoded) > MAX_RECEIPT:
            raise BrowserJobError("browser_job_receipt_too_large")
        self._new(receipt_path, encoded)
        return receipt

    def read_receipt(self, job_id: UUID) -> tuple[dict[str, Any], str] | None:
        try:
            raw = self._read(self._path("receipts", job_id, "json"), MAX_RECEIPT)
        except FileNotFoundError:
            return None
        try:
            value = json.loads(raw, object_pairs_hook=_unique_keys)
        except ValueError as error:
            raise BrowserJobError("browser_job_receipt_invalid") from error
        if not isinstance(value, dict) or value.get("job_id") != str(job_id):
            raise BrowserJobError("browser_job_receipt_invalid")
        return value, _digest(raw)
