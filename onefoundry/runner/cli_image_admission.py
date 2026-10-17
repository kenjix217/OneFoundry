"""Shared Docker boundary for offline coding CLI image admission."""

from __future__ import annotations

import json
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4


class AdmissionError(RuntimeError):
    """Image build or offline admission check failed."""


@dataclass(frozen=True)
class DockerEndpoint:
    executable: Path
    socket: Path

    def validate(self) -> None:
        if not self.executable.is_absolute() or not self.socket.is_absolute():
            raise AdmissionError("docker_endpoint_invalid")
        try:
            if not self.executable.is_file() or not stat.S_ISSOCK(self.socket.stat().st_mode):
                raise AdmissionError("docker_endpoint_invalid")
        except OSError as exc:
            raise AdmissionError("docker_endpoint_invalid") from exc


@dataclass(frozen=True)
class AdmissionSpec:
    name: str
    image_dir: Path
    expected_version: str
    help_terms: tuple[str, ...]
    home: str
    private_dir: str
    absent_paths: tuple[str, ...]
    env: tuple[str, ...]
    memory_bytes: int
    tmpfs_bytes: int


DockerCall = Callable[..., str]


def docker(endpoint: DockerEndpoint, *args: str, timeout: int = 180) -> str:
    try:
        result = subprocess.run(  # noqa: S603 - validated executable and argument vector
            (str(endpoint.executable), "--host", f"unix://{endpoint.socket}", *args),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AdmissionError("docker_unavailable") from exc
    if result.returncode:
        raise AdmissionError(f"docker_{args[0]}_failed")
    return result.stdout.strip()


def inspect(endpoint: DockerEndpoint, kind: str, name: str, *, call: DockerCall = docker) -> dict[str, Any]:
    try:
        value = json.loads(call(endpoint, "inspect", "--type", kind, name))
    except ValueError as exc:
        raise AdmissionError("invalid_docker_inspection") from exc
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise AdmissionError("invalid_docker_inspection")
    return value[0]


def _boundary_valid(state: dict[str, Any], image_id: str, spec: AdmissionSpec) -> bool:
    host = state["HostConfig"]
    config = state["Config"]
    tmpfs = {
        spec.home: f"rw,nosuid,nodev,size={spec.tmpfs_bytes}",
        "/tmp": f"rw,nosuid,nodev,size={spec.tmpfs_bytes}",  # noqa: S108
    }
    return (
        host.get("NetworkMode") == "none"
        and host.get("ReadonlyRootfs") is True
        and host.get("Binds") in (None, [])
        and state.get("Mounts") == []
        and host.get("CapDrop") == ["ALL"]
        and host.get("SecurityOpt") == ["no-new-privileges:true"]
        and host.get("PidsLimit") == 32
        and host.get("Memory") == spec.memory_bytes
        and host.get("MemorySwap") == spec.memory_bytes
        and host.get("NanoCpus") == 1_000_000_000
        and config.get("User") == "1000:1000"
        and host.get("Tmpfs") == tmpfs
        and state.get("Image") == image_id
    )


def offline_container(
    endpoint: DockerEndpoint,
    image_id: str,
    spec: AdmissionSpec,
    *,
    entrypoint: str | None,
    args: tuple[str, ...],
    call: DockerCall = docker,
) -> str:
    name = f"onefoundry-{spec.name}-admission-{uuid4().hex[:12]}"
    tmpfs = f"rw,nosuid,nodev,size={spec.tmpfs_bytes}"
    command = [
        "create",
        "--name",
        name,
        "--pull=never",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--pids-limit=32",
        f"--memory={spec.memory_bytes}",
        f"--memory-swap={spec.memory_bytes}",
        "--cpus=1",
        "--user=1000:1000",
        "--tmpfs",
        f"{spec.home}:{tmpfs}",
        "--tmpfs",
        f"/tmp:{tmpfs}",  # noqa: S108
        "--env",
        f"HOME={spec.home}",
    ]
    for value in spec.env:
        command.extend(("--env", value))
    if entrypoint:
        command.extend(("--entrypoint", entrypoint))
    command.extend((image_id, *args))
    try:
        call(endpoint, *command)
        state = inspect(endpoint, "container", name, call=call)
        if not _boundary_valid(state, image_id, spec):
            raise AdmissionError("container_boundary_invalid")
        output = call(endpoint, "start", "--attach", name, timeout=30)
        final = inspect(endpoint, "container", name, call=call)
        if final.get("State", {}).get("ExitCode") != 0:
            raise AdmissionError("container_command_failed")
        return output
    finally:
        active_error = sys.exception()
        try:
            call(endpoint, "rm", "--force", name)
        except AdmissionError as exc:
            try:
                remaining = call(endpoint, "ps", "--all", "--quiet", "--filter", f"name=^/{name}$")
                cleanup_error = AdmissionError("container_cleanup_failed") if remaining else None
            except AdmissionError:
                cleanup_error = AdmissionError("container_cleanup_unverified")
            if cleanup_error is not None:
                if active_error is not None:
                    active_error.add_note(str(cleanup_error))
                else:
                    raise cleanup_error from exc


def remove_tag_if_present(endpoint: DockerEndpoint, tag: str, *, call: DockerCall = docker) -> None:
    try:
        call(endpoint, "image", "rm", "--no-prune", tag)
    except AdmissionError as exc:
        remaining = call(endpoint, "images", "--quiet", "--filter", f"reference={tag}")
        if remaining:
            raise AdmissionError("image_tag_cleanup_failed") from exc


def _home_probe(spec: AdmissionSpec) -> str:
    return (
        "const fs=require('fs');"
        "const paths=" + json.dumps(spec.absent_paths) + ";"
        "const env=" + json.dumps(spec.env) + ";"
        "if(process.env.HOME!==" + json.dumps(spec.home) + ")process.exit(2);"
        "if(env.some(e=>process.env[e.split('=')[0]]!==e.split('=').slice(1).join('=')))process.exit(2);"
        "if(!fs.existsSync(process.env.HOME)||fs.readdirSync(process.env.HOME).length)process.exit(3);"
        "if(process.getuid()!==1000)process.exit(5);"
        "fs.mkdirSync(" + json.dumps(spec.private_dir) + ",{recursive:true});"
        "fs.writeFileSync(" + json.dumps(spec.private_dir + "/write-probe") + ",'ok');"
        "if(fs.readFileSync("
        + json.dumps(spec.private_dir + "/write-probe")
        + ",'utf8')!=='ok')process.exit(6);"
        "if(paths.some(p=>fs.existsSync(p)))process.exit(4);"
        "process.stdout.write('isolated-home-and-paths-ok');"
    )


def build_and_prove(
    endpoint: DockerEndpoint, spec: AdmissionSpec, *, call: DockerCall = docker
) -> dict[str, str]:
    endpoint.validate()
    tag = f"onefoundry-{spec.name}-admission:{uuid4().hex}"
    admitted = False
    try:
        call(
            endpoint,
            "build",
            "--platform=linux/amd64",
            f"--tag={tag}",
            "--file",
            str(spec.image_dir / "Dockerfile"),
            str(spec.image_dir),
            timeout=300,
        )
        image = inspect(endpoint, "image", tag, call=call)
        image_id = image.get("Id")
        if not isinstance(image_id, str) or not image_id.startswith("sha256:"):
            raise AdmissionError("image_digest_missing")
        if image.get("Config", {}).get("User") != "node":
            raise AdmissionError("image_user_invalid")
        version = offline_container(endpoint, image_id, spec, entrypoint=None, args=("--version",), call=call)
        if version != spec.expected_version:
            raise AdmissionError(f"{spec.name}_version_mismatch")
        help_text = offline_container(endpoint, image_id, spec, entrypoint=None, args=("--help",), call=call)
        if any(term not in help_text for term in spec.help_terms):
            raise AdmissionError(f"{spec.name}_help_invalid")
        paths = offline_container(
            endpoint,
            image_id,
            spec,
            entrypoint="/usr/local/bin/node",
            args=("-e", _home_probe(spec)),
            call=call,
        )
        if paths != "isolated-home-and-paths-ok":
            raise AdmissionError("path_probe_invalid")
        admitted = True
        return {"image_id": image_id, "image_tag": tag, "version": version, "paths": paths}
    finally:
        if not admitted:
            active_error = sys.exception()
            try:
                remove_tag_if_present(endpoint, tag, call=call)
            except AdmissionError as exc:
                if active_error is not None:
                    active_error.add_note(str(exc))
                else:
                    raise
