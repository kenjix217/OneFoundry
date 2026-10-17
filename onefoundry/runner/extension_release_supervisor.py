"""Separate, one-shot supervisor for exact approved prebuilt release archives.

Uses the existing install/checkpoint/rollback owner. Host paths are supplied by
the operator's supervisor configuration, never by a model or job manifest.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from onefoundry.extension_activation_contract import (
    ExtensionActivationJob,
    ExtensionActivationReceipt,
    approval_matches,
)
from onefoundry.runner_spool import RunnerSpool
from runner.extension_source_checkout import SourceCheckout
from runner.extension_source_checkout import prepare as prepare_source
from runner.fixture_git import Deadline, ExecutionError
from runner.repository_transport import publish_new
from scripts import onefoundry_update as installer
from scripts.release_bundle import runtime_image_id, validate_manifest


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


@dataclass(frozen=True)
class ReleaseSupervisorConfig:
    install_home: Path
    archive_root: Path
    health_timeout_seconds: int
    source_root: Path | None = None
    managed_source_alias: str | None = None

    def validate(self) -> None:
        if not 1 <= self.health_timeout_seconds <= 600:
            raise ValueError("activation_health_timeout_invalid")
        for path in (self.install_home, self.archive_root):
            if not path.is_absolute() or path.resolve() != path or path.is_symlink() or not path.is_dir():
                raise ValueError("activation_supervisor_path_invalid")


Readback = Callable[[ExtensionActivationJob], bool]


def process_activation(
    path: Path,
    spool: Path,
    config: ReleaseSupervisorConfig,
    *,
    capability_readback: Readback,
    quiescent: Callable[[], bool],
    approval_current: Readback,
    acquire_maintenance: Readback | None = None,
    release_maintenance: Readback | None = None,
    configure_role_grants: Readback | None = None,
) -> dict[str, Any] | None:
    """Quiesce before claiming; any claimed uncertain activation is never retried."""
    config.validate()
    if path.parent != spool / "jobs" or path.is_symlink():
        raise ValueError("activation_job_path_invalid")
    job = ExtensionActivationJob.model_validate_json(RunnerSpool._read_regular(path, 8192))
    if path.name != f"{job.job_id}.json":
        raise ValueError("activation_job_identity_invalid")
    saved = RunnerSpool(str(spool)).read_receipt(job.job_id)
    if saved is not None:
        prior = ExtensionActivationReceipt.model_validate(saved[0])
        if prior.proposal_digest != job.proposal_digest or prior.project_id != job.project_id:
            raise ValueError("activation prior receipt identity changed")
        if prior.status != "unknown" and release_maintenance is not None:
            release_maintenance(job)
        return None
    if not approval_current(job) or not quiescent():
        return None
    if acquire_maintenance is not None and not acquire_maintenance(job):
        return None
    try:
        publish_new(spool / "claims" / f"{job.job_id}.claim", str(job.job_id).encode())
    except FileExistsError:
        return None
    status, reason = "failed", "activation_preflight_failed"
    version: str | None = None
    image: str | None = None
    checkpoint_saved = False
    ready = False
    started = False
    source: SourceCheckout | None = None
    try:
        previous, _ = installer.read_install(config.install_home)
        previous_manifest = validate_manifest(previous)
        previous_image = runtime_image_id(previous_manifest)
        if (
            previous_manifest["version"] != job.release.expected_installed_version
            or previous_image != job.release.expected_installed_image
        ):
            ready = False
            raise ValueError("activation_installed_base_changed")
        # The archive name is content-addressed; existing installer validates all
        # entries, file hashes and images before it changes the current pointer.
        archive = config.archive_root / f"{job.release.archive_sha256}.tar.gz"
        if archive.is_symlink() or not archive.is_file():
            raise ValueError("activation_archive_unavailable")
        with tempfile.TemporaryDirectory(prefix="onefoundry-activation-check-") as temporary:
            candidate = installer.unpack(archive, job.release.archive_sha256, Path(temporary) / "release")
        if (
            candidate["version"] != job.release.version
            or candidate["commit"] != job.candidate_commit
            or runtime_image_id(candidate) != job.release.app_image
        ):
            raise ValueError("activation_package_candidate_mismatch")
        arguments = argparse.Namespace(
            home=config.install_home,
            archive=archive,
            sha256=job.release.archive_sha256,
            health_timeout_seconds=config.health_timeout_seconds,
        )
        if config.managed_source_alias is not None:
            if config.source_root is None:
                raise ValueError("activation_maintenance_source_unconfigured")
            source = prepare_source(job, spool, config.source_root, config.managed_source_alias)
        started = True
        installer.update(arguments)
        current, _ = installer.read_install(config.install_home)
        installed = validate_manifest(current)
        version = str(installed["version"])
        image = runtime_image_id(installed)
        configuration_ready = not job.tool_grants
        if version == job.release.version and image == job.release.app_image:
            publish_new(
                spool / "claims" / f"{job.job_id}.installed.json",
                json.dumps(
                    {
                        "job_id": str(job.job_id),
                        "proposal_digest": job.proposal_digest,
                        "version": version,
                        "image": image,
                    },
                    sort_keys=True,
                ).encode(),
            )
            if job.tool_grants and configure_role_grants is not None:
                try:
                    configuration_ready = configure_role_grants(job)
                except (OSError, ValueError, RuntimeError, ExecutionError):
                    configuration_ready = False
        rollback = json.loads((config.install_home / "data/updates/rollback.json").read_text())
        checkpoint_saved = (
            rollback.get("state") == "applied"
            and rollback.get("previous") == job.release.expected_installed_version
        )
        try:
            if configuration_ready and source is not None:
                source.advance()
            ready = configuration_ready and capability_readback(job)
        except (OSError, ValueError, RuntimeError, ExecutionError):
            ready = False
        if (
            version != job.release.version
            or image != job.release.app_image
            or not checkpoint_saved
            or not ready
        ):
            installer.rollback(
                argparse.Namespace(
                    home=config.install_home,
                    restore_data=True,
                    yes=True,
                    health_timeout_seconds=config.health_timeout_seconds,
                )
            )
            restored, _ = installer.read_install(config.install_home)
            restored_manifest = validate_manifest(restored)
            if source is not None:
                source.restore()
            version = str(restored_manifest["version"])
            image = runtime_image_id(restored_manifest)
            status, reason = "failed", "activation_owner_readback_failed_restored"
        else:
            status, reason = "activated", "completed"
    except (OSError, ValueError, RuntimeError, ExecutionError):
        status, reason = (
            ("unknown", "activation_outcome_unknown")
            if started
            else ("failed", "activation_preflight_failed")
        )
    receipt = ExtensionActivationReceipt.model_validate(
        {
            "job_id": job.job_id,
            "project_id": job.project_id,
            "proposal_digest": job.proposal_digest,
            "status": status,
            "reason": reason,
            "installed_version": version,
            "installed_image": image,
            "capability_ready": ready,
            "checkpoint_saved": checkpoint_saved,
            "observed_at": datetime.now(UTC),
        }
    )
    value = receipt.model_dump(mode="json")
    publish_new(spool / "receipts" / f"{job.job_id}.json", json.dumps(value, sort_keys=True).encode())
    if status != "unknown" and release_maintenance is not None:
        release_maintenance(job)
    return value


def process_online_activation(
    path: Path, spool: Path, config: ReleaseSupervisorConfig, origin: str
) -> dict[str, Any] | None:
    args = argparse.Namespace(
        job=path,
        spool=spool,
        install_home=config.install_home,
        archive_root=config.archive_root,
        health_timeout_seconds=config.health_timeout_seconds,
        origin=origin,
    )
    parsed = urlsplit(args.origin)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("origin must be an exact localhost HTTP origin")
    job = ExtensionActivationJob.model_validate_json(RunnerSpool._read_regular(args.job, 8192))
    opener = build_opener(ProxyHandler({}), NoRedirect())

    def get(route: str) -> Any:
        with opener.open(args.origin + route, timeout=args.health_timeout_seconds) as response:  # noqa: S310 - exact validated loopback origin
            raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError("activation_readback_exceeds_bound")
            return json.loads(raw)

    def post(route: str, body: dict[str, str]) -> Any:
        request = Request(  # noqa: S310 - exact validated loopback origin
            args.origin + route,
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "Origin": args.origin},
        )
        with opener.open(request, timeout=args.health_timeout_seconds) as response:  # noqa: S310 - exact validated loopback origin
            return json.loads(response.read(4097))

    def ready(value: ExtensionActivationJob) -> bool:
        report = get(
            f"/api/projects/{value.project_id}/work-orders/{value.original_work_order_id}/team-session/readiness"
        )
        checks = report.get("capability_readiness", {}).get("checks", [])
        return any(
            check.get("operation") == value.operation
            and check.get("source_id") == (str(value.gap_source_id) if value.gap_source_id else None)
            and check.get("status") == "ready"
            for check in checks
        )

    def approved(value: ExtensionActivationJob) -> bool:
        from runner.extension_approval_readback import matches_approval

        deadline = Deadline(args.health_timeout_seconds)
        route = f"/api/projects/{value.project_id}/approvals?status=approved"
        with opener.open(args.origin + route, timeout=deadline.remaining()) as response:  # noqa: S310 - exact validated loopback origin
            return matches_approval(
                response.read,
                lambda item: approval_matches(value, item),
                record_limit=65536,
                remaining=deadline.remaining,
            )

    receipt = process_activation(
        args.job,
        args.spool,
        config,
        capability_readback=ready,
        approval_current=approved,
        acquire_maintenance=lambda value: (
            post(
                f"/api/projects/{value.project_id}/extension-activation/{value.approval_id}/quiesce",
                {"proposal_digest": value.proposal_digest},
            ).get("acquired")
            is True
        ),
        release_maintenance=lambda value: (
            post(
                f"/api/projects/{value.project_id}/extension-activation/{value.approval_id}/release", {}
            ).get("released")
            is True
        ),
        configure_role_grants=lambda value: (
            post(
                f"/api/projects/{value.project_id}/extension-activation/{value.approval_id}/configure", {}
            ).get("configured")
            is True
        ),
        quiescent=lambda: (
            get(f"/api/projects/{job.project_id}/extension-activation/preflight").get("quiescent") is True
        ),
    )
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply one exact approved OneFoundry extension release")
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--spool", type=Path, required=True)
    parser.add_argument("--install-home", type=Path, required=True)
    parser.add_argument("--archive-root", type=Path, required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--health-timeout-seconds", type=int, required=True)
    args = parser.parse_args()
    receipt = process_online_activation(
        args.job,
        args.spool,
        ReleaseSupervisorConfig(args.install_home, args.archive_root, args.health_timeout_seconds),
        args.origin,
    )
    print(json.dumps(receipt if receipt is not None else {"status": "not_claimed"}))


if __name__ == "__main__":
    main()
