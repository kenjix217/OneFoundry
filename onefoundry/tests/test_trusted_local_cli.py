"""Trusted local CLI mode uses existing operator logins and one disposable job."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest

import runner.trusted_local_supervisor as supervisor
from runner.trusted_local_cli import (
    TrustedLocalConfig,
    TrustedLocalInvalid,
    process_trusted_local_job,
    write_trusted_capabilities,
)
from runner.trusted_local_supervisor import run_pending, supervise


def _job(spool: Path, adapter: str) -> Path:
    for name in ("jobs", "claims", "receipts", "artifacts"):
        (spool / name).mkdir(parents=True, exist_ok=True)
    attempt = str(uuid4())
    payload = {
        "schema_version": 1,
        "job_id": attempt,
        "project_id": str(uuid4()),
        "run_id": str(uuid4()),
        "attempt_id": attempt,
        "adapter_id": adapter,
        "fixture_id": "engineer_qa_v1",
        "stage": "engineer",
        "actor_id": f"profile:{uuid4()}",
        "task_title": "Repair calculator",
        "task_instructions": "Make add_one return value + 1.",
        "limits": {"wall_seconds": 15, "max_log_bytes": 16384, "max_diff_bytes": 32768},
    }
    import hashlib

    payload["request_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    path = spool / "jobs" / f"{attempt}.json"
    path.write_text(json.dumps(payload))
    return path


def _fake_cli(tmp_path: Path, adapter: str, operator_home: Path) -> Path:
    script = tmp_path / f"fake-{adapter}"
    auth = ".codex" if adapter == "codex_cli" else ".claude"
    events = (
        "{'type':'thread.started','thread_id':'local-codex'},"
        "{'type':'turn.completed','usage':{'input_tokens':2,'output_tokens':3}}"
        if adapter == "codex_cli"
        else "{'type':'system','session_id':'local-claude'},"
        "{'type':'result','subtype':'success','is_error':False,'session_id':'local-claude',"
        "'usage':{'input_tokens':2,'output_tokens':3},'total_cost_usd':0.01}"
    )
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        f"assert os.environ['HOME'] == {str(operator_home)!r}\n"
        f"assert os.environ[{'CODEX_HOME' if adapter == 'codex_cli' else 'CLAUDE_CONFIG_DIR'!r}] "
        f"== {str(operator_home / auth)!r}\n"
        + (
            "if sys.argv[1:] == ['login', 'status']: sys.exit(0)\n"
            if adapter == "codex_cli"
            else "if sys.argv[1:] == ['auth', 'status', '--json']:\n"
            "    print(json.dumps({'loggedIn': True})); sys.exit(0)\n"
        )
        + "assert 'Repair calculator' in sys.stdin.read()\n"
        + (
            "assert '--bare' not in sys.argv\n"
            "assert sys.argv[sys.argv.index('--allowed-tools') + 1] == 'Edit,Read,Write'\n"
            if adapter == "claude_code"
            else ""
        )
        + "path = pathlib.Path.cwd() / 'calculator.py'\n"
        "path.write_text(path.read_text().replace('return value\\n', 'return value + 1\\n'))\n"
        f"for event in ({events}): print(json.dumps(event), flush=True)\n"
    )
    script.chmod(0o700)
    return script


@pytest.mark.parametrize("adapter", ["codex_cli", "claude_code"])
def test_trusted_local_cli_records_receipt_session_and_progress(tmp_path: Path, adapter: str) -> None:
    spool = tmp_path / "spool"
    job = _job(spool, adapter)
    operator_home = tmp_path / "operator"
    operator_home.mkdir()
    auth = operator_home / (".codex" if adapter == "codex_cli" else ".claude")
    auth.mkdir()
    marker = auth / "login-marker"
    marker.write_text("existing-login-stays")
    fake = _fake_cli(tmp_path, adapter, operator_home)
    config = TrustedLocalConfig(
        spool,
        operator_home,
        os.environ["PATH"],
        codex_executable=fake if adapter == "codex_cli" else None,
        claude_executable=fake if adapter == "claude_code" else None,
    )
    receipt = process_trusted_local_job(job, config)
    assert receipt is not None and receipt["status"] == "done", receipt
    assert receipt["provider_session_id"] == ("local-codex" if adapter == "codex_cli" else "local-claude")
    assert marker.read_text() == "existing-login-stays"
    attempt = receipt["attempt_id"]
    launch = json.loads((spool / "claims" / f"{attempt}.launch.json").read_text())
    session = json.loads((spool / "claims" / f"{attempt}.session.json").read_text())
    assert launch["invocation_kind"] == "trusted_local_cli_v1"
    assert launch["trust_mode"] == "operator_host"
    assert launch["executable_ref"] == str(fake.resolve())
    assert session["provider_status"] == "done"
    assert (spool / "claims" / f"{attempt}.progress.jsonl").is_file()
    for path in (
        spool / "claims" / f"{attempt}.claim",
        spool / "claims" / f"{attempt}.launch.json",
        spool / "claims" / f"{attempt}.session.json",
        spool / "claims" / f"{attempt}.progress.jsonl",
        spool / "receipts" / f"{attempt}.json",
        spool / "artifacts" / attempt / "change.diff",
    ):
        assert path.stat().st_mode & 0o777 == 0o640, path
    assert process_trusted_local_job(job, config) is None


def test_missing_host_login_store_rejects_before_claim(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    job = _job(spool, "codex_cli")
    home = tmp_path / "operator"
    home.mkdir()
    fake = _fake_cli(tmp_path, "codex_cli", home)
    config = TrustedLocalConfig(spool, home, os.environ["PATH"], codex_executable=fake)
    with pytest.raises(TrustedLocalInvalid, match="trusted_local_login_unavailable"):
        process_trusted_local_job(job, config)
    assert not list((spool / "claims").iterdir())


@pytest.mark.parametrize("adapter", ["codex_cli", "claude_code"])
def test_unsuccessful_auth_status_rejects_before_claim(tmp_path: Path, adapter: str) -> None:
    spool = tmp_path / "spool"
    job = _job(spool, adapter)
    home = tmp_path / "operator"
    home.mkdir()
    (home / (".codex" if adapter == "codex_cli" else ".claude")).mkdir()
    fake = _fake_cli(tmp_path, adapter, home)
    if adapter == "codex_cli":
        fake.write_text(
            fake.read_text().replace("['login', 'status']: sys.exit(0)", "['login', 'status']: sys.exit(1)")
        )
    else:
        fake.write_text(fake.read_text().replace("'loggedIn': True", "'loggedIn': False"))
    config = TrustedLocalConfig(
        spool,
        home,
        os.environ["PATH"],
        codex_executable=fake if adapter == "codex_cli" else None,
        claude_executable=fake if adapter == "claude_code" else None,
    )
    assert write_trusted_capabilities(config)["auth_ready"] is False
    with pytest.raises(TrustedLocalInvalid, match="trusted_local_login_unavailable"):
        process_trusted_local_job(job, config)
    assert not list((spool / "claims").iterdir())


def test_capability_reports_each_login_and_revokes_on_stop(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    _job(spool, "codex_cli")
    home = tmp_path / "operator"
    home.mkdir()
    (home / ".codex").mkdir()
    codex = _fake_cli(tmp_path, "codex_cli", home)
    claude = _fake_cli(tmp_path, "claude_code", home)
    config = TrustedLocalConfig(spool, home, os.environ["PATH"], codex, claude)
    published = write_trusted_capabilities(config)
    assert published["mode"] == "trusted_local_cli_v1"
    assert published["isolated"] is False and published["auth_ready"] is True
    assert published["adapters"] == [
        {"adapter_id": "codex_cli", "available": True, "reason": None},
        {
            "adapter_id": "claude_code",
            "available": False,
            "reason": "trusted_local_login_unavailable",
        },
    ]
    assert (spool / "capabilities.json").stat().st_mode & 0o777 == 0o640
    stopped = write_trusted_capabilities(config, active=False)
    assert stopped["auth_ready"] is False
    assert all(not item["available"] for item in stopped["adapters"])


def test_supervisor_once_processes_valid_job_and_revokes_capability(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    job = _job(spool, "codex_cli")
    home = tmp_path / "operator"
    home.mkdir()
    (home / ".codex").mkdir()
    fake = _fake_cli(tmp_path, "codex_cli", home)
    config = TrustedLocalConfig(spool, home, os.environ["PATH"], codex_executable=fake)
    assert supervise(config, once=True, poll_seconds=1, job=None) == 1
    assert json.loads((spool / "receipts" / job.name).read_text())["status"] == "done"
    assert json.loads((spool / "capabilities.json").read_text())["auth_ready"] is False
    assert run_pending(config) == 0


def test_supervisor_once_reports_unknown_receipt_as_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = tmp_path / "spool"
    job = _job(spool, "codex_cli")
    home = tmp_path / "operator"
    home.mkdir()
    (home / ".codex").mkdir()
    fake = _fake_cli(tmp_path, "codex_cli", home)
    config = TrustedLocalConfig(spool, home, os.environ["PATH"], codex_executable=fake)
    monkeypatch.setattr(
        supervisor,
        "process_trusted_local_job",
        lambda _path, _config: {"status": "unknown", "reason_code": "provider_timeout"},
    )
    with pytest.raises(TrustedLocalInvalid, match="trusted_local_provider_timeout"):
        supervise(config, once=True, poll_seconds=1, job=job)
    assert json.loads((spool / "capabilities.json").read_text())["auth_ready"] is False


def test_supervisor_heartbeats_while_job_processing_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = tmp_path / "spool"
    _job(spool, "codex_cli")
    home = tmp_path / "operator"
    home.mkdir()
    (home / ".codex").mkdir()
    fake = _fake_cli(tmp_path, "codex_cli", home)
    config = TrustedLocalConfig(spool, home, os.environ["PATH"], codex_executable=fake)
    second_heartbeat = Event()
    calls: list[bool] = []

    def publish(
        _config: TrustedLocalConfig,
        *,
        active: bool = True,
        auth_cache: object = None,
    ) -> dict[str, object]:
        calls.append(active)
        if calls.count(True) >= 2:
            second_heartbeat.set()
        return {}

    class Finished(Exception):
        pass

    def blocked(_config: TrustedLocalConfig, *, ignored: object = None) -> int:
        assert second_heartbeat.wait(2), "capability heartbeat stopped during job"
        raise Finished

    monkeypatch.setattr(supervisor, "write_trusted_capabilities", publish)
    monkeypatch.setattr(supervisor, "run_pending", blocked)
    with pytest.raises(Finished):
        supervisor.supervise(config, once=False, poll_seconds=0.1, job=None)
    assert calls.count(True) >= 2
    assert calls[-1] is False


def test_pending_ignores_unsupported_manifest_after_first_rejection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spool = tmp_path / "spool"
    job = _job(spool, "codex_cli")
    payload = json.loads(job.read_text())
    payload["stage"] = "offline_probe"
    job.write_text(json.dumps(payload))
    home = tmp_path / "operator"
    home.mkdir()
    (home / ".codex").mkdir()
    fake = _fake_cli(tmp_path, "codex_cli", home)
    config = TrustedLocalConfig(spool, home, os.environ["PATH"], codex_executable=fake)
    ignored: set[tuple[str, int, int]] = set()
    assert run_pending(config, ignored=ignored) == 0
    assert run_pending(config, ignored=ignored) == 0
    assert capsys.readouterr().err.count("trusted_local_job_skipped") == 1
    assert not list((spool / "claims").iterdir())


@pytest.mark.parametrize("auth_check_seconds,auth_cache_seconds", [(0, 30), (5, 0), (True, 30)])
def test_invalid_auth_probe_bounds_rejected(
    tmp_path: Path, auth_check_seconds: int, auth_cache_seconds: int
) -> None:
    spool = tmp_path / "spool"
    _job(spool, "codex_cli")
    home = tmp_path / "operator"
    home.mkdir()
    fake = _fake_cli(tmp_path, "codex_cli", home)
    config = TrustedLocalConfig(
        spool, home, os.environ["PATH"], fake, None, auth_check_seconds, auth_cache_seconds
    )
    with pytest.raises(TrustedLocalInvalid, match="trusted_local_config_invalid"):
        config.validate()


def test_job_outside_spool_and_unconfigured_adapter_rejected_before_claim(tmp_path: Path) -> None:
    spool = tmp_path / "spool"
    job = _job(spool, "claude_code")
    home = tmp_path / "operator"
    home.mkdir()
    fake = _fake_cli(tmp_path, "codex_cli", home)
    config = TrustedLocalConfig(spool, home, os.environ["PATH"], codex_executable=fake)
    with pytest.raises(TrustedLocalInvalid, match="trusted_local_job_path_invalid"):
        process_trusted_local_job(tmp_path / "other.json", config)
    with pytest.raises(TrustedLocalInvalid, match="trusted_local_adapter_unavailable"):
        process_trusted_local_job(job, config)
    assert not list((spool / "claims").iterdir())
