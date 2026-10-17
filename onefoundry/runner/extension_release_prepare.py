"""Reconstruct an independently tested candidate and build its immutable package."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import Field

from onefoundry.extension_activation_contract import ExtensionRelease
from onefoundry.extension_qa_contract import ExtensionQaReceipt
from onefoundry.extension_release_preparation_contract import ReleasePreparationJob, ReleasePreparationReceipt
from onefoundry.runner_spool import RunnerSpool, SpoolInvalid
from onefoundry.workflow_schemas import StrictModel
from runner.extension_release_record import record_release
from runner.fixture_git import Deadline, _git
from runner.repository_code_stage import candidate, stage
from runner.repository_transport import publish_new
from scripts import onefoundry_update, release_bundle


class ReleasePreparationSettings(StrictModel):
    install_home: Path
    archive_root: Path
    source_root: Path
    build_timeout_seconds: int = Field(ge=60, le=7200)
    managed_source_alias: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")

    def validate_host(self) -> None:
        for path in (self.install_home, self.archive_root, self.source_root):
            if not path.is_absolute() or path.resolve() != path or path.is_symlink() or not path.is_dir():
                raise ValueError("release_preparation_storage_invalid")
        if any(
            left == right or left in right.parents or right in left.parents
            for index, left in enumerate((self.install_home, self.archive_root, self.source_root))
            for right in (self.install_home, self.archive_root, self.source_root)[index + 1 :]
        ):
            raise ValueError("release_preparation_storage_overlaps")


def _build_inputs(repo: Path, baseline: str, deadline: Deadline) -> None:
    """Candidate code never supplies a new host/build program without separate setup."""
    changed = _git(repo, deadline, "diff", "--name-only", baseline, "HEAD").decode().splitlines()
    for path in changed:
        if path in {
            "onefoundry/Dockerfile",
            "onefoundry/.dockerignore",
            "onefoundry/compose.yaml",
            "onefoundry/compose.release.yaml",
            "onefoundry/onefoundry",
        } or path.startswith("onefoundry/scripts/"):
            raise ValueError("release_preparation_build_program_changed")
    # Version metadata may change, but a dependency/build-system change needs a
    # separately configured QA/build environment; do not silently fetch new code.
    from tomllib import loads

    for path in (
        "onefoundry/pyproject.toml",
        "onefoundry/uv.lock",
        "onefoundry/frontend/package.json",
        "onefoundry/frontend/package-lock.json",
    ):
        if path not in changed:
            continue
        before = _git(repo, deadline, "show", f"{baseline}:{path}").decode()
        after = (repo / path).read_text()
        parser = loads if path.endswith((".toml", ".lock")) else json.loads
        values = [parser(before), parser(after)]
        for value in values:
            if path.endswith("pyproject.toml"):
                value["project"].pop("version", None)
            elif path.endswith("uv.lock"):
                for package in value["package"]:
                    if package["name"] == "onefoundry":
                        package.pop("version", None)
            else:
                value.pop("version", None)
                if "packages" in value:
                    value["packages"][""].pop("version", None)
        if values[0] != values[1]:
            raise ValueError("release_preparation_dependencies_changed")


def process_preparation(
    path: Path,
    spool: Path,
    settings: ReleasePreparationSettings,
    *,
    builder: Callable[..., Path] = release_bundle.build,
) -> dict[str, Any] | None:
    settings.validate_host()
    if path.parent != spool / "jobs" or path.is_symlink():
        raise ValueError("release_preparation_path_invalid")
    UUID(path.stem)
    descriptor = os.open(
        spool / "claims" / f"{path.stem}.prepare-lock", os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600
    )
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        return _process_preparation(path, spool, settings, builder=builder)
    finally:
        os.close(descriptor)


def _process_preparation(
    path: Path, spool: Path, settings: ReleasePreparationSettings, *, builder: Callable[..., Path]
) -> dict[str, Any] | None:
    job = ReleasePreparationJob.model_validate_json(RunnerSpool._read_regular(path, 32768))
    if path.name != f"{job.job_id}.json":
        raise ValueError("release_preparation_job_changed")
    prior = RunnerSpool(str(spool)).read_receipt(job.job_id)
    if prior is not None:
        receipt = ReleasePreparationReceipt.model_validate(prior[0])
        if (
            receipt.job_id != job.job_id
            or receipt.project_id != job.project_id
            or receipt.request_sha256 != job.request_sha256
        ):
            raise ValueError("release_preparation_receipt_changed")
        return None
    try:
        publish_new(spool / "claims" / f"{job.job_id}.claim", job.request_sha256.encode())
    except FileExistsError:
        receipt = ReleasePreparationReceipt(
            job_id=job.job_id,
            project_id=job.project_id,
            request_sha256=job.request_sha256,
            status="unknown",
            reason="release_preparation_interrupted",
            observed_at=datetime.now(UTC),
        )
        publish_new(spool / "receipts" / f"{job.job_id}.json", receipt.model_dump_json().encode())
        return receipt.model_dump(mode="json")
    status, reason = "failed", "release_preparation_failed"
    record = None
    try:
        transport = RunnerSpool(str(spool))
        qa = transport.read_receipt(job.qa_job.job_id)
        if qa is None or qa[1] != job.qa_receipt_sha256:
            raise ValueError("release_preparation_QA_changed")
        review = ExtensionQaReceipt.model_validate(qa[0])
        if (
            review.status != "passed"
            or review.request_sha256 != job.qa_job.request_sha256
            or review.candidate_commit != job.qa_job.candidate_commit
            or review.diff_sha256 != job.qa_job.diff_sha256
        ):
            raise ValueError("release_preparation_QA_changed")
        current, _ = onefoundry_update.read_install(settings.install_home)
        installed = release_bundle.validate_manifest(current)
        engineer = job.qa_job.engineer_job
        patch = transport.read_artifact(
            engineer.attempt_id,
            f"artifacts/{engineer.attempt_id}/change.diff",
            job.qa_job.diff_byte_size,
            job.qa_job.diff_sha256,
        )
        with tempfile.TemporaryDirectory(prefix="onefoundry-release-preparation-") as temporary:
            work = Path(temporary)
            repo = work / "repo"
            _, baseline = stage(engineer, settings.source_root, repo)
            if baseline != job.qa_job.baseline_commit:
                raise ValueError("release_preparation_baseline_changed")
            diff = work / "candidate.diff"
            diff.write_bytes(patch)
            deadline = Deadline(engineer.stage_limits.stage_seconds)
            _git(repo, deadline, "apply", "--index", str(diff))
            commit, observed = candidate(engineer, repo, baseline, deadline)
            if commit != job.qa_job.candidate_commit or observed != patch:
                raise ValueError("release_preparation_candidate_changed")
            _build_inputs(repo, baseline, deadline)
            version = release_bundle.source_version(repo / "onefoundry")
            if tuple(map(int, version.split("."))) <= tuple(map(int, str(installed["version"]).split("."))):
                raise ValueError("release_preparation_version_not_new")
            archive = builder(
                work / "output",
                repository=repo,
                revision=commit,
                timeout_seconds=settings.build_timeout_seconds,
            )
            checksum = release_bundle.sha256(archive)
            verified = onefoundry_update.unpack(archive, checksum, work / "verified")
            destination = settings.archive_root / f"{checksum}.tar.gz"
            # Packaging has no installation side effects. Persist the archive
            # before publishing the record that makes activation possible.
            with archive.open("rb") as source, destination.open("xb") as target:
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
            destination.chmod(0o640)
            release = ExtensionRelease(
                **_release_effects(job, settings),
                version=version,
                archive_sha256=checksum,
                app_image=release_bundle.runtime_image_id(verified),
                expected_installed_version=str(installed["version"]),
                expected_installed_image=release_bundle.runtime_image_id(installed),
            )
            record = record_release(spool, settings.archive_root, engineer, release)
            status, reason = "prepared", "completed"
    except (TimeoutError, subprocess.TimeoutExpired):
        status, reason = "unknown", "release_preparation_timeout"
    except (OSError, ValueError, RuntimeError, SpoolInvalid, subprocess.CalledProcessError) as exc:
        if type(exc) is ValueError and str(exc).startswith("release_preparation_"):
            reason = str(exc).lower()
    receipt = ReleasePreparationReceipt.model_validate(
        {
            "job_id": job.job_id,
            "project_id": job.project_id,
            "request_sha256": job.request_sha256,
            "status": status,
            "reason": reason,
            "record": record,
            "observed_at": datetime.now(UTC),
        }
    )
    publish_new(spool / "receipts" / f"{job.job_id}.json", receipt.model_dump_json().encode())
    return receipt.model_dump(mode="json")


def _release_effects(job: ReleasePreparationJob, settings: ReleasePreparationSettings) -> dict[str, Any]:
    value = job.request.model_dump()
    if settings.managed_source_alias is not None:
        from runner.extension_source_checkout import SOURCE_EFFECT

        if job.qa_job.engineer_job.source["relative_path"] != settings.managed_source_alias:
            raise ValueError("release_preparation_managed_source_differs")
        if SOURCE_EFFECT not in value["configuration_effects"]:
            value["configuration_effects"].append(SOURCE_EFFECT)
    return value
