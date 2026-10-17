"""Real networkless namespace acceptance with fake coding executables only."""

from __future__ import annotations

import hashlib
import json
import socket
import time
from pathlib import Path
from uuid import uuid4

import pytest

from onefoundry.external_code_schemas import ExternalCodeReceipt
from runner.external_cli import CliRequest
from runner.external_service import process_external_job, write_capabilities
from runner.fixture_git import _test
from runner.isolated_cli import SandboxLimits, run_fake_cli_isolated

BWRAP = Path("/usr/bin/bwrap")
PRLIMIT = Path("/usr/bin/prlimit")
LIMITS = SandboxLimits(max_processes=16, max_address_bytes=512 * 1024 * 1024, max_file_bytes=65536)


def _require_boundary() -> None:
    assert BWRAP.is_file() and PRLIMIT.is_file(), "Bubblewrap and prlimit are required"


def _job(spool: Path, adapter: str, *, seconds: int = 15) -> Path:
    attempt = str(uuid4())
    payload: dict[str, object] = {
        "schema_version": 1,
        "job_id": attempt,
        "project_id": str(uuid4()),
        "run_id": str(uuid4()),
        "attempt_id": attempt,
        "adapter_id": adapter,
        "fixture_id": "engineer_qa_v1",
        "stage": "engineer",
        "actor_id": "profile:fake-only",
        "task_title": "Repair the fixture",
        "task_instructions": "Make add_one add one and run the tests.",
        "limits": {"wall_seconds": seconds, "max_log_bytes": 16384, "max_diff_bytes": 32768},
    }
    payload["request_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    path = spool / "jobs" / f"{attempt}.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _fake(path: Path, body: str) -> Path:
    path.write_text("#!/usr/bin/python3\n" + body, encoding="utf-8")
    path.chmod(0o700)
    return path


@pytest.mark.parametrize("adapter", ["codex_cli", "claude_code"])
def test_fake_cli_receipt_proves_no_host_access(tmp_path: Path, adapter: str) -> None:
    _require_boundary()
    spool = tmp_path / "receipt-spool"
    original = tmp_path / "original-repository"
    secret = tmp_path / "onefoundry-secret-volume"
    for hidden in (original, secret):
        hidden.mkdir()
        (hidden / "marker").write_text("hidden")
    path = _job(spool, adapter)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        if adapter == "codex_cli":
            events = [
                {"type": "thread.started", "thread_id": "fake-codex"},
                {"type": "turn.completed", "usage": {"input_tokens": 2, "output_tokens": 3}},
            ]
        else:
            events = [
                {"type": "system", "session_id": "fake-claude"},
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "session_id": "fake-claude",
                    "usage": {"input_tokens": 2, "output_tokens": 3},
                    "total_cost_usd": 0.01,
                },
            ]
        script = _fake(
            tmp_path / f"fake-{adapter}",
            "import json, os, pathlib, socket, sys\n"
            "assert 'Repair the fixture' in sys.stdin.read()\n"
            "assert pathlib.Path.cwd() == pathlib.Path('/work')\n"
            "assert os.environ['HOME'] == '/home/runner'\n"
            "assert os.environ['CODEX_HOME'] == '/home/runner/codex'\n"
            "assert os.environ['CLAUDE_CONFIG_DIR'] == '/home/runner/claude'\n"
            "assert 'DB_PASSWORD' not in os.environ\n"
            f"assert not pathlib.Path({str(Path.home())!r}).exists()\n"
            f"assert not pathlib.Path({str(original)!r}).exists()\n"
            f"assert not pathlib.Path({str(secret)!r}).exists()\n"
            f"assert not pathlib.Path({str(spool)!r}).exists()\n"
            "assert not pathlib.Path('/var/run/docker.sock').exists()\n"
            f"\ntry: socket.create_connection(('127.0.0.1', {port}), timeout=0.2)\n"
            "except OSError: pass\n"
            "else: raise AssertionError('host network reachable')\n"
            "target = pathlib.Path('/work/calculator.py')\n"
            "target.write_text(target.read_text().replace('return value\\n', 'return value + 1\\n'))\n"
            f"for event in {events!r}: print(json.dumps(event))\n",
        )
        receipt = process_external_job(
            path,
            spool,
            executable=script,
            invoke=lambda request: run_fake_cli_isolated(
                request, bubblewrap=BWRAP, prlimit=PRLIMIT, limits=LIMITS
            ),
            test_runner=_test,
        )
    assert receipt is not None
    assert receipt["status"] == "done", receipt["reason_code"]
    ExternalCodeReceipt.model_validate(receipt)
    assert receipt["tokens_used"] == 5
    assert receipt["provider_session_id"] == ("fake-codex" if adapter == "codex_cli" else "fake-claude")
    assert receipt["cost_usd"] == (None if adapter == "codex_cli" else 0.01)
    assert [(item["name"], item["status"]) for item in receipt["tests"]] == [
        ("before", "failed"),
        ("after", "passed"),
    ]
    assert [item["kind"] for item in receipt["artifacts"]] == ["diff", "log", "trace"]
    for artifact in receipt["artifacts"]:
        data = (spool / artifact["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == artifact["sha256"]
    assert (
        process_external_job(
            path,
            spool,
            executable=script,
            invoke=lambda _: pytest.fail("attempt relaunched"),
            test_runner=_test,
        )
        is None
    )
    write_capabilities(spool)
    capabilities = json.loads((spool / "capabilities.json").read_text())
    assert capabilities["isolated"] is False
    assert all(not entry["available"] for entry in capabilities["adapters"])


def test_timeout_kills_child_process_tree_and_records_unknown(tmp_path: Path) -> None:
    _require_boundary()
    spool = tmp_path / "spool"
    path = _job(spool, "codex_cli", seconds=3)
    script = _fake(
        tmp_path / "fake-hung",
        "import os, pathlib, time\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    time.sleep(5)\n"
        "    pathlib.Path('/work/late').write_text('escaped')\n"
        "    os._exit(0)\n"
        "time.sleep(5)\n",
    )
    receipt = process_external_job(
        path,
        spool,
        executable=script,
        invoke=lambda request: run_fake_cli_isolated(
            CliRequest(
                request.adapter_id,
                request.executable,
                request.repository,
                request.private_home,
                request.prompt,
                1,
                request.max_output_bytes,
            ),
            bubblewrap=BWRAP,
            prlimit=PRLIMIT,
            limits=LIMITS,
        ),
        test_runner=_test,
    )
    assert receipt is not None
    assert (receipt["status"], receipt["reason_code"]) == ("unknown", "provider_timeout")
    assert [item["kind"] for item in receipt["artifacts"]] == ["trace"]
    assert (
        process_external_job(
            path,
            spool,
            executable=script,
            invoke=lambda _: pytest.fail("uncertain attempt retried"),
            test_runner=_test,
        )
        is None
    )


def test_invalid_or_spoofed_executable_fails_before_launch(tmp_path: Path) -> None:
    _require_boundary()
    repo = tmp_path / "repo"
    home = tmp_path / "private-home"
    repo.mkdir()
    home.mkdir()
    script = _fake(tmp_path / "fake", "raise AssertionError('launched')\n")
    link = tmp_path / "spoofed"
    link.symlink_to(script)
    for executable in (link, tmp_path / "missing"):
        request = CliRequest("codex_cli", executable, repo, home, "task", 2, 4096)
        with pytest.raises(ValueError):
            run_fake_cli_isolated(request, bubblewrap=BWRAP, prlimit=PRLIMIT, limits=LIMITS)
    assert not (home / "codex").exists()


def test_process_and_address_limits_apply_inside_namespace(tmp_path: Path) -> None:
    _require_boundary()
    repo = tmp_path / "repo"
    home = tmp_path / "private-home"
    repo.mkdir()
    home.mkdir()
    script = _fake(
        tmp_path / "fake-limits",
        "import json, mmap, os, time\n"
        "try: mmap.mmap(-1, 1024 * 1024 * 1024)\n"
        "except (OSError, MemoryError): pass\n"
        "else: raise AssertionError('address limit missing')\n"
        "children = []\n"
        "for index in range(64):\n"
        "    try: child = os.fork()\n"
        "    except OSError: break\n"
        "    if child == 0: time.sleep(1); os._exit(0)\n"
        "    children.append(child)\n"
        "else: raise AssertionError('process limit missing')\n"
        "for child in children: os.waitpid(child, 0)\n"
        "print(json.dumps({'type':'thread.started','thread_id':str(len(children))}))\n"
        "print(json.dumps({'type':'turn.completed'}))\n",
    )
    result = run_fake_cli_isolated(
        CliRequest("codex_cli", script, repo, home, "probe", 5, 4096),
        bubblewrap=BWRAP,
        prlimit=PRLIMIT,
        limits=LIMITS,
    )
    assert result.status == "done", result.reason_code
    assert result.provider_session_id is not None
    assert 1 <= int(result.provider_session_id) <= LIMITS.max_processes


def test_timeout_terminates_descendant_before_late_write(tmp_path: Path) -> None:
    _require_boundary()
    repo = tmp_path / "repo"
    home = tmp_path / "private-home"
    repo.mkdir()
    home.mkdir()
    script = _fake(
        tmp_path / "fake-descendant",
        "import os, pathlib, time\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    time.sleep(2)\n"
        "    pathlib.Path('/work/late').write_text('escaped')\n"
        "    os._exit(0)\n"
        "time.sleep(10)\n",
    )
    result = run_fake_cli_isolated(
        CliRequest("codex_cli", script, repo, home, "probe", 1, 4096),
        bubblewrap=BWRAP,
        prlimit=PRLIMIT,
        limits=LIMITS,
    )
    assert (result.status, result.reason_code) == ("unknown", "provider_timeout")
    time.sleep(2.2)
    assert not (repo / "late").exists()
