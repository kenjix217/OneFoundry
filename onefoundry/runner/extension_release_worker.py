"""Separate host packaging/activation worker; coding runs in its own supervisor."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, field_validator

from onefoundry.extension_release_capability import ReleaseWorkerCapability
from onefoundry.runner_spool import RunnerSpool, SpoolInvalid
from onefoundry.workflow_schemas import StrictModel
from runner.capability_setup_settings import HostSetupSettings
from runner.capability_setup_worker import SetupHttp, process_setup
from runner.extension_release_prepare import ReleasePreparationSettings, process_preparation
from runner.extension_release_supervisor import ReleaseSupervisorConfig, process_online_activation
from runner.repository_transport import publish_replace
from scripts import onefoundry_update as installer
from scripts.release_bundle import runtime_image_id, validate_manifest


class ReleaseWorkerSettings(StrictModel):
    spool: Path
    origin: str
    preparation: ReleasePreparationSettings
    health_timeout_seconds: int = Field(ge=1, le=600)
    poll_seconds: float = Field(ge=0.1, le=10)
    capability_refresh_seconds: int = Field(ge=1, le=10)
    setup: HostSetupSettings | None = None

    @field_validator("origin")
    @classmethod
    def loopback(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("release worker requires an exact loopback HTTP origin")
        return value

    def validate_host(self) -> None:
        self.preparation.validate_host()
        if (
            not self.spool.is_absolute()
            or self.spool.resolve() != self.spool
            or self.spool.is_symlink()
            or not self.spool.is_dir()
        ):
            raise ValueError("release worker spool unavailable")


def pending(config: ReleaseWorkerSettings) -> int:
    count = 0
    activation = ReleaseSupervisorConfig(
        config.preparation.install_home,
        config.preparation.archive_root,
        config.health_timeout_seconds,
        config.preparation.source_root,
        config.preparation.managed_source_alias,
    )
    for path in sorted((config.spool / "jobs").glob("*.json")):
        try:
            value = json.loads(RunnerSpool._read_regular(path, 32768))
            if not isinstance(value, dict):
                continue
            if value.get("kind") == "extension_release_prepare_v1":
                result = process_preparation(path, config.spool, config.preparation)
            elif value.get("kind") == "extension_activation_v1":
                # The activation owner also reconciles a saved receipt/lease;
                # never skip it just because its claim exists.
                result = process_online_activation(path, config.spool, activation, config.origin)
            elif value.get("kind") == "capability_setup_host_v1" and config.setup is not None:
                result = process_setup(
                    path,
                    config.spool,
                    config.preparation.install_home,
                    config.setup,
                    SetupHttp(config.origin, config.health_timeout_seconds),
                )
            else:
                continue
            count += result is not None
        except FileNotFoundError:
            continue
        except (ValueError, OSError, RuntimeError, SpoolInvalid):
            print(
                json.dumps(
                    {
                        "status": "blocked",
                        "reason": "release_job_admission_or_owner_failed",
                        "job_file": path.name,
                    }
                ),
                flush=True,
            )
    return count


def run(config: ReleaseWorkerSettings, *, once: bool = False) -> None:
    config.validate_host()
    descriptor = os.open(config.spool / "release-worker.lock", os.O_CREAT | os.O_NOFOLLOW | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("release worker already owns this transport") from None
        _run(config, once=once)
    finally:
        os.close(descriptor)


def _run(config: ReleaseWorkerSettings, *, once: bool) -> None:
    stopped = threading.Event()
    capability = config.spool / "release_capabilities.json"
    setup_capability = config.spool / "setup_capabilities.json"

    def publish() -> None:
        config.validate_host()
        installed, _ = installer.read_install(config.preparation.install_home)
        manifest = validate_manifest(installed)
        publish_replace(
            capability,
            ReleaseWorkerCapability(
                observed_at=datetime.now(UTC),
                installed_version=str(manifest["version"]),
                installed_commit=str(manifest["commit"]),
                installed_image=runtime_image_id(manifest),
            )
            .model_dump_json()
            .encode(),
        )
        if config.setup is not None:
            publish_replace(setup_capability, config.setup.catalog().model_dump_json().encode())

    def heartbeat() -> None:
        while not stopped.wait(config.capability_refresh_seconds):
            try:
                publish()
            except (ValueError, OSError, subprocess.SubprocessError):
                capability.unlink(missing_ok=True)
                setup_capability.unlink(missing_ok=True)
                stopped.set()

    publish()
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        while not stopped.is_set():
            pending(config)
            if once:
                break
            stopped.wait(config.poll_seconds)
    finally:
        stopped.set()
        thread.join(timeout=config.capability_refresh_seconds)
        capability.unlink(missing_ok=True)
        setup_capability.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    info = args.config.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        parser.error("release worker config must be an owned mode-0600 regular file")
    config = ReleaseWorkerSettings.model_validate_json(RunnerSpool._read_regular(args.config, 8192))
    run(config, once=args.once)


if __name__ == "__main__":
    main()
