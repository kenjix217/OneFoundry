"""The separate fake-only host supervisor advertises only its own capability."""

import json
import os
import stat
from pathlib import Path

import pytest

from runner.github_snapshot_codex_supervisor import (
    SnapshotCodexConfig,
    SnapshotCodexSupervisorError,
    acquire_supervisor,
    run_pending,
)

IMAGE = "sha256:" + "a" * 64


def _config(tmp_path: Path) -> SnapshotCodexConfig:
    spool = tmp_path / "spool"
    snapshots = tmp_path / "snapshots-root"
    for name in ("jobs", "claims", "receipts", "artifacts"):
        (spool / name).mkdir(parents=True)
    (snapshots / "snapshots").mkdir(parents=True)
    return SnapshotCodexConfig(spool, snapshots, IMAGE, IMAGE, 90)


def test_host_supervisor_advertises_separate_synthetic_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    inspected: list[tuple[str, str]] = []

    def fake_proof(image: str, fixture: str) -> dict[str, object]:
        inspected.append((image, fixture))
        return {
            "image_id": image,
            "fixture_image_id": fixture,
            "provider_requests": 2,
            "reported_tokens": 28,
            "cost_usd": None,
        }

    monkeypatch.setattr(
        "runner.github_snapshot_codex_supervisor.broker_proof.prove_broker_turn",
        fake_proof,
    )
    assert run_pending(config) == 0
    assert inspected == [(IMAGE, IMAGE)]
    capability = json.loads((config.spool / "snapshot_codex_capabilities.json").read_text())
    assert capability["synthetic_only"] is True
    assert capability["brokered"] is True
    assert not (config.spool / "capabilities.json").exists()


def test_supervisor_rejects_alias_and_unpinned_image(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with pytest.raises(SnapshotCodexSupervisorError, match="snapshot_codex_config_invalid"):
        SnapshotCodexConfig(config.spool, config.spool, IMAGE, IMAGE, 90).validate()
    with pytest.raises(SnapshotCodexSupervisorError, match="snapshot_codex_config_invalid"):
        SnapshotCodexConfig(config.spool, config.snapshot_root, "latest", IMAGE, 90).validate()


def test_lock_is_readable_by_separate_app_uid(tmp_path: Path) -> None:
    config = _config(tmp_path)
    descriptor = acquire_supervisor(config)
    try:
        assert stat.S_IMODE((config.spool / ".snapshot-codex.lock").stat().st_mode) == 0o644
    finally:
        os.close(descriptor)
