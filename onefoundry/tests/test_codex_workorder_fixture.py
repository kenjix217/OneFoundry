"""Synthetic Codex CLI work-order bridge; no real provider or source checkout."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from onefoundry.external_code_schemas import ExternalCodeReceipt
from onefoundry.settings import Settings
from runner import codex_cli_admission
from runner import codex_workorder_fixture as bridge
from runner import docker_egress_proof as egress
from runner.codex_cli_admission import DockerEndpoint, build_and_prove
from runner.docker_egress_proof import ProofError
from runner.fixture_git import FIXTURE, Deadline, _test
from tests.test_external_code_postgres import client_for, heartbeat, launch, post, ready_order
from tests.test_external_service import _job


def test_fixture_waits_for_the_latest_command_before_completing() -> None:
    from runner.codex_workorder_fixture_child import _next_command

    initial = _next_command([])
    assert initial is not None and initial[0] == "exec_command"
    pending = {"type": "function_call_output", "output": "Process running with session ID 123\n"}
    assert _next_command([pending]) == (
        "write_stdin",
        {"session_id": 123, "chars": "", "yield_time_ms": 1000},
    )
    done = {"type": "function_call_output", "output": "Process exited with code 0\n"}
    assert _next_command([pending, done]) is None
    resumed = _next_command([done, pending])
    assert resumed is not None and resumed[0] == "write_stdin"


@pytest.mark.parametrize("output", ["", "Process exited with code 1\n", "Process still running", {}, None])
def test_fixture_rejects_failed_or_unverified_command_output(output: object) -> None:
    from runner.codex_workorder_fixture_child import _next_command

    with pytest.raises(ValueError, match="fixture_command_not_completed"):
        _next_command([{"type": "function_call_output", "output": output}])


@pytest.fixture
def admitted_images() -> Iterator[tuple[str, str]]:
    executable = shutil.which("docker")
    assert executable is not None
    endpoint = DockerEndpoint(Path(executable).resolve(), Path("/var/run/docker.sock"))
    fixture_tag = os.environ["ONEFOUNDRY_EGRESS_TEST_IMAGE"]
    fixture_image_id = egress._inspect("image", fixture_tag)["Id"]
    assert isinstance(fixture_image_id, str) and bridge.SHA_IMAGE.fullmatch(fixture_image_id)
    admitted = build_and_prove(endpoint)
    try:
        image_id = admitted["image_id"]
        assert codex_cli_admission._inspect(endpoint, "image", admitted["image_tag"])["Id"] == image_id
        yield image_id, fixture_image_id
        # The exact admitted image remained available throughout the attempt.
        assert codex_cli_admission._inspect(endpoint, "image", admitted["image_tag"])["Id"] == image_id
    finally:
        codex_cli_admission._remove_tag_if_present(endpoint, admitted["image_tag"])


def _unit_fixture_test(
    repo: Path, deadline: Deadline, log_limit: int, name: str, *, image_id: str
) -> tuple[dict[str, object], bytes]:
    """Exercise the one-shot receipt contract without starting Docker in a unit test."""
    return _test(repo, deadline, log_limit, name)


def test_rejects_unpinned_image_after_claim_without_invoking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "run_isolated_fixture_test", _unit_fixture_test)
    spool = tmp_path / "external"
    job = _job(spool)
    receipt = bridge.process_synthetic_codex_job(job, spool, "codex:latest", "fixture:latest")
    assert receipt is not None
    assert receipt["status"] == "unknown"
    assert receipt["reason_code"] == "synthetic_codex_prelaunch_invalid"
    assert receipt["tests"] == [{"name": "before", "status": "failed", "exit_code": 1}]
    assert list((spool / "claims").glob("*.launch.json")) == []
    assert list((spool / "claims").glob("*.session.json")) == []
    assert bridge.process_synthetic_codex_job(job, spool, "codex:latest", "fixture:latest") is None
    assert len(list((spool / "claims").glob("*.claim"))) == 1
    assert len(list((spool / "receipts").glob("*.json"))) == 1


def test_ambiguous_docker_launch_is_unknown_and_never_relaunched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "run_isolated_fixture_test", _unit_fixture_test)
    spool = tmp_path / "external"
    job = _job(spool)
    attempts: list[str] = []

    def uncertain(*_args: object) -> None:
        attempts.append("launched")
        raise ProofError("docker_wait_unavailable")

    monkeypatch.setattr(bridge, "invoke_synthetic_codex", uncertain)
    receipt = bridge.process_synthetic_codex_job(job, spool, "sha256:" + "a" * 64, "sha256:" + "b" * 64)
    assert receipt is not None
    assert receipt["status"] == "unknown"
    assert receipt["reason_code"] == "synthetic_codex_launch_uncertain"
    assert [item.kind for item in ExternalCodeReceipt.model_validate(receipt).artifacts] == ["trace"]
    assert bridge.process_synthetic_codex_job(job, spool, "sha256:" + "a" * 64, "sha256:" + "b" * 64) is None
    assert attempts == ["launched"]


@pytest.mark.integration
def test_api_work_order_runs_pinned_synthetic_codex_and_reconciles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, admitted_images: tuple[str, str]
) -> None:
    image_id, fixture_image_id = admitted_images
    assert bridge.SHA_IMAGE.fullmatch(image_id)
    assert bridge.SHA_IMAGE.fullmatch(fixture_image_id)
    original = hashlib.sha256((FIXTURE / "calculator.py").read_bytes()).hexdigest()
    secrets = tmp_path / "model-secrets"
    secrets.mkdir(mode=0o700)
    monkeypatch.setenv("MODEL_SECRET_ROOT", str(secrets))
    spool = tmp_path / "external"
    for name in ("jobs", "claims", "receipts", "artifacts"):
        (spool / name).mkdir(parents=True, exist_ok=True)
    # A test-only heartbeat admits this isolated API client. The installed
    # external runner continues reporting unavailable capabilities.
    heartbeat(spool)
    with client_for(spool, enabled=True) as client:
        project_id, order_id = ready_order(client)
        created = launch(client, project_id, order_id)
        assert created.status_code == 201, created.text
        launched = created.json()
        run_id = launched["run"]["id"]
        attempt_id = launched["attempts"][0]["id"]
        path = spool / "jobs" / f"{attempt_id}.json"
        manifest = json.loads(path.read_text())
        assert manifest["fixture_id"] == "engineer_qa_v1"
        unsigned = {key: value for key, value in manifest.items() if key != "request_sha256"}
        assert (
            hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            == manifest["request_sha256"]
        )
        receipt = bridge.process_synthetic_codex_job(path, spool, image_id, fixture_image_id)
        assert receipt is not None and receipt["status"] == "done"
        validated = ExternalCodeReceipt.model_validate(receipt)
        assert [(item.name, item.status) for item in validated.tests] == [
            ("before", "failed"),
            ("after", "passed"),
        ]
        assert receipt["provider_session_id"] and receipt["tokens_used"] == 28
        assert receipt["cost_usd"] is None
        log_meta = next(item for item in validated.artifacts if item.kind == "log")
        log = (spool / log_meta.path).read_bytes()
        expected_prompt = f"{manifest['task_title']}\n\n{manifest['task_instructions']}\n"
        assert f"prompt_sha256={hashlib.sha256(expected_prompt.encode()).hexdigest()}".encode() in log
        assert f"image_sha256={image_id}".encode() in log
        assert b"model=gpt-5 command=fixture_calculator_repair" in log
        assert b"Ran 1 test" in log and b"OK" in log
        assert b"fixture_test=passed" not in log
        assert bridge.process_synthetic_codex_job(path, spool, image_id, fixture_image_id) is None
        assert len(list((spool / "claims").glob("*.claim"))) == 1
        assert len(list((spool / "receipts").glob("*.json"))) == 1
        assert hashlib.sha256((FIXTURE / "calculator.py").read_bytes()).hexdigest() == original
        receipt_bytes = (spool / "receipts" / f"{attempt_id}.json").read_bytes()
        assert b"local-test-only" not in receipt_bytes

        reconciled = post(client, f"/api/projects/{project_id}/runs/{run_id}/external-code/reconcile")
        assert reconciled.status_code == 200, reconciled.text
        detail = reconciled.json()
        assert detail["run"]["status"] == "done"
        attempt = detail["attempts"][0]
        assert attempt["receipt_sha256"] == hashlib.sha256(receipt_bytes).hexdigest()
        assert attempt["diff_sha256"] == receipt["diff_sha256"]
        assert [item["kind"] for item in attempt["artifacts"]] == ["diff", "log", "trace"]
        for artifact in attempt["artifacts"]:
            data = (spool / artifact["path"]).read_bytes()
            assert hashlib.sha256(data).hexdigest() == artifact["sha256"]
            preview = client.get(
                f"/api/projects/{project_id}/runs/{run_id}/external-code/artifacts/{artifact['id']}"
            )
            assert preview.status_code == 200
            assert preview.json()["sha256"] == artifact["sha256"]
        events = client.get(f"/api/projects/{project_id}/runs/{run_id}").json()["events"]
        assert [item["event_type"] for item in events].count("external_code_queued") == 1
        assert [item["event_type"] for item in events].count("external_code_receipt") == 1
        assert (
            post(client, f"/api/projects/{project_id}/runs/{run_id}/external-code/reconcile").json() == detail
        )
        engine = create_engine(
            Settings(
                db_user=os.environ.get("APP_DB_USER", "onefoundry_app"),
                db_password=os.environ["APP_DB_PASSWORD"],
            ).database_url
        )
        try:
            with engine.connect() as connection:
                row = connection.execute(
                    text(
                        "SELECT status,receipt_sha256,diff_sha256 FROM external_code_attempts "
                        "WHERE project_id=:project_id AND id=:attempt_id"
                    ),
                    {"project_id": project_id, "attempt_id": attempt_id},
                ).one()
                assert row.status == "done"
                assert row.receipt_sha256 == attempt["receipt_sha256"]
                assert row.diff_sha256 == receipt["diff_sha256"]
                artifact_count = connection.scalar(
                    text(
                        "SELECT count(*) FROM external_code_artifacts WHERE project_id=:project_id "
                        "AND attempt_id=:attempt_id"
                    ),
                    {"project_id": project_id, "attempt_id": attempt_id},
                )
                assert artifact_count == 2
        finally:
            engine.dispose()
