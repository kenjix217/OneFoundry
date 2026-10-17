"""Bounded public progress extracted from coding CLI JSONL.

Only outward-facing text and known tool event types enter this summary. Raw
reasoning, tool arguments, tool output, patches, and unknown events are ignored.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

AdapterId = Literal["codex_cli", "claude_code"]
MAX_PROGRESS = 24
MAX_EVENT_BYTES = 65536
MAX_COMMAND_CHARS = 160
AGENT_UPDATE_TEXT = "The coding agent shared an update. Review its final result for details."
STAGE_BASELINE_FAILED = (
    "The starting check reported a failure. Next, the coding agent will repair the calculator."
)
STAGE_CLI_FINISHED = (
    "The coding agent finished. Next, the runner will inspect the changed file and rerun the test."
)
STAGE_TEST_PASSED = "The repaired calculator passed its test. Next, the runner will save the evidence."
RUNNER_UPDATE_TEXTS = frozenset({STAGE_BASELINE_FAILED, STAGE_CLI_FINISHED, STAGE_TEST_PASSED})
_SAFE_COMMANDS = frozenset(
    {
        "pwd",
        "git status --short",
        "git diff --check",
        "python -m unittest discover -s tests -v",
        "python3 -m unittest discover -s tests -v",
        "pytest -q",
    }
)
_TOOL_TEXT = {
    "shell": "Ran a shell command.",
    "file_change": "Changed project files.",
    "connected_tool": "Used a connected tool.",
    "web_search": "Searched the web.",
    "Read": "Read a project file.",
    "Edit": "Requested a project file edit.",
    "Write": "Requested a project file write.",
    "Bash": "Ran a shell command.",
}
_LEGACY_TOOL_TEXT = {"Edit": "Edited a project file.", "Write": "Wrote a project file."}


@dataclass(frozen=True)
class ProviderProgress:
    kind: Literal["agent_update", "tool_activity", "runner_update"]
    text: str
    tool: str | None = None
    command: str | None = None
    source: Literal["cli_output", "runner_observed"] = "cli_output"


@dataclass(frozen=True)
class ProgressSummary:
    items: tuple[ProviderProgress, ...]
    omitted: int


def admit_progress(item: ProviderProgress) -> ProviderProgress:
    """Validate one public record at the durable spool boundary."""
    if not isinstance(item, ProviderProgress):
        raise ValueError("progress_invalid")
    if item.kind == "runner_update":
        if (
            item.source != "runner_observed"
            or item.tool is not None
            or item.command is not None
            or item.text not in RUNNER_UPDATE_TEXTS
        ):
            raise ValueError("progress_invalid")
    elif item.kind == "agent_update":
        if (
            item.source != "cli_output"
            or item.tool is not None
            or item.command is not None
            or item.text != AGENT_UPDATE_TEXT
        ):
            raise ValueError("progress_invalid")
    elif item.kind == "tool_activity":
        if (
            item.source != "cli_output"
            or not isinstance(item.tool, str)
            or item.tool not in _TOOL_TEXT
            or item.text not in (_TOOL_TEXT[item.tool], _LEGACY_TOOL_TEXT.get(item.tool))
            or (item.command is not None and (item.tool != "shell" or _safe_command(item.command) is None))
        ):
            raise ValueError("progress_invalid")
    else:
        raise ValueError("progress_invalid")
    if (
        item.kind == "tool_activity"
        and item.tool is not None
        and item.text == _LEGACY_TOOL_TEXT.get(item.tool)
    ):
        return ProviderProgress(item.kind, _TOOL_TEXT[item.tool], item.tool, item.command, item.source)
    return item


def _safe_command(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > MAX_COMMAND_CHARS:
        return None
    return value if value in _SAFE_COMMANDS else None


def _codex_event(event: dict[str, Any]) -> list[ProviderProgress]:
    if event.get("type") != "item.completed":
        return []
    item = event.get("item")
    if not isinstance(item, dict):
        return []
    kind = item.get("type")
    if kind == "agent_message":
        return (
            [ProviderProgress("agent_update", AGENT_UPDATE_TEXT)] if isinstance(item.get("text"), str) else []
        )
    if kind == "command_execution":
        command = _safe_command(item.get("command"))
        return [ProviderProgress("tool_activity", "Ran a shell command.", "shell", command)]
    if kind == "file_change":
        return [ProviderProgress("tool_activity", "Changed project files.", "file_change")]
    if kind == "mcp_tool_call":
        return [ProviderProgress("tool_activity", "Used a connected tool.", "connected_tool")]
    if kind == "web_search":
        return [ProviderProgress("tool_activity", "Searched the web.", "web_search")]
    return []


def _claude_event(event: dict[str, Any]) -> list[ProviderProgress]:
    if event.get("type") != "assistant":
        return []
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    progress: list[ProviderProgress] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            if isinstance(block.get("text"), str):
                progress.append(ProviderProgress("agent_update", AGENT_UPDATE_TEXT))
        elif kind == "tool_use":
            name = block.get("name")
            if name == "Read":
                progress.append(ProviderProgress("tool_activity", "Read a project file.", "Read"))
            elif name == "Edit":
                progress.append(ProviderProgress("tool_activity", "Requested a project file edit.", "Edit"))
            elif name == "Write":
                progress.append(ProviderProgress("tool_activity", "Requested a project file write.", "Write"))
            elif name == "Bash":
                progress.append(ProviderProgress("tool_activity", "Ran a shell command.", "Bash"))
    return progress


def extract_progress(adapter_id: AdapterId, raw: bytes) -> ProgressSummary:
    """Summarize recognized JSONL events without retaining private payloads."""
    if adapter_id not in ("codex_cli", "claude_code"):
        raise ValueError("unsupported_adapter")
    items: list[ProviderProgress] = []
    omitted = 0
    for line in raw.splitlines():
        if len(line) > MAX_EVENT_BYTES:
            omitted += 1
            continue
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, ValueError):
            omitted += 1
            continue
        if not isinstance(event, dict):
            omitted += 1
            continue
        observed = _codex_event(event) if adapter_id == "codex_cli" else _claude_event(event)
        for progress in observed:
            if len(items) == MAX_PROGRESS:
                omitted += 1
            else:
                items.append(progress)
    return ProgressSummary(tuple(items), omitted)


class LiveProgressParser:
    """Turn bounded stdout chunks into safe updates without retaining raw JSONL."""

    def __init__(self, adapter_id: AdapterId) -> None:
        if adapter_id not in ("codex_cli", "claude_code"):
            raise ValueError("unsupported_adapter")
        self.adapter_id = adapter_id
        self._pending = bytearray()
        self._skip_line = False
        self._published = 0

    def feed(self, chunk: bytes) -> tuple[ProviderProgress, ...]:
        if not isinstance(chunk, bytes):
            raise ValueError("progress_chunk_invalid")
        if self._skip_line:
            newline = chunk.find(b"\n")
            if newline < 0:
                return ()
            chunk = chunk[newline + 1 :]
            self._skip_line = False
        self._pending.extend(chunk)
        updates: list[ProviderProgress] = []
        while True:
            newline = self._pending.find(b"\n")
            if newline < 0:
                break
            line = bytes(self._pending[:newline])
            del self._pending[: newline + 1]
            if len(line) > MAX_EVENT_BYTES or self._published >= MAX_PROGRESS:
                continue
            for item in extract_progress(self.adapter_id, line).items:
                if self._published >= MAX_PROGRESS:
                    break
                updates.append(item)
                self._published += 1
        if len(self._pending) > MAX_EVENT_BYTES:
            self._pending.clear()
            self._skip_line = True
        return tuple(updates)
