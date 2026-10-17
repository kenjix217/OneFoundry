"""Pinned Claude Code synthetic work order; no real account or provider."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest

from onefoundry.external_code_schemas import ExternalCodeReceipt
from onefoundry.runner_spool import RunnerSpool
from runner import claude_cli_admission
from runner import claude_workorder_fixture as bridge
from runner import docker_egress_proof as egress
from runner.claude_cli_admission import DockerEndpoint, build_and_prove
from runner.docker_egress_proof import ProofError
from runner.fixture_git import FIXTURE, Deadline, _test
from tests.test_external_service import _job


@pytest.fixture
def admitted_images() -> Iterator[tuple[str, str]]:
    executable = shutil.which("docker")
    assert executable is not None
    endpoint = DockerEndpoint(Path(executable).resolve(), Path("/var/run/docker.sock"))
    fixture_image = egress._inspect("image", os.environ["ONEFOUNDRY_EGRESS_TEST_IMAGE"])["Id"]
    assert bridge.IMAGE.fullmatch(fixture_image)
    admitted = build_and_prove(endpoint)
    try:
        assert (
            claude_cli_admission._inspect(endpoint, "image", admitted["image_tag"])["Id"]
            == admitted["image_id"]
        )
        yield admitted["image_id"], fixture_image
    finally:
        claude_cli_admission._remove_tag_if_present(endpoint, admitted["image_tag"])


def _unit_fixture_test(
    repo: Path, deadline: Deadline, log_limit: int, name: str, *, image_id: str
) -> tuple[dict[str, object], bytes]:
    """Exercise the one-shot receipt contract without starting Docker in a unit test."""
    return _test(repo, deadline, log_limit, name)


def test_rejects_unpinned_image_after_claim_without_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "run_isolated_fixture_test", _unit_fixture_test)
    spool = tmp_path / "external"
    job = _job(spool, "claude_code")
    receipt = bridge.process_synthetic_claude_job(job, spool, "claude:latest", "fixture:latest")
    assert receipt is not None
    assert receipt["status"] == "unknown"
    assert receipt["reason_code"] == "synthetic_claude_prelaunch_invalid"
    assert not list((spool / "claims").glob("*.launch.json"))
    assert bridge.process_synthetic_claude_job(job, spool, "claude:latest", "fixture:latest") is None
    assert len(list((spool / "claims").glob("*.claim"))) == 1


def test_ambiguous_launch_is_unknown_without_relaunch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "run_isolated_fixture_test", _unit_fixture_test)
    spool = tmp_path / "external"
    job = _job(spool, "claude_code")
    attempts: list[str] = []

    def uncertain(*_args: object) -> None:
        attempts.append("launched")
        raise ProofError("docker_wait_unavailable")

    monkeypatch.setattr(bridge, "invoke_synthetic_claude", uncertain)
    image = "sha256:" + "a" * 64
    fixture = "sha256:" + "b" * 64
    receipt = bridge.process_synthetic_claude_job(job, spool, image, fixture)
    assert receipt is not None
    assert receipt["status"] == "unknown"
    assert receipt["reason_code"] == "synthetic_claude_launch_uncertain"
    assert [item.kind for item in ExternalCodeReceipt.model_validate(receipt).artifacts] == ["trace"]
    assert bridge.process_synthetic_claude_job(job, spool, image, fixture) is None
    assert attempts == ["launched"]
    intent = json.loads(next((spool / "claims").glob("*.launch.json")).read_text())
    session = json.loads(next((spool / "claims").glob("*.session.json")).read_text())
    assert intent["invocation_kind"] == "synthetic_claude_fixture"
    assert intent["executable_ref"] == image
    assert session["provider_status"] == "unknown"
    manifest = json.loads(job.read_text())
    evidence = RunnerSpool(str(spool)).read_launch_evidence(
        UUID(manifest["attempt_id"]),
        UUID(manifest["project_id"]),
        UUID(manifest["run_id"]),
        manifest["request_sha256"],
    )
    assert evidence is not None and evidence["provider_status"] == "unknown"


@pytest.mark.integration
def test_real_pinned_claude_repairs_disposable_work_order(
    tmp_path: Path, admitted_images: tuple[str, str]
) -> None:
    image, fixture_image = admitted_images
    original = hashlib.sha256((FIXTURE / "calculator.py").read_bytes()).hexdigest()
    spool = tmp_path / "external"
    job = _job(spool, "claude_code")
    manifest = json.loads(job.read_text())
    receipt = bridge.process_synthetic_claude_job(job, spool, image, fixture_image)
    assert receipt is not None and receipt["status"] == "done", receipt
    validated = ExternalCodeReceipt.model_validate(receipt)
    assert [(test.name, test.status) for test in validated.tests] == [
        ("before", "failed"),
        ("after", "passed"),
    ]
    assert receipt["provider_session_id"]
    assert receipt["tokens_used"] is not None
    # The local fixture reports synthetic usage. It cannot prove billed cost.
    assert receipt["cost_usd"] is None
    log = (spool / next(item.path for item in validated.artifacts if item.kind == "log")).read_bytes()
    expected_prompt = f"{manifest['task_title']}\n\n{manifest['task_instructions']}\n"
    assert f"prompt_sha256={hashlib.sha256(expected_prompt.encode()).hexdigest()}".encode() in log
    assert f"image_sha256={image}".encode() in log
    assert b"allowed_provider_requests=4" in log
    assert b"Ran 1 test" in log and b"OK" in log
    assert b"local-test-only" not in log
    diff = (spool / next(item.path for item in validated.artifacts if item.kind == "diff")).read_bytes()
    assert b"+    return value + 1" in diff
    assert bridge.process_synthetic_claude_job(job, spool, image, fixture_image) is None
    assert hashlib.sha256((FIXTURE / "calculator.py").read_bytes()).hexdigest() == original
    assert b"local-test-only" not in next((spool / "receipts").glob("*.json")).read_bytes()
    intent = json.loads(next((spool / "claims").glob("*.launch.json")).read_text())
    session = json.loads(next((spool / "claims").glob("*.session.json")).read_text())
    assert intent["invocation_kind"] == "synthetic_claude_fixture"
    assert intent["request_sha256"] == manifest["request_sha256"]
    assert session["provider_session_id"] == receipt["provider_session_id"]
    evidence = RunnerSpool(str(spool)).read_launch_evidence(
        UUID(manifest["attempt_id"]),
        UUID(manifest["project_id"]),
        UUID(manifest["run_id"]),
        manifest["request_sha256"],
    )
    assert evidence is not None and evidence["session_observed"] is True
