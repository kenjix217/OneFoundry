"""Trusted one-shot controller for a deterministic, offline local-source probe.

The controller can read the admitted source and canonical spool. The bwrap child
can write only its disposable repository, with an empty network namespace.
This module never launches Codex, Claude, or another coding provider.
"""

# ruff: noqa: S108 - /tmp is a fresh tmpfs inside the bubblewrap namespace.

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from runner.contract import ContractError, reject_duplicate_keys
from runner.local_source_stage import (
    LocalSource,
    StageError,
    StageLimits,
    stage_local_source,
    verify_local_source_pin,
)
from runner.service import _atomic_bytes, _atomic_new

SHA40 = re.compile(r"[0-9a-f]{40}\Z")
MANIFEST_FIELDS = {
    "schema_version",
    "job_id",
    "project_id",
    "run_id",
    "attempt_id",
    "adapter_id",
    "target_id",
    "stage",
    "actor_id",
    "task_title",
    "task_instructions",
    "limits",
    "source",
    "request_sha256",
}
SYSTEM_MOUNTS = (Path("/usr"), Path("/bin"), Path("/lib"), Path("/lib64"), Path("/proc"), Path("/dev"))


class ProbeError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ProbeConfig:
    spool: Path
    admitted_root: Path
    bwrap: Path
    stage_limits: StageLimits

    def validate(self) -> None:
        self.stage_limits.validate()
        for directory in (self.spool, self.admitted_root):
            if not directory.is_absolute() or directory.is_symlink() or directory.resolve() != directory:
                raise ProbeError("invalid_controller_root")
            if not directory.is_dir() or not stat.S_ISDIR(directory.lstat().st_mode):
                raise ProbeError("invalid_controller_root")
            if any(directory == mount or mount in directory.parents for mount in SYSTEM_MOUNTS):
                raise ProbeError("controller_root_visible_to_child")
        if (
            self.spool == self.admitted_root
            or self.spool in self.admitted_root.parents
            or self.admitted_root in self.spool.parents
        ):
            raise ProbeError("controller_roots_overlap")
        for name in ("jobs", "claims", "receipts", "artifacts"):
            path = self.spool / name
            if (path.exists() or path.is_symlink()) and (
                path.is_symlink() or not stat.S_ISDIR(path.lstat().st_mode)
            ):
                raise ProbeError("invalid_spool_directory")
        if (
            not self.bwrap.is_absolute()
            or not self.bwrap.is_file()
            or self.bwrap.is_symlink()
            or not os.access(self.bwrap, os.X_OK)
        ):
            raise ProbeError("bwrap_unavailable")


def _uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = UUID(value)
    except ValueError:
        return False
    return parsed.version == 4 and str(parsed) == value


def _manifest(path: Path, spool: Path) -> dict[str, Any]:
    if path.parent != spool / "jobs" or path.is_symlink():
        raise ContractError("invalid_probe_manifest_path")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 8192:
                raise ContractError("invalid_probe_manifest")
            raw = os.read(descriptor, 8193)
            if len(raw) != info.st_size:
                raise ContractError("invalid_probe_manifest")
        finally:
            os.close(descriptor)
        value = json.loads(raw, object_pairs_hook=reject_duplicate_keys)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise ContractError("invalid_probe_manifest") from exc
    if not isinstance(value, dict) or set(value) != MANIFEST_FIELDS:
        raise ContractError("invalid_probe_manifest_fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 2:
        raise ContractError("invalid_probe_schema")
    if value["target_id"] != "local_source_probe_v1" or value["stage"] != "engineer":
        raise ContractError("invalid_probe_target")
    for field in ("job_id", "project_id", "run_id", "attempt_id"):
        if not _uuid(value[field]):
            raise ContractError("invalid_probe_identity")
    if value["job_id"] != value["attempt_id"] or path.name != f"{value['attempt_id']}.json":
        raise ContractError("invalid_probe_identity")
    if value["adapter_id"] != "offline_probe":
        raise ContractError("invalid_probe_adapter")
    for field, maximum in (("actor_id", 128), ("task_title", 256), ("task_instructions", 4096)):
        item = value[field]
        if not isinstance(item, str) or not item or len(item.encode("utf-8")) > maximum:
            raise ContractError("invalid_probe_task")
    limits = value["limits"]
    if not isinstance(limits, dict) or set(limits) != {"wall_seconds", "max_log_bytes", "max_diff_bytes"}:
        raise ContractError("invalid_probe_limits")
    for name, low, high in (
        ("wall_seconds", 1, 3600),
        ("max_log_bytes", 512, 65536),
        ("max_diff_bytes", 512, 65536),
    ):
        if type(limits[name]) is not int or not low <= limits[name] <= high:
            raise ContractError("invalid_probe_limits")
    try:
        LocalSource.parse(value["source"])
    except (StageError, TypeError) as exc:
        raise ContractError("invalid_probe_source") from exc
    unsigned = {key: item for key, item in value.items() if key != "request_sha256"}
    digest = hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if value["request_sha256"] != digest:
        raise ContractError("invalid_probe_digest")
    return value


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _run(command: list[str], cwd: Path, deadline: float, limit: int) -> bytes:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    try:
        result = subprocess.run(  # noqa: S603 - fixed executable and validated arguments
            command,
            cwd=cwd,
            capture_output=True,
            timeout=remaining,
            check=False,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": "/tmp",  # noqa: S108 - private bwrap tmpfs
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_NO_LAZY_FETCH": "1",
                "GIT_AUTHOR_NAME": "OneFoundry offline probe",
                "GIT_AUTHOR_EMAIL": "probe@example.invalid",
                "GIT_COMMITTER_NAME": "OneFoundry offline probe",
                "GIT_COMMITTER_EMAIL": "probe@example.invalid",
            },
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError from exc
    if len(result.stdout) > limit or len(result.stderr) > limit:
        raise ProbeError("probe_output_limit")
    if result.returncode != 0:
        raise ProbeError("probe_command_failed")
    return result.stdout


def _git(repo: Path, deadline: float, *args: str, limit: int = 65536) -> bytes:
    return _run(["/usr/bin/git", "-c", "core.hooksPath=/dev/null", *args], repo, deadline, limit)


def _prepare_repo(repo: Path, attempt_id: str, tree: str, deadline: float) -> str:
    branch = f"onefoundry/probe-{attempt_id}"
    _git(repo, deadline, "init", "-q", "-b", branch)
    _git(repo, deadline, "add", "-A")
    staged_tree = _git(repo, deadline, "write-tree").decode().strip()
    if staged_tree != tree:
        raise ProbeError("staged_tree_mismatch")
    _git(repo, deadline, "commit", "-qm", f"Offline staged baseline {attempt_id}")
    baseline = _git(repo, deadline, "rev-parse", "HEAD").decode().strip()
    if SHA40.fullmatch(baseline) is None:
        raise ProbeError("baseline_commit_invalid")
    if _git(repo, deadline, "rev-parse", "HEAD^{tree}").decode().strip() != tree:
        raise ProbeError("baseline_tree_mismatch")
    if _git(repo, deadline, "status", "--porcelain", "--untracked-files=all"):
        raise ProbeError("staged_tree_dirty")
    return baseline


def _child(
    repo: Path, attempt_id: str, config: ProbeConfig, deadline: float, max_log: int, parent_net: str
) -> bytes:
    script = Path(__file__).with_name("probe_child.py")
    command = [
        str(config.bwrap),
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--setenv",
        "PATH",
        "/usr/bin:/bin",
        "--setenv",
        "HOME",
        "/tmp",
        "--setenv",
        "PYTHONNOUSERSITE",
        "1",
        "--setenv",
        "PYTHONDONTWRITEBYTECODE",
        "1",
    ]
    for mount in SYSTEM_MOUNTS[:4]:
        if mount.is_dir():
            command.extend(("--ro-bind", str(mount), str(mount)))
    command.extend(
        (
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--bind",
            str(repo),
            "/workspace",
            "--ro-bind",
            str(script),
            "/probe_child.py",
            "--chdir",
            "/workspace",
            "--",
            "/usr/bin/python3",
            "/probe_child.py",
            attempt_id,
            parent_net,
        )
    )
    return _run(command, repo, deadline, max_log)


def _check_observation(raw: bytes, parent_net: str) -> None:
    observation = json.loads(raw)
    allowed_environment = {
        "HOME",
        "LC_CTYPE",
        "PATH",
        "PWD",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONNOUSERSITE",
    }
    if (
        observation.get("network_namespace") == parent_net
        or observation.get("network_connect") != "blocked"
        or set(observation.get("environment_keys", [])) != allowed_environment
        or any(
            name in observation.get("root_entries", [])
            for name in ("home", "runner-spool", "admitted-source")
        )
    ):
        raise ProbeError("child_isolation_unverified")


def write_probe_capabilities(config: ProbeConfig) -> bool:
    """Publish offline readiness only after a real namespace and network test."""
    config.validate()
    available = False
    try:
        with tempfile.TemporaryDirectory(prefix="onefoundry-probe-selftest-") as temporary:
            repo = Path(temporary)
            parent_net = os.readlink("/proc/self/ns/net")
            raw = _child(
                repo,
                str(uuid4()),
                config,
                time.monotonic() + config.stage_limits.wall_seconds,
                4096,
                parent_net,
            )
            _check_observation(raw, parent_net)
            available = True
    except (ProbeError, TimeoutError, OSError, subprocess.SubprocessError, ValueError):
        pass
    payload = {
        "schema_version": 1,
        "observed_at": _now(),
        "isolated": available,
        "source_root_ready": True,
        "available": available,
        "reason": None if available else "isolation_unavailable",
    }
    _atomic_bytes(
        config.spool / "probe_capabilities.json",
        (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(),
    )
    return available


def _artifact(spool: Path, attempt_id: str, kind: str, name: str, data: bytes) -> dict[str, Any]:
    relative = f"artifacts/{attempt_id}/{name}"
    directory = spool / "artifacts" / attempt_id
    if directory.is_symlink() or (directory.exists() and not stat.S_ISDIR(directory.lstat().st_mode)):
        raise ProbeError("invalid_artifact_directory")
    _atomic_new(spool / relative, data)
    return {
        "kind": kind,
        "path": relative,
        "sha256": hashlib.sha256(data).hexdigest(),
        "byte_size": len(data),
    }


def _verify_local_claim(claim: Path, attempt_id: str) -> None:
    if claim.is_symlink():
        raise ContractError("invalid_probe_claim")
    try:
        descriptor = os.open(claim, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size != len(attempt_id):
                raise ContractError("invalid_probe_claim")
            if os.read(descriptor, len(attempt_id) + 1) != attempt_id.encode("ascii"):
                raise ContractError("invalid_probe_claim")
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ContractError("invalid_probe_claim") from exc


def process_probe_job(path: Path, config: ProbeConfig, *, preclaimed: bool = False) -> dict[str, Any] | None:
    """Claim one validated v2 manifest; never retry a claimed attempt."""
    config.validate()
    job = _manifest(path, config.spool)
    attempt_id = job["attempt_id"]
    if (config.spool / "receipts" / f"{attempt_id}.json").exists():
        return None
    claim = config.spool / "claims" / f"{attempt_id}.claim"
    if preclaimed:
        _verify_local_claim(claim, attempt_id)
        # The bridge owns the canonical claim. This local marker prevents a
        # second launch from the copied exchange if its receipt is lost.
        try:
            _atomic_new(config.spool / "claims" / f"{attempt_id}.started", attempt_id.encode("ascii"))
        except FileExistsError:
            return None
    else:
        try:
            _atomic_new(claim, attempt_id.encode("ascii"))
        except FileExistsError:
            return None
    started_at = _now()
    status, reason = "unknown", "probe_failure_after_claim"
    branch: str | None = None
    baseline: str | None = None
    commit: str | None = None
    diff_sha: str | None = None
    artifacts: list[dict[str, Any]] = []
    tests: list[dict[str, Any]] = []
    child_started = False
    try:
        deadline = time.monotonic() + job["limits"]["wall_seconds"]
        with tempfile.TemporaryDirectory(prefix="onefoundry-source-probe-") as temporary:
            root = Path(temporary)
            repo = root / "repo"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            stage_limits = replace(
                config.stage_limits, wall_seconds=min(config.stage_limits.wall_seconds, remaining)
            )
            stage = stage_local_source(job["source"], config.admitted_root, repo, stage_limits)
            baseline = _prepare_repo(repo, attempt_id, stage.source_tree, deadline)
            verify_local_source_pin(job["source"], config.admitted_root, stage.source_tree, deadline)
            parent_net = os.readlink("/proc/self/ns/net")
            child_started = True
            observation_raw = _child(
                repo, attempt_id, config, deadline, job["limits"]["max_log_bytes"], parent_net
            )
            _check_observation(observation_raw, parent_net)
            filename = f"onefoundry-probe-{attempt_id}.txt"
            candidate = repo / filename
            expected = f"OneFoundry offline source probe {attempt_id}\n".encode("ascii")
            if candidate.is_symlink() or candidate.read_bytes() != expected:
                raise ProbeError("probe_edit_invalid")
            observed = _git(repo, deadline, "status", "--porcelain", "--untracked-files=all").decode()
            if observed != f"?? {filename}\n":
                raise ProbeError("probe_tree_invalid")
            _git(repo, deadline, "add", "--", filename)
            _git(repo, deadline, "diff", "--cached", "--check")
            tests.append({"name": "git_diff_check", "status": "passed", "exit_code": 0})
            _git(repo, deadline, "commit", "-qm", "Offline source probe")
            commit = _git(repo, deadline, "rev-parse", "HEAD").decode().strip()
            if SHA40.fullmatch(commit) is None:
                raise ProbeError("probe_commit_invalid")
            changed = _git(repo, deadline, "diff", "--name-only", f"{baseline}..HEAD").decode()
            if changed != f"{filename}\n":
                raise ProbeError("probe_tree_invalid")
            _git(repo, deadline, "diff", "--check", f"{baseline}..HEAD")
            diff = _git(
                repo,
                deadline,
                "diff",
                "--binary",
                f"{baseline}..HEAD",
                limit=job["limits"]["max_diff_bytes"],
            )
            if not diff:
                raise ProbeError("probe_diff_empty")
            diff_sha = hashlib.sha256(diff).hexdigest()
            artifacts = [
                _artifact(config.spool, attempt_id, "diff", "change.diff", diff),
                _artifact(
                    config.spool,
                    attempt_id,
                    "log",
                    "after.log",
                    b"offline_probe=passed\ngit_diff_check=passed\n" + observation_raw,
                ),
            ]
            branch = f"onefoundry/probe-{attempt_id}"
            status, reason = "done", "completed"
    except StageError as exc:
        status, reason = ("unknown", "probe_failure_after_child") if child_started else ("failed", exc.code)
    except (ProbeError, TimeoutError, OSError, subprocess.SubprocessError, ValueError):
        status, reason = (
            ("unknown", "probe_failure_after_child")
            if child_started
            else ("failed", "probe_prelaunch_failed")
        )
    if status != "done":
        branch = commit = diff_sha = None
        artifacts = []
        tests = []
    receipt: dict[str, Any] = {
        "schema_version": 2,
        "job_id": attempt_id,
        "project_id": job["project_id"],
        "run_id": job["run_id"],
        "attempt_id": attempt_id,
        "adapter_id": job["adapter_id"],
        "target_id": "local_source_probe_v1",
        "source": job["source"],
        "request_sha256": job["request_sha256"],
        "status": status,
        "reason_code": reason,
        "started_at": started_at,
        "finished_at": _now(),
        "provider_session_id": None,
        "baseline_commit": baseline if status == "done" else None,
        "branch": branch,
        "commit": commit,
        "diff_sha256": diff_sha,
        "artifacts": artifacts,
        "tests": tests,
        "tokens_used": 0,
        "cost_usd": "0",
    }
    _atomic_new(
        config.spool / "receipts" / f"{attempt_id}.json",
        (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode(),
    )
    return receipt


def run_pending(config: ProbeConfig) -> int:
    config.validate()
    processed = 0
    for path in sorted((config.spool / "jobs").glob("*.json")):
        try:
            if process_probe_job(path, config) is not None:
                processed += 1
        except FileNotFoundError:
            continue
        except ContractError as exc:
            print(f"rejected probe manifest {path.name}: {exc}", file=sys.stderr)
    return processed


def main() -> None:
    parser = argparse.ArgumentParser(description="Host-side offline local-source probe supervisor")
    parser.add_argument("--spool", required=True, type=Path)
    parser.add_argument("--admitted-root", required=True, type=Path)
    parser.add_argument("--bwrap", required=True, type=Path)
    parser.add_argument("--stage-max-files", required=True, type=int)
    parser.add_argument("--stage-max-bytes", required=True, type=int)
    parser.add_argument("--stage-max-tree-bytes", required=True, type=int)
    parser.add_argument("--stage-wall-seconds", required=True, type=float)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--preclaimed-attempt", type=str)
    args = parser.parse_args()
    config = ProbeConfig(
        args.spool,
        args.admitted_root,
        args.bwrap,
        StageLimits(
            args.stage_max_files, args.stage_max_bytes, args.stage_max_tree_bytes, args.stage_wall_seconds
        ),
    )
    config.validate()
    if args.preclaimed_attempt is not None:
        if not args.once or not _uuid(args.preclaimed_attempt):
            parser.error("--preclaimed-attempt requires --once and a canonical UUIDv4")
        if write_probe_capabilities(config):
            process_probe_job(
                config.spool / "jobs" / f"{args.preclaimed_attempt}.json", config, preclaimed=True
            )
        return
    while True:
        if write_probe_capabilities(config):
            run_pending(config)
        if args.once:
            return
        time.sleep(1)


if __name__ == "__main__":
    main()
