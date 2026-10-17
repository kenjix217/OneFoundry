"""Host admission and receipt tests for extension workers."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from onefoundry_sdk import ExtensionManifest, build_package, inspect_package

from runner import extension_worker_host as worker


def _policy() -> worker.WorkerPolicy:
    return worker.WorkerPolicy(
        image_id="sha256:" + "a" * 64,
        docker_binary=Path("/usr/bin/docker"),
        docker_socket=Path("/var/run/docker.sock"),
        memory_mb=256,
        processes=16,
        cpus=1,
        max_timeout_seconds=20,
        max_output_bytes=4096,
    )


def _job(policy: worker.WorkerPolicy) -> dict[str, object]:
    manifest = ExtensionManifest.model_validate(
        {
            "id": "worker-fixture",
            "version": "1.0.0",
            "name": "Worker fixture",
            "tools": [
                {
                    "id": "calculate",
                    "description": "Return a value",
                    "parameters": {"type": "object", "properties": {"value": {"type": "integer"}}},
                    "returns": {"type": "object", "properties": {"answer": {"type": "integer"}}},
                    "effect": "pure",
                    "runtime": "python",
                    "entrypoint": "calculator:calculate",
                }
            ],
        }
    )
    archive = build_package(
        manifest,
        {"src/calculator.py": b"def calculate(context, value):\n    return {'answer': value}\n"},
    )
    identity = str(uuid4())
    job: dict[str, object] = {
        "schema_version": 1,
        "invocation_id": identity,
        "project_id": identity,
        "actor_id": "agent:qa",
        "run_id": identity,
        "profile_id": identity,
        "package_sha256": inspect_package(archive).package_sha256,
        "archive_base64": base64.b64encode(archive).decode(),
        "tool_id": "calculate",
        "arguments": {"value": 4},
        "timeout_seconds": 5,
        "max_output_bytes": 4096,
        "worker_image": policy.image_id,
        "memory_mb": policy.memory_mb,
        "processes": policy.processes,
        "cpus": policy.cpus,
        "execution_kind": "python",
    }
    job["job_sha256"] = worker.canonical_job_sha256(job)
    return job


def test_job_admission_rejects_tampering_and_unbounded_input(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = _policy()
    monkeypatch.setattr(worker.WorkerPolicy, "validate", lambda self: None)
    job = _job(policy)
    worker.validate_job(job, policy)
    job["arguments"] = {"value": "not an integer"}
    with pytest.raises(worker.WorkerError, match="worker_job_hash_mismatch"):
        worker.validate_job(job, policy)
    job["job_sha256"] = worker.canonical_job_sha256(job)
    with pytest.raises(worker.WorkerError, match="worker_tool_input_invalid"):
        worker.validate_job(job, policy)


def test_attempt_identity_is_hash_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = _policy()
    monkeypatch.setattr(worker.WorkerPolicy, "validate", lambda self: None)
    job = _job(policy)
    job["attempt_id"] = str(uuid4())
    job["job_sha256"] = worker.canonical_job_sha256(job)
    worker.validate_job(job, policy)
    job["attempt_id"] = str(uuid4())
    with pytest.raises(worker.WorkerError, match="worker_job_hash_mismatch"):
        worker.validate_job(job, policy)


def test_expired_job_never_reaches_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = _policy()
    monkeypatch.setattr(worker.WorkerPolicy, "validate", lambda self: None)
    monkeypatch.setattr(worker, "_docker", lambda *args, **kwargs: pytest.fail("expired job launched"))
    job = _job(policy)
    job["expires_at"] = "2000-01-01T00:00:00Z"
    job["job_sha256"] = worker.canonical_job_sha256(job)
    assert worker.job_expired(job, now=datetime(2000, 1, 1, tzinfo=UTC))
    receipt = worker.run_job(job, policy)
    assert receipt["status"] == "failed"
    assert receipt["reason"] == "worker_job_expired"
    assert receipt["job_sha256"] == job["job_sha256"]
    for invalid in ("2000-01-01", "2000-02-30T00:00:00Z", "2000-01-01T00:00:00+01:00"):
        job["expires_at"] = invalid
        job["job_sha256"] = worker.canonical_job_sha256(job)
        with pytest.raises(worker.WorkerError, match="worker_job_expiry_invalid"):
            worker.validate_job(job, policy)


def test_container_boundary_checks_every_required_isolation_field() -> None:
    policy = _policy()
    host = {
        "NetworkMode": "none",
        "ReadonlyRootfs": True,
        "Binds": None,
        "CapDrop": ["ALL"],
        "SecurityOpt": ["no-new-privileges:true"],
        "PidsLimit": 16,
        "Memory": 256 * 1024 * 1024,
        "MemorySwap": 256 * 1024 * 1024,
        "NanoCpus": 1_000_000_000,
        "Tmpfs": {worker.CONTAINER_TMP: "rw,nosuid,nodev,size=128m"},
    }
    state = {"Image": policy.image_id, "HostConfig": host, "Config": {"User": "1000:1000"}, "Mounts": []}
    assert worker._boundary_valid(state, policy)
    for key in tuple(host):
        bad = {**state, "HostConfig": {**host, key: ["/host:/guest"] if key == "Binds" else None}}
        assert not worker._boundary_valid(bad, policy), key


def test_bounded_host_receipt_and_private_atomic_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    policy = _policy()
    monkeypatch.setattr(worker.WorkerPolicy, "validate", lambda self: None)
    monkeypatch.setattr(worker, "_docker", lambda *args, **kwargs: "container-id")
    monkeypatch.setattr(worker, "_inspect", lambda *args: {"State": {"ExitCode": 0}})
    monkeypatch.setattr(worker, "_boundary_valid", lambda *args: True)
    monkeypatch.setattr(
        worker,
        "_start_bounded",
        lambda *args: (b'{"kind":"result","status":"success","output":{"answer":5}}', None),
    )
    job = _job(policy)
    job_path = tmp_path / "job.json"
    job_path.write_text(json.dumps(job))
    job_path.chmod(0o600)
    receipts = tmp_path / "receipts"
    receipt = worker.process_job_file(job_path, receipts, policy)
    assert receipt["status"] == "done"
    assert receipt["result"] == {"answer": 5}
    receipt_path = receipts / f"{job['invocation_id']}.json"
    assert receipt_path.stat().st_mode & 0o777 == 0o600
    assert json.loads(receipt_path.read_text()) == receipt
    with pytest.raises(worker.WorkerError, match="worker_receipt_already_exists"):
        worker.process_job_file(job_path, receipts, policy)


def test_nested_unknown_keeps_exact_child_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = _policy()
    monkeypatch.setattr(worker.WorkerPolicy, "validate", lambda self: None)
    monkeypatch.setattr(worker, "_docker", lambda *args, **kwargs: "container-id")
    monkeypatch.setattr(worker, "_inspect", lambda *args: {"State": {"ExitCode": 0}})
    monkeypatch.setattr(worker, "_boundary_valid", lambda *args: True)
    nested_id = str(uuid4())
    monkeypatch.setattr(
        worker,
        "_start_bounded",
        lambda *args: (
            b'{"kind":"result","status":"error","error_code":"connection_outcome_unknown"}',
            {"status": "unknown", "waiting_invocation_id": nested_id},
        ),
    )
    receipt = worker.run_job(_job(policy), policy)
    assert receipt["status"] == "unknown"
    assert receipt["reason"] == "connection_outcome_unknown"
    assert receipt["waiting_invocation_id"] == nested_id
