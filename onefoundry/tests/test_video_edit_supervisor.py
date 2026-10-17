"""Renderer admission rejects weakened process, filesystem and network boundaries."""

import subprocess
from copy import deepcopy
from threading import Event
from typing import Any

import pytest

from onefoundry.settings import Settings
from runner.video_edit_supervisor import VideoWorkerFailure, check_boundary, docker_argv


def boundary(settings: Settings) -> dict[str, Any]:
    return {
        "Image": settings.video_edit_image,
        "Mounts": [],
        "Config": {"User": "10001:10001", "Env": ["PATH=/usr/bin:/bin", "GPG_KEY="]},
        "HostConfig": {
            "Binds": [],
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "Privileged": False,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "PidsLimit": settings.video_edit_pids,
            "Memory": settings.video_edit_memory_mb * 1024 * 1024,
            "NanoCpus": settings.video_edit_cpus * 1_000_000_000,
            "PortBindings": {},
            "Tmpfs": {"/tmp": "rw,nosuid,nodev,size=512m"},  # noqa: S108 - container-private tmpfs
            "LogConfig": {"Type": "none"},
        },
    }  # noqa: S108


def test_exact_boundary() -> None:
    settings = Settings(db_password="test-only", video_edit_image="sha256:" + "a" * 64)
    check_boundary(boundary(settings), settings)


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        (None, "Image", "mutable:latest"),
        (None, "Mounts", [{"Source": "/protected"}]),
        ("HostConfig", "NetworkMode", "bridge"),
        ("HostConfig", "ReadonlyRootfs", False),
        ("HostConfig", "Privileged", True),
        ("HostConfig", "CapDrop", []),
        ("HostConfig", "Memory", 0),
        ("HostConfig", "PidsLimit", 0),
        ("HostConfig", "NanoCpus", 0),
        ("HostConfig", "Binds", ["/host:/data"]),
        ("HostConfig", "Tmpfs", {"/tmp": "rw,size=64g"}),  # noqa: S108
        ("HostConfig", "LogConfig", {"Type": "json-file"}),
        ("Config", "User", "0:0"),
        ("Config", "Env", ["API_KEY=not-a-real-credential"]),
    ],
)
def test_boundary_rejects_change(section: str | None, field: str, value: object) -> None:
    settings = Settings(db_password="test-only", video_edit_image="sha256:" + "a" * 64)
    info = deepcopy(boundary(settings))
    (info[section] if section else info)[field] = value
    with pytest.raises(VideoWorkerFailure):
        check_boundary(info, settings)


def test_local_docker_paths_are_configuration() -> None:
    settings = Settings(
        db_password="test-only",
        video_edit_docker_binary="/custom/docker",
        video_edit_docker_socket="/custom.sock",
    )
    assert docker_argv(settings, "inspect", "fixture") == [
        "/custom/docker",
        "--host",
        "unix:///custom.sock",
        "inspect",
        "fixture",
    ]


def test_docker_dependency_failure_preserves_safe_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    from runner import video_edit_supervisor as supervisor

    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("untrusted path and dependency detail")

    monkeypatch.setattr(subprocess, "run", unavailable)
    with pytest.raises(VideoWorkerFailure, match="video_docker_unavailable_or_timed_out"):
        supervisor._inspect(Settings(db_password="test-only"), "image", "fixture")


def test_docker_cleanup_requires_exact_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    from runner import video_edit_supervisor as supervisor

    monkeypatch.setattr(supervisor, "_docker", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(
        supervisor, "_docker_result", lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "[]", "")
    )
    with pytest.raises(VideoWorkerFailure, match="video_docker_cleanup_unverified"):
        supervisor._remove(Settings(db_password="test-only"), "container", "fixture")


@pytest.mark.parametrize("fault", ["cancel", "timeout"])
def test_render_removes_child_and_stops_transport_on_cancel_or_timeout(
    monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    from runner import video_edit_supervisor as supervisor

    settings = Settings(db_password="test-only", video_edit_image="sha256:" + "a" * 64)
    started, released = Event(), Event()
    calls: list[str] = []

    class Process:
        returncode = 0
        communicates = 0

        def __enter__(self) -> "Process":
            return self

        def __exit__(self, *_args: object) -> None:
            calls.append("exited")

        def communicate(self, *_args: object, **_kwargs: object) -> tuple[bytes, bytes]:
            self.communicates += 1
            started.set()
            if fault == "timeout" and self.communicates == 1:
                raise subprocess.TimeoutExpired("fixture", 1)
            assert released.wait(2), "test must release the bounded child transport"
            return b"", b""

        def terminate(self) -> None:
            calls.append("terminated")

        def kill(self) -> None:
            calls.append("killed")

    def inspect(_settings: Any, kind: str, _name: str) -> dict[str, Any]:
        if kind == "image":
            return {
                "Id": settings.video_edit_image,
                "Config": {"Labels": {"onefoundry.video_edit.contract": "1"}},
            }
        return boundary(settings)

    def remove(*_args: object) -> None:
        calls.append("removed")
        released.set()

    monkeypatch.setattr(supervisor, "_inspect", inspect)
    monkeypatch.setattr(supervisor, "_docker", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(supervisor, "_remove", remove)
    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: Process())
    expected = supervisor.VideoWorkerCanceled if fault == "cancel" else supervisor.VideoWorkerFailure
    with pytest.raises(
        expected, match="video_edit_canceled" if fault == "cancel" else "video_edit_timed_out"
    ):
        supervisor.run_render({"assets": []}, [], settings, lambda: fault == "cancel" and started.is_set())
    assert calls.count("removed") == 1
    assert ("terminated" if fault == "cancel" else "killed") in calls
    assert calls[-1] == "exited"
