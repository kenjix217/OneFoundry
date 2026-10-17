"""Public coding progress is bounded and excludes private CLI payloads."""

import json
from collections.abc import Mapping
from pathlib import Path

from runner.provider_progress import (
    AGENT_UPDATE_TEXT,
    MAX_EVENT_BYTES,
    MAX_PROGRESS,
    STAGE_BASELINE_FAILED,
    STAGE_CLI_FINISHED,
    STAGE_TEST_PASSED,
    LiveProgressParser,
    ProviderProgress,
    admit_progress,
    extract_progress,
)


def _jsonl(*events: Mapping[str, object]) -> bytes:
    return b"".join(json.dumps(event).encode() + b"\n" for event in events)


def test_frontend_stage_templates_match_runner_source() -> None:
    contract = (Path(__file__).resolve().parents[1] / "frontend/src/workOrderTrace.ts").read_text()
    for name, value in (
        ("STAGE_BASELINE_FAILED", STAGE_BASELINE_FAILED),
        ("STAGE_CLI_FINISHED", STAGE_CLI_FINISHED),
        ("STAGE_TEST_PASSED", STAGE_TEST_PASSED),
    ):
        assert f"export const {name} = '{value}';" in contract


def test_codex_updates_and_tools_omit_reasoning_and_unsafe_command() -> None:
    raw = _jsonl(
        {"type": "item.completed", "item": {"type": "reasoning", "text": "PRIVATE COT"}},
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "I found the failing test. Next I will repair it.",
            },
        },
        {"type": "item.completed", "item": {"type": "command_execution", "command": "cat secret.env"}},
        {"type": "item.completed", "item": {"type": "command_execution", "command": "git diff --check"}},
        {"type": "item.completed", "item": {"type": "file_change", "changes": [{"diff": "SECRET CODE"}]}},
    )
    summary = extract_progress("codex_cli", raw)
    assert [item.text for item in summary.items] == [
        AGENT_UPDATE_TEXT,
        "Ran a shell command.",
        "Ran a shell command.",
        "Changed project files.",
    ]
    assert [item.command for item in summary.items] == [None, None, "git diff --check", None]
    assert b"PRIVATE COT" not in repr(summary).encode()
    assert b"SECRET CODE" not in repr(summary).encode()
    assert b"cat secret.env" not in repr(summary).encode()


def test_claude_text_and_tools_exclude_thinking_arguments_and_results() -> None:
    raw = _jsonl(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "PRIVATE COT"},
                    {"type": "text", "text": "The test fails on empty input. I will update the handler."},
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "secret.env"}},
                    {"type": "tool_use", "name": "Edit", "input": {"new_string": "SECRET CODE"}},
                ]
            },
        },
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "SECRET RESULT"}]}},
        {"type": "stream_event", "event": {"delta": {"type": "thinking_delta", "thinking": "PRIVATE COT"}}},
    )
    summary = extract_progress("claude_code", raw)
    assert [item.text for item in summary.items] == [
        AGENT_UPDATE_TEXT,
        "Read a project file.",
        "Requested a project file edit.",
    ]
    for forbidden in ("PRIVATE COT", "secret.env", "SECRET CODE", "SECRET RESULT"):
        assert forbidden not in repr(summary)


def test_older_claude_edit_progress_is_read_as_a_request() -> None:
    item = admit_progress(ProviderProgress("tool_activity", "Edited a project file.", "Edit"))
    assert item.text == "Requested a project file edit."


def test_redaction_unsupported_events_and_limits_are_explicit() -> None:
    raw = (
        _jsonl(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "I found token=" + (("A1" * 6)[:12]) + " at https://private.example/path",
                },
            },
            {"type": "item.completed", "item": {"type": "agent_message", "text": "```private code```"}},
            *(
                {"type": "item.completed", "item": {"type": "command_execution", "command": "pwd"}}
                for _ in range(MAX_PROGRESS + 2)
            ),
        )
        + b"not-json\n"
        + b"x" * 65537
        + b"\n"
    )
    summary = extract_progress("codex_cli", raw)
    assert len(summary.items) == MAX_PROGRESS
    assert summary.omitted >= 4
    assert "abc123def456" not in repr(summary)
    assert "private.example" not in repr(summary)


def test_customer_identifier_never_enters_public_progress() -> None:
    raw = _jsonl(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "The customer SSN is 123-45-6789."},
        }
    )
    summary = extract_progress("codex_cli", raw)
    assert [item.text for item in summary.items] == [AGENT_UPDATE_TEXT]
    assert "123-45-6789" not in repr(summary)


def test_live_parser_waits_for_complete_line_and_discards_oversized_line() -> None:
    parser = LiveProgressParser("codex_cli")
    event = _jsonl(
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "I found the failing test.",
            },
        }
    )
    assert parser.feed(event[:15]) == ()
    assert [item.text for item in parser.feed(event[15:])] == [AGENT_UPDATE_TEXT]
    assert parser.feed(b"x" * (MAX_EVENT_BYTES + 1)) == ()
    assert [item.text for item in parser.feed(b"\n" + event)] == [AGENT_UPDATE_TEXT]
