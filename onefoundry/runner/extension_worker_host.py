"""Host-side isolated extension invoker and private receipt writer."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import selectors
import stat
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from onefoundry_sdk import inspect_package

IMAGE_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
UTC_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)$"
)
CONTAINER_TMP = "/tmp"  # noqa: S108 - private tmpfs inside isolated container
MAX_JOB_BYTES = 7 * 1024 * 1024
MAX_RESPONSE_BYTES = 128 * 1024
SAFE_CHILD_ERRORS = frozenset(
    {
        "request_too_large",
        "request_invalid",
        "package_hash_mismatch",
        "tool_not_declared",
        "python_tool_not_declared",
        "mcp_tool_not_declared",
        "arguments_not_object",
        "tool_input_invalid",
        "tool_output_not_object",
        "tool_output_invalid",
        "tool_output_too_large",
        "tool_execution_failed",
        "connection_approval_required",
        "connection_denied",
        "connection_failed",
        "connection_outcome_unknown",
        "connection_response_invalid",
    }
)


class WorkerError(RuntimeError):
    """Safe reason code for worker failure. No untrusted payload excerpts."""


@dataclass(frozen=True)
class WorkerPolicy:
    image_id: str
    docker_binary: Path
    docker_socket: Path
    memory_mb: int
    processes: int
    cpus: int
    max_timeout_seconds: int
    max_output_bytes: int

    def validate(self) -> None:
        if not IMAGE_ID.fullmatch(self.image_id):
            raise WorkerError("worker_image_unpinned")
        if not self.docker_binary.is_file() or not os.access(self.docker_binary, os.X_OK):
            raise WorkerError("docker_unavailable")
        if not self.docker_socket.is_socket():
            raise WorkerError("docker_socket_unavailable")
        if not (128 <= self.memory_mb <= 2048 and 2 <= self.processes <= 64 and 1 <= self.cpus <= 4):
            raise WorkerError("worker_resource_policy_invalid")
        if not (1 <= self.max_timeout_seconds <= 300 and 1024 <= self.max_output_bytes <= 65536):
            raise WorkerError("worker_bounds_invalid")


def canonical_job_sha256(job: Mapping[str, Any]) -> str:
    body = {key: value for key, value in job.items() if key != "job_sha256"}
    canonical = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def _job_expiry(job: Mapping[str, Any]) -> datetime | None:
    value = job.get("expires_at")
    if value is None:
        return None
    if not isinstance(value, str) or UTC_TIMESTAMP.fullmatch(value) is None:
        raise WorkerError("worker_job_expiry_invalid")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkerError("worker_job_expiry_invalid") from exc


def job_expired(job: Mapping[str, Any], *, now: datetime | None = None) -> bool:
    """Reject stale queue work before a claim or direct Docker launch."""
    expiry = _job_expiry(job)
    if expiry is None:
        return False
    current = datetime.now(UTC) if now is None else now
    if current.tzinfo is None:
        raise WorkerError("worker_clock_invalid")
    return current.astimezone(UTC) >= expiry


def validate_job(job: Mapping[str, Any], policy: WorkerPolicy) -> bytes:
    policy.validate()
    expected_keys = {
        "schema_version",
        "invocation_id",
        "project_id",
        "actor_id",
        "run_id",
        "profile_id",
        "package_sha256",
        "archive_base64",
        "tool_id",
        "arguments",
        "timeout_seconds",
        "max_output_bytes",
        "worker_image",
        "memory_mb",
        "processes",
        "cpus",
        "execution_kind",
        "job_sha256",
    }
    optional_keys = {"stdio_module", "operation", "expected_schema", "attempt_id", "expires_at"}
    if set(job) - expected_keys - optional_keys or expected_keys - set(job):
        raise WorkerError("worker_job_fields_invalid")
    if job["schema_version"] != 1 or job["execution_kind"] not in {"python", "mcp_stdio"}:
        raise WorkerError("worker_job_schema_invalid")
    _job_expiry(job)
    try:
        for field in ("invocation_id", "project_id", "run_id", "profile_id"):
            UUID(str(job[field]))
        if "attempt_id" in job:
            UUID(str(job["attempt_id"]))
    except (ValueError, TypeError) as exc:
        raise WorkerError("worker_job_identity_invalid") from exc
    if not isinstance(job["actor_id"], str) or not re.fullmatch(r"[A-Za-z0-9:_-]{1,128}", job["actor_id"]):
        raise WorkerError("worker_job_actor_invalid")
    if job["worker_image"] != policy.image_id or any(
        job[field] != getattr(policy, field) for field in ("memory_mb", "processes", "cpus")
    ):
        raise WorkerError("worker_job_policy_mismatch")
    if (
        type(job["timeout_seconds"]) is not int
        or not 1 <= job["timeout_seconds"] <= policy.max_timeout_seconds
        or type(job["max_output_bytes"]) is not int
        or not 1024 <= job["max_output_bytes"] <= policy.max_output_bytes
    ):
        raise WorkerError("worker_job_bounds_invalid")
    if not isinstance(job["arguments"], dict):
        raise WorkerError("worker_job_arguments_invalid")
    if job["job_sha256"] != canonical_job_sha256(job):
        raise WorkerError("worker_job_hash_mismatch")
    try:
        archive = base64.b64decode(job["archive_base64"], validate=True)
        package = inspect_package(archive)
    except (ValueError, TypeError) as exc:
        raise WorkerError("worker_package_invalid") from exc
    if package.package_sha256 != job["package_sha256"]:
        raise WorkerError("worker_package_hash_mismatch")
    tool = next((item for item in package.manifest.tools if item.id == job["tool_id"]), None)
    if tool is None:
        raise WorkerError("worker_tool_unknown")
    if job["execution_kind"] == "python" and tool.runtime != "python":
        raise WorkerError("worker_tool_runtime_mismatch")
    if job["execution_kind"] == "python" and tool.required_connections:
        if tool.effect != "pure":
            raise WorkerError("worker_connection_parent_effect_unsupported")
        for slot in tool.required_connections:
            if not any(template.id == slot for template in package.manifest.connections):
                raise WorkerError("worker_connection_slot_invalid")
    if list(Draft202012Validator(tool.parameters).iter_errors(job["arguments"])):
        raise WorkerError("worker_tool_input_invalid")
    if job["execution_kind"] == "mcp_stdio":
        if tool.runtime != "mcp" or not all(
            key in job for key in ("stdio_module", "operation", "expected_schema")
        ):
            raise WorkerError("worker_mcp_contract_invalid")
        module = job["stdio_module"]
        if not isinstance(module, str) or not re.fullmatch(r"src/[A-Za-z0-9_/]+\.py", module):
            raise WorkerError("worker_mcp_module_invalid")
        if module not in package.file_sha256 or job["operation"] != tool.operation:
            raise WorkerError("worker_mcp_operation_invalid")
        if job["expected_schema"] != tool.returns:
            raise WorkerError("worker_mcp_schema_mismatch")
    return archive


def _docker(policy: WorkerPolicy, *arguments: str, timeout: int = 30) -> str:
    try:
        result = subprocess.run(  # noqa: S603 - exact validated binary and argv
            (str(policy.docker_binary), "--host", f"unix://{policy.docker_socket}", *arguments),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkerError("docker_unavailable") from exc
    if result.returncode:
        raise WorkerError(f"docker_{arguments[0]}_failed")
    return result.stdout.strip()


def _inspect(policy: WorkerPolicy, name: str) -> dict[str, Any]:
    try:
        value = json.loads(_docker(policy, "inspect", "--type", "container", name))
    except ValueError as exc:
        raise WorkerError("docker_inspection_invalid") from exc
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise WorkerError("docker_inspection_invalid")
    return value[0]


def _boundary_valid(state: Mapping[str, Any], policy: WorkerPolicy) -> bool:
    host = state.get("HostConfig", {})
    config = state.get("Config", {})
    return (
        state.get("Image") == policy.image_id
        and host.get("NetworkMode") == "none"
        and host.get("ReadonlyRootfs") is True
        and host.get("Binds") in (None, [])
        and state.get("Mounts") == []
        and host.get("CapDrop") == ["ALL"]
        and host.get("SecurityOpt") == ["no-new-privileges:true"]
        and host.get("PidsLimit") == policy.processes
        and host.get("Memory") == policy.memory_mb * 1024 * 1024
        and host.get("MemorySwap") == policy.memory_mb * 1024 * 1024
        and host.get("NanoCpus") == policy.cpus * 1_000_000_000
        and host.get("Tmpfs") == {CONTAINER_TMP: f"rw,nosuid,nodev,size={policy.memory_mb // 2}m"}
        and config.get("User") == "1000:1000"
    )


def _publish_private(path: Path, body: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise WorkerError("worker_callback_duplicate")
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _callback(
    frame: object,
    job: Mapping[str, Any],
    root: Path | None,
    sequence: int,
    deadline: float,
    cancellation_path: Path | None = None,
) -> dict[str, Any]:
    if not isinstance(frame, dict) or frame.get("kind") != "connection_request":
        raise WorkerError("worker_frame_invalid")
    if frame.get("job_sha256") != job["job_sha256"] or frame.get("sequence") != sequence:
        raise WorkerError("worker_callback_identity_invalid")
    slot, operation, arguments = frame.get("slot"), frame.get("operation"), frame.get("arguments")
    package = inspect_package(base64.b64decode(job["archive_base64"], validate=True))
    tool = next(item for item in package.manifest.tools if item.id == job["tool_id"])
    template = next((item for item in package.manifest.connections if item.id == slot), None)
    if (
        root is None
        or job["execution_kind"] != "python"
        or tool.effect != "pure"
        or slot not in tool.required_connections
        or template is None
        or operation not in {item.id for item in template.operations}
        or not isinstance(arguments, dict)
    ):
        raise WorkerError("worker_callback_not_declared")
    identity = UUID(str(job["invocation_id"]))
    request_path = root / "requests" / f"{identity}-{sequence}.json"
    response_path = root / "responses" / request_path.name
    envelope = {
        "schema_version": 1,
        "invocation_id": str(identity),
        "job_sha256": job["job_sha256"],
        "sequence": sequence,
        "slot": slot,
        "operation": operation,
        "arguments": arguments,
    }
    encoded = json.dumps(envelope, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(encoded) > 65536:
        raise WorkerError("worker_callback_too_large")
    _publish_private(request_path, encoded)
    while time.monotonic() < deadline:
        if cancellation_path is not None and cancellation_path.exists():
            raise WorkerError("worker_cancelled_during_callback")
        if response_path.exists():
            if response_path.is_symlink() or stat.S_IMODE(response_path.stat().st_mode) != 0o600:
                raise WorkerError("worker_callback_response_invalid")
            with response_path.open("rb") as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise WorkerError("worker_callback_response_too_large")
            try:
                result = json.loads(raw)
            except ValueError as exc:
                raise WorkerError("worker_callback_response_invalid") from exc
            if (
                not isinstance(result, dict)
                or result.get("schema_version") != 1
                or result.get("invocation_id") != str(identity)
                or result.get("job_sha256") != job["job_sha256"]
                or result.get("sequence") != sequence
                or result.get("status") not in {"done", "approval_required", "denied", "failed", "unknown"}
            ):
                raise WorkerError("worker_callback_response_invalid")
            return result
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    raise WorkerError("worker_callback_timeout")


def _start_bounded(
    policy: WorkerPolicy,
    name: str,
    request: bytes,
    timeout: int,
    job: Mapping[str, Any] | None = None,
    callback_root: Path | None = None,
    cancellation_path: Path | None = None,
) -> tuple[bytes, dict[str, Any] | None]:
    command = (
        str(policy.docker_binary),
        "--host",
        f"unix://{policy.docker_socket}",
        "start",
        "-a",
        "-i",
        name,
    )
    process = subprocess.Popen(  # noqa: S603 - fixed Docker invocation
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"},
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    for stream in (process.stdin, process.stdout, process.stderr):
        os.set_blocking(stream.fileno(), False)
    outputs: dict[Any, bytearray] = {process.stdout: bytearray(), process.stderr: bytearray()}
    deadline = time.monotonic() + timeout
    sent = 0
    pending = request + b"\n"
    final: bytes | None = None
    last_callback: dict[str, Any] | None = None
    waiting_callback: dict[str, Any] | None = None
    sequence = 0
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdin, selectors.EVENT_WRITE)
            selector.register(process.stdout, selectors.EVENT_READ)
            selector.register(process.stderr, selectors.EVENT_READ)
            while selector.get_map():
                if cancellation_path is not None and cancellation_path.exists():
                    raise WorkerError("worker_cancelled_during_execution")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkerError("worker_timeout")
                for key, _ in selector.select(min(remaining, 0.2)):
                    stream_obj = key.fileobj
                    if stream_obj is process.stdin:
                        if sent < len(pending):
                            sent += os.write(process.stdin.fileno(), pending[sent : sent + 65536])
                        else:
                            selector.unregister(process.stdin)
                    else:
                        data = os.read(key.fd, 8192)
                        if not data:
                            selector.unregister(stream_obj)
                        else:
                            outputs[stream_obj].extend(data)
                            if sum(map(len, outputs.values())) > MAX_RESPONSE_BYTES:
                                raise WorkerError("worker_response_too_large")
                            if stream_obj is process.stdout:
                                while b"\n" in outputs[process.stdout]:
                                    line, _, tail = bytes(outputs[process.stdout]).partition(b"\n")
                                    outputs[process.stdout] = bytearray(tail)
                                    try:
                                        frame = json.loads(line)
                                    except ValueError as exc:
                                        raise WorkerError("worker_frame_invalid") from exc
                                    if isinstance(frame, dict) and frame.get("kind") == "result":
                                        if final is not None:
                                            raise WorkerError("worker_duplicate_result")
                                        final = line
                                        try:
                                            selector.unregister(process.stdin)
                                        except KeyError:
                                            pass
                                        process.stdin.close()
                                    else:
                                        if final is not None or job is None:
                                            raise WorkerError("worker_frame_invalid")
                                        sequence += 1
                                        if sequence > 16:
                                            raise WorkerError("worker_callback_limit")
                                        last_callback = _callback(
                                            frame,
                                            job,
                                            callback_root,
                                            sequence,
                                            deadline,
                                            cancellation_path,
                                        )
                                        if last_callback["status"] in {"approval_required", "unknown"}:
                                            waiting_callback = last_callback
                                        pending = (
                                            json.dumps(
                                                {
                                                    "kind": "connection_response",
                                                    **{
                                                        key: value
                                                        for key, value in last_callback.items()
                                                        if key != "invocation_id"
                                                    },
                                                },
                                                sort_keys=True,
                                                separators=(",", ":"),
                                                allow_nan=False,
                                            ).encode()
                                            + b"\n"
                                        )
                                        sent = 0
                                        try:
                                            selector.unregister(process.stdin)
                                        except KeyError:
                                            pass
                                        selector.register(process.stdin, selectors.EVENT_WRITE)
        if process.wait(timeout=max(1, deadline - time.monotonic())) != 0:
            raise WorkerError("worker_container_failed")
        if final is None or outputs[process.stdout]:
            raise WorkerError("worker_result_invalid")
        return final, waiting_callback or last_callback
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def run_job(
    job: Mapping[str, Any],
    policy: WorkerPolicy,
    callback_root: Path | None = None,
    cancellation_path: Path | None = None,
) -> dict[str, Any]:
    """Run one admitted job and return a bounded host receipt; no retry on unknown effect."""
    validate_job(job, policy)
    if job_expired(job):
        return _receipt(job, "failed", reason="worker_job_expired")
    if callback_root is not None:
        for directory in (callback_root / "requests", callback_root / "responses"):
            if (
                not directory.is_dir()
                or directory.is_symlink()
                or directory.stat().st_uid != os.geteuid()
                or stat.S_IMODE(directory.stat().st_mode) != 0o700
            ):
                raise WorkerError("worker_callback_spool_invalid")
    name = f"onefoundry-extension-{uuid4().hex[:16]}"
    tmpfs = f"rw,nosuid,nodev,size={policy.memory_mb // 2}m"
    created = False
    try:
        _docker(
            policy,
            "create",
            "--name",
            name,
            "--pull=never",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            f"--pids-limit={policy.processes}",
            f"--memory={policy.memory_mb}m",
            f"--memory-swap={policy.memory_mb}m",
            f"--cpus={policy.cpus}",
            "--user=1000:1000",
            "--tmpfs",
            f"{CONTAINER_TMP}:{tmpfs}",
            "--env",
            "HOME=/tmp",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "-i",
            "--entrypoint",
            "/opt/extension/.venv/bin/python",
            policy.image_id,
            "/opt/extension_worker_child.py",
        )
        created = True
        if not _boundary_valid(_inspect(policy, name), policy):
            raise WorkerError("container_boundary_invalid")
        response, callback = _start_bounded(
            policy,
            name,
            json.dumps(job, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(),
            job["timeout_seconds"] + 10,
            job,
            callback_root,
            cancellation_path,
        )
        if _inspect(policy, name).get("State", {}).get("ExitCode") != 0:
            raise WorkerError("worker_exit_nonzero")
        payload = json.loads(response)
        if not isinstance(payload, dict) or payload.get("status") not in {"success", "error"}:
            raise WorkerError("worker_result_invalid")
        if callback is not None and callback["status"] == "approval_required":
            return _receipt(
                job,
                "approval_required",
                reason="connection_approval_required",
                approval_id=callback.get("approval_id"),
                waiting_invocation_id=callback.get("waiting_invocation_id"),
            )
        if callback is not None and callback["status"] == "unknown":
            return _receipt(
                job,
                "unknown",
                reason="connection_outcome_unknown",
                waiting_invocation_id=callback.get("waiting_invocation_id"),
            )
        if payload["status"] == "success":
            package = inspect_package(base64.b64decode(job["archive_base64"], validate=True))
            tool = next(item for item in package.manifest.tools if item.id == job["tool_id"])
            output = payload.get("output")
            if not isinstance(output, dict) or list(Draft202012Validator(tool.returns).iter_errors(output)):
                raise WorkerError("worker_output_schema_invalid")
            if (
                len(json.dumps(output, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())
                > job["max_output_bytes"]
            ):
                raise WorkerError("worker_output_too_large")
            return _receipt(job, "done", result=payload.get("output"))
        reason = payload.get("error_code")
        if reason == "connection_approval_required" and callback is not None:
            return _receipt(
                job,
                "approval_required",
                reason=reason,
                approval_id=callback.get("approval_id"),
                waiting_invocation_id=callback.get("waiting_invocation_id"),
            )
        if reason == "connection_outcome_unknown" and callback is not None:
            return _receipt(
                job,
                "unknown",
                reason=reason,
                waiting_invocation_id=callback.get("waiting_invocation_id"),
            )
        return _receipt(job, "failed", reason=reason if reason in SAFE_CHILD_ERRORS else "worker_error")
    except WorkerError as exc:
        return _receipt(job, "unknown", reason=str(exc))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return _receipt(job, "unknown", reason="worker_transport_failed")
    finally:
        if created:
            try:
                _docker(policy, "rm", "--force", name)
            except WorkerError:
                pass


def _receipt(
    job: Mapping[str, Any],
    status: str,
    *,
    result: Any = None,
    reason: str = "",
    approval_id: object = None,
    waiting_invocation_id: object = None,
) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "schema_version": 1,
        "invocation_id": job["invocation_id"],
        "package_sha256": job["package_sha256"],
        "job_sha256": job["job_sha256"],
        "status": status,
    }
    if status == "done":
        receipt["result"] = result
    else:
        receipt["reason"] = reason
    if status in {"approval_required", "unknown"}:
        if isinstance(approval_id, str):
            receipt["approval_id"] = approval_id
        if isinstance(waiting_invocation_id, str):
            receipt["waiting_invocation_id"] = waiting_invocation_id
    return receipt


def process_job_file(
    job_path: Path,
    receipts_dir: Path,
    policy: WorkerPolicy,
    callback_root: Path | None = None,
    cancellation_path: Path | None = None,
) -> dict[str, Any]:
    """Consume one private app-owned job and atomically publish one host-owned receipt."""
    if job_path.is_symlink() or not job_path.is_file() or stat.S_IMODE(job_path.stat().st_mode) != 0o600:
        raise WorkerError("worker_job_file_invalid")
    if job_path.stat().st_size > MAX_JOB_BYTES:
        raise WorkerError("worker_job_too_large")
    try:
        job = json.loads(job_path.read_bytes())
    except ValueError as exc:
        raise WorkerError("worker_job_json_invalid") from exc
    if not isinstance(job, dict):
        raise WorkerError("worker_job_json_invalid")
    receipt = run_job(job, policy, callback_root, cancellation_path)
    receipts_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    output = receipts_dir / f"{UUID(str(job['invocation_id']))}.json"
    if output.exists() or output.is_symlink():
        raise WorkerError("worker_receipt_already_exists")
    temporary = receipts_dir / f".{output.name}.{uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return receipt
