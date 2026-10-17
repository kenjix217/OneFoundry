"""Offline supervisor routing and one-shot spool evidence without Docker."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest

from onefoundry.external_code_schemas import ExternalCodeReceipt
from runner import (
    claude_cli_admission,
    claude_workorder_fixture,
    codex_cli_admission,
    codex_workorder_fixture,
    docker_egress_proof,
)
from runner.cli_image_admission import DockerEndpoint
from runner.contract import ContractError
from runner.external_cli import AdapterId, CliRequest, CliResult
from runner.external_offline_supervisor import (
    OfflineSupervisorConfig,
    OfflineSupervisorError,
    acquire_supervisor,
    process_job,
    run_pending,
)
from runner.fixture_git import Deadline, _test
from runner.live_progress_spool import LiveProgressJournal, read_progress
from runner.provider_progress import LiveProgressParser

CODEX_IMAGE = "sha256:" + "a" * 64
CLAUDE_IMAGE = "sha256:" + "b" * 64
FIXTURE_IMAGE = "sha256:" + "c" * 64


def _prove_node_wrapper_streams_before_child_exit(tmp_path: Path, adapter: AdapterId, image_id: str) -> None:
    """Hold a fake CLI child open after one public event; inspect the live journal."""
    name = f"onefoundry-wrapper-latch-{uuid4().hex[:12]}"
    root = tmp_path / adapter
    seed, latch, spool = root / "seed", root / "latch", root / "spool"
    for directory in (seed, latch, spool / "claims"):
        directory.mkdir(parents=True)
    (seed / "calculator.py").write_text("def add_one(value: int) -> int:\n    return value\n")
    prompt = root / "prompt.txt"
    prompt.write_text("Repair the fixture")
    fake = root / "fake-cli"
    progress_event = (
        "{type:'item.completed',item:{type:'agent_message',text:'Checking the repair.'}}"
        if adapter == "codex_cli"
        else "{type:'assistant',message:{content:[{type:'text',text:'Checking the repair.'}]}}"
    )
    fake.write_text(
        "#!/usr/local/bin/node\n"
        "const fs = require('fs');\n"
        "fs.writeFileSync('/workspace/calculator.py',"
        " 'def add_one(value: int) -> int:\\n    return value + 1\\n');\n"
        f"process.stdout.write(JSON.stringify({progress_event}) + '\\n');\n"
        "const timer = setInterval(() => {\n"
        "  if (fs.existsSync('/latch/release')) {\n"
        "    clearInterval(timer); clearTimeout(watchdog); process.exitCode = 0;\n"
        "  }\n"
        "}, 20);\n"
        "const watchdog = setTimeout(() => {clearInterval(timer); process.exitCode = 3;}, 10000);\n"
    )
    fake.chmod(0o755)
    project_id, run_id, attempt_id = (str(uuid4()) for _ in range(3))
    request_sha256 = "a" * 64
    claim = spool / "claims" / f"{attempt_id}.claim"
    claim.write_text(attempt_id)
    claim.chmod(0o600)
    parser = LiveProgressParser(adapter)
    observed = Event()
    node_boot = (
        codex_workorder_fixture.NODE_BOOT.replace("/opt/codex-cli/node_modules/.bin/codex", "/opt/fake-cli")
        if adapter == "codex_cli"
        else claude_workorder_fixture.NODE_BOOT.replace(
            "/opt/claude-code/node_modules/.bin/claude", "/opt/fake-cli"
        )
    )
    home = "/home/codex" if adapter == "codex_cli" else "/home/claude"

    try:
        docker_egress_proof._docker(
            "run",
            "--detach",
            "--pull=never",
            "--name",
            name,
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--user=1000:1000",
            "--tmpfs",
            "/workspace:rw,nosuid,nodev,size=8m,nr_inodes=128,uid=1000,gid=1000",
            "--tmpfs",
            f"{home}:rw,nosuid,nodev,size=32m,uid=1000,gid=1000",
            "--mount",
            f"type=bind,src={seed},dst=/seed,readonly",
            "--mount",
            f"type=bind,src={latch},dst=/latch,readonly",
            "--mount",
            f"type=bind,src={prompt},dst=/prompt.txt,readonly",
            "--mount",
            f"type=bind,src={fake},dst=/opt/fake-cli,readonly",
            "--env",
            f"HOME={home}",
            "--env",
            f"CODEX_HOME={home}/.codex",
            "--env",
            "FIXTURE_IP=127.0.0.1",
            "--workdir=/",
            "--entrypoint=/usr/local/bin/node",
            image_id,
            "-e",
            node_boot,
        )
        with LiveProgressJournal.create(spool, project_id, run_id, attempt_id, request_sha256) as journal:

            def publish(chunk: bytes) -> None:
                for item in parser.feed(chunk):
                    journal.append(item)
                    observed.set()

            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    docker_egress_proof.bounded_logs,
                    name,
                    8192,
                    timeout=15,
                    follow=True,
                    on_stdout_chunk=publish,
                )
                assert observed.wait(6), "the wrapper held progress until its child exited"
                snapshot = read_progress(spool, project_id, run_id, attempt_id, request_sha256)
                assert snapshot.latest_sequence >= 1
                assert (
                    docker_egress_proof._docker("inspect", "--format", "{{.State.Running}}", name) == "true"
                )
                assert not (latch / "release").exists()
                (latch / "release").write_text("go")
                raw = future.result(timeout=10)
        assert b"workspace.result" in raw
        assert docker_egress_proof._inspect("container", name)["State"]["ExitCode"] == 0
    finally:
        (latch / "release").write_text("go")
        docker_egress_proof._remove("container", name)


def _unit_fixture_test(
    repo: Path, deadline: Deadline, log_limit: int, name: str, *, image_id: str
) -> tuple[dict[str, object], bytes]:
    """Use the fixture test double for supervisor routing tests without Docker."""
    return _test(repo, deadline, log_limit, name)


def _config(spool: Path) -> OfflineSupervisorConfig:
    for name in ("jobs", "claims", "receipts", "artifacts"):
        (spool / name).mkdir(parents=True, exist_ok=True)
    return OfflineSupervisorConfig(spool, FIXTURE_IMAGE, CODEX_IMAGE, CLAUDE_IMAGE)


def _job(spool: Path, adapter: str = "codex_cli") -> Path:
    attempt_id = str(uuid4())
    payload: dict[str, object] = {
        "schema_version": 1,
        "job_id": attempt_id,
        "project_id": str(uuid4()),
        "run_id": str(uuid4()),
        "attempt_id": attempt_id,
        "adapter_id": adapter,
        "fixture_id": "engineer_qa_v1",
        "stage": "engineer",
        "actor_id": f"profile:{uuid4()}",
        "task_title": "Repair the fixture",
        "task_instructions": "Make add_one add one and run the tests.",
        "limits": {"wall_seconds": 45, "max_log_bytes": 16384, "max_diff_bytes": 32768},
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["request_sha256"] = hashlib.sha256(encoded).hexdigest()
    path = spool / "jobs" / f"{attempt_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.parametrize("adapter", ["codex_cli", "claude_code"])
def test_routes_one_claim_and_receipt_with_exact_image_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, adapter: str
) -> None:
    config = _config(tmp_path / "external")
    job = _job(config.spool, adapter)
    expected_image = CODEX_IMAGE if adapter == "codex_cli" else CLAUDE_IMAGE
    calls: list[str] = []

    def invoke(request: CliRequest, image: str, fixture_image: str) -> CliResult:
        calls.append(request.adapter_id)
        assert image == expected_image and fixture_image == FIXTURE_IMAGE
        intent = json.loads((config.spool / "claims" / f"{job.stem}.launch.json").read_text())
        assert intent["executable_ref"] == expected_image
        assert intent["request_sha256"] == json.loads(job.read_text())["request_sha256"]
        assert intent["prompt_sha256"] == hashlib.sha256(request.prompt.encode()).hexdigest()
        assert not (config.spool / "receipts" / f"{job.stem}.json").exists()
        (request.repository / "calculator.py").write_text(
            "def add_one(value: int) -> int:\n    return value + 1\n"
        )
        if adapter == "codex_cli":
            log = (
                f"synthetic_cli=codex_cli model=gpt-5 command=fixture_calculator_repair "
                f"image_sha256={image} prompt_sha256={intent['prompt_sha256']} "
                "allowed_provider_requests=2 blocked_other_connects=0\n"
            )
        else:
            log = (
                "synthetic_cli=claude_code model=claude-sonnet-4-5 "
                f"command=fixture_calculator_repair image_sha256={image} "
                f"prompt_sha256={intent['prompt_sha256']} "
                "allowed_provider_requests=4 blocked_other_connects=0\n"
            )
        return CliResult("done", "provider_completed", "fixture-session", 7, None, log.encode())

    target = codex_workorder_fixture if adapter == "codex_cli" else claude_workorder_fixture
    monkeypatch.setattr(target, f"invoke_synthetic_{'codex' if adapter == 'codex_cli' else 'claude'}", invoke)
    monkeypatch.setattr(target, "run_isolated_fixture_test", _unit_fixture_test)
    # The supervisor imports the bridge function; the bridge resolves its invoker at call time.
    assert run_pending(config) == 1
    receipt = json.loads((config.spool / "receipts" / f"{job.stem}.json").read_text())
    ExternalCodeReceipt.model_validate(receipt)
    assert receipt["status"] == "done"
    assert receipt["adapter_id"] == adapter
    assert receipt["provider_session_id"] == "fixture-session"
    assert [item["kind"] for item in receipt["artifacts"]] == ["diff", "log", "trace"]
    assert [item["status"] for item in receipt["tests"]] == ["failed", "passed"]
    assert run_pending(config) == 0
    assert calls == [adapter]
    assert len(list((config.spool / "claims").glob("*.claim"))) == 1
    assert len(list((config.spool / "receipts").glob("*.json"))) == 1


def test_uncertain_invocation_has_terminal_unknown_and_no_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path / "external")
    job = _job(config.spool)
    calls = 0

    def uncertain(_request: CliRequest, _image: str, _fixture: str) -> CliResult:
        nonlocal calls
        calls += 1
        raise OSError("fixture connection lost")

    monkeypatch.setattr(codex_workorder_fixture, "invoke_synthetic_codex", uncertain)
    monkeypatch.setattr(codex_workorder_fixture, "run_isolated_fixture_test", _unit_fixture_test)
    assert run_pending(config) == 1
    receipt = json.loads((config.spool / "receipts" / f"{job.stem}.json").read_text())
    assert receipt["status"] == "unknown"
    assert receipt["reason_code"] == "synthetic_codex_launch_uncertain"
    assert (config.spool / "claims" / f"{job.stem}.launch.json").exists()
    assert (config.spool / "claims" / f"{job.stem}.session.json").exists()
    assert run_pending(config) == 0
    assert calls == 1


def test_invalid_manifest_and_unconfigured_adapter_never_claim(tmp_path: Path) -> None:
    config = _config(tmp_path / "external")
    job = _job(config.spool)
    payload = json.loads(job.read_text())
    payload["task_instructions"] = "tampered"
    job.write_text(json.dumps(payload))
    with pytest.raises(ContractError, match="external_request_digest_mismatch"):
        process_job(job, config)
    assert run_pending(config) == 0
    assert not list((config.spool / "claims").iterdir())

    job.unlink()
    claude_job = _job(config.spool, "claude_code")
    codex_only = OfflineSupervisorConfig(config.spool, FIXTURE_IMAGE, CODEX_IMAGE)
    with pytest.raises(OfflineSupervisorError, match="offline_adapter_unconfigured"):
        process_job(claude_job, codex_only)
    assert run_pending(codex_only) == 0
    assert not list((config.spool / "claims").iterdir())


def test_spool_and_image_boundaries_fail_before_launch(tmp_path: Path) -> None:
    config = _config(tmp_path / "external")
    job = _job(config.spool)
    outside = tmp_path / "outside.json"
    outside.write_bytes(job.read_bytes())
    with pytest.raises(OfflineSupervisorError, match="offline_job_path_invalid"):
        process_job(outside, config)
    with pytest.raises(OfflineSupervisorError, match="offline_supervisor_config_invalid"):
        process_job(job, OfflineSupervisorConfig(config.spool, "fixture:latest", CODEX_IMAGE))
    (config.spool / "claims").rmdir()
    (config.spool / "claims").symlink_to(config.spool / "receipts", target_is_directory=True)
    with pytest.raises(OfflineSupervisorError, match="offline_supervisor_spool_invalid"):
        process_job(job, config)
    assert not list((config.spool / "receipts").iterdir())


def test_only_one_supervisor_can_lock_spool(tmp_path: Path) -> None:
    config = _config(tmp_path / "external")
    descriptor = acquire_supervisor(config)
    try:
        with pytest.raises(OfflineSupervisorError, match="offline_supervisor_already_running"):
            acquire_supervisor(config)
    finally:
        os.close(descriptor)


@pytest.mark.integration
def test_pinned_containers_complete_both_offline_jobs(tmp_path: Path) -> None:
    """Exercise the supervisor through both real CLI images and fake TLS providers."""
    executable = shutil.which("docker")
    assert executable is not None
    endpoint = DockerEndpoint(Path(executable).resolve(), Path("/var/run/docker.sock"))
    fixture_tag = os.environ["ONEFOUNDRY_EGRESS_TEST_IMAGE"]
    fixture_image = docker_egress_proof._inspect("image", fixture_tag)["Id"]
    assert isinstance(fixture_image, str)
    codex = codex_cli_admission.build_and_prove(endpoint)
    try:
        claude = claude_cli_admission.build_and_prove(endpoint)
        try:
            config = OfflineSupervisorConfig(
                tmp_path / "external", fixture_image, codex["image_id"], claude["image_id"]
            )
            for name in ("jobs", "claims", "receipts", "artifacts"):
                (config.spool / name).mkdir(parents=True, exist_ok=True)
            jobs = [_job(config.spool, adapter) for adapter in ("codex_cli", "claude_code")]
            assert run_pending(config) == 2
            assert run_pending(config) == 0
            for job in jobs:
                manifest = json.loads(job.read_text())
                progress = read_progress(
                    config.spool,
                    manifest["project_id"],
                    manifest["run_id"],
                    manifest["attempt_id"],
                    manifest["request_sha256"],
                )
                assert progress.latest_sequence > 0
                receipt = ExternalCodeReceipt.model_validate_json(
                    (config.spool / "receipts" / f"{job.stem}.json").read_bytes()
                )
                assert receipt.status == "done"
                assert [(item.name, item.status) for item in receipt.tests] == [
                    ("before", "failed"),
                    ("after", "passed"),
                ]
                assert receipt.provider_session_id is not None
                assert receipt.cost_usd is None
            _prove_node_wrapper_streams_before_child_exit(tmp_path, "codex_cli", codex["image_id"])
            _prove_node_wrapper_streams_before_child_exit(tmp_path, "claude_code", claude["image_id"])
        finally:
            claude_cli_admission._remove_tag_if_present(endpoint, claude["image_tag"])
    finally:
        codex_cli_admission._remove_tag_if_present(endpoint, codex["image_tag"])
