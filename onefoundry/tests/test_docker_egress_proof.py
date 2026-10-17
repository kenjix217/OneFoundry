"""Actual-Docker, local-only proxy egress acceptance; no provider account or CLI."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from runner.docker_egress_proof import ProofError, _docker, bounded_logs, run_local_egress_proof
from runner.live_progress_spool import LiveProgressJournal, read_progress
from runner.provider_progress import AGENT_UPDATE_TEXT, LiveProgressParser


@pytest.mark.integration
def test_local_egress_is_proxy_only() -> None:
    image = os.environ.get("ONEFOUNDRY_EGRESS_TEST_IMAGE")
    assert image, "Set ONEFOUNDRY_EGRESS_TEST_IMAGE to a preinstalled Python runner image"
    observed = run_local_egress_proof(image)
    assert set(observed) == {
        "allowed_tls",
        "other_host_denied",
        "other_port_denied",
        "ip_literal_denied",
        "wrong_sni_denied",
        "http_form_denied",
        "fixture_direct_denied",
        "host_direct_denied",
        "no_default_route",
        "no_host_paths",
        "no_app_source",
        "no_provider_auth",
    }
    assert all(observed.values())


@pytest.mark.integration
def test_docker_logs_are_bounded_before_buffering() -> None:
    image = os.environ["ONEFOUNDRY_EGRESS_TEST_IMAGE"]
    name = f"onefoundry-log-bound-{uuid4().hex[:12]}"
    try:
        _docker(
            "run",
            "--detach",
            "--pull=never",
            "--name",
            name,
            "--network=none",
            "--entrypoint=/usr/local/bin/python3",
            image,
            "-c",
            "import sys; sys.stdout.write('x' * 70000)",
        )
        _docker("wait", name)
        with pytest.raises(ProofError, match="docker_logs_limit"):
            bounded_logs(name, 4096)
        assert bounded_logs(name, 80000) == b"x" * 70000
    finally:
        _docker("rm", "--force", name)


@pytest.mark.integration
def test_docker_progress_reaches_spool_before_container_exits(tmp_path: Path) -> None:
    image = os.environ["ONEFOUNDRY_EGRESS_TEST_IMAGE"]
    name = f"onefoundry-live-progress-{uuid4().hex[:12]}"
    parser = LiveProgressParser("codex_cli")
    observed_while_running: list[str] = []
    spool = tmp_path / "spool"
    (spool / "claims").mkdir(parents=True)
    project_id, run_id, attempt_id = (str(uuid4()) for _ in range(3))
    request_sha256 = "a" * 64
    claim = spool / "claims" / f"{attempt_id}.claim"
    claim.write_text(attempt_id)
    claim.chmod(0o600)

    def publish(chunk: bytes) -> None:
        for item in parser.feed(chunk):
            assert journal.append(item) is True
            snapshot = read_progress(spool, project_id, run_id, attempt_id, request_sha256)
            assert snapshot.latest_sequence >= 1
            state = _docker("inspect", "--format", "{{.State.Running}}", name)
            assert state == "true"
            observed_while_running.append(item.text)

    try:
        with LiveProgressJournal.create(spool, project_id, run_id, attempt_id, request_sha256) as journal:
            _docker(
                "run",
                "--detach",
                "--pull=never",
                "--name",
                name,
                "--network=none",
                "--entrypoint=/usr/local/bin/python3",
                image,
                "-u",
                "-c",
                "import json, time; print(json.dumps({'type':'item.completed','item':"
                "{'type':'agent_message','text':'Checking the repair.'}}), flush=True); time.sleep(2)",
            )
            raw = bounded_logs(name, 4096, timeout=5, follow=True, on_stdout_chunk=publish)
        assert observed_while_running == [AGENT_UPDATE_TEXT]
        assert b"item.completed" in raw
        assert _docker("inspect", "--format", "{{.State.Running}}", name) == "false"
    finally:
        _docker("rm", "--force", name)
