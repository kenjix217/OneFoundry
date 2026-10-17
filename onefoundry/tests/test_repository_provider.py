"""Provider bounds, secrets separation, and real Codex against local Responses."""

import hashlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from onefoundry.repository_code_contract import RepositoryCodeManifest, digest
from runner import codex_local_responses_proof as local
from runner import codex_workorder_fixture as fixture
from runner import docker_egress_proof as docker
from runner.repository_code_service import process_job
from runner.repository_codex_bridge import RepositoryCodexBridge
from runner.repository_provider_policy import ProviderBounds, admit_request
from runner.repository_runner_settings import RepositoryRunnerSettings
from tests.test_repository_code import job_at


def config_at(
    tmp_path: Path, job: RepositoryCodeManifest, root: Path, spool: Path, cli_image: str, broker_image: str
) -> RepositoryRunnerSettings:
    secret = tmp_path / "credential"
    secret.write_bytes(b"local-test-only")
    secret.chmod(0o600)
    return RepositoryRunnerSettings(
        spool=spool,
        source_root=root,
        credential_file=secret,
        cli_image=cli_image,
        broker_image=broker_image,
        cli_executable="/opt/codex-cli/node_modules/.bin/codex",
        broker_executable="/usr/local/bin/python3",
        model_id=job.model_id,
        provider_url=job.target.provider_url,
        broker_port=3128,
        request_timeout_seconds=30,
        poll_seconds=1,
        capability_refresh_seconds=5,
        child_memory_mb=512,
        child_processes=64,
        child_cpus=1,
        private_tmp_mb=32,
        policy=job.stage_limits,
    )


def test_exact_model_local_tools_and_provider_request_bounds() -> None:
    bounds = ProviderBounds("fixture-model", 2, 4096, 4096, 64)
    value: dict[str, Any] = {
        "model": bounds.model_id,
        "stream": True,
        "tools": [{"type": "function", "name": "exec_command"}],
        "store": True,
        "max_output_tokens": 10000,
    }
    admitted = json.loads(admit_request(json.dumps(value).encode(), bounds, 0))
    assert admitted["max_output_tokens"] == 64 and admitted["store"] is False
    for changed in (
        {"model": "other"},
        {"tools": [{"type": "web_search"}]},
        {"tools": [{"type": "function", "name": "send_email"}]},
        {"stream": False},
    ):
        with pytest.raises(ValueError):
            admit_request(json.dumps({**value, **changed}).encode(), bounds, 0)
    for raw, previous in ((b"not-json", 0), (b"x" * 4097, 0), (json.dumps(value).encode(), 2)):
        with pytest.raises(ValueError):
            admit_request(raw, bounds, previous)


def test_runner_requires_disjoint_locked_paths_and_approved_model(tmp_path: Path) -> None:
    job, root, spool = job_at(tmp_path)
    config = config_at(tmp_path, job, root, spool, job.target.test_image, job.target.test_image)
    config.validate_host()
    assert "credential_file" not in config.model_dump()
    config.credential_file.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        config.validate_host()
    value = {**config.model_dump(), "credential_file": str(config.credential_file), "source_root": spool}
    with pytest.raises(ValidationError, match="disjoint"):
        RepositoryRunnerSettings.model_validate(value)
    config.credential_file.chmod(0o600)
    changed = job.model_dump(mode="json", exclude={"request_sha256"})
    changed["model_id"] = changed["target"]["model_id"] = "other-model"
    other = RepositoryCodeManifest.model_validate({**changed, "request_sha256": digest(changed)})
    with pytest.raises(ValueError, match="approved_pilot"):
        RepositoryCodexBridge(config).validate_job(other)


@pytest.mark.integration
def test_real_codex_generic_candidate_with_separate_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture_image = docker._inspect("image", os.environ["ONEFOUNDRY_EGRESS_TEST_IMAGE"])["Id"]
    from runner import codex_cli_admission
    from runner.cli_image_admission import DockerEndpoint

    admitted: dict[str, Any] | None = None
    endpoint = DockerEndpoint(Path("/usr/bin/docker"), Path("/var/run/docker.sock"))
    cli_tag = os.environ.get("ONEFOUNDRY_CODEX_TEST_IMAGE")
    if cli_tag:
        cli_image = docker._inspect("image", cli_tag)["Id"]
    else:
        admitted = codex_cli_admission.build_and_prove(endpoint)
        cli_image = admitted["image_id"]
    job, root, spool = job_at(tmp_path, image=fixture_image)
    value = job.model_dump(mode="json", exclude={"request_sha256"})
    value["model_id"] = value["target"]["model_id"] = "gpt-5"
    job = RepositoryCodeManifest.model_validate({**value, "request_sha256": digest(value)})
    (spool / "jobs" / f"{job.attempt_id}.json").write_text(job.model_dump_json())
    config = config_at(tmp_path, job, root, spool, cli_image, fixture_image)
    from runner.isolated_fixture_test import _bounded_attach

    captured: list[bytes] = []

    def capture(*args: Any) -> bytes:
        raw = _bounded_attach(*args)
        captured.append(raw)
        return raw

    monkeypatch.setattr("runner.repository_codex_bridge._bounded_attach", capture)
    tls = tmp_path / "tls"
    tls.mkdir()
    docker._certificate(tls)
    local._server_certificate(tls)
    (tls / "leaf-key.pem").chmod(0o644)
    child = tmp_path / "fixture.py"
    child.write_text(
        fixture.CHILD.read_text()
        .replace(
            "sed -i 's/return value$/return value + 1/' calculator.py",
            "sed -i 's/return a-b/return a+b/' calculator.py",
        )
        .replace(
            "                    secure.sendall(\n",
            '                    payload += b"data: [DONE]\\n\\n"\n                    secure.sendall(\n',
        )
        .replace("        while True:\n", "        rejection_sent = False\n        while True:\n")
        .replace(
            "                    secure.sendall(\n",
            "                    if not rejection_sent:\n"
            "                        rejection_sent = True\n"
            '                        payload = _event("error", '
            '{"type": "error", "code": "server_is_overloaded"})\n'
            "                    secure.sendall(\n",
        )
    )
    monkeypatch.setattr(fixture, "CHILD", child)
    network, provider = (
        "onefoundry-generic-fixture-" + uuid4().hex[:12],
        "onefoundry-generic-provider-" + uuid4().hex[:12],
    )
    prompt = (
        job.task_title
        + "\n"
        + job.task_instructions
        + "\nEdit only these approved paths:\n"
        + "\n".join(job.target.allowed_paths)
    )
    try:
        docker._network(network)
        fixture._start_fixture(
            provider, network, tls, fixture_image, uuid4().hex, hashlib.sha256(prompt.encode()).hexdigest()
        )
        docker._ready(provider)
        bridge = RepositoryCodexBridge(config, network, docker._ip(provider, network), tls)
        assert len(bridge.prove()) == 64
        errors: list[str] = []

        def observe_provider(*args: Any) -> Any:
            try:
                return bridge(*args)
            except ValueError as exc:
                errors.append(str(exc))
                raise

        receipt = process_job(spool / "jobs" / f"{job.attempt_id}.json", spool, root, observe_provider)
        logs = docker.bounded_logs(provider, 16384)
        assert receipt is not None and receipt["status"] == "done", (receipt, captured, errors, logs)
        assert receipt["tests"][0]["status"] == "failed" and receipt["tests"][1]["status"] == "passed"
        assert receipt["provider_session_id"] and receipt["tokens_used"] is not None
        session = json.loads((spool / "claims" / f"{job.attempt_id}.repository-session.json").read_text())
        broker = next(item for item in session["diagnostics"] if item["type"] == "repository.broker")
        assert broker["forwarded_requests"] > 0 and broker["last_denial"] is None
        assert broker["retried_requests"] == 1
        assert (root / "repo" / "calculator.py").read_text().endswith("return a-b\n")
        assert b'"authorized": true' in logs
        assert (
            config.credential_file.read_bytes()
            not in (spool / "artifacts" / str(job.attempt_id) / "after.log").read_bytes()
        )
    finally:
        docker._remove("container", provider)
        docker._remove("network", network)
        if admitted is not None:
            codex_cli_admission._remove_tag_if_present(endpoint, admitted["image_tag"])
