"""Offline admission of a pinned Claude Code image; never starts a coding turn."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from runner import cli_image_admission as shared
from runner.cli_image_admission import AdmissionError as AdmissionError
from runner.cli_image_admission import DockerEndpoint as DockerEndpoint

IMAGE_DIR = Path(__file__).with_name("claude_cli_image")
EXPECTED_VERSION = "2.1.274"
SPEC = shared.AdmissionSpec(
    name="claude",
    image_dir=IMAGE_DIR,
    expected_version=f"{EXPECTED_VERSION} (Claude Code)",
    help_terms=("Claude Code", "--print"),
    home="/home/claude",
    private_dir="/home/claude/.claude",
    absent_paths=(
        "/root/.claude",
        "/home/node/.claude",
        "/root/.claude.json",
        "/home/node/.claude.json",
        "/runner-spool",
        "/var/run/docker.sock",
        "/run/secrets",
        "/app",
    ),
    env=("DISABLE_UPDATES=1",),
    memory_bytes=4 * 1024 * 1024 * 1024,
    tmpfs_bytes=32 * 1024 * 1024,
)


def _docker(endpoint: DockerEndpoint, *args: str, timeout: int = 180) -> str:
    return shared.docker(endpoint, *args, timeout=timeout)


def _inspect(endpoint: DockerEndpoint, kind: str, name: str) -> dict[str, Any]:
    return shared.inspect(endpoint, kind, name, call=_docker)


def _offline_container(
    endpoint: DockerEndpoint, image_id: str, *, entrypoint: str | None, args: tuple[str, ...]
) -> str:
    return shared.offline_container(endpoint, image_id, SPEC, entrypoint=entrypoint, args=args, call=_docker)


def _remove_tag_if_present(endpoint: DockerEndpoint, tag: str) -> None:
    shared.remove_tag_if_present(endpoint, tag, call=_docker)


def build_and_prove(endpoint: DockerEndpoint) -> dict[str, str]:
    return shared.build_and_prove(endpoint, SPEC, call=_docker)
