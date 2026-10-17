"""End-to-end, networkless external runner fixture checks with fake CLIs."""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from onefoundry.action_trace_contract import SavedActionTrace
from onefoundry.external_code_schemas import ExternalCodeReceipt
from runner.action_trace import ActionTrace
from runner.contract import ContractError
from runner.external_cli import CliRequest, CliResult, run_cli
from runner.external_service import process_external_job, write_capabilities
from runner.fixture_git import TRACE_OBSERVER, Deadline, ExecutionError, _run, _test
from runner.live_progress_spool import read_progress
from runner.provider_progress import (
    AGENT_UPDATE_TEXT,
    STAGE_BASELINE_FAILED,
    STAGE_CLI_FINISHED,
    STAGE_TEST_PASSED,
    ProviderProgress,
)


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
        "limits": {"wall_seconds": 15, "max_log_bytes": 16384, "max_diff_bytes": 32768},
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["request_sha256"] = hashlib.sha256(encoded).hexdigest()
    path = spool / "jobs" / f"{attempt_id}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _fake_cli(tmp_path: Path, adapter: str) -> Path:
    script = tmp_path / "fake-coding-cli"
    event = (
        (
            "{'type':'thread.started','thread_id':'fake-codex'},"
            "{'type':'turn.completed','usage':{'input_tokens':2,'output_tokens':3}}"
        )
        if adapter == "codex_cli"
        else (
            "{'type':'system','session_id':'fake-claude'},"
            "{'type':'result','subtype':'success','is_error':False,'session_id':'fake-claude',"
            "'usage':{'input_tokens':2,'output_tokens':3},'total_cost_usd':0.01}"
        )
    )
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "assert 'Repair the fixture' in sys.stdin.read()\n"
        "path = pathlib.Path.cwd() / 'calculator.py'\n"
        "path.write_text(path.read_text().replace('return value\\n', 'return value + 1\\n'))\n"
        f"for event in ({event}): print(json.dumps(event))\n"
    )
    script.chmod(0o700)
    return script


@pytest.mark.parametrize("adapter", ["codex_cli", "claude_code"])
def test_fake_provider_repairs_disposable_fixture_and_receipt_is_single_use(
    tmp_path: Path,
    adapter: str,
) -> None:
    spool = tmp_path / "external"
    path = _job(spool, adapter)
    executable = _fake_cli(tmp_path, adapter)
    assert process_external_job(path, spool) is None
    assert not (spool / "claims").exists()
    receipt = process_external_job(
        path,
        spool,
        executable=executable,
        invoke=lambda request: run_cli(request, environment={"PATH": "/usr/bin:/bin"}),
        test_runner=_test,
    )
    assert receipt is not None
    assert receipt["status"] == "done"
    ExternalCodeReceipt.model_validate(receipt)
    assert receipt["branch"] == f"fixture/repair-{receipt['attempt_id']}"
    assert receipt["tokens_used"] == 5
    assert [(test["name"], test["status"]) for test in receipt["tests"]] == [
        ("before", "failed"),
        ("after", "passed"),
    ]
    assert receipt["cost_usd"] == (None if adapter == "codex_cli" else 0.01)
    assert [item["kind"] for item in receipt["artifacts"]] == ["diff", "log", "trace"]
    for artifact in receipt["artifacts"]:
        data = (spool / artifact["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == artifact["sha256"]
    assert b"+    return value + 1" in (spool / receipt["artifacts"][0]["path"]).read_bytes()
    after_log = (spool / receipt["artifacts"][1]["path"]).read_bytes()
    assert b"Ran 1 test" in after_log and b"OK" in after_log
    assert f"adapter={adapter} outcome=provider_completed".encode() in after_log
    assert b"fixture_test=passed" not in after_log
    trace = json.loads((spool / receipt["artifacts"][2]["path"]).read_text())
    assert trace["coverage"] == "partial"
    assert trace["provider_tool_calls_captured"] is False
    assert [action["kind"] for action in trace["actions"]].count("provider_invocation") == 1
    assert any(action["kind"] == "command" and action["exit_code"] == 1 for action in trace["actions"])
    assert any(action["command"][:2] == ["python", "-m"] for action in trace["actions"])
    assert (
        process_external_job(
            path, spool, executable=executable, invoke=lambda _: pytest.fail("retried"), test_runner=_test
        )
        is None
    )


def test_fake_invoker_publishes_claim_bound_progress_before_receipt(tmp_path: Path) -> None:
    spool = tmp_path / "external"
    path = _job(spool)
    manifest = json.loads(path.read_text())
    observed_before_receipt: list[int] = []
    progress = ProviderProgress("agent_update", AGENT_UPDATE_TEXT)

    def invoke(request: CliRequest) -> CliResult:
        assert request.on_progress is not None
        request.on_progress(progress)
        snapshot = read_progress(
            spool,
            manifest["project_id"],
            manifest["run_id"],
            manifest["attempt_id"],
            manifest["request_sha256"],
        )
        observed_before_receipt.append(snapshot.latest_sequence)
        assert not (spool / "receipts" / f"{manifest['attempt_id']}.json").exists()
        calculator = request.repository / "calculator.py"
        calculator.write_text(calculator.read_text().replace("return value\n", "return value + 1\n"))
        return CliResult(
            "done",
            "provider_completed",
            "fake-session",
            5,
            None,
            b"adapter=codex_cli outcome=provider_completed\n",
            (progress,),
            0,
        )

    receipt = process_external_job(
        path,
        spool,
        executable=_fake_cli(tmp_path, "codex_cli"),
        invoke=invoke,
        test_runner=_test,
    )
    assert receipt is not None and receipt["status"] == "done"
    assert observed_before_receipt == [2]
    final_progress = read_progress(
        spool,
        manifest["project_id"],
        manifest["run_id"],
        manifest["attempt_id"],
        manifest["request_sha256"],
    )
    assert [record.progress.text for record in final_progress.records] == [
        STAGE_BASELINE_FAILED,
        AGENT_UPDATE_TEXT,
        STAGE_CLI_FINISHED,
        STAGE_TEST_PASSED,
    ]
    assert [(record.progress.source, record.progress.kind) for record in final_progress.records] == [
        ("runner_observed", "runner_update"),
        ("cli_output", "agent_update"),
        ("runner_observed", "runner_update"),
        ("runner_observed", "runner_update"),
    ]
    trace = next(item for item in receipt["artifacts"] if item["kind"] == "trace")
    saved_trace = SavedActionTrace.model_validate_json((spool / trace["path"]).read_bytes())
    assert [(item.source, item.kind) for item in saved_trace.progress] == [
        ("runner_observed", "runner_update"),
        ("cli_output", "agent_update"),
        ("runner_observed", "runner_update"),
        ("runner_observed", "runner_update"),
    ]


def test_tampered_manifest_never_claims(tmp_path: Path) -> None:
    spool = tmp_path / "external"
    path = _job(spool)
    payload = json.loads(path.read_text())
    payload["task_instructions"] = "Read another repository"
    path.write_text(json.dumps(payload))
    with pytest.raises(ContractError, match="external_request_digest_mismatch"):
        process_external_job(
            path,
            spool,
            executable=Path(sys.executable),
            invoke=lambda _: pytest.fail("launched"),
            test_runner=_test,
        )
    assert not (spool / "claims").exists()


def test_untrusted_cli_log_cannot_be_persisted(tmp_path: Path) -> None:
    spool = tmp_path / "external"
    path = _job(spool)
    executable = _fake_cli(tmp_path, "codex_cli")

    def invoke(request: CliRequest) -> CliResult:
        result = run_cli(request, environment={"PATH": "/usr/bin:/bin"})
        return replace(result, sanitized_log=b"Bearer synthetic-secret\n")

    receipt = process_external_job(path, spool, executable=executable, invoke=invoke, test_runner=_test)
    assert receipt is not None and receipt["status"] == "unknown"
    assert [item["kind"] for item in receipt["artifacts"]] == ["trace"]
    trace = (spool / receipt["artifacts"][0]["path"]).read_text()
    assert "Bearer synthetic-secret" not in trace


def test_provider_modified_git_config_never_runs_host_fsmonitor(tmp_path: Path) -> None:
    spool = tmp_path / "external"
    path = _job(spool)
    executable = _fake_cli(tmp_path, "codex_cli")
    marker = tmp_path / "host-git-executed"
    hook = tmp_path / "fsmonitor-hook"
    hook.write_text(f"#!/bin/sh\nprintf executed > {marker}\n")
    hook.chmod(0o700)

    def invoke(request: CliRequest) -> CliResult:
        result = run_cli(request, environment={"PATH": "/usr/bin:/bin"})
        config = request.repository / ".git" / "config"
        config.write_text(config.read_text() + f"\n[core]\n\tfsmonitor = {hook}\n")
        return result

    receipt = process_external_job(path, spool, executable=executable, invoke=invoke, test_runner=_test)
    assert receipt is not None
    assert receipt["status"] == "unknown"
    assert receipt["reason_code"] == "runner_failure_after_claim"
    assert not marker.exists()
    assert all(item["kind"] == "trace" for item in receipt["artifacts"])


def test_prelaunch_intent_precedes_invoker_and_session_observation(tmp_path: Path) -> None:
    spool = tmp_path / "external"
    path = _job(spool)
    attempt_id = path.stem
    executable = _fake_cli(tmp_path, "codex_cli")
    launch_path = spool / "claims" / f"{attempt_id}.launch.json"
    session_path = spool / "claims" / f"{attempt_id}.session.json"

    def invoke(request: CliRequest) -> CliResult:
        launch = json.loads(launch_path.read_text())
        assert launch["record_type"] == "prelaunch_intent"
        assert launch["prompt_sha256"] == hashlib.sha256(request.prompt.encode()).hexdigest()
        assert launch["request_sha256"] == json.loads(path.read_text())["request_sha256"]
        assert not session_path.exists()
        return run_cli(request, environment={"PATH": "/usr/bin:/bin"})

    receipt = process_external_job(path, spool, executable=executable, invoke=invoke, test_runner=_test)
    assert receipt is not None and receipt["status"] == "done"
    session = json.loads(session_path.read_text())
    assert session["record_type"] == "session_observation"
    assert session["launch_sha256"] == hashlib.sha256(launch_path.read_bytes()).hexdigest()
    assert session["provider_session_id"] == receipt["provider_session_id"]


def test_crash_after_prelaunch_leaves_claim_and_never_restarts(tmp_path: Path) -> None:
    spool = tmp_path / "external"
    path = _job(spool)
    executable = _fake_cli(tmp_path, "codex_cli")
    calls = 0

    def crash(_request: CliRequest) -> CliResult:
        nonlocal calls
        calls += 1
        raise SystemExit("simulated runner crash")

    with pytest.raises(SystemExit, match="simulated runner crash"):
        process_external_job(path, spool, executable=executable, invoke=crash, test_runner=_test)
    assert calls == 1
    assert (spool / "claims" / f"{path.stem}.claim").is_file()
    assert (spool / "claims" / f"{path.stem}.launch.json").is_file()
    assert not (spool / "claims" / f"{path.stem}.session.json").exists()
    assert not (spool / "receipts" / f"{path.stem}.json").exists()
    assert process_external_job(path, spool, executable=executable, invoke=crash, test_runner=_test) is None
    assert calls == 1


def test_installed_capability_heartbeat_is_explicitly_unavailable(tmp_path: Path) -> None:
    spool = tmp_path / "external"
    write_capabilities(spool)
    heartbeat = json.loads((spool / "capabilities.json").read_text())
    assert heartbeat["schema_version"] == 1
    assert heartbeat["isolated"] is False
    assert heartbeat["auth_ready"] is False
    assert [entry["available"] for entry in heartbeat["adapters"]] == [False, False]


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_host_fixture_command_output_is_bounded(tmp_path: Path, stream: str) -> None:
    command = (sys.executable, "-c", f"import sys; sys.{stream}.write('x' * 70000)")
    trace = ActionTrace(repository=tmp_path, private_root=tmp_path)
    token = TRACE_OBSERVER.set(trace.command)
    try:
        with pytest.raises(ExecutionError, match="fixture_command_output_limit"):
            _run(command, tmp_path, Deadline(5))
    finally:
        TRACE_OBSERVER.reset(token)
    saved = SavedActionTrace.model_validate_json(trace.encode())
    assert saved.actions[0].outcome == "finished"
    assert saved.actions[0].output_truncated is True
