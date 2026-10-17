"""Bounded, redacted observations from the fixed external coding harness."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from runner.provider_progress import MAX_PROGRESS, ProviderProgress, admit_progress

SECRET = re.compile(
    r"(?:\b(?:sk-[A-Za-z0-9_-]{12,}|sk_live_[A-Za-z0-9]{12,}|ghp_[A-Za-z0-9]{12,}|"
    r"github_pat_[A-Za-z0-9_]{12,}|xoxb-[A-Za-z0-9-]{12,}|AKIA[A-Z0-9]{16})\b|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b(?:api[_-]?key|access[_-]?token|token|password|secret)\s*[:=]\s*\S+|"
    r"\bBearer\s+[A-Za-z0-9._~-]{12,})",
    re.IGNORECASE,
)
MAX_ACTIONS = 64
MAX_OUTPUT_CHARS = 2048


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _safe_text(raw: bytes) -> tuple[str, bool]:
    text = SECRET.sub("[redacted]", raw.decode("utf-8", errors="replace"))
    truncated = len(text) > MAX_OUTPUT_CHARS
    return text[:MAX_OUTPUT_CHARS], truncated


class ActionTrace:
    def __init__(self, *, repository: Path, private_root: Path) -> None:
        self.repository = repository
        self.private_root = private_root
        self.actions: list[dict[str, object]] = []
        self.omitted = 0
        self.progress: list[dict[str, object]] = []
        self.omitted_progress = 0

    def _command(self, command: tuple[str, ...]) -> list[str]:
        result: list[str] = []
        for part in command:
            if part == str(self.repository):
                result.append(".")
            elif part.startswith(str(self.private_root)):
                result.append("[temporary path]")
            elif Path(part).name == "python" or Path(part).name.startswith("python3"):
                result.append("python")
            else:
                result.append(SECRET.sub("[redacted]", part))
        return result

    def command(
        self,
        command: tuple[str, ...],
        started_at: str,
        exit_code: int | None,
        stdout: bytes,
        stderr: bytes,
        outcome: str = "finished",
    ) -> None:
        if len(self.actions) >= MAX_ACTIONS:
            self.omitted += 1
            return
        safe_stdout, stdout_truncated = _safe_text(stdout)
        safe_stderr, stderr_truncated = _safe_text(stderr)
        self.actions.append(
            {
                "source": "runner_observed",
                "kind": "command",
                "command": self._command(command),
                "cwd": ".",
                "started_at": started_at,
                "finished_at": _now(),
                "outcome": outcome,
                "exit_code": exit_code,
                "stdout": safe_stdout,
                "stderr": safe_stderr,
                "output_truncated": stdout_truncated or stderr_truncated,
            }
        )

    def progress_event(self, item: ProviderProgress) -> None:
        if len(self.progress) >= MAX_PROGRESS:
            self.omitted_progress += 1
            return
        try:
            safe = admit_progress(item)
        except ValueError:
            self.omitted_progress += 1
            return
        self.progress.append(
            {
                "source": safe.source,
                "kind": safe.kind,
                "text": safe.text,
                "tool": safe.tool,
                "command": safe.command,
            }
        )

    def provider(
        self,
        adapter_id: str,
        started_at: str,
        outcome: str,
        progress: tuple[ProviderProgress, ...] = (),
        progress_omitted: int = 0,
    ) -> None:
        self.omitted_progress += max(0, progress_omitted)
        for item in progress:
            self.progress_event(item)
        if len(self.actions) >= MAX_ACTIONS:
            self.omitted += 1
            return
        self.actions.append(
            {
                "source": "runner_observed",
                "kind": "provider_invocation",
                "command": ["Codex CLI" if adapter_id == "codex_cli" else "Claude Code"],
                "cwd": ".",
                "started_at": started_at,
                "finished_at": _now(),
                "outcome": outcome,
                "exit_code": None,
                "stdout": "",
                "stderr": "",
                "output_truncated": False,
            }
        )

    def encode(self) -> bytes:
        payload = {
            "schema_version": 1,
            "coverage": "partial",
            "provider_tool_calls_captured": False,
            "omitted_actions": self.omitted,
            "omitted_progress": self.omitted_progress,
            "actions": self.actions,
            "progress": self.progress,
        }
        encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
        if len(encoded) > 65536:
            # Never claim completeness when the bounded artifact cannot hold its observations.
            while self.actions and len(encoded) > 65536:
                self.actions.pop()
                self.omitted += 1
                payload["omitted_actions"] = self.omitted
                encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
            while self.progress and len(encoded) > 65536:
                self.progress.pop()
                self.omitted_progress += 1
                payload["omitted_progress"] = self.omitted_progress
                encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
        return encoded
