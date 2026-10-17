"""Canonical claim and receipt-order checks for the host Docker volume bridge."""

import hashlib
import json
import os
import subprocess
import sys
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest

from runner import host_probe_bridge, probe_volume_bridge


def _spool(path: Path) -> Path:
    for name in ("jobs", "claims", "receipts", "artifacts"):
        (path / name).mkdir(parents=True)
    return path


def test_canonical_claim_is_invisible_until_complete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "attempt.claim"
    data = b"complete-claim-bytes"
    before_link = Event()
    allow_link = Event()
    original_link = os.link

    def pause_link(source: Path, destination: Path, *, follow_symlinks: bool = True) -> None:
        assert source.read_bytes() == data
        assert destination == target
        before_link.set()
        assert allow_link.wait(timeout=5)
        original_link(source, destination, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "link", pause_link)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(probe_volume_bridge._new, target, data)
        assert before_link.wait(timeout=5)
        assert not target.exists()
        allow_link.set()
        pending.result(timeout=5)
    assert target.read_bytes() == data
    monkeypatch.setattr(os, "link", original_link)
    with pytest.raises(FileExistsError):
        probe_volume_bridge._new(target, b"different")
    assert target.read_bytes() == data


def test_claim_is_canonical_and_exclusive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spool = _spool(tmp_path / "spool")
    monkeypatch.setattr(probe_volume_bridge, "SPOOL", spool)
    attempt = str(uuid4())
    manifest = {
        "schema_version": 2,
        "attempt_id": attempt,
        "job_id": attempt,
        "adapter_id": "offline_probe",
        "target_id": "local_source_probe_v1",
    }
    (spool / "jobs" / f"{attempt}.json").write_text(json.dumps(manifest))
    probe_volume_bridge.claim(attempt)
    assert (spool / "claims" / f"{attempt}.claim").read_bytes() == attempt.encode()
    with pytest.raises(FileExistsError):
        probe_volume_bridge.claim(attempt)


def test_old_claims_do_not_hide_a_new_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    spool = _spool(tmp_path / "spool")
    monkeypatch.setattr(probe_volume_bridge, "SPOOL", spool)
    for index in range(1001):
        attempt = f"00000000-0000-4000-8000-{index:012x}"
        (spool / "jobs" / f"{attempt}.json").write_text("{}")
        if index < 1000:
            (spool / "claims" / f"{attempt}.claim").write_text(attempt)
    probe_volume_bridge.list_jobs()
    assert capsys.readouterr().out.strip() == "00000000-0000-4000-8000-0000000003e8"


def test_publish_checks_artifacts_before_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spool = _spool(tmp_path / "spool")
    transfer = tmp_path / "transfer"
    transfer.mkdir()
    monkeypatch.setattr(probe_volume_bridge, "SPOOL", spool)
    monkeypatch.setattr(probe_volume_bridge, "TRANSFER", transfer)
    attempt = str(uuid4())
    digest = "a" * 64
    (spool / "claims" / f"{attempt}.claim").write_text(attempt)
    (spool / "jobs" / f"{attempt}.json").write_text(json.dumps({"request_sha256": digest}))
    local = transfer / attempt
    local.mkdir()
    artifact = b"offline probe diff\n"
    receipt = {
        "schema_version": 2,
        "attempt_id": attempt,
        "job_id": attempt,
        "adapter_id": "offline_probe",
        "target_id": "local_source_probe_v1",
        "request_sha256": digest,
        "artifacts": [
            {
                "path": f"artifacts/{attempt}/change.diff",
                "sha256": hashlib.sha256(artifact).hexdigest(),
                "byte_size": len(artifact),
            }
        ],
    }
    (local / "receipt.json").write_text(json.dumps(receipt))
    (local / "change.diff").write_bytes(b"wrong")
    with pytest.raises(ValueError, match="artifact size mismatch"):
        probe_volume_bridge.publish(attempt)
    assert not (spool / "receipts" / f"{attempt}.json").exists()
    (local / "change.diff").unlink()
    (local / "change.diff").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError, match="artifact size mismatch"):
        probe_volume_bridge.publish(attempt)
    assert not (spool / "receipts" / f"{attempt}.json").exists()
    (local / "change.diff").unlink()
    (local / "change.diff").write_bytes(artifact)
    probe_volume_bridge.publish(attempt)
    assert (spool / "artifacts" / attempt / "change.diff").read_bytes() == artifact
    assert (spool / "receipts" / f"{attempt}.json").is_file()
    probe_volume_bridge.publish(attempt)  # Interrupted publication can finish idempotently.


def test_docker_bridge_confines_transient_container(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[str] = []

    def fake_run(command: list[str], **_: object) -> SimpleNamespace:
        observed.extend(command)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    host_probe_bridge._docker("onefoundry_runner_spool", "onefoundry-external-runner", "list")
    for required in ("--network", "none", "--read-only", "--cap-drop", "ALL", "--pids-limit", "64"):
        assert required in observed
    assert "docker.sock" not in " ".join(observed)
    assert "--pull=never" in observed
    assert f"10001:{os.getgid()}" in observed
    with pytest.raises(ValueError, match="invalid Docker"):
        host_probe_bridge._docker("unexpected,volume", "onefoundry-external-runner", "list")


def test_unavailable_image_fails_without_network_pull(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[str] = []

    def missing_image(command: list[str], **_: object) -> SimpleNamespace:
        observed.extend(command)
        return SimpleNamespace(returncode=125, stdout=b"", stderr=b"image unavailable")

    monkeypatch.setattr(subprocess, "run", missing_image)
    with pytest.raises(RuntimeError, match="exit 125"):
        host_probe_bridge._docker("onefoundry_runner_spool", "missing-image", "list")
    assert "--pull=never" in observed
    assert "none" in observed


def test_unavailable_self_test_does_not_claim_jobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "private-state"
    root = tmp_path / "admitted-root"
    root.mkdir()
    published: list[bytes] = []

    def unavailable(_args: object, spool: Path, _attempt: str | None = None) -> None:
        (spool / "probe_capabilities.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "isolated": False,
                    "source_root_ready": True,
                    "available": False,
                    "reason": "isolation_unavailable",
                }
            )
        )

    def publish(_args: object, source: Path) -> None:
        published.append(source.read_bytes())

    def no_docker(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("unavailable supervisor must not list or claim jobs")

    monkeypatch.setattr(host_probe_bridge, "_supervisor", unavailable)
    monkeypatch.setattr(host_probe_bridge, "_publish_capability", publish)
    monkeypatch.setattr(host_probe_bridge, "_docker", no_docker)
    args = Namespace(
        state_root=state,
        admitted_root=root,
        bwrap=Path("/usr/bin/bwrap"),
        publish_existing=None,
    )
    host_probe_bridge._run(args)
    assert published and b'"available": false' in published[0]


def test_watch_rejects_unbounded_poll(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "host_probe_bridge",
            "--volume",
            "onefoundry_runner_spool",
            "--image",
            "onefoundry-external-runner",
            "--admitted-root",
            str(tmp_path),
            "--state-root",
            str(tmp_path / "state"),
            "--stage-max-files",
            "1",
            "--stage-max-bytes",
            "1",
            "--stage-max-tree-bytes",
            "1",
            "--stage-wall-seconds",
            "1",
            "--watch",
            "--poll-seconds",
            "31",
        ],
    )
    with pytest.raises(SystemExit) as raised:
        host_probe_bridge.main()
    assert raised.value.code == 2
