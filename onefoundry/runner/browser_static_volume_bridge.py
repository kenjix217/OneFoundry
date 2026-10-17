"""Fixed browser spool operations inside a transient networkless volume helper.

The host invokes this as UID 10001 with the named spool volume. It validates
exact input before an atomic claim and never launches a browser or sees Docker.
"""

from __future__ import annotations

import json
import os
import stat
import struct
import sys
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

from onefoundry.role_sensitive_text import contains_sensitive_text
from runner.browser_static_child import MAX_TEXT, MAX_TITLE
from runner.browser_static_job_spool import (
    MAX_MANIFEST,
    MAX_RECEIPT,
    MAX_SOURCE,
    BrowserJobError,
    BrowserJobTransient,
    BrowserStaticJobSpool,
    _canonical,
    _unique_keys,
)

SPOOL = Path("/runner-spool/browser-static")
TRANSFER = Path("/transfer")
MAX_FRAME = 4 + MAX_MANIFEST + MAX_SOURCE + 4
PAGE_SIZE = 1000


def _id(raw: str) -> UUID:
    try:
        value = UUID(raw, version=4)
    except (ValueError, AttributeError) as error:
        raise ValueError("invalid browser job ID") from error
    if str(value) != raw:
        raise ValueError("invalid browser job ID")
    return value


def _new(path: Path, data: bytes) -> None:
    """Link a complete private temporary file as the exclusive final name."""
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        BrowserStaticJobSpool._new(temporary, data)
        os.link(temporary, path, follow_symlinks=False)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _read_transfer(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_mode & 0o007
            or not 1 <= info.st_size <= MAX_RECEIPT
        ):
            raise ValueError("invalid browser transfer receipt")
        data = os.read(descriptor, MAX_RECEIPT + 1)
        if len(data) != info.st_size:
            raise ValueError("browser transfer receipt changed")
        return data
    finally:
        os.close(descriptor)


def frame(manifest: bytes, source: bytes) -> bytes:
    if not 1 <= len(manifest) <= MAX_MANIFEST or not 5 <= len(source) <= MAX_SOURCE + 4:
        raise ValueError("invalid browser export frame")
    return struct.pack(">I", len(manifest)) + manifest + source


def list_jobs(after: str | None = None) -> list[str]:
    spool = BrowserStaticJobSpool(SPOOL)
    if after is not None:
        after = str(_id(after))
    result: list[str] = []
    for path in sorted((spool.root / "jobs").glob("*.json")):
        try:
            job_id = _id(path.stem)
        except ValueError:
            continue
        if after is not None and str(job_id) <= after:
            continue
        if not (spool.root / "claims" / f"{job_id}.claim").exists():
            result.append(str(job_id))
            if len(result) >= PAGE_SIZE:
                break
    return result


def claim(raw_id: str) -> tuple[bytes, bytes]:
    job_id = _id(raw_id)
    spool = BrowserStaticJobSpool(SPOOL)
    try:
        manifest, _target, html, css = spool._load(job_id)
        raw_manifest = spool._read(spool._path("jobs", job_id, "json"), MAX_MANIFEST)
        if raw_manifest != _canonical(manifest):
            raise ValueError("noncanonical browser manifest")
        source = struct.pack(">I", len(html)) + html + css
        if source != spool._read(spool._path("sources", job_id, "bin"), MAX_SOURCE + 4):
            raise ValueError("browser source changed")
    except BrowserJobTransient:
        raise
    except (BrowserJobError, OSError) as error:
        raise ValueError("invalid job before claim") from error
    if (spool.root / "receipts" / f"{job_id}.json").exists():
        raise ValueError("browser receipt already exists")
    _new(spool._path("claims", job_id, "claim"), manifest["request_sha256"].encode("ascii"))
    return raw_manifest, source


def publish(raw_id: str) -> None:
    job_id = _id(raw_id)
    spool = BrowserStaticJobSpool(SPOOL)
    manifest, _target = spool._load_manifest(job_id)
    if not spool.claimed(job_id, manifest["request_sha256"]):
        raise ValueError("browser host claim missing")
    source = TRANSFER / f"{job_id}.json"
    raw = _read_transfer(source)
    try:
        value = json.loads(raw, object_pairs_hook=_unique_keys)
    except (ValueError, TypeError) as error:
        raise ValueError("invalid browser receipt") from error
    expected = {
        "schema_version": 1,
        "job_id": str(job_id),
        "project_id": manifest["project_id"],
        "run_id": manifest["run_id"],
        "fixture_id": manifest["fixture_id"],
        "request_sha256": manifest["request_sha256"],
        "binding_digest": manifest["binding_digest"],
        "source_sha256": manifest["source_sha256"],
        "image_id": manifest["image_id"],
    }
    if (
        not isinstance(value, dict)
        or any(value.get(key) != item for key, item in expected.items())
        or set(value)
        != {*expected, "status", "reason_code", "title", "text", "blocked_request_count", "finished_at"}
        or value.get("status") not in ("done", "unknown")
        or (value["status"] == "done" and value.get("reason_code") != "inspection_complete")
        or (value["status"] == "unknown" and value.get("reason_code") != "runner_failure_after_claim")
        or raw != _canonical(value)
    ):
        raise ValueError("invalid browser receipt")
    try:
        finished = datetime.fromisoformat(value["finished_at"])
    except (TypeError, ValueError) as error:
        raise ValueError("invalid browser receipt") from error
    if finished.tzinfo is None or finished.utcoffset() is None:
        raise ValueError("invalid browser receipt")
    if value["status"] == "done":
        title, body, blocked = value["title"], value["text"], value["blocked_request_count"]
        if (
            not isinstance(title, str)
            or not isinstance(body, str)
            or len(title) > MAX_TITLE
            or len(body) > MAX_TEXT
            or contains_sensitive_text(title)
            or contains_sensitive_text(body)
            or type(blocked) is not int
            or not 0 <= blocked <= 1000
        ):
            raise ValueError("invalid browser receipt")
    elif any(value[key] is not None for key in ("title", "text", "blocked_request_count")):
        raise ValueError("invalid browser receipt")
    target = spool._path("receipts", job_id, "json")
    try:
        _new(target, raw)
    except FileExistsError:
        if spool._read(target, MAX_RECEIPT) != raw:
            raise ValueError("conflicting browser receipt") from None


def main() -> None:
    if len(sys.argv) in (2, 3) and sys.argv[1] == "list":
        after = sys.argv[2] if len(sys.argv) == 3 else None
        sys.stdout.write("".join(f"{job_id}\n" for job_id in list_jobs(after)))
    elif len(sys.argv) == 3 and sys.argv[1] == "claim":
        sys.stdout.buffer.write(frame(*claim(sys.argv[2])))
    elif len(sys.argv) == 3 and sys.argv[1] == "publish":
        publish(sys.argv[2])
    else:
        raise ValueError("invalid browser bridge command")


def entrypoint() -> None:
    """Reserve exit 4 for an invalid job, distinct from helper failure."""
    try:
        main()
    except FileExistsError:
        if len(sys.argv) > 1 and sys.argv[1] == "claim":
            raise SystemExit(3) from None
        raise
    except ValueError:
        if len(sys.argv) > 1 and sys.argv[1] == "claim":
            raise SystemExit(4) from None
        raise
    except BrowserJobTransient:
        if len(sys.argv) > 1 and sys.argv[1] == "claim":
            raise SystemExit(5) from None
        raise


if __name__ == "__main__":
    entrypoint()
