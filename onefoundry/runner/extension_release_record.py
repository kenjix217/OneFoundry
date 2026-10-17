"""Verify a prebuilt candidate archive before it can reach an activation inbox."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

from onefoundry.extension_activation_contract import ExtensionRelease, ExtensionReleaseRecord
from onefoundry.extension_qa_contract import ExtensionQaReceipt
from onefoundry.repository_code_contract import RepositoryCodeManifest
from onefoundry.runner_spool import RunnerSpool
from runner.repository_transport import publish_new, publish_replace
from scripts.onefoundry_update import unpack
from scripts.release_bundle import runtime_image_id


def record_release(
    spool: Path, archive_root: Path, coding_job: RepositoryCodeManifest, release: ExtensionRelease
) -> ExtensionReleaseRecord:
    """Read-only package verification; this neither builds nor installs software."""
    if archive_root.is_symlink() or not archive_root.is_absolute() or archive_root.resolve() != archive_root:
        raise ValueError("release_archive_root_invalid")
    transport = RunnerSpool(str(spool))
    code = transport.read_receipt(coding_job.attempt_id)
    qa = transport.read_receipt(release.qa_job_id)
    if code is None or qa is None:
        raise ValueError("release_candidate_receipts_unavailable")
    from onefoundry.external_code_schemas import ExternalCodeReceipt

    coding = ExternalCodeReceipt.model_validate(code[0])
    review = ExtensionQaReceipt.model_validate(qa[0])
    if (
        coding.status != "done"
        or coding.target_id != "repository_code_v1"
        or coding.project_id != coding_job.project_id
        or coding.run_id != release.coding_run_id
        or coding.request_sha256 != coding_job.request_sha256
        or review.status != "passed"
        or review.project_id != coding.project_id
        or review.candidate_commit != coding.commit
        or review.diff_sha256 != coding.diff_sha256
    ):
        raise ValueError("release_candidate_and_QA_disagree")
    archive = archive_root / f"{release.archive_sha256}.tar.gz"
    if archive.is_symlink() or not archive.is_file():
        raise ValueError("release_archive_unavailable")
    with tempfile.TemporaryDirectory(prefix="onefoundry-release-record-") as temporary:
        manifest = unpack(archive, release.archive_sha256, Path(temporary) / "release")
    if (
        manifest["commit"] != coding.commit
        or manifest["version"] != release.version
        or runtime_image_id(manifest) != release.app_image
    ):
        raise ValueError("release_archive_is_not_the_reviewed_candidate")
    record = ExtensionReleaseRecord.model_validate(
        {
            "project_id": coding.project_id,
            "candidate_commit": coding.commit,
            "diff_sha256": coding.diff_sha256,
            "qa_receipt_sha256": qa[1],
            "release": release.model_dump(mode="json"),
        }
    )
    path = spool / "artifacts" / str(coding_job.attempt_id) / "release.json"
    _publish_record(path, record)
    return record


def _publish_record(path: Path, record: ExtensionReleaseRecord) -> None:
    """Keep immutable versions; the legacy path selects the latest verified package."""
    records = []
    if path.exists() or path.is_symlink():
        previous = ExtensionReleaseRecord.model_validate_json(RunnerSpool._read_regular(path, 8192))
        for field in ("project_id", "candidate_commit", "diff_sha256", "qa_receipt_sha256"):
            if getattr(previous, field) != getattr(record, field):
                raise ValueError("release_record_candidate_changed")
        records.append(previous)
    records.append(record)
    for item in records:
        raw = json.dumps(item.model_dump(mode="json"), sort_keys=True).encode()
        version = path.with_name(f"release-{hashlib.sha256(raw).hexdigest()}.json")
        try:
            publish_new(version, raw)
        except FileExistsError:
            if RunnerSpool._read_regular(version, 8192) != raw:
                raise ValueError("release_record_version_changed") from None
    # Existing approvals already bind the entire record, archive, image and QA.
    # Moving this selection cannot change any approved installation job.
    publish_replace(path, json.dumps(record.model_dump(mode="json"), sort_keys=True).encode())
