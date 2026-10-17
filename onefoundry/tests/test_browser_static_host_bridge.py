"""The Docker-capable host takes one browser claim without sharing its socket."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import struct
import sys
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest

from onefoundry.browser_static_fixtures import ApprovedStaticFixture
from onefoundry.browser_target import BrowserTarget, BrowserTargetLimits
from runner import browser_static_host_bridge as host
from runner import browser_static_volume_bridge as volume
from runner.browser_static_job_spool import BrowserJobError, BrowserJobTransient, BrowserStaticJobSpool
from runner.browser_static_supervisor import StaticBrowserReceipt

IMAGE = "sha256:" + "a" * 64


def fixture() -> ApprovedStaticFixture:
    now = datetime.now(UTC)
    target = BrowserTarget.bind(
        project_id=uuid4(),
        run_id=uuid4(),
        actor_id="operator",
        approved_url="http://127.0.0.1:8765/fixture.html",
        expires_at=now + timedelta(seconds=120),
        idempotency_key=uuid4(),
        viewport_width=800,
        viewport_height=600,
        max_page_bytes=262_144,
        timeout_seconds=10,
        limits=BrowserTargetLimits(2048, 1920, 1200, 262_144, 30, 300),
        now=now,
    )
    html, css = b"<title>Fixture</title><main>Approved page</main>", b"main{color:navy}"
    return ApprovedStaticFixture(
        uuid4(), target, html, css, hashlib.sha256(html + b"\0" + css).hexdigest(), "b" * 64
    )


def spool(tmp_path: Path) -> BrowserStaticJobSpool:
    root = tmp_path / "browser-jobs"
    root.mkdir(mode=0o700)
    return BrowserStaticJobSpool(root)


def test_helper_claim_exports_exact_job_and_loses_to_timeout_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = spool(tmp_path)
    monkeypatch.setattr(volume, "SPOOL", transport.root)
    job_id = uuid4()
    manifest = transport.stage(job_id, fixture(), IMAGE)
    listed = volume.list_jobs()
    assert listed == [str(job_id)]
    exported = volume.claim(str(job_id))
    assert exported[0] == (transport.root / "jobs" / f"{job_id}.json").read_bytes()
    assert exported[1] == (transport.root / "sources" / f"{job_id}.bin").read_bytes()
    assert transport.claimed(job_id, manifest["request_sha256"])
    with pytest.raises(FileExistsError):
        volume.claim(str(job_id))

    other = uuid4()
    other_manifest = transport.stage(other, fixture(), IMAGE)
    assert transport.fence_unclaimed(other, other_manifest["request_sha256"])
    with pytest.raises(FileExistsError):
        volume.claim(str(other))


def test_helper_rejects_tampered_source_before_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    transport = spool(tmp_path)
    monkeypatch.setattr(volume, "SPOOL", transport.root)
    job_id = uuid4()
    transport.stage(job_id, fixture(), IMAGE)
    (transport.root / "sources" / f"{job_id}.bin").write_bytes(b"\0\0\0\4evil")
    with pytest.raises(ValueError, match="invalid job"):
        volume.claim(str(job_id))
    assert not (transport.root / "claims" / f"{job_id}.claim").exists()


def test_host_helper_docker_has_no_socket_and_requires_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr("runner.browser_static_host_bridge.subprocess.run", fake_run)
    host._docker("onefoundry_runner_spool", IMAGE, "list")
    command = commands[0]
    assert "--pull=never" in command
    assert command[command.index("--network") + 1] == "none"
    assert command[command.index("--user") + 1] == f"10001:{os.getgid()}"
    assert "docker.sock" not in " ".join(command)
    with pytest.raises(ValueError, match="helper image"):
        host._docker("onefoundry_runner_spool", "onefoundry-runner:latest", "list")


def test_helper_rejected_claim_is_classified_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    job_id = str(uuid4())

    def rejected(_command: list[str], **_: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=4, stdout=b"", stderr=b"invalid job before claim")

    monkeypatch.setattr("runner.browser_static_host_bridge.subprocess.run", rejected)
    with pytest.raises(host.PoisonedJob):
        host._docker("onefoundry_runner_spool", IMAGE, "claim", job_id)


def test_helper_staging_window_is_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    job_id = str(uuid4())

    def staging(_command: list[str], **_: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=5, stdout=b"", stderr=b"")

    monkeypatch.setattr("runner.browser_static_host_bridge.subprocess.run", staging)
    with pytest.raises(host.TransientJob):
        host._docker("onefoundry_runner_spool", IMAGE, "claim", job_id)


def test_transient_claim_is_polled_again_without_poison_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = spool(tmp_path)
    job_id = uuid4()
    manifest = transport.stage(job_id, fixture(), IMAGE)
    frame = volume.frame(
        (transport.root / "jobs" / f"{job_id}.json").read_bytes(),
        (transport.root / "sources" / f"{job_id}.bin").read_bytes(),
    )
    claims = 0

    def fake_docker(
        _volume: str, _image: str, action: str, _job: str | None = None, _transfer: Path | None = None
    ) -> bytes:
        nonlocal claims
        if action == "list":
            return f"{job_id}\n".encode()
        if action == "claim":
            claims += 1
            if claims == 1:
                raise host.TransientJob
            return frame
        return b""

    def fake_inspect(target: BrowserTarget, *, html: bytes, css: bytes, image: str) -> StaticBrowserReceipt:
        assert html and isinstance(css, bytes)
        return StaticBrowserReceipt(
            target.binding_digest,
            manifest["source_sha256"],
            "Fixture",
            "Approved page",
            target.approved_url,
            0,
            image,
        )

    monkeypatch.setattr(host, "_docker", fake_docker)
    monkeypatch.setattr(host, "run_static_browser_inspection", fake_inspect)
    args = Namespace(volume="onefoundry_runner_spool", helper_image=IMAGE, state_root=tmp_path / "host")
    host._run(args)
    assert not (args.state_root / "poisoned" / f"{job_id}.json").exists()
    host._run(args)
    assert claims == 2
    local_receipt = args.state_root / "attempts" / str(job_id) / "receipts" / f"{job_id}.json"
    assert json.loads(local_receipt.read_bytes())["status"] == "done"


def test_invalid_volume_job_exits_with_poison_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    transport = spool(tmp_path)
    monkeypatch.setattr(volume, "SPOOL", transport.root)
    job_id = uuid4()
    transport.stage(job_id, fixture(), IMAGE)
    (transport.root / "sources" / f"{job_id}.bin").write_bytes(b"\0\0\0\4evil")
    monkeypatch.setattr(sys, "argv", ["browser_static_volume_bridge", "claim", str(job_id)])
    with pytest.raises(SystemExit) as raised:
        volume.entrypoint()
    assert raised.value.code == 4
    assert not (transport.root / "claims" / f"{job_id}.claim").exists()


def test_linked_manifest_is_transient_until_temp_name_is_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = spool(tmp_path)
    monkeypatch.setattr(volume, "SPOOL", transport.root)
    job_id = uuid4()
    jobs_inode = (transport.root / "jobs").stat().st_ino
    linked = Event()
    allow_unlink = Event()
    actual_fsync = os.fsync

    def pause_directory_fsync(fd: int) -> None:
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode) and info.st_ino == jobs_inode:
            linked.set()
            assert allow_unlink.wait(timeout=5)
        actual_fsync(fd)

    monkeypatch.setattr(os, "fsync", pause_directory_fsync)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(transport.stage, job_id, fixture(), IMAGE)
        assert linked.wait(timeout=5)
        manifest_path = transport.root / "jobs" / f"{job_id}.json"
        assert manifest_path.stat().st_nlink == 2
        with pytest.raises(BrowserJobTransient):
            volume.claim(str(job_id))
        monkeypatch.setattr(sys, "argv", ["browser_static_volume_bridge", "claim", str(job_id)])
        with pytest.raises(SystemExit) as raised:
            volume.entrypoint()
        assert raised.value.code == 5
        assert not (transport.root / "claims" / f"{job_id}.claim").exists()
        allow_unlink.set()
        pending.result(timeout=5)
    assert manifest_path.stat().st_nlink == 1
    volume.claim(str(job_id))
    assert (transport.root / "claims" / f"{job_id}.claim").is_file()


def test_volume_job_listing_pages_past_poisoned_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = spool(tmp_path)
    monkeypatch.setattr(volume, "SPOOL", transport.root)
    monkeypatch.setattr(volume, "PAGE_SIZE", 2)
    ids = sorted(str(uuid4()) for _ in range(3))
    for job_id in ids:
        (transport.root / "jobs" / f"{job_id}.json").write_bytes(b"{}")
    assert volume.list_jobs() == ids[:2]
    assert volume.list_jobs(after=ids[1]) == ids[2:]


def test_poisoned_claim_does_not_starve_later_job_or_retry_on_next_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transport = spool(tmp_path)
    bad_id, good_id = sorted((uuid4(), uuid4()), key=str)
    manifest = transport.stage(good_id, fixture(), IMAGE)
    frame = volume.frame(
        (transport.root / "jobs" / f"{good_id}.json").read_bytes(),
        (transport.root / "sources" / f"{good_id}.bin").read_bytes(),
    )
    claims: list[str] = []

    def fake_docker(
        _volume: str, _image: str, action: str, job: str | None = None, _transfer: Path | None = None
    ) -> bytes:
        if action == "list":
            if job is None:
                return f"{bad_id}\n".encode()
            if job == str(bad_id) and str(good_id) not in claims:
                return f"{good_id}\n".encode()
            return b""
        if action == "claim":
            assert job is not None
            claims.append(job)
            if job == str(bad_id):
                raise host.PoisonedJob
            return frame
        return b""

    def fake_inspect(target: BrowserTarget, *, html: bytes, css: bytes, image: str) -> StaticBrowserReceipt:
        assert html and isinstance(css, bytes)
        return StaticBrowserReceipt(
            target.binding_digest,
            manifest["source_sha256"],
            "Fixture",
            "Approved page",
            target.approved_url,
            0,
            image,
        )

    monkeypatch.setattr(host, "_docker", fake_docker)
    monkeypatch.setattr(host, "run_static_browser_inspection", fake_inspect)
    monkeypatch.setattr(host, "PAGE_SIZE", 1)
    args = Namespace(volume="onefoundry_runner_spool", helper_image=IMAGE, state_root=tmp_path / "host")
    host._run(args)
    host._run(args)
    assert claims == [str(bad_id), str(good_id)]
    assert "poisoned browser static job" in capsys.readouterr().err
    marker = args.state_root / "poisoned" / f"{bad_id}.json"
    assert marker.read_bytes() == str(bad_id).encode()


def test_host_claim_runs_once_and_publish_recovery_does_not_relaunch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = spool(tmp_path)
    job_id = uuid4()
    manifest = transport.stage(job_id, fixture(), IMAGE)
    frame = volume.frame(
        (transport.root / "jobs" / f"{job_id}.json").read_bytes(),
        (transport.root / "sources" / f"{job_id}.bin").read_bytes(),
    )
    calls: list[str] = []

    def fake_docker(
        _volume: str, _image: str, action: str, _job: str | None = None, _transfer: Path | None = None
    ) -> bytes:
        calls.append(action)
        if action == "claim":
            return frame
        if action == "list":
            return f"{job_id}\n".encode()
        return b""

    def fake_inspect(target: BrowserTarget, *, html: bytes, css: bytes, image: str) -> StaticBrowserReceipt:
        assert html and isinstance(css, bytes)
        return StaticBrowserReceipt(
            target.binding_digest,
            manifest["source_sha256"],
            "Fixture",
            "Approved page",
            target.approved_url,
            0,
            image,
        )

    monkeypatch.setattr(host, "_docker", fake_docker)
    monkeypatch.setattr(host, "run_static_browser_inspection", fake_inspect)
    args = Namespace(volume="onefoundry_runner_spool", helper_image=IMAGE, state_root=tmp_path / "host")
    host._run(args)
    assert calls == ["list", "claim", "publish"]
    local_receipt = args.state_root / "attempts" / str(job_id) / "receipts" / f"{job_id}.json"
    assert json.loads(local_receipt.read_bytes())["status"] == "done"
    local_source = args.state_root / "attempts" / str(job_id) / "sources" / f"{job_id}.bin"
    assert not local_source.exists()
    calls.clear()
    host._publish_existing(args, str(job_id))
    assert calls == ["publish"]


def test_host_purges_local_source_after_unknown_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = spool(tmp_path)
    job_id = uuid4()
    transport.stage(job_id, fixture(), IMAGE)
    frame = volume.frame(
        (transport.root / "jobs" / f"{job_id}.json").read_bytes(),
        (transport.root / "sources" / f"{job_id}.bin").read_bytes(),
    )

    def fake_docker(
        _volume: str, _image: str, action: str, _job: str | None = None, _transfer: Path | None = None
    ) -> bytes:
        if action == "list":
            return f"{job_id}\n".encode()
        if action == "claim":
            return frame
        return b""

    def fail_inspect(*_args: object, **_kwargs: object) -> StaticBrowserReceipt:
        raise RuntimeError("browser failed")

    monkeypatch.setattr(host, "_docker", fake_docker)
    monkeypatch.setattr(host, "run_static_browser_inspection", fail_inspect)
    args = Namespace(volume="onefoundry_runner_spool", helper_image=IMAGE, state_root=tmp_path / "host")
    host._run(args)
    local_source = args.state_root / "attempts" / str(job_id) / "sources" / f"{job_id}.bin"
    local_receipt = args.state_root / "attempts" / str(job_id) / "receipts" / f"{job_id}.json"
    assert not local_source.exists()
    assert json.loads(local_receipt.read_bytes())["status"] == "unknown"


def test_host_purges_local_source_if_process_job_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = spool(tmp_path)
    job_id = uuid4()
    transport.stage(job_id, fixture(), IMAGE)
    frame = volume.frame(
        (transport.root / "jobs" / f"{job_id}.json").read_bytes(),
        (transport.root / "sources" / f"{job_id}.bin").read_bytes(),
    )

    def fake_docker(
        _volume: str, _image: str, action: str, _job: str | None = None, _transfer: Path | None = None
    ) -> bytes:
        if action == "list":
            return f"{job_id}\n".encode()
        if action == "claim":
            return frame
        pytest.fail("failed execution published a receipt")

    def fail_processing(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("simulated local failure")

    monkeypatch.setattr(host, "_docker", fake_docker)
    monkeypatch.setattr(BrowserStaticJobSpool, "process_job", fail_processing)
    args = Namespace(volume="onefoundry_runner_spool", helper_image=IMAGE, state_root=tmp_path / "host")
    with pytest.raises(RuntimeError, match="local failure"):
        host._run(args)
    local_source = args.state_root / "attempts" / str(job_id) / "sources" / f"{job_id}.bin"
    assert not local_source.exists()


@pytest.mark.parametrize("linked_final", [False, True])
def test_host_restart_purges_abandoned_source_without_relaunch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linked_final: bool
) -> None:
    job_id = uuid4()
    args = Namespace(volume="onefoundry_runner_spool", helper_image=IMAGE, state_root=tmp_path / "host")
    local = host._local_spool(args.state_root, str(job_id))
    final = local.root / "sources" / f"{job_id}.bin"
    temporary = local.root / "sources" / f".{job_id}.bin.{uuid4().hex}.tmp"
    temporary.write_bytes(b"abandoned private source")
    temporary.chmod(0o600)
    if linked_final:
        os.link(temporary, final)
    actions: list[str] = []

    def no_jobs(
        _volume: str, _image: str, action: str, _job: str | None = None, _transfer: Path | None = None
    ) -> bytes:
        actions.append(action)
        assert action == "list"
        return b""

    monkeypatch.setattr(host, "_docker", no_jobs)
    host._run(args)
    assert actions == ["list"]
    assert not final.exists() and not temporary.exists()


def test_host_state_lock_rejects_concurrent_cleanup(tmp_path: Path) -> None:
    state = tmp_path / "host"
    with host._host_lock(state):
        with pytest.raises(BlockingIOError):
            with host._host_lock(state):
                pytest.fail("concurrent host entered private state")


@pytest.mark.parametrize("failed_write", ["source", "job", "claim"])
def test_host_purges_local_source_when_staging_fails_after_remote_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_write: str
) -> None:
    transport = spool(tmp_path)
    job_id = uuid4()
    transport.stage(job_id, fixture(), IMAGE)
    frame = volume.frame(
        (transport.root / "jobs" / f"{job_id}.json").read_bytes(),
        (transport.root / "sources" / f"{job_id}.bin").read_bytes(),
    )
    claims = 0

    def fake_docker(
        _volume: str, _image: str, action: str, _job: str | None = None, _transfer: Path | None = None
    ) -> bytes:
        nonlocal claims
        if action == "list":
            return f"{job_id}\n".encode()
        if action == "claim":
            claims += 1
            return frame
        pytest.fail("failed staging published a receipt")

    actual_new = BrowserStaticJobSpool._new

    def failing_new(path: Path, content: bytes) -> None:
        if path.parent.name == {"source": "sources", "job": "jobs", "claim": "claims"}[failed_write]:
            if failed_write == "source":
                path.write_bytes(b"")  # simulate a partial exclusive write before failure
                path.chmod(0o600)
            raise OSError("simulated local staging failure")
        actual_new(path, content)

    monkeypatch.setattr(host, "_docker", fake_docker)
    monkeypatch.setattr(BrowserStaticJobSpool, "_new", staticmethod(failing_new))
    args = Namespace(volume="onefoundry_runner_spool", helper_image=IMAGE, state_root=tmp_path / "host")
    with pytest.raises(OSError, match="staging failure"):
        host._run(args)
    assert claims == 1
    local_source = args.state_root / "attempts" / str(job_id) / "sources" / f"{job_id}.bin"
    assert not local_source.exists()


def test_host_rejects_partial_claim_frame_before_launch(tmp_path: Path) -> None:
    transport: BrowserStaticJobSpool = spool(tmp_path)
    job_id = uuid4()
    transport.stage(job_id, fixture(), IMAGE)
    payload = (transport.root / "jobs" / f"{job_id}.json").read_bytes()
    frame = struct.pack(">I", len(payload)) + payload  # missing source
    with pytest.raises(ValueError, match="frame"):
        host._decode_frame(frame, str(job_id))


def test_partial_transfer_write_keeps_final_absent_and_recovery_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = uuid4()
    args = Namespace(volume="onefoundry_runner_spool", helper_image=IMAGE, state_root=tmp_path / "host")
    local = host._local_spool(args.state_root, str(job_id))
    receipt = json.dumps({"job_id": str(job_id)}, separators=(",", ":")).encode()
    local._new(local._path("receipts", job_id, "json"), receipt)
    target = args.state_root / "transfer" / str(job_id) / f"{job_id}.json"
    published: list[str] = []

    def fake_docker(
        _volume: str, _image: str, action: str, _job: str | None = None, _transfer: Path | None = None
    ) -> bytes:
        published.append(action)
        return b""

    monkeypatch.setattr(host, "_docker", fake_docker)
    actual_fsync = os.fsync

    def partial_then_fail(fd: int) -> None:
        if stat.S_ISREG(os.fstat(fd).st_mode):
            os.ftruncate(fd, 3)
            raise OSError("simulated interrupted transfer write")
        actual_fsync(fd)

    with monkeypatch.context() as interrupted:
        interrupted.setattr(os, "fsync", partial_then_fail)
        with pytest.raises(OSError, match="interrupted transfer"):
            host._publish_existing(args, str(job_id))
    assert not target.exists()
    assert published == []
    assert os.fsync is actual_fsync
    host._publish_existing(args, str(job_id))
    assert target.read_bytes() == receipt
    assert published == ["publish"]


def test_preclaimed_execution_requires_exact_host_digest(tmp_path: Path) -> None:
    transport = spool(tmp_path)
    job_id = uuid4()
    transport.stage(job_id, fixture(), IMAGE)

    def unexpected(*_args: object, **_kwargs: object) -> StaticBrowserReceipt:
        pytest.fail("unclaimed browser job launched")

    with pytest.raises(BrowserJobError, match="host_claim_missing"):
        transport.process_job(job_id, inspect=unexpected, preclaimed=True)


def test_receipt_publication_recovers_after_source_purge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = spool(tmp_path)
    monkeypatch.setattr(volume, "SPOOL", transport.root)
    transfer = tmp_path / "transfer"
    transfer.mkdir(mode=0o750)
    monkeypatch.setattr(volume, "TRANSFER", transfer)
    job_id = uuid4()
    manifest = transport.stage(job_id, fixture(), IMAGE)
    volume.claim(str(job_id))
    receipt = {
        "schema_version": 1,
        "job_id": str(job_id),
        "project_id": manifest["project_id"],
        "run_id": manifest["run_id"],
        "fixture_id": manifest["fixture_id"],
        "request_sha256": manifest["request_sha256"],
        "binding_digest": manifest["binding_digest"],
        "source_sha256": manifest["source_sha256"],
        "image_id": manifest["image_id"],
        "status": "unknown",
        "reason_code": "runner_failure_after_claim",
        "title": None,
        "text": None,
        "blocked_request_count": None,
        "finished_at": datetime.now(UTC).isoformat(),
    }
    raw = json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode()
    (transfer / f"{job_id}.json").write_bytes(raw)
    (transfer / f"{job_id}.json").chmod(0o640)
    transport.purge_source(job_id)
    volume.publish(str(job_id))
    assert transport.read_receipt(job_id) == (receipt, hashlib.sha256(raw).hexdigest())
    volume.publish(str(job_id))
    (transfer / f"{job_id}.json").unlink()
    (transfer / f"{job_id}.json").symlink_to(transport.root / "jobs" / f"{job_id}.json")
    with pytest.raises(OSError):
        volume.publish(str(job_id))


@pytest.mark.parametrize(
    ("change", "replacement"),
    [
        ("request_sha256", "0" * 64),
        ("reason_code", "arbitrary_success"),
        ("title", None),
        ("blocked_request_count", -1),
    ],
)
def test_publish_rejects_receipt_mismatch_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str, replacement: object
) -> None:
    transport = spool(tmp_path)
    monkeypatch.setattr(volume, "SPOOL", transport.root)
    transfer = tmp_path / "transfer"
    transfer.mkdir(mode=0o750)
    monkeypatch.setattr(volume, "TRANSFER", transfer)
    job_id = uuid4()
    manifest = transport.stage(job_id, fixture(), IMAGE)
    volume.claim(str(job_id))
    invalid = {
        "schema_version": 1,
        "job_id": str(job_id),
        "project_id": manifest["project_id"],
        "run_id": manifest["run_id"],
        "fixture_id": manifest["fixture_id"],
        "request_sha256": manifest["request_sha256"],
        "binding_digest": manifest["binding_digest"],
        "source_sha256": manifest["source_sha256"],
        "image_id": manifest["image_id"],
        "status": "done",
        "reason_code": "inspection_complete",
        "title": "Fixture",
        "text": "Approved page",
        "blocked_request_count": 0,
        "finished_at": datetime.now(UTC).isoformat(),
    }
    invalid[change] = replacement
    path = transfer / f"{job_id}.json"
    path.write_bytes(json.dumps(invalid, sort_keys=True, separators=(",", ":")).encode())
    path.chmod(0o640)
    with pytest.raises(ValueError, match="invalid browser receipt"):
        volume.publish(str(job_id))
    assert transport.read_receipt(job_id) is None
