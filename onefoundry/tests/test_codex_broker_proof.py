"""Offline broker admission: exact destinations, bounds and dummy secret split."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from runner import codex_broker_policy, codex_broker_proof, codex_cli_admission
from runner import docker_egress_proof as egress
from runner.codex_broker_policy import BrokerDenied
from runner.codex_cli_admission import DockerEndpoint


def _headers(body: bytes) -> dict[str, str]:
    return {
        "host": "provider.test",
        "authorization": "Bearer client-placeholder-only",
        "content-type": "application/json",
        "content-length": str(len(body)),
    }


def _request(**changes: Any) -> tuple[dict[str, str], bytes]:
    payload: dict[str, Any] = {"model": "gpt-5", "stream": True, "input": "fixture"}
    payload.update(changes)
    body = json.dumps(payload).encode()
    return _headers(body), body


def _sse(input_count: int, output_count: int) -> bytes:
    event = {
        "type": "response.completed",
        "response": {"usage": {"input_tokens": input_count, "output_tokens": output_count}},
    }
    return b"data: " + json.dumps(event).encode() + b"\n\n"


def test_exact_connect_and_request_rewrite() -> None:
    codex_broker_policy.admit_connect(b"CONNECT provider.test:443 HTTP/1.1")
    for target in (
        b"CONNECT other.test:443 HTTP/1.1",
        b"CONNECT provider.test:80 HTTP/1.1",
        b"GET https://provider.test/v1/responses HTTP/1.1",
    ):
        with pytest.raises(BrokerDenied, match="connect_target_denied"):
            codex_broker_policy.admit_connect(target)
    headers, body = _request()
    bounded = codex_broker_policy.admit_request("POST /v1/responses HTTP/1.1", headers, body)
    assert json.loads(bounded)["max_output_tokens"] == 8
    assert b"local-test-only" not in bounded
    assert codex_broker_policy.response_tokens(_sse(10, 4)) == 14


@pytest.mark.parametrize(
    ("line", "header_changes", "payload_changes", "reason"),
    [
        ("POST /v1/chat/completions HTTP/1.1", {}, {}, "request_path_denied"),
        ("POST /v1/responses HTTP/1.1", {"host": "other.test"}, {}, "request_host_denied"),
        (
            "POST /v1/responses HTTP/1.1",
            {"authorization": "Bearer local-test-only"},
            {},
            "client_credential_invalid",
        ),
        ("POST /v1/responses HTTP/1.1", {"transfer-encoding": "chunked"}, {}, "request_framing_invalid"),
        ("POST /v1/responses HTTP/1.1", {}, {"max_output_tokens": 9}, "output_token_limit"),
        ("POST /v1/responses HTTP/1.1", {}, {"max_output_tokens": True}, "output_token_limit"),
        ("POST /v1/responses HTTP/1.1", {}, {"max_completion_tokens": 1}, "alternate_token_limit_denied"),
        ("POST /v1/responses HTTP/1.1", {}, {"model": "other"}, "request_model_denied"),
    ],
)
def test_request_denials(
    line: str, header_changes: dict[str, str], payload_changes: dict[str, Any], reason: str
) -> None:
    headers, body = _request(**payload_changes)
    headers.update(header_changes)
    with pytest.raises(BrokerDenied, match=reason):
        codex_broker_policy.admit_request(line, headers, body)


def test_malformed_and_size_limits() -> None:
    body = b"not-json"
    with pytest.raises(BrokerDenied, match="request_json_invalid"):
        codex_broker_policy.admit_request("POST /v1/responses HTTP/1.1", _headers(body), body)
    body = b"x" * (codex_broker_policy.LIMITS.max_request_bytes + 1)
    with pytest.raises(BrokerDenied, match="request_body_limit"):
        codex_broker_policy.admit_request("POST /v1/responses HTTP/1.1", _headers(body), body)
    with pytest.raises(BrokerDenied, match="request_count_limit"):
        codex_broker_policy.admit_request_number(2)
    with pytest.raises(BrokerDenied, match="response_token_limit"):
        codex_broker_policy.response_tokens(_sse(10, 9))
    with pytest.raises(BrokerDenied, match="cumulative_token_limit"):
        codex_broker_policy.admit_total_tokens(60, 14)
    with pytest.raises(BrokerDenied, match="response_usage_missing"):
        codex_broker_policy.response_tokens(b"data: {}\n\n")


@pytest.mark.integration
def test_disposable_codex_broker_turn() -> None:
    executable = shutil.which("docker")
    assert executable is not None
    fixture_tag = os.environ["ONEFOUNDRY_EGRESS_TEST_IMAGE"]
    fixture_image = egress._inspect("image", fixture_tag)["Id"]
    assert isinstance(fixture_image, str)
    endpoint = DockerEndpoint(Path(executable).resolve(), Path("/var/run/docker.sock"))
    admitted = codex_cli_admission.build_and_prove(endpoint)
    try:
        result = codex_broker_proof.prove_broker_turn(admitted["image_id"], fixture_image)
        assert result["provider_requests"] == 2
        assert isinstance(result["denied_connects"], int)
        assert result["denied_connects"] >= 2
        assert result["reported_tokens"] == 28
        assert result["cost_usd"] is None
        assert result["before"] == {"name": "before", "status": "failed", "exit_code": 1}
        assert result["after"] == {"name": "after", "status": "passed", "exit_code": 0}
    finally:
        codex_cli_admission._remove_tag_if_present(endpoint, admitted["image_tag"])
