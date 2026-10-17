"""Terminal metadata survives noisy commands without recording their contents."""

import json
import os
from pathlib import Path
from typing import Any

import pytest

from runner.repository_codex_observation import CLI_OBSERVER, result


def test_host_attach_deadline_retains_its_timeout_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    from runner import repository_codex_bridge as bridge
    from runner.fixture_git import Deadline, ExecutionError

    def expired(*_args: object) -> bytes:
        raise TimeoutError("wall_time_exceeded")

    monkeypatch.setattr(bridge, "_bounded_attach", expired)
    with pytest.raises(ExecutionError, match="maintenance_cli_timeout"):
        bridge._attach_client("fixture-client", Deadline(1), 4096)


def events(**changed: Any) -> bytes:
    observation = {
        "type": "repository.cli",
        "exit_code": 0,
        "signaled": False,
        "timed_out": False,
        "spawn_error": False,
        "terminal_count": 1,
        "stdout_bytes": 400000,
        "stderr_bytes": 100,
    }
    observation.update(changed)
    return b"\n".join(
        json.dumps(item).encode()
        for item in [
            {"type": "thread.started", "thread_id": "fixture-session"},
            {"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 3}},
            observation,
        ]
    )


def test_terminal_usage_survives_compacted_observation() -> None:
    observed = result(events())
    assert observed.status == "done" and observed.tokens_used == 15
    assert observed.provider_session_id == "fixture-session"
    assert len(observed.sanitized_log) < 512


@pytest.mark.parametrize(
    "changed",
    [
        {"exit_code": 1},
        {"signaled": True},
        {"timed_out": True},
        {"spawn_error": True},
        {"terminal_count": 2},
        {"stdout_bytes": "invalid"},
        {"extra": "untrusted"},
    ],
)
def test_invalid_or_uncertain_client_never_produces_candidate(changed: dict[str, Any]) -> None:
    assert result(events(**changed)).status == "unknown"


def test_missing_and_malformed_terminal_stay_unknown() -> None:
    assert result(b"invalid\n[]\n").status == "unknown"
    assert result(events() + b'\n{"type":"repository.cli"}').status == "unknown"


@pytest.mark.parametrize("code", ["repository_provider_request_limit", "repository_provider_http_429"])
def test_failed_turn_preserves_allowlisted_broker_cause(code: str) -> None:
    raw = events(exit_code=1).replace(b"turn.completed", b"turn.failed")
    raw += (
        b"\n"
        + json.dumps(
            {
                "type": "repository.broker",
                "forwarded_requests": 20,
                "request_bytes": 20000,
                "response_bytes": 10000,
                "last_denial": code,
            }
        ).encode()
    )
    observed = result(raw)
    assert observed.status == "unknown" and observed.reason_code == code
    assert code.encode() in observed.sanitized_log


def test_untrusted_diagnostic_content_is_not_retained() -> None:
    raw = events(exit_code=1).replace(b"turn.completed", b"turn.failed")
    raw += (
        b'\n{"type":"repository.broker","forwarded_requests":1,"request_bytes":1,'
        b'"response_bytes":1,"last_denial":"private-content"}'
    )
    observed = result(raw)
    assert observed.reason_code == "maintenance_provider_turn_failed"
    assert b"private-content" not in observed.sanitized_log


@pytest.mark.integration
def test_noisy_cli_is_drained_and_only_terminal_fields_leave_client() -> None:
    from runner import codex_cli_admission
    from runner import docker_egress_proof as docker
    from runner.cli_image_admission import DockerEndpoint

    tag = os.environ.get("ONEFOUNDRY_CODEX_TEST_IMAGE")
    image = (
        docker._inspect("image", tag)["Id"]
        if tag
        else codex_cli_admission.build_and_prove(
            DockerEndpoint(Path("/usr/bin/docker"), Path("/var/run/docker.sock"))
        )["image_id"]
    )
    child = (
        "process.stdout.write(JSON.stringify({type:'thread.started',thread_id:'fixture-session'})+'\\n');"
        "process.stdout.write(JSON.stringify({type:'item.completed',payload:'private-fixture'.repeat(100000)})+'\\n');"
        "process.stderr.write('private-stderr-fixture'.repeat(10000));"
        "process.stdout.write(JSON.stringify({type:'turn.completed',usage:{input_tokens:12,output_tokens:3}})+'\\n');"
    )
    code = (
        "const cp=require('child_process');\n"
        + CLI_OBSERVER
        + "\nobserveCli(process.execPath,['-e',"
        + json.dumps(child)
        + "],{cwd:'/tmp',env:process.env,input:'',wallMilliseconds:10000,recordLimit:4096});"
    )
    raw = docker._docker(
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--entrypoint=/usr/local/bin/node",
        image,
        "-e",
        code,
    )
    assert "private-fixture" not in raw and "private-stderr-fixture" not in raw
    assert len(raw) < 1024
    assert result(raw.encode()).status == "done"
