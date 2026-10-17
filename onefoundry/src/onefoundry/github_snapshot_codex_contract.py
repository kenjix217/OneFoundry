"""Exact fake-only v3 snapshot coding manifest and receipt bindings."""

import hashlib
import json
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from onefoundry.workflow_schemas import StrictModel


class SnapshotCodeSource(StrictModel):
    snapshot_attempt_id: UUID
    source_id: UUID
    account_id: UUID
    owner: str = Field(min_length=1, max_length=100)
    repo: str = Field(min_length=1, max_length=100)
    branch: str = Field(min_length=1, max_length=200)
    saved_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    source_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    snapshot_path: str = Field(pattern=r"^snapshots/[0-9a-f]{64}\.json$")
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_byte_size: int = Field(ge=1, le=16_777_216)
    snapshot_request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def exact_path(self) -> "SnapshotCodeSource":
        if self.snapshot_path != f"snapshots/{self.snapshot_sha256}.json":
            raise ValueError("snapshot CAS path mismatch")
        return self


class SnapshotCodeManifest(StrictModel):
    schema_version: Literal[3]
    target_id: Literal["github_snapshot_codex_fixture_v1"]
    job_id: UUID
    project_id: UUID
    run_id: UUID
    attempt_id: UUID
    work_order_id: UUID
    idempotency_key: UUID
    intent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    adapter_id: Literal["codex_cli"]
    stage: Literal["engineer"]
    actor_id: str = Field(min_length=1, max_length=128)
    task_title: str = Field(min_length=1, max_length=256)
    task_instructions: str = Field(min_length=1, max_length=4096)
    source: SnapshotCodeSource
    limits: dict[str, int]
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def exact_digest(self) -> "SnapshotCodeManifest":
        if self.job_id != self.attempt_id or set(self.limits) != {
            "wall_seconds",
            "max_log_bytes",
            "max_diff_bytes",
        }:
            raise ValueError("snapshot coding manifest identity invalid")
        if not (
            1 <= self.limits["wall_seconds"] <= 60
            and 512 <= self.limits["max_log_bytes"] <= 65536
            and 512 <= self.limits["max_diff_bytes"] <= 65536
        ):
            raise ValueError("snapshot coding manifest limits invalid")
        unsigned = self.model_dump(mode="json", exclude={"request_sha256"})
        canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
        if hashlib.sha256(canonical).hexdigest() != self.request_sha256:
            raise ValueError("snapshot coding request digest invalid")
        return self


def manifest_payload(unsigned: dict[str, object]) -> dict[str, object]:
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    value = {**unsigned, "request_sha256": hashlib.sha256(canonical).hexdigest()}
    SnapshotCodeManifest.model_validate(value)
    return value


def expected_diff() -> bytes:
    """Exact fixed file edit proof; no repository code is executed."""
    before = b"synthetic HTTPS fixture only\n"
    after = b"synthetic HTTPS fixture repaired\n"
    old = hashlib.sha1(f"blob {len(before)}\0".encode() + before).hexdigest()  # noqa: S324
    new = hashlib.sha1(f"blob {len(after)}\0".encode() + after).hexdigest()  # noqa: S324
    return (
        "diff --git a/file.txt b/file.txt\n"
        f"index {old[:7]}..{new[:7]} 100644\n"
        "--- a/file.txt\n+++ b/file.txt\n@@ -1 +1 @@\n"
        "-synthetic HTTPS fixture only\n+synthetic HTTPS fixture repaired\n"
    ).encode()
