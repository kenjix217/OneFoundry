"""Bounded, symlink-resistant local transport for synthetic runner jobs."""

import hashlib
import json
import os
import re
import stat
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from onefoundry.workflow_schemas import SECRET_PATTERN
from runner.live_progress_spool import ProgressSnapshot, ProgressSpoolInvalid, read_progress

SHA256 = re.compile(r"^[0-9a-f]{64}$")
IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
SESSION_ID = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
REASON = re.compile(r"^[a-z][a-z0-9_]{0,159}$")
ARTIFACT_NAMES = {
    "before.log",
    "after.log",
    "change.diff",
    "repair.bundle",
    "qa.log",
    "merge.log",
    "actions.json",
}


class SpoolInvalid(Exception):
    pass


class RunnerSpool:
    def __init__(self, root: str) -> None:
        self.root = Path(root)

    def _directory(self, name: str) -> Path:
        if self.root.is_symlink() or not self.root.is_dir():
            raise SpoolInvalid("runner spool unavailable")
        directory = self.root / name
        if directory.is_symlink() or not directory.is_dir():
            raise SpoolInvalid("runner spool directory unavailable")
        return directory

    def write_job(
        self, job_id: UUID, payload: dict[str, Any], *, group_readable: bool = False, max_bytes: int = 8192
    ) -> None:
        directory = self._directory("jobs")
        target = directory / f"{job_id}.json"
        if target.exists() or target.is_symlink():
            raise SpoolInvalid("job manifest already exists")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        if not 8192 <= max_bytes <= 32768 or len(encoded) > max_bytes:
            raise SpoolInvalid("job manifest too large")
        temporary = directory / f".{job_id}.{os.getpid()}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        with os.fdopen(os.open(temporary, flags, 0o640 if group_readable else 0o600), "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, target, follow_symlinks=False)
            os.unlink(temporary)
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        finally:
            if temporary.exists():
                temporary.unlink()

    def read_receipt(self, job_id: UUID) -> tuple[dict[str, Any], str] | None:
        directory = self._directory("receipts")
        path = directory / f"{job_id}.json"
        try:
            data = self._read_regular(path, 65536)
        except FileNotFoundError:
            return None
        try:
            value = json.loads(data)
        except (ValueError, UnicodeDecodeError) as exc:
            raise SpoolInvalid("invalid receipt JSON") from exc
        if not isinstance(value, dict):
            raise SpoolInvalid("invalid receipt shape")
        return value, hashlib.sha256(data).hexdigest()

    def read_live_progress(
        self, project_id: UUID, run_id: UUID, attempt_id: UUID, request_sha256: str
    ) -> ProgressSnapshot:
        """Read only a claimed, identity-bound prefix; no receipt is required."""
        try:
            owner_uid = self._progress_claim_owner(attempt_id)
            if owner_uid is None:
                return ProgressSnapshot((), 0)
            return read_progress(
                self.root,
                str(project_id),
                str(run_id),
                str(attempt_id),
                request_sha256,
                owner_uid=owner_uid,
                group_readable=True,
            )
        except ProgressSpoolInvalid as exc:
            raise SpoolInvalid(str(exc)) from exc

    def _progress_claim_owner(self, job_id: UUID) -> int | None:
        path = self._directory("claims") / f"{job_id}.claim"
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_mode & 0o037
                or os.read(descriptor, 65) != str(job_id).encode("ascii")
            ):
                raise SpoolInvalid("runner claim mismatch")
            return info.st_uid
        finally:
            os.close(descriptor)

    @staticmethod
    def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise SpoolInvalid("duplicate launch record key")
            value[key] = item
        return value

    def _launch_file(self, job_id: UUID, suffix: str) -> tuple[dict[str, Any], str] | None:
        path = self._directory("claims") / f"{job_id}.{suffix}.json"
        try:
            data = self._read_regular(path, 4096)
        except FileNotFoundError:
            return None
        try:
            value = json.loads(data, object_pairs_hook=self._no_duplicate_keys)
        except (UnicodeDecodeError, ValueError) as exc:
            raise SpoolInvalid("invalid launch record JSON") from exc
        if not isinstance(value, dict):
            raise SpoolInvalid("invalid launch record shape")
        return value, hashlib.sha256(data).hexdigest()

    @staticmethod
    def _time(value: Any) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise SpoolInvalid("invalid launch record time") from exc
        if parsed.tzinfo is None:
            raise SpoolInvalid("launch record time lacks timezone")
        return parsed

    def read_snapshot_codex_fence(
        self, job_id: UUID, project_id: UUID, run_id: UUID, request_sha256: str
    ) -> bool:
        path = self._directory("claims") / f"{job_id}.fenced.json"
        try:
            raw = self._read_regular(path, 1024)
        except FileNotFoundError:
            return False
        try:
            value = json.loads(raw, object_pairs_hook=self._no_duplicate_keys)
        except (UnicodeDecodeError, ValueError) as exc:
            raise SpoolInvalid("snapshot coding fence invalid") from exc
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "schema_version",
                "attempt_id",
                "project_id",
                "run_id",
                "request_sha256",
                "reason_code",
                "inspected_at",
            }
            or value["schema_version"] != 1
            or value["attempt_id"] != str(job_id)
            or value["project_id"] != str(project_id)
            or value["run_id"] != str(run_id)
            or value["request_sha256"] != request_sha256
            or value["reason_code"] != "claimed_receipt_missing"
        ):
            raise SpoolInvalid("snapshot coding fence identity invalid")
        self._time(value["inspected_at"])
        return True

    def read_launch_evidence(
        self, job_id: UUID, project_id: UUID, run_id: UUID, request_sha256: str
    ) -> dict[str, Any] | None:
        """Read exact, immutable runner observations; intent alone proves no launch."""
        observed_launch = self._launch_file(job_id, "launch")
        observed_session = self._launch_file(job_id, "session")
        if observed_launch is None:
            if observed_session is not None:
                raise SpoolInvalid("session without prelaunch intent")
            return None
        launch, launch_digest = observed_launch
        expected_keys = {
            "schema_version",
            "record_type",
            "launch_id",
            "attempt_id",
            "project_id",
            "run_id",
            "adapter_id",
            "request_sha256",
            "prepared_at",
            "baseline_commit",
            "prompt_sha256",
            "invocation_kind",
            "executable_ref",
            "repository",
            "private_home",
            "wall_seconds",
            "max_output_bytes",
        }
        if launch.get("invocation_kind") == "synthetic_snapshot_codex_fixture":
            expected_keys.update(
                {"snapshot_sha256", "source_tree", "snapshot_revision", "snapshot_receipt_sha256"}
            )
        if launch.get("invocation_kind") == "trusted_local_cli_v1":
            expected_keys.add("trust_mode")
        try:
            launch_id = UUID(launch["launch_id"], version=4)
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            raise SpoolInvalid("invalid launch record ID") from exc
        if (
            set(launch) != expected_keys
            or launch["schema_version"] != 1
            or launch["record_type"] != "prelaunch_intent"
            or str(launch_id) != launch["launch_id"]
            or launch["attempt_id"] != str(job_id)
            or launch["project_id"] != str(project_id)
            or launch["run_id"] != str(run_id)
            or launch["adapter_id"] not in ("codex_cli", "claude_code")
            or launch["request_sha256"] != request_sha256
            or not isinstance(launch["baseline_commit"], str)
            or COMMIT.fullmatch(launch["baseline_commit"]) is None
            or not isinstance(launch["prompt_sha256"], str)
            or SHA256.fullmatch(launch["prompt_sha256"]) is None
            or launch["invocation_kind"]
            not in (
                "injected_cli",
                "trusted_local_cli_v1",
                "synthetic_codex_fixture",
                "synthetic_claude_fixture",
                "synthetic_snapshot_codex_fixture",
            )
            or not isinstance(launch["executable_ref"], str)
            or not launch["executable_ref"]
            or (
                launch["invocation_kind"] == "trusted_local_cli_v1"
                and (
                    launch.get("trust_mode") != "operator_host"
                    or not launch["executable_ref"].startswith("/")
                )
            )
            or any(
                not isinstance(launch[key], str) or not launch[key].startswith("/")
                for key in ("repository", "private_home")
            )
            or type(launch["wall_seconds"]) is not int
            or not 1 <= launch["wall_seconds"] <= 3600
            or type(launch["max_output_bytes"]) is not int
            or not 512 <= launch["max_output_bytes"] <= 65536
            or (
                launch["invocation_kind"] == "synthetic_codex_fixture"
                and (
                    launch["adapter_id"] != "codex_cli"
                    or IMAGE_ID.fullmatch(launch["executable_ref"]) is None
                )
            )
            or (
                launch["invocation_kind"] == "synthetic_claude_fixture"
                and (
                    launch["adapter_id"] != "claude_code"
                    or IMAGE_ID.fullmatch(launch["executable_ref"]) is None
                )
            )
            or (
                launch["invocation_kind"] == "synthetic_snapshot_codex_fixture"
                and (
                    launch["adapter_id"] != "codex_cli"
                    or IMAGE_ID.fullmatch(launch["executable_ref"]) is None
                    or any(
                        not isinstance(launch.get(name), str)
                        or (SHA256 if name.endswith("sha256") else COMMIT).fullmatch(launch[name]) is None
                        for name in (
                            "snapshot_sha256",
                            "source_tree",
                            "snapshot_revision",
                            "snapshot_receipt_sha256",
                        )
                    )
                )
            )
        ):
            raise SpoolInvalid("launch record identity invalid")
        prepared_at = self._time(launch["prepared_at"])
        if self.claim_age_seconds(job_id) is None:
            raise SpoolInvalid("prelaunch intent lacks exclusive claim")
        evidence: dict[str, Any] = {
            "launch_id": launch["launch_id"],
            "launch_sha256": launch_digest,
            "prepared_at": prepared_at,
            "invocation_kind": launch["invocation_kind"],
            "session_observed": False,
            "provider_session_id": None,
        }
        if launch["invocation_kind"] == "synthetic_snapshot_codex_fixture":
            evidence.update(
                {
                    name: launch[name]
                    for name in (
                        "snapshot_sha256",
                        "source_tree",
                        "snapshot_revision",
                        "snapshot_receipt_sha256",
                    )
                }
            )
        if observed_session is None:
            return evidence
        session, session_digest = observed_session
        if (
            set(session)
            != {
                "schema_version",
                "record_type",
                "launch_id",
                "attempt_id",
                "request_sha256",
                "launch_sha256",
                "observed_at",
                "provider_session_id",
                "provider_status",
                "reason_code",
            }
            or session["schema_version"] != 1
            or session["record_type"] != "session_observation"
            or session["launch_id"] != launch["launch_id"]
            or session["attempt_id"] != str(job_id)
            or session["request_sha256"] != request_sha256
            or session["launch_sha256"] != launch_digest
            or session["provider_status"] not in ("done", "unknown")
            or not isinstance(session["reason_code"], str)
            or REASON.fullmatch(session["reason_code"]) is None
            or (
                session["provider_session_id"] is not None
                and (
                    not isinstance(session["provider_session_id"], str)
                    or SESSION_ID.fullmatch(session["provider_session_id"]) is None
                    or SECRET_PATTERN.search(session["provider_session_id"]) is not None
                )
            )
        ):
            raise SpoolInvalid("session observation identity invalid")
        observed_at = self._time(session["observed_at"])
        if observed_at < prepared_at:
            raise SpoolInvalid("session observation precedes intent")
        evidence.update(
            {
                "session_observed": True,
                "session_sha256": session_digest,
                "provider_session_id": session["provider_session_id"],
                "provider_status": session["provider_status"],
                "observed_at": observed_at,
            }
        )
        return evidence

    def claim_age_seconds(self, job_id: UUID) -> float | None:
        directory = self._directory("claims")
        path = directory / f"{job_id}.claim"
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        try:
            info = os.fstat(descriptor)
            data = os.read(descriptor, 65)
            if not stat.S_ISREG(info.st_mode) or data != str(job_id).encode("ascii"):
                raise SpoolInvalid("runner claim mismatch")
            return max(0.0, time.time() - info.st_mtime)
        finally:
            os.close(descriptor)

    def cancel_unclaimed(self, job_id: UUID) -> bool:
        """Fence a never-claimed job before withdrawing its manifest.

        The runner's exclusive claim creation uses the same path. Exactly one side
        wins; a runner that already read the manifest cannot execute after we win.
        """
        claims = self._directory("claims")
        jobs = self._directory("jobs")
        claim = claims / f"{job_id}.claim"
        try:
            descriptor = os.open(claim, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            return False
        with os.fdopen(descriptor, "wb") as output:
            output.write(str(job_id).encode("ascii"))
            output.flush()
            os.fsync(output.fileno())
        for directory in (claims, jobs):
            if directory == jobs:
                (jobs / f"{job_id}.json").unlink(missing_ok=True)
            directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return True

    def read_artifact(self, job_id: UUID, relative_path: str, size: int, digest: str) -> bytes:
        prefix = f"artifacts/{job_id}/"
        if not relative_path.startswith(prefix):
            raise SpoolInvalid("artifact path outside job")
        name = relative_path[len(prefix) :]
        if name not in ARTIFACT_NAMES or not SHA256.fullmatch(digest) or size < 0 or size > 65536:
            raise SpoolInvalid("invalid artifact metadata")
        directory = self._directory("artifacts") / str(job_id)
        if directory.is_symlink() or not directory.is_dir():
            raise SpoolInvalid("artifact directory unavailable")
        data = self._read_regular(directory / name, 65536)
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise SpoolInvalid("artifact hash mismatch")
        return data

    @staticmethod
    def _read_regular(path: Path, limit: int) -> bytes:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise SpoolInvalid("spool file invalid or oversized")
            data = os.read(descriptor, limit + 1)
            if len(data) > limit or len(data) != info.st_size:
                raise SpoolInvalid("spool file changed while reading")
            return data
        finally:
            os.close(descriptor)
