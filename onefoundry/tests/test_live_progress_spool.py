"""Claim-bound live progress journal never exposes raw CLI evidence."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from onefoundry.runner_spool import RunnerSpool
from runner.live_progress_spool import (
    LiveProgressJournal,
    ProgressSpoolInvalid,
    read_progress,
)
from runner.provider_progress import (
    AGENT_UPDATE_TEXT,
    MAX_PROGRESS,
    STAGE_BASELINE_FAILED,
    ProviderProgress,
)


def _identity(tmp_path: Path) -> tuple[Path, str, str, str, str]:
    spool = tmp_path / "external"
    claims = spool / "claims"
    claims.mkdir(parents=True)
    project_id, run_id, attempt_id = (str(uuid4()) for _ in range(3))
    (claims / f"{attempt_id}.claim").write_text(attempt_id)
    (claims / f"{attempt_id}.claim").chmod(0o600)
    return spool, project_id, run_id, attempt_id, "a" * 64


def test_append_is_visible_before_receipt_and_never_reopened(tmp_path: Path) -> None:
    spool, project, run, attempt, request = _identity(tmp_path)
    assert read_progress(spool, project, run, attempt, request).records == ()
    with LiveProgressJournal.create(spool, project, run, attempt, request) as journal:
        assert journal.append(ProviderProgress("agent_update", AGENT_UPDATE_TEXT))
        first = read_progress(spool, project, run, attempt, request)
        assert first.latest_sequence == 1
        assert first.records[0].progress.text == AGENT_UPDATE_TEXT
        assert first.records[0].progress.source == "cli_output"
        assert journal.append(
            ProviderProgress("runner_update", STAGE_BASELINE_FAILED, source="runner_observed")
        )
        second = read_progress(spool, project, run, attempt, request)
        assert second.records[1].progress.source == "runner_observed"
        assert journal.append(
            ProviderProgress("tool_activity", "Ran a shell command.", "shell", "git diff --check")
        )
    second = read_progress(spool, project, run, attempt, request)
    assert [item.sequence for item in second.records] == [1, 2, 3]
    with pytest.raises(ProgressSpoolInvalid, match="progress_exists_or_unavailable"):
        LiveProgressJournal.create(spool, project, run, attempt, request)


def test_group_readable_progress_from_host_worker_has_a_different_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool, project, run, attempt, request = _identity(tmp_path)
    (spool / "claims" / f"{attempt}.claim").chmod(0o640)
    with LiveProgressJournal.create(spool, project, run, attempt, request, group_readable=True) as journal:
        journal.append(ProviderProgress("agent_update", AGENT_UPDATE_TEXT))
    worker_uid = os.geteuid()
    monkeypatch.setattr("runner.live_progress_spool.os.geteuid", lambda: worker_uid + 1)
    snapshot = RunnerSpool(str(spool)).read_live_progress(
        UUID(project),
        UUID(run),
        UUID(attempt),
        request,
    )
    assert snapshot.records[0].progress.text == AGENT_UPDATE_TEXT


def test_identity_claim_and_corruption_fail_closed(tmp_path: Path) -> None:
    spool, project, run, attempt, request = _identity(tmp_path)
    with LiveProgressJournal.create(spool, project, run, attempt, request) as journal:
        journal.append(ProviderProgress("tool_activity", "Read a project file.", "Read"))
    with pytest.raises(ProgressSpoolInvalid, match="progress_identity_invalid"):
        read_progress(spool, str(uuid4()), run, attempt, request)
    path = spool / "claims" / f"{attempt}.progress.jsonl"
    lines = path.read_bytes().splitlines(keepends=True)
    record = json.loads(lines[1])
    record["text"] = "PRIVATE COT"
    lines[1] = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.write_bytes(b"".join(lines))
    with pytest.raises(ProgressSpoolInvalid, match="progress_record_invalid"):
        read_progress(spool, project, run, attempt, request)


def test_secret_command_and_overflow_never_enter_journal(tmp_path: Path) -> None:
    spool, project, run, attempt, request = _identity(tmp_path)
    with LiveProgressJournal.create(spool, project, run, attempt, request) as journal:
        with pytest.raises(ValueError, match="progress_invalid"):
            journal.append(ProviderProgress("agent_update", "token=protected-value"))
        with pytest.raises(ValueError, match="progress_invalid"):
            journal.append(ProviderProgress("agent_update", STAGE_BASELINE_FAILED))
        with pytest.raises(ValueError, match="progress_invalid"):
            journal.append(ProviderProgress("runner_update", AGENT_UPDATE_TEXT, source="runner_observed"))
        with pytest.raises(ValueError, match="progress_invalid"):
            journal.append(
                ProviderProgress("tool_activity", "Ran a shell command.", "shell", "cat secret.env")
            )
        for _ in range(MAX_PROGRESS):
            assert journal.append(ProviderProgress("tool_activity", "Read a project file.", "Read"))
        assert journal.append(ProviderProgress("tool_activity", "Read a project file.", "Read")) is False
    assert read_progress(spool, project, run, attempt, request).latest_sequence == MAX_PROGRESS
    assert b"protected-value" not in (spool / "claims" / f"{attempt}.progress.jsonl").read_bytes()


def test_missing_or_symlink_claim_and_progress_denied(tmp_path: Path) -> None:
    spool, project, run, attempt, request = _identity(tmp_path)
    claim = spool / "claims" / f"{attempt}.claim"
    claim.unlink()
    with pytest.raises(ProgressSpoolInvalid):
        LiveProgressJournal.create(spool, project, run, attempt, request)
    claim.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ProgressSpoolInvalid):
        LiveProgressJournal.create(spool, project, run, attempt, request)
    claim.unlink()
    claim.write_text(attempt)
    claim.chmod(0o600)
    with LiveProgressJournal.create(spool, project, run, attempt, request):
        pass
    path = spool / "claims" / f"{attempt}.progress.jsonl"
    path.unlink()
    path.symlink_to(claim)
    with pytest.raises(ProgressSpoolInvalid):
        read_progress(spool, project, run, attempt, request)
