"""Separate maintenance entrypoint: prove admission, then consume exact jobs once."""

from __future__ import annotations

import argparse
import json
import os
import stat
import threading
from datetime import UTC, datetime
from pathlib import Path

from onefoundry.repository_code_contract import RepositoryCodeManifest, RepositoryRunnerCapability
from onefoundry.runner_spool import RunnerSpool, SpoolInvalid
from runner.extension_qa_service import process_qa
from runner.repository_code_service import process_job
from runner.repository_codex_bridge import RepositoryCodexBridge
from runner.repository_runner_settings import RepositoryRunnerSettings
from runner.repository_transport import publish_replace


def load_config(path: Path) -> RepositoryRunnerSettings:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("maintenance runner config must be a regular mode 0600 file")
    return RepositoryRunnerSettings.model_validate_json(RunnerSpool._read_regular(path, 8192))


def pending(config: RepositoryRunnerSettings, bridge: RepositoryCodexBridge) -> int:
    count = 0
    for path in sorted((config.spool / "jobs").glob("*.json")):
        if (config.spool / "claims" / f"{path.stem}.claim").exists():
            continue
        try:
            raw = RunnerSpool._read_regular(path, 32768)
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("maintenance job must be an object")
            if value.get("target_id") != "repository_code_v1" and value.get("kind") != "extension_qa_v1":
                continue
            if len(raw) > 8192:
                raise ValueError("maintenance job exceeds its contract bound")
            if value.get("target_id") == "repository_code_v1":
                job = RepositoryCodeManifest.model_validate(value)
                bridge.validate_job(job)
                result = process_job(path, config.spool, config.source_root, bridge)
            elif value.get("kind") == "extension_qa_v1":
                result = process_qa(path, config.spool, config.source_root)
            else:
                continue
            count += result is not None
        except FileNotFoundError:
            continue  # A canceled unclaimed dispatch may disappear after the scan.
        except (OSError, ValueError, SpoolInvalid):
            print(
                json.dumps({"status": "not_launched", "reason": "maintenance_job_admission_failed"}),
                flush=True,
            )
    return count


def run(config: RepositoryRunnerSettings, *, once: bool) -> None:
    config.validate_host()
    bridge = RepositoryCodexBridge(config)
    if config.qa_image is not None:
        from runner.docker_egress_proof import _inspect

        if _inspect("image", config.qa_image)["Id"] != config.qa_image:
            raise ValueError("maintenance QA image is unavailable")
    proof = bridge.prove()  # No provider request; failure publishes no capability.
    stopped = threading.Event()

    def publish() -> None:
        config.validate_host()
        report = RepositoryRunnerCapability(
            observed_at=datetime.now(UTC),
            isolated=True,
            provider_only_egress=True,
            credentials_separate=True,
            receipts_separate=True,
            source_root_ready=True,
            adapter_id="codex_cli",
            model_id=config.model_id,
            provider_url=config.provider_url,
            image_id=config.cli_image,
            qa_image=config.qa_image,
            proof_sha256=proof,
        )
        publish_replace(config.spool / "repository_capabilities.json", report.model_dump_json().encode())

    def heartbeat() -> None:
        while not stopped.wait(config.capability_refresh_seconds):
            try:
                publish()
            except (OSError, ValueError):
                (config.spool / "repository_capabilities.json").unlink(missing_ok=True)
                stopped.set()

    publish()
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        while not stopped.is_set():
            pending(config, bridge)
            if once:
                break
            stopped.wait(config.poll_seconds)
    finally:
        stopped.set()
        thread.join(timeout=config.capability_refresh_seconds)
        (config.spool / "repository_capabilities.json").unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Isolated exact-source OneFoundry maintenance runner")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    run(load_config(args.config), once=args.once)


if __name__ == "__main__":
    main()
