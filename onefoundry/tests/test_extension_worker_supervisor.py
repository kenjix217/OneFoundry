"""Durable worker claim, cancellation, and callback admission checks."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from onefoundry_sdk import (
    ConnectionTemplate,
    ExtensionManifest,
    OperationSpec,
    ToolSpec,
    build_package,
    inspect_package,
)

from runner import extension_worker_host as host
from runner import extension_worker_supervisor as supervisor
from tests.test_extension_worker_host import _job, _policy


def _prepare(tmp_path: Path) -> Path:
    for name in ("jobs", "claims", "receipts", "cancellations", "callbacks/requests", "callbacks/responses"):
        (tmp_path / name).mkdir(parents=True, mode=0o700)
    return tmp_path


def test_supervisor_rejects_spool_owned_by_different_uid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    tmp_path.chmod(0o700)
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(tmp_path).st_uid + 1)
    with pytest.raises(host.WorkerError, match="worker_spool_not_private"):
        supervisor.process_one(tmp_path, _policy())


def test_heartbeat_replaces_private_matching_policy(tmp_path: Path) -> None:
    policy = _policy()
    supervisor_id = uuid4()
    first = datetime(2026, 1, 2, tzinfo=UTC)
    supervisor._write_heartbeat(tmp_path, policy, supervisor_id, first)
    path = tmp_path / "heartbeat.json"
    assert path.stat().st_mode & 0o777 == 0o600
    record = json.loads(path.read_text())
    assert record == {
        "schema_version": 1,
        "image_id": policy.image_id,
        "updated_at": "2026-01-02T00:00:00Z",
        "supervisor_id": str(supervisor_id),
        "memory_mb": policy.memory_mb,
        "processes": policy.processes,
        "cpus": policy.cpus,
        "max_timeout_seconds": policy.max_timeout_seconds,
        "max_output_bytes": policy.max_output_bytes,
    }
    supervisor._write_heartbeat(tmp_path, policy, supervisor_id, datetime(2026, 1, 2, 0, 0, 1, tzinfo=UTC))
    assert json.loads(path.read_text())["updated_at"] == "2026-01-02T00:00:01Z"
    assert not list(tmp_path.glob(".heartbeat-*"))


def test_one_shot_supervisor_does_not_claim_live_readiness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(supervisor, "_policy_from_config", lambda _: (tmp_path, _policy(), 0.1))
    monkeypatch.setattr(supervisor, "process_one", lambda *args: None)
    monkeypatch.setattr(sys, "argv", ["extension-worker-supervisor", "--config", "unused", "--once"])
    assert supervisor.main() == 0
    assert not (tmp_path / "heartbeat.json").exists()


def test_interrupted_claim_yields_unknown_without_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _prepare(tmp_path)
    policy = _policy()
    monkeypatch.setattr(host.WorkerPolicy, "validate", lambda self: None)
    job = _job(policy)
    job["expires_at"] = "2000-01-01T00:00:00Z"
    job["job_sha256"] = host.canonical_job_sha256(job)
    name = f"{job['invocation_id']}.json"
    (root / "jobs" / name).write_text(json.dumps(job))
    (root / "jobs" / name).chmod(0o600)
    (root / "claims" / name).write_text(str(job["job_sha256"]))
    (root / "claims" / name).chmod(0o600)
    monkeypatch.setattr(supervisor, "process_job_file", lambda *args: pytest.fail("replayed claim"))
    result = supervisor.process_one(root, policy)
    assert result is not None and result["status"] == "unknown"
    assert json.loads((root / "receipts" / name).read_text()) == result
    assert supervisor.process_one(root, policy) is None


def test_cancel_before_launch_has_exact_failed_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _prepare(tmp_path)
    policy = _policy()
    monkeypatch.setattr(host.WorkerPolicy, "validate", lambda self: None)
    job = _job(policy)
    name = f"{job['invocation_id']}.json"
    (root / "jobs" / name).write_text(json.dumps(job))
    (root / "jobs" / name).chmod(0o600)
    (root / "cancellations" / name).write_text("cancel")
    result = supervisor.process_one(root, policy)
    assert result is not None and result["status"] == "failed"
    assert result["reason"] == "worker_cancelled_before_launch"


def test_expired_unclaimed_job_receipt_has_no_claim_or_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _prepare(tmp_path)
    policy = _policy()
    monkeypatch.setattr(host.WorkerPolicy, "validate", lambda self: None)
    monkeypatch.setattr(supervisor, "process_job_file", lambda *args: pytest.fail("expired job launched"))
    job = _job(policy)
    job["expires_at"] = "2000-01-01T00:00:00Z"
    job["job_sha256"] = host.canonical_job_sha256(job)
    name = f"{job['invocation_id']}.json"
    (root / "jobs" / name).write_text(json.dumps(job))
    (root / "jobs" / name).chmod(0o600)
    receipt = supervisor.process_one(root, policy)
    assert receipt is not None and receipt["status"] == "failed"
    assert receipt["reason"] == "worker_job_expired"
    assert not (root / "claims" / name).exists()
    assert json.loads((root / "receipts" / name).read_text()) == receipt


def test_declared_connection_callback_requires_pure_tool_and_exact_operation(tmp_path: Path) -> None:
    manifest = ExtensionManifest(
        id="callback-fixture",
        version="1.0.0",
        name="Callback fixture",
        connections=[
            ConnectionTemplate(
                id="records",
                name="Records",
                kind="mcp",
                operations=[OperationSpec(id="lookup")],
            )
        ],
        tools=[
            ToolSpec(
                id="brief",
                description="Brief",
                parameters={"type": "object"},
                returns={"type": "object"},
                effect="pure",
                runtime="python",
                entrypoint="brief:run",
                required_connections=["records"],
            )
        ],
    )
    archive = build_package(manifest, {"src/brief.py": b"def run(context): return {}\n"})
    job = _job(_policy())
    import base64

    job["archive_base64"] = base64.b64encode(archive).decode()
    job["package_sha256"] = inspect_package(archive).package_sha256
    job["tool_id"] = "brief"
    job["arguments"] = {}
    job["job_sha256"] = host.canonical_job_sha256(job)
    for name in ("requests", "responses"):
        (tmp_path / name).mkdir(mode=0o700)
    frame = {
        "kind": "connection_request",
        "job_sha256": job["job_sha256"],
        "sequence": 1,
        "slot": "records",
        "operation": "wrong",
        "arguments": {},
    }
    with pytest.raises(host.WorkerError, match="worker_callback_not_declared"):
        host._callback(frame, job, tmp_path, 1, 0)

    unsafe = manifest.model_copy(deep=True)
    unsafe.tools[0].effect = "write"
    unsafe_archive = build_package(unsafe, {"src/brief.py": b"def run(context): return {}\n"})
    job["archive_base64"] = base64.b64encode(unsafe_archive).decode()
    job["package_sha256"] = inspect_package(unsafe_archive).package_sha256
    job["job_sha256"] = host.canonical_job_sha256(job)
    policy = _policy()
    # The image check is independent of the Docker daemon in this admission test.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(host.WorkerPolicy, "validate", lambda self: None)
        with pytest.raises(host.WorkerError, match="worker_connection_parent_effect_unsupported"):
            host.validate_job(job, policy)
