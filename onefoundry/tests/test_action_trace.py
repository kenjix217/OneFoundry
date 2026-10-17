"""Observed coding actions are bounded, redacted, and explicitly partial."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from onefoundry.action_trace_contract import SavedActionTrace
from runner.action_trace import ActionTrace
from runner.provider_progress import AGENT_UPDATE_TEXT, STAGE_BASELINE_FAILED, ProviderProgress


def test_observed_command_keeps_failure_and_masks_secret(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    trace = ActionTrace(repository=repo, private_root=tmp_path)
    trace.command(
        ("python", "-m", "unittest"),
        "2026-09-29T10:00:00Z",
        1,
        b"FAIL token=" + ((b"A1" * 6)[:12]) + b"\n",
        b"",
    )
    encoded = trace.encode()
    assert b"abc123def456" not in encoded
    saved = SavedActionTrace.model_validate_json(encoded)
    assert saved.coverage == "partial"
    assert saved.actions[0].exit_code == 1
    assert "[redacted]" in saved.actions[0].stdout


def test_trace_rejects_claimed_complete_provider_capture(tmp_path: Path) -> None:
    trace = ActionTrace(repository=tmp_path / "repo", private_root=tmp_path)
    trace.provider("codex_cli", "2026-09-29T10:00:00Z", "unknown")
    payload = json.loads(trace.encode())
    payload["provider_tool_calls_captured"] = True
    with pytest.raises(ValidationError):
        SavedActionTrace.model_validate(payload)


def test_provider_progress_is_saved_as_partial_safe_summary(tmp_path: Path) -> None:
    trace = ActionTrace(repository=tmp_path / "repo", private_root=tmp_path)
    trace.provider(
        "claude_code",
        "2026-09-29T10:00:00Z",
        "done",
        (
            ProviderProgress("agent_update", AGENT_UPDATE_TEXT),
            ProviderProgress("tool_activity", "Requested a project file edit.", "Edit"),
        ),
        2,
    )
    saved = SavedActionTrace.model_validate_json(trace.encode())
    assert saved.coverage == "partial"
    assert saved.provider_tool_calls_captured is False
    assert saved.omitted_progress == 2
    assert [entry.kind for entry in saved.progress] == ["agent_update", "tool_activity"]
    payload = json.loads(trace.encode())
    payload["progress"][1]["command"] = "cat secret.env"
    with pytest.raises(ValidationError):
        SavedActionTrace.model_validate(payload)


def test_runner_stage_has_distinct_provenance_in_saved_trace(tmp_path: Path) -> None:
    trace = ActionTrace(repository=tmp_path / "repo", private_root=tmp_path)
    trace.progress_event(ProviderProgress("runner_update", STAGE_BASELINE_FAILED, source="runner_observed"))
    saved = SavedActionTrace.model_validate_json(trace.encode())
    assert [(item.source, item.kind) for item in saved.progress] == [("runner_observed", "runner_update")]
    payload = json.loads(trace.encode())
    payload["progress"][0]["source"] = "cli_output"
    with pytest.raises(ValidationError):
        SavedActionTrace.model_validate(payload)
