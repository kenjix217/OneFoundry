"""Run fixed Python fixture tests in a disposable, networkless Docker container.

The trusted supervisor may inspect changed repository bytes, but never imports
or executes them. A pinned image and an exact read-only mount are required.
"""

from __future__ import annotations

import os
import re
import selectors
import subprocess
from datetime import UTC, datetime
from math import ceil
from pathlib import Path
from typing import Any
from uuid import uuid4

from runner import docker_egress_proof as docker
from runner.fixture_git import TRACE_OBSERVER, Deadline, ExecutionError

IMAGE = re.compile(r"^sha256:[0-9a-f]{64}$")
TEST_COMMAND = ("python", "-m", "unittest", "discover", "-s", "tests", "-v")
MEMORY_BYTES = 256 * 1024 * 1024
TMPFS = "/tmp:rw,noexec,nosuid,nodev,size=16m"  # noqa: S108 - private container tmpfs


def _inspect_boundary(name: str, image_id: str, repo: Path) -> None:
    state = docker._inspect("container", name)
    host = state.get("HostConfig", {})
    config = state.get("Config", {})
    mounts = state.get("Mounts", [])
    if (
        state.get("Image") != image_id
        or host.get("NetworkMode") != "none"
        or host.get("ReadonlyRootfs") is not True
        or host.get("Privileged") is not False
        or host.get("CapDrop") != ["ALL"]
        or host.get("SecurityOpt") != ["no-new-privileges:true"]
        or host.get("PidsLimit") != 32
        or host.get("Memory") != MEMORY_BYTES
        or host.get("MemorySwap") != MEMORY_BYTES
        or host.get("NanoCpus") != 1_000_000_000
        or host.get("Tmpfs")
        != {
            "/tmp": "rw,noexec,nosuid,nodev,size=16m",  # noqa: S108 - private container tmpfs
            "/app": "rw,noexec,nosuid,nodev,size=16m",
        }
        or host.get("LogConfig", {}).get("Type") != "none"
        or config.get("User") != f"{os.getuid()}:{os.getgid()}"
        or config.get("WorkingDir") != "/workspace"
        or len(mounts) != 1
        or mounts[0].get("Destination") != "/workspace"
        or mounts[0].get("Source") != str(repo)
        or mounts[0].get("RW") is not False
        or set(state.get("NetworkSettings", {}).get("Networks", {})) != {"none"}
    ):
        raise ExecutionError("fixture_test_boundary_invalid")


def _bounded_attach(name: str, deadline: Deadline, log_limit: int) -> bytes:
    command = (
        "/usr/bin/docker",
        "--host",
        "unix:///var/run/docker.sock",
        "start",
        "--attach",
        name,
    )
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed Docker argv and unique container name
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"},
        )
    except OSError as exc:
        raise ExecutionError("fixture_test_start_failed") from exc
    output = bytearray()
    assert process.stdout is not None
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                ready = selector.select(deadline.remaining())
                if not ready:
                    raise TimeoutError("wall_time_exceeded")
                chunk = os.read(process.stdout.fileno(), min(8192, log_limit + 1 - len(output)))
                if not chunk:
                    selector.unregister(process.stdout)
                    continue
                output.extend(chunk)
                if len(output) > log_limit:
                    raise ExecutionError("test_log_limit_exceeded")
        process.wait(timeout=deadline.remaining())
    except BaseException:
        process.kill()
        process.wait(timeout=5)
        raise
    finally:
        process.stdout.close()
    return bytes(output)


def run_isolated_fixture_test(
    repo: Path,
    deadline: Deadline,
    log_limit: int,
    name: str,
    *,
    image_id: str,
    command: tuple[str, ...] = TEST_COMMAND,
) -> tuple[dict[str, Any], bytes]:
    """Return the existing test receipt shape after proving the container boundary."""
    if (
        IMAGE.fullmatch(image_id) is None
        or repo.is_symlink()
        or not repo.is_dir()
        or repo.resolve() != repo
        or not 512 <= log_limit <= 65536
        or name not in ("before", "after", "qa")
        or not 1 <= len(command) <= 32
        or any(not arg or len(arg) > 500 or any(ord(c) < 32 for c in arg) for arg in command)
    ):
        raise ExecutionError("fixture_test_input_invalid")
    docker._inspect("image", image_id)
    container = f"onefoundry-fixture-test-{uuid4().hex[:12]}"
    created = False
    started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    try:
        docker._docker(
            "create",
            "--name",
            container,
            "--pull=never",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--pids-limit=32",
            f"--memory={MEMORY_BYTES}",
            f"--memory-swap={MEMORY_BYTES}",
            "--cpus=1",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--tmpfs",
            TMPFS,
            "--tmpfs",
            "/app:rw,noexec,nosuid,nodev,size=16m",
            "--log-driver=none",
            "--mount",
            f"type=bind,src={repo},dst=/workspace,readonly",
            "--workdir=/workspace",
            "--env",
            "HOME=/tmp",
            "--env",
            "PYTHONNOUSERSITE=1",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            f"--entrypoint={command[0]}",
            image_id,
            *command[1:],
            timeout=ceil(deadline.remaining()),
        )
        created = True
        _inspect_boundary(container, image_id, repo)
        log = _bounded_attach(container, deadline, log_limit)
        state = docker._inspect("container", container).get("State", {})
        code = state.get("ExitCode")
        if state.get("Status") != "exited" or type(code) is not int:
            raise ExecutionError("fixture_test_result_unverified")
        observer = TRACE_OBSERVER.get()
        if observer is not None:
            observer(command, started_at, code, log, b"", "finished")
        return {"name": name, "status": "passed" if code == 0 else "failed", "exit_code": code}, log
    except (docker.ProofError, OSError, subprocess.SubprocessError) as exc:
        raise ExecutionError("fixture_test_container_failed") from exc
    finally:
        if created:
            try:
                docker._remove("container", container)
            except docker.ProofError as exc:
                raise ExecutionError("fixture_test_cleanup_failed") from exc
