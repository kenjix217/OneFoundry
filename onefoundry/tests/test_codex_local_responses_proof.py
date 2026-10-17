"""Pinned Codex CLI against a fake local Responses provider through Docker proxy egress."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from runner import codex_local_responses_proof, docker_egress_proof
from runner.codex_cli_admission import DockerEndpoint, build_and_prove
from runner.codex_local_responses_proof import prove_local_codex_turn
from runner.docker_egress_proof import ProofError


def test_cleanup_attempts_every_disposal_and_preserves_original_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_remove(kind: str, name: str) -> None:
        calls.append((kind, name))
        if name == "client":
            raise ProofError("cleanup_failed")

    monkeypatch.setattr(docker_egress_proof, "_remove", fake_remove)
    original = ProofError("original_failure")
    codex_local_responses_proof._cleanup(["fixture", "client"], ["network"], original)
    assert calls == [("container", "client"), ("container", "fixture"), ("network", "network")]
    assert any("proof_cleanup_failed" in note for note in original.__notes__)
    with pytest.raises(ProofError, match="proof_cleanup_failed"):
        codex_local_responses_proof._cleanup(["fixture", "client"], ["network"], None)


@pytest.mark.integration
def test_pinned_codex_turn_reaches_only_local_responses_fixture() -> None:
    docker = shutil.which("docker")
    assert docker is not None
    endpoint = DockerEndpoint(Path(docker).resolve(), Path("/var/run/docker.sock"))
    fixture_image = os.environ.get("ONEFOUNDRY_EGRESS_TEST_IMAGE")
    assert fixture_image, "Set ONEFOUNDRY_EGRESS_TEST_IMAGE to the Compose-built test image"
    admitted = build_and_prove(endpoint)
    try:
        proof = prove_local_codex_turn(admitted["image_id"], fixture_image)
        boundary = proof["boundary"]
        assert isinstance(boundary, dict)
        assert boundary["image_id"] == admitted["image_id"]
        assert len(boundary["networks"]) == 1
        assert boundary["networks"][0].startswith("onefoundry-codex-client-")
        assert boundary["mounts"] == ["/etc/ssl/certs/ca-certificates.crt"]
        assert boundary["read_only"] is True
        assert boundary["cap_drop"] == ["ALL"]
        assert boundary["security_opt"] == ["no-new-privileges:true"]
        assert boundary["memory"] == 512 * 1024 * 1024
        assert boundary["memory_swap"] == 512 * 1024 * 1024
        assert boundary["pids_limit"] == 64
        assert boundary["nano_cpus"] == 1_000_000_000
        assert boundary["user"] == "1000:1000"
        cli = proof["cli"]
        assert isinstance(cli, dict)
        assert cli["exit_code"] == 0
        assert cli["signal"] is None
        assert cli["fixture_direct_denied"] is True
        assert cli["no_default_route"] is True
        assert cli["no_host_paths"] is True
        assert cli["private_home_was_empty"] is True
        events = [json.loads(line) for line in cli["stdout"].splitlines()]
        assert any(event["type"] == "thread.started" and event["thread_id"] for event in events)
        assert any(
            event["type"] == "item.completed"
            and event["item"]["type"] == "agent_message"
            and event["item"]["text"] == f"local-proof-{proof['nonce']}"
            for event in events
            if event["type"] == "item.completed"
        )
        assert any(
            event["type"] == "turn.completed" and event["usage"]["output_tokens"] == 4 for event in events
        )
        assert proof["requests"] == [
            {
                "request_line": "POST /v1/responses HTTP/1.1",
                "authorized": True,
                "stream": True,
                "model": "gpt-5",
            }
        ]
        proxy = proof["proxy"]
        assert isinstance(proxy, list)
        assert sum(item.get("tls_sni") == "provider.test" for item in proxy) == 1
        connects = [item for item in proxy if "connect" in item]
        assert sum(item["allowed"] is True for item in connects) == 1
        assert all(
            item["connect"] == "CONNECT provider.test:443 HTTP/1.1"
            if item["allowed"]
            else item["connect"] != "CONNECT provider.test:443 HTTP/1.1"
            for item in connects
        )
        assert "local-test-only" not in json.dumps(proof)
    finally:
        removed = subprocess.run(  # noqa: S603 - fixed Docker executable and argument vector
            (
                str(endpoint.executable),
                "--host",
                f"unix://{endpoint.socket}",
                "image",
                "rm",
                "--no-prune",
                admitted["image_tag"],
            ),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert removed.returncode == 0, "disposable admission image tag cleanup failed"
