"""Apply approved named read-only mounts through the installed configuration owner."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener
from uuid import UUID

from onefoundry.capability_setup_host_contract import HostSetupJob, HostSetupReceipt, RepositoryOffer
from onefoundry.repository_code_contract import digest
from onefoundry.runner_spool import RunnerSpool
from runner.capability_setup_settings import HostSetupSettings
from runner.extension_release_supervisor import NoRedirect
from runner.repository_transport import publish_new
from runner.service import _atomic_bytes
from scripts import onefoundry_update as installer
from scripts.operator_config import configured_values


def write_mount(home: Path, settings: HostSetupSettings, selected: RepositoryOffer) -> None:
    configured = next(r for r in settings.repositories if r.alias == selected.alias)
    configured.validate_host()
    path = home / "data/config/local-sources.compose.json"
    existing = installer.local_sources_override(home)
    value = json.loads(path.read_text()) if existing else {"services": {"app": {"volumes": []}}}
    root = configured_values(home / "data/config/.env", ("LOCAL_SOURCE_ROOT",))["LOCAL_SOURCE_ROOT"]
    if not root:
        raise ValueError("setup_source_root_unconfigured")
    mount = {
        "type": "bind",
        "source": str(configured.path),
        "target": root + "/" + selected.alias,
        "read_only": True,
    }
    volumes = value["services"]["app"]["volumes"]
    matches = [m for m in volumes if m["source"] == mount["source"] or m["target"] == mount["target"]]
    if matches and matches != [mount]:
        raise ValueError("setup_existing_mount_changed")
    if not matches:
        volumes.append(mount)
    _atomic_bytes(path, json.dumps(value, sort_keys=True).encode(), mode=0o600)
    installer.local_sources_override(home)


class SetupHttp:
    def __init__(self, origin: str, timeout: int) -> None:
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("setup origin must be an exact loopback HTTP origin")
        self.origin, self.timeout = origin, timeout
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def post(self, job: HostSetupJob, action: str) -> dict[str, Any]:
        request = Request(  # noqa: S310 - validated loopback origin, internal route
            self.origin + f"/api/projects/{job.project_id}/capability-setup/{job.job_id}/{action}",
            data=json.dumps({"proposal_digest": job.proposal_digest} if action != "release" else {}).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "Origin": self.origin},
        )
        with self.opener.open(request, timeout=self.timeout) as response:  # noqa: S310 - validated loopback
            raw = response.read(4097)
        if len(raw) > 4096:
            raise ValueError("setup_response_too_large")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("setup_response_invalid")
        return value


def process_setup(
    path: Path, spool: Path, home: Path, settings: HostSetupSettings, http: SetupHttp
) -> dict[str, Any] | None:
    if path.parent != spool / "jobs" or path.is_symlink():
        raise ValueError("setup_job_path_invalid")
    UUID(path.stem)
    descriptor = os.open(
        spool / "claims" / f"{path.stem}.setup-lock", os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600
    )
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        return _process_setup(path, spool, home, settings, http)
    finally:
        os.close(descriptor)


def _process_setup(
    path: Path, spool: Path, home: Path, settings: HostSetupSettings, http: SetupHttp
) -> dict[str, Any] | None:
    job = HostSetupJob.model_validate_json(RunnerSpool._read_regular(path, 32768))
    if (
        path.name != f"{job.job_id}.json"
        or digest({"binding": job.binding, "target": job.target, "content": job.content})
        != job.proposal_digest
    ):
        raise ValueError("setup_job_identity_invalid")
    saved = RunnerSpool(str(spool)).read_receipt(job.job_id)
    if saved is not None:
        prior = HostSetupReceipt.model_validate(saved[0])
        if (
            prior.job_id != job.job_id
            or prior.project_id != job.project_id
            or prior.proposal_digest != job.proposal_digest
        ):
            raise ValueError("setup_saved_identity_changed")
        if prior.status != "unknown":
            http.post(job, "release")
        return None
    selected = RepositoryOffer.model_validate(job.binding["source"])
    request = job.binding["request"]
    if (
        selected.project_id != job.project_id
        or request["repository_alias"] != selected.alias
        or request["revision"] != selected.revision
    ):
        raise ValueError("setup_offer_identity_invalid")
    if http.post(job, "quiesce").get("acquired") is not True:
        return None
    try:
        publish_new(spool / "claims" / f"{job.job_id}.claim", job.proposal_digest.encode())
    except FileExistsError:
        receipt = HostSetupReceipt(
            job_id=job.job_id,
            project_id=job.project_id,
            proposal_digest=job.proposal_digest,
            status="unknown",
            reason="setup_interrupted_reconciliation_required",
        )
    else:
        receipt = _apply(job, selected, home, settings, http)
    result = receipt.model_dump(mode="json")
    publish_new(spool / "receipts" / f"{job.job_id}.json", json.dumps(result, sort_keys=True).encode())
    if receipt.status != "unknown":
        http.post(job, "release")
    return result


def _apply(
    job: HostSetupJob, selected: RepositoryOffer, home: Path, settings: HostSetupSettings, http: SetupHttp
) -> HostSetupReceipt:
    receipt = HostSetupReceipt(
        job_id=job.job_id,
        project_id=job.project_id,
        proposal_digest=job.proposal_digest,
        status="failed",
        reason="setup_preflight_failed",
    )
    started = False
    try:
        if selected not in settings.catalog().repositories:
            raise ValueError("setup_selected_repository_changed")
        release, installation = installer.read_install(home)
        path = home / "data/config/local-sources.compose.json"
        installer.local_sources_override(home)
        prior = path.read_bytes() if path.exists() else None
        started = True
        try:
            write_mount(home, settings, selected)
            installer.compose(
                home,
                release,
                installation["project"],
                "up",
                "-d",
                "--no-build",
                "--pull",
                "never",
                "--no-deps",
                "app",
                timeout=settings.restart_timeout_seconds,
            )
            installer.health(home, release, installation["project"], settings.restart_timeout_seconds)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            if prior is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_bytes(path, prior, mode=0o600)
            installer.compose(
                home,
                release,
                installation["project"],
                "up",
                "-d",
                "--no-build",
                "--pull",
                "never",
                "--no-deps",
                "app",
                timeout=settings.restart_timeout_seconds,
            )
            installer.health(home, release, installation["project"], settings.restart_timeout_seconds)
            receipt.reason = "setup_failed_previous_mounts_restored"
            return receipt
        registered = http.post(job, "register")
        if registered.get("revision") != selected.revision:
            raise ValueError("setup_registration_mismatch")
        receipt.source_id = UUID(registered["source_id"])
        receipt.status, receipt.reason = "configured", "setup_source_registered"
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
        if started:
            receipt.status, receipt.reason = "unknown", "setup_outcome_unknown"
    return receipt
