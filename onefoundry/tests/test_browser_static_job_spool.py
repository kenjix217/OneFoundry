"""Browser host job bytes and receipt cannot be replayed or changed unnoticed."""

from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest

from onefoundry.browser_static_fixtures import ApprovedStaticFixture
from onefoundry.browser_target import BrowserTarget, BrowserTargetLimits
from runner.browser_static_job_spool import BrowserJobError, BrowserJobTransient, BrowserStaticJobSpool
from runner.browser_static_supervisor import StaticBrowserReceipt

NOW = datetime(2026, 1, 1, tzinfo=UTC)
IMAGE = "sha256:" + "a" * 64
HTML = b"<!doctype html><title>Fixture</title><main>Approved page</main>"
CSS = b"main { color: navy; }"


@pytest.fixture(autouse=True)
def frozen_receipt_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    class Clock:
        fromisoformat = staticmethod(datetime.fromisoformat)

        @staticmethod
        def now(tz: tzinfo | None = None) -> datetime:
            return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)

    monkeypatch.setattr("runner.browser_static_job_spool.datetime", Clock)


def fixture() -> ApprovedStaticFixture:
    target = BrowserTarget.bind(
        project_id=uuid4(),
        run_id=uuid4(),
        actor_id="operator",
        approved_url="http://127.0.0.1:8765/fixture.html",
        expires_at=NOW + timedelta(seconds=120),
        idempotency_key=uuid4(),
        viewport_width=800,
        viewport_height=600,
        max_page_bytes=262_144,
        timeout_seconds=10,
        limits=BrowserTargetLimits(2048, 1920, 1200, 262_144, 30, 300),
        now=NOW,
    )
    return ApprovedStaticFixture(
        uuid4(),
        target,
        HTML,
        CSS,
        hashlib.sha256(HTML + b"\x00" + CSS).hexdigest(),
        "b" * 64,
    )


def spool(tmp_path: Path) -> BrowserStaticJobSpool:
    root = tmp_path / "browser-jobs"
    root.mkdir(mode=0o700)
    return BrowserStaticJobSpool(root)


def test_exact_private_source_claim_and_text_receipt(tmp_path: Path) -> None:
    owner = fixture()
    transport = spool(tmp_path)
    job_id = uuid4()
    manifest = transport.stage(job_id, owner, IMAGE)
    assert manifest["binding_digest"] == owner.target.binding_digest
    assert manifest["source_sha256"] == owner.source_sha256
    assert "Approved page" not in json.dumps(manifest)
    called = 0

    def inspect(target: BrowserTarget, *, html: bytes, css: bytes, image: str) -> StaticBrowserReceipt:
        nonlocal called
        called += 1
        assert target == owner.target
        assert html == HTML and css == CSS and image == IMAGE
        return StaticBrowserReceipt(
            target.binding_digest,
            owner.source_sha256,
            "Fixture",
            "Approved page",
            target.approved_url,
            2,
            IMAGE,
        )

    receipt = transport.process_job(job_id, inspect=inspect)
    assert receipt is not None and receipt["status"] == "done"
    assert receipt["text"] == "Approved page"
    assert transport.process_job(job_id, inspect=inspect) is None
    assert called == 1
    observed = transport.read_receipt(job_id)
    assert observed is not None and observed[0] == receipt
    assert len(observed[1]) == 64
    for kind, suffix in (("sources", "bin"), ("jobs", "json"), ("claims", "claim"), ("receipts", "json")):
        assert (transport.root / kind / f"{job_id}.{suffix}").stat().st_mode & 0o077 == 0


def test_staging_manifest_is_invisible_until_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = spool(tmp_path)
    job_id = uuid4()
    manifest_path = transport.root / "jobs" / f"{job_id}.json"
    before_publish = Event()
    allow_publish = Event()
    actual_link = os.link

    def pause_link(source: Path, target: Path, *, follow_symlinks: bool = True) -> None:
        if target == manifest_path:
            assert source.read_bytes()
            before_publish.set()
            assert allow_publish.wait(timeout=5)
        actual_link(source, target, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "link", pause_link)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(transport.stage, job_id, fixture(), IMAGE)
        assert before_publish.wait(timeout=5)
        assert not manifest_path.exists()
        assert transport.root.joinpath("sources", f"{job_id}.bin").exists()
        allow_publish.set()
        manifest = pending.result(timeout=5)
    assert json.loads(manifest_path.read_bytes()) == manifest


def test_terminal_source_purge_removes_orphaned_private_stage_temp(tmp_path: Path) -> None:
    transport = spool(tmp_path)
    job_id = uuid4()
    temporary = transport.root / "sources" / f".{job_id}.bin.{uuid4().hex}.tmp"
    temporary.write_bytes(b"incomplete source")
    temporary.chmod(0o600)
    transport.purge_source(job_id)
    assert not temporary.exists()


def test_terminal_purge_removes_crashed_source_final_and_exact_temp_hardlink(tmp_path: Path) -> None:
    transport = spool(tmp_path)
    job_id = uuid4()
    final = transport.root / "sources" / f"{job_id}.bin"
    temporary = transport.root / "sources" / f".{job_id}.bin.{uuid4().hex}.tmp"
    temporary.write_bytes(b"staged source")
    temporary.chmod(0o600)
    os.link(temporary, final)
    assert final.stat().st_nlink == 2
    transport.purge_source(job_id)
    assert not final.exists()
    assert not temporary.exists()


def test_terminal_purge_rejects_two_link_source_without_own_temp(tmp_path: Path) -> None:
    transport = spool(tmp_path)
    job_id = uuid4()
    final = transport.root / "sources" / f"{job_id}.bin"
    unrelated = tmp_path / "unrelated"
    final.write_bytes(b"staged source")
    final.chmod(0o600)
    os.link(final, unrelated)
    with pytest.raises(BrowserJobTransient):
        transport.purge_source(job_id)
    assert final.exists() and unrelated.exists()


def test_tampered_source_rejected_before_claim(tmp_path: Path) -> None:
    owner = fixture()
    transport = spool(tmp_path)
    job_id = uuid4()
    transport.stage(job_id, owner, IMAGE)
    (transport.root / "sources" / f"{job_id}.bin").write_bytes(b"\x00\x00\x00\x04evil")
    with pytest.raises(BrowserJobError, match="source_changed"):
        transport.process_job(job_id)
    assert not (transport.root / "claims" / f"{job_id}.claim").exists()


def test_claim_with_failed_execution_is_unknown_and_not_retried(tmp_path: Path) -> None:
    owner = fixture()
    transport = spool(tmp_path)
    job_id = uuid4()
    transport.stage(job_id, owner, IMAGE)

    def fail(*_args: object, **_kwargs: object) -> StaticBrowserReceipt:
        raise RuntimeError("private failure detail")

    receipt = transport.process_job(job_id, inspect=fail)
    assert receipt is not None and receipt["status"] == "unknown"
    assert receipt["reason_code"] == "runner_failure_after_claim"
    assert "private failure" not in json.dumps(receipt)
    assert transport.process_job(job_id, inspect=fail) is None


def test_timeout_fence_blocks_late_host_claim_and_is_idempotent(tmp_path: Path) -> None:
    owner = fixture()
    transport = spool(tmp_path)
    job_id = uuid4()
    manifest = transport.stage(job_id, owner, IMAGE)
    assert transport.fence_unclaimed(job_id, manifest["request_sha256"]) is True
    assert transport.fence_unclaimed(job_id, manifest["request_sha256"]) is True
    transport.purge_source(job_id)

    def unexpected(*_args: object, **_kwargs: object) -> StaticBrowserReceipt:
        pytest.fail("timed-out browser job launched")

    assert transport.process_job(job_id, inspect=unexpected) is None
    assert transport.read_receipt(job_id) is None


def test_host_claim_wins_timeout_fence(tmp_path: Path) -> None:
    owner = fixture()
    transport = spool(tmp_path)
    job_id = uuid4()
    manifest = transport.stage(job_id, owner, IMAGE)
    transport._new(transport._path("claims", job_id, "claim"), manifest["request_sha256"].encode())
    assert transport.fence_unclaimed(job_id, manifest["request_sha256"]) is False
    assert transport.claimed(job_id, manifest["request_sha256"]) is True


@pytest.mark.parametrize("prefix", [b"", b"timeout:", b"digest"])
def test_partial_claim_blocks_launch_and_remains_observable(tmp_path: Path, prefix: bytes) -> None:
    owner = fixture()
    transport = spool(tmp_path)
    job_id = uuid4()
    manifest = transport.stage(job_id, owner, IMAGE)
    partial = manifest["request_sha256"].encode()[:8] if prefix == b"digest" else prefix
    transport._new(transport._path("claims", job_id, "claim"), partial)
    assert transport.claim_age_seconds(job_id, manifest["request_sha256"]) is not None
    assert transport.fence_unclaimed(job_id, manifest["request_sha256"]) is False

    def unexpected(*_args: object, **_kwargs: object) -> StaticBrowserReceipt:
        pytest.fail("partial claim allowed browser launch")

    assert transport.process_job(job_id, inspect=unexpected) is None


@pytest.mark.parametrize("wrong", [b"b" * 64, b"timeout:" + b"b" * 64])
def test_complete_wrong_claim_is_rejected(tmp_path: Path, wrong: bytes) -> None:
    owner = fixture()
    transport = spool(tmp_path)
    job_id = uuid4()
    manifest = transport.stage(job_id, owner, IMAGE)
    transport._new(transport._path("claims", job_id, "claim"), wrong)
    with pytest.raises(BrowserJobError, match="claim_mismatch"):
        transport.claim_age_seconds(job_id, manifest["request_sha256"])
    with pytest.raises(BrowserJobError, match="claim_mismatch"):
        transport.fence_unclaimed(job_id, manifest["request_sha256"])


def test_unverified_manifest_and_private_root_rejected(tmp_path: Path) -> None:
    owner = fixture()
    transport = spool(tmp_path)
    job_id = uuid4()
    transport.stage(job_id, owner, IMAGE)
    path = transport.root / "jobs" / f"{job_id}.json"
    raw = json.loads(path.read_bytes())
    raw["approved_url"] = "http://example.invalid:8765/fixture.html"
    path.write_text(json.dumps(raw))
    with pytest.raises(BrowserJobError, match="manifest_invalid"):
        transport.process_job(job_id)
    root = tmp_path / "public"
    root.mkdir(mode=0o755)
    with pytest.raises(BrowserJobError, match="not_private"):
        BrowserStaticJobSpool(root)
