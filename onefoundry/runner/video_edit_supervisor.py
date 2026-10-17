"""Host-owned Docker boundary for a media child with no network or host mounts."""

from __future__ import annotations

import hashlib
import json
import struct
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import Any
from uuid import uuid4

from onefoundry.settings import Settings
from onefoundry.video_edit_contract import canonical


class VideoWorkerFailure(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class VideoWorkerCanceled(VideoWorkerFailure):
    pass


def docker_argv(settings: Settings, *arguments: str) -> list[str]:
    return [
        settings.video_edit_docker_binary,
        "--host",
        "unix://" + settings.video_edit_docker_socket,
        *arguments,
    ]


def _docker_result(settings: Settings, *arguments: str, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603 - typed local Docker paths, fixed argv, no shell
            docker_argv(settings, *arguments),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise VideoWorkerFailure("video_docker_unavailable_or_timed_out") from error


def _docker(settings: Settings, *arguments: str, timeout: int | None = None) -> str:
    result = _docker_result(settings, *arguments, timeout=timeout or settings.video_edit_wall_seconds)
    if result.returncode:
        raise VideoWorkerFailure("video_docker_command_failed")
    if len(result.stdout.encode()) > settings.runner_max_log_bytes:
        raise VideoWorkerFailure("video_docker_reply_exceeds_bound")
    return result.stdout.strip()


def _inspect(settings: Settings, kind: str, name: str) -> dict[str, Any]:
    try:
        value = json.loads(_docker(settings, "inspect", "--type", kind, name))
    except (ValueError, TypeError) as error:
        raise VideoWorkerFailure("video_docker_inspect_invalid") from error
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise VideoWorkerFailure("video_docker_inspect_invalid")
    return value[0]


def _remove(settings: Settings, kind: str, name: str) -> None:
    if kind != "container":
        raise VideoWorkerFailure("video_docker_cleanup_kind_invalid")
    _docker(settings, "rm", "--force", name)
    result = _docker_result(
        settings, "inspect", "--type", kind, name, timeout=settings.video_edit_wall_seconds
    )
    absent = f"Error response from daemon: No such container: {name}"
    if not (result.returncode == 1 and result.stdout.strip() == "[]" and result.stderr.strip() == absent):
        raise VideoWorkerFailure("video_docker_cleanup_unverified")


def policy(settings: Settings) -> dict[str, Any]:
    return {
        "wall_seconds": settings.video_edit_wall_seconds,
        "max_bytes": settings.media_max_bytes,
        "max_width": settings.media_max_width,
        "max_height": settings.media_max_height,
        "max_duration_seconds": settings.media_max_duration_seconds,
        "frame_rate": settings.media_frame_rate,
        "max_scenes": settings.video_edit_max_scenes,
        "max_captions": settings.video_edit_max_captions,
        "max_receipt_bytes": settings.runner_max_log_bytes,
    }


def check_boundary(info: dict[str, Any], settings: Settings) -> None:
    host, config = info["HostConfig"], info["Config"]
    if (
        info["Image"] != settings.video_edit_image
        or info.get("Mounts") != []
        or host.get("Binds") not in (None, [])
        or host["NetworkMode"] != "none"
        or host["ReadonlyRootfs"] is not True
        or host["Privileged"] is not False
        or host.get("CapDrop") != ["ALL"]
        or "no-new-privileges:true" not in host.get("SecurityOpt", [])
        or host["PidsLimit"] != settings.video_edit_pids
        or host["Memory"] != settings.video_edit_memory_mb * 1024 * 1024
        or host["NanoCpus"] != settings.video_edit_cpus * 1_000_000_000
        or host.get("PortBindings") not in (None, {})
        or config["User"] != "10001:10001"
        or set(host.get("Tmpfs", {})) != {"/tmp"}  # noqa: S108 - container-private tmpfs
        or host.get("Tmpfs", {}).get("/tmp") != f"rw,nosuid,nodev,size={settings.video_edit_memory_mb}m"  # noqa: S108
        or host.get("LogConfig", {}).get("Type") != "none"
    ):
        raise VideoWorkerFailure("video_container_boundary_invalid")
    if any(
        item.partition("=")[2]
        and item.split("=", 1)[0].lower().endswith(("password", "token", "secret", "key"))
        for item in config.get("Env", [])
    ):
        raise VideoWorkerFailure("video_container_environment_invalid")


def run_render(
    manifest: dict[str, Any], assets: list[bytes], settings: Settings, canceled: Callable[[], bool]
) -> tuple[bytes, dict[str, Any]]:
    encoded = canonical(manifest)
    if len(encoded) > settings.runner_max_log_bytes or len(assets) != len(manifest["assets"]):
        raise VideoWorkerFailure("video_manifest_exceeds_bound")
    payload = struct.pack(">I", len(encoded)) + encoded + b"".join(assets)
    installed = _inspect(settings, "image", settings.video_edit_image)
    if (
        installed.get("Id") != settings.video_edit_image
        or installed.get("Config", {}).get("Labels", {}).get("onefoundry.video_edit.contract") != "1"
    ):
        raise VideoWorkerFailure("video_renderer_image_unverified")
    name = f"onefoundry-video-edit-{uuid4().hex}"
    created = False
    try:
        _docker(
            settings,
            "create",
            "--interactive",
            "--pull=never",
            "--name",
            name,
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            f"--pids-limit={settings.video_edit_pids}",
            f"--memory={settings.video_edit_memory_mb}m",
            f"--cpus={settings.video_edit_cpus}",
            "--user=10001:10001",
            "--log-driver=none",
            "--tmpfs",
            f"/tmp:rw,nosuid,nodev,size={settings.video_edit_memory_mb}m",  # noqa: S108
            settings.video_edit_image,
            timeout=settings.video_edit_wall_seconds,
        )
        created = True
        info = _inspect(settings, "container", name)
        check_boundary(info, settings)
        if canceled():
            raise VideoWorkerCanceled("video_edit_canceled")
        with subprocess.Popen(  # noqa: S603 - exact Docker image and inspected boundary
            docker_argv(settings, "start", "--attach", "--interactive", name),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"},
        ) as process:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(process.communicate, payload, timeout=settings.video_edit_wall_seconds)
                waiter = Event()
                while not future.done():
                    if canceled():
                        _remove(settings, "container", name)
                        created = False
                        process.terminate()
                        future.result()
                        raise VideoWorkerCanceled("video_edit_canceled")
                    waiter.wait(0.1)
                try:
                    stdout, stderr = future.result()
                except subprocess.TimeoutExpired:
                    _remove(settings, "container", name)
                    created = False
                    process.kill()
                    process.communicate()
                    raise VideoWorkerFailure("video_edit_timed_out") from None
        if process.returncode:
            reason = stderr.decode(errors="replace").strip()
            # Never store dependency stderr, paths, metadata or external bytes.
            allowed = {
                "source_trim_exceeds_duration",
                "audio_trim_exceeds_duration",
                "source_duration_exceeds_bounds",
                "source_dimensions_exceed_bounds",
                "caption_exceeds_reserved_area",
                "scene_duration_must_align_to_frames",
                "audio_tail_silent",
                "audio_tail_encoding_mismatch",
                "export_audio_end_mismatch",
            }
            raise VideoWorkerFailure(reason if reason in allowed else "video_render_dependency_failure")
        if len(stdout) < 4:
            raise VideoWorkerFailure("video_render_receipt_missing")
        size = struct.unpack(">I", stdout[:4])[0]
        if not 1 <= size <= settings.runner_max_log_bytes:
            raise VideoWorkerFailure("video_render_receipt_invalid")
        receipt = json.loads(stdout[4 : 4 + size])
        raw = stdout[4 + size :]
        if (
            not 1 <= len(raw) <= settings.media_max_bytes
            or receipt["byte_size"] != len(raw)
            or receipt["sha256"] != hashlib.sha256(raw).hexdigest()
            or receipt["recipe_sha256"] != manifest["recipe_sha256"]
            or receipt["assets"] != manifest["assets"]
            or receipt["full_decode_verified"] is not True
        ):
            raise VideoWorkerFailure("video_render_receipt_invalid")
        receipt["isolation"] = {
            "image_id": settings.video_edit_image,
            "network": "none",
            "host_mounts": [],
            "credentials_mounted": False,
            "boundary_inspected": True,
        }
        return raw, receipt
    finally:
        if created:
            _remove(settings, "container", name)
