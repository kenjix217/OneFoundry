"""Read contract for bounded runner-observed action traces."""

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from onefoundry.workflow_schemas import StrictModel
from runner.provider_progress import ProviderProgress, admit_progress


class ObservedAction(StrictModel):
    source: Literal["runner_observed"]
    kind: Literal["command", "provider_invocation"]
    command: list[str] = Field(min_length=1, max_length=32)
    cwd: Literal["."]
    started_at: datetime
    finished_at: datetime
    outcome: Literal["finished", "timed_out", "done", "unknown"]
    exit_code: int | None
    stdout: str = Field(max_length=2048)
    stderr: str = Field(max_length=2048)
    output_truncated: bool

    @model_validator(mode="after")
    def valid_action(self) -> "ObservedAction":
        if self.finished_at < self.started_at or any(len(part) > 1000 for part in self.command):
            raise ValueError("invalid observed action bounds")
        if self.kind == "command" and self.outcome not in ("finished", "timed_out"):
            raise ValueError("invalid command outcome")
        if self.kind == "provider_invocation" and self.outcome not in ("done", "unknown"):
            raise ValueError("invalid provider outcome")
        return self


class ProviderProgressEntry(StrictModel):
    source: Literal["cli_output", "runner_observed"]
    kind: Literal["agent_update", "tool_activity", "runner_update"]
    text: str = Field(min_length=1, max_length=240)
    tool: (
        Literal["shell", "file_change", "connected_tool", "web_search", "Read", "Edit", "Write", "Bash"]
        | None
    )
    command: (
        Literal[
            "pwd",
            "git status --short",
            "git diff --check",
            "python -m unittest discover -s tests -v",
            "python3 -m unittest discover -s tests -v",
            "pytest -q",
        ]
        | None
    )

    @model_validator(mode="after")
    def valid_progress(self) -> "ProviderProgressEntry":
        admit_progress(ProviderProgress(self.kind, self.text, self.tool, self.command, self.source))
        return self


class SavedActionTrace(StrictModel):
    schema_version: Literal[1]
    coverage: Literal["partial"]
    provider_tool_calls_captured: Literal[False]
    omitted_actions: int = Field(ge=0, le=100000)
    actions: list[ObservedAction] = Field(max_length=64)
    omitted_progress: int = Field(default=0, ge=0, le=100000)
    progress: list[ProviderProgressEntry] = Field(default_factory=list, max_length=24)
