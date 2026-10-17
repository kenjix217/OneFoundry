"""Append-only runner intent and observed-session records for one claimed attempt.

An intent is written before calling an injected invoker. It does not prove that
the provider process started. A session record is written only after the
invoker returns an observation. Neither record authorizes a second launch.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from runner.external_cli import CliRequest, CliResult
from runner.service import _atomic_new

SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
SESSION = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
SESSION_SECRET = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9]{12,}|"
    r"github_pat_[A-Za-z0-9_]{12,}|xoxb-[A-Za-z0-9-]{12,}|AKIA[A-Z0-9]{16})",
    re.IGNORECASE,
)
REASON = re.compile(r"^[a-z][a-z0-9_]{0,159}$")
LaunchKind = Literal[
    "injected_cli",
    "trusted_local_cli_v1",
    "synthetic_codex_fixture",
    "synthetic_claude_fixture",
    "synthetic_snapshot_codex_fixture",
]


class PrelaunchInvalid(ValueError):
    """The invocation was rejected before the external invoker was called."""


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _claims(spool: Path) -> Path:
    claims = spool / "claims"
    if spool.is_symlink() or not spool.is_dir() or claims.is_symlink() or not claims.is_dir():
        raise ValueError("external_claim_directory_invalid")
    return claims


def _write(path: Path, payload: dict[str, Any], *, mode: int = 0o600) -> str:
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(encoded) > 4096:
        raise ValueError("external_launch_record_too_large")
    _atomic_new(path, encoded, mode=mode)
    return hashlib.sha256(encoded).hexdigest()


def record_prelaunch(
    spool: Path,
    job: dict[str, Any],
    request: CliRequest,
    baseline_commit: str,
    *,
    kind: LaunchKind = "injected_cli",
    image_id: str | None = None,
    evidence_file_mode: int = 0o600,
) -> tuple[dict[str, Any], str]:
    """Persist the exact invoker inputs before handoff; never store prompt text."""
    if kind in ("synthetic_codex_fixture", "synthetic_claude_fixture", "synthetic_snapshot_codex_fixture"):
        expected_adapter = "claude_code" if kind == "synthetic_claude_fixture" else "codex_cli"
        if request.adapter_id != expected_adapter or image_id is None or not SHA256.fullmatch(image_id):
            raise PrelaunchInvalid("synthetic_prelaunch_image_invalid")
        executable_ref = image_id
    elif kind == "injected_cli" and image_id is None:
        executable_ref = str(request.executable)
    elif kind == "trusted_local_cli_v1" and image_id is None:
        if not request.executable.is_absolute() or request.executable.is_symlink():
            raise PrelaunchInvalid("trusted_local_executable_invalid")
        executable_ref = str(request.executable)
    else:
        raise PrelaunchInvalid("external_prelaunch_kind_invalid")
    payload: dict[str, Any] = {
        "schema_version": 1,
        "record_type": "prelaunch_intent",
        "launch_id": str(uuid4()),
        "attempt_id": job["attempt_id"],
        "project_id": job["project_id"],
        "run_id": job["run_id"],
        "adapter_id": request.adapter_id,
        "request_sha256": job["request_sha256"],
        "prepared_at": _now(),
        "baseline_commit": baseline_commit,
        "prompt_sha256": hashlib.sha256(request.prompt.encode()).hexdigest(),
        "invocation_kind": kind,
        "executable_ref": executable_ref,
        "repository": str(request.repository.resolve()),
        "private_home": str(request.private_home.resolve()),
        "wall_seconds": request.wall_seconds,
        "max_output_bytes": request.max_output_bytes,
    }
    if kind == "trusted_local_cli_v1":
        payload["trust_mode"] = "operator_host"
    if kind == "synthetic_snapshot_codex_fixture":
        source = job["source"]
        payload.update(
            snapshot_sha256=source["snapshot_sha256"],
            source_tree=source["source_tree"],
            snapshot_revision=source["saved_revision"],
            snapshot_receipt_sha256=source["snapshot_receipt_sha256"],
        )
    digest = _write(_claims(spool) / f"{job['attempt_id']}.launch.json", payload, mode=evidence_file_mode)
    return payload, digest


def record_session(
    spool: Path,
    launch: dict[str, Any],
    launch_digest: str,
    result: CliResult,
    *,
    evidence_file_mode: int = 0o600,
) -> str:
    """Persist only an observed invoker result, bound to the prior intent."""
    if (
        result.provider_session_id is not None
        and (
            SESSION.fullmatch(result.provider_session_id) is None
            or SESSION_SECRET.search(result.provider_session_id) is not None
        )
    ) or (result.status not in ("done", "unknown") or REASON.fullmatch(result.reason_code) is None):
        raise ValueError("external_session_observation_invalid")
    payload: dict[str, Any] = {
        "schema_version": 1,
        "record_type": "session_observation",
        "launch_id": launch["launch_id"],
        "attempt_id": launch["attempt_id"],
        "request_sha256": launch["request_sha256"],
        "launch_sha256": launch_digest,
        "observed_at": _now(),
        "provider_session_id": result.provider_session_id,
        "provider_status": result.status,
        "reason_code": result.reason_code,
    }
    return _write(_claims(spool) / f"{launch['attempt_id']}.session.json", payload, mode=evidence_file_mode)
