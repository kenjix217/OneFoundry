"""Actual Docker admission; no credential, provider request, or coding turn."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from runner import claude_cli_admission
from runner.claude_cli_admission import AdmissionError, DockerEndpoint, build_and_prove


def local_endpoint() -> DockerEndpoint:
    executable = shutil.which("docker")
    assert executable is not None
    return DockerEndpoint(Path(executable).resolve(), Path("/var/run/docker.sock"))


@pytest.mark.integration
def test_pinned_claude_image_offline_admission() -> None:
    endpoint = local_endpoint()
    proof = build_and_prove(endpoint)
    try:
        assert proof["image_id"].startswith("sha256:")
        assert claude_cli_admission._inspect(endpoint, "image", proof["image_id"])["Id"] == proof["image_id"]
        assert claude_cli_admission._inspect(endpoint, "image", proof["image_tag"])["Id"] == proof["image_id"]
        assert proof["version"] == "2.1.274 (Claude Code)"
        assert proof["paths"] == "isolated-home-and-paths-ok"
    finally:
        claude_cli_admission._remove_tag_if_present(endpoint, proof["image_tag"])


def test_ambiguous_create_still_cleans_named_container(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_docker(endpoint: DockerEndpoint, *args: str, timeout: int = 180) -> str:
        calls.append(args)
        if args[0] == "create":
            raise AdmissionError("docker_unavailable")
        return ""

    monkeypatch.setattr(claude_cli_admission, "_docker", fake_docker)
    with pytest.raises(AdmissionError, match="docker_unavailable"):
        claude_cli_admission._offline_container(
            DockerEndpoint(Path("/docker"), Path("/socket")),
            "sha256:fixture",
            entrypoint=None,
            args=("--version",),
        )
    assert calls[0][0] == "create"
    assert calls[1] == ("rm", "--force", calls[0][calls[0].index("--name") + 1])


def test_cleanup_failure_preserves_original_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_docker(endpoint: DockerEndpoint, *args: str, timeout: int = 180) -> str:
        if args[0] in {"create", "rm"}:
            raise AdmissionError(f"docker_{args[0]}_failed")
        return "container-still-present"

    monkeypatch.setattr(claude_cli_admission, "_docker", fake_docker)
    with pytest.raises(AdmissionError, match="docker_create_failed") as caught:
        claude_cli_admission._offline_container(
            DockerEndpoint(Path("/docker"), Path("/socket")),
            "sha256:fixture",
            entrypoint=None,
            args=("--version",),
        )
    assert "container_cleanup_failed" in caught.value.__notes__


def test_docker_endpoint_rejects_relative_or_missing_paths() -> None:
    with pytest.raises(AdmissionError, match="docker_endpoint_invalid"):
        DockerEndpoint(Path("docker"), Path("/var/run/docker.sock")).validate()
    with pytest.raises(AdmissionError, match="docker_endpoint_invalid"):
        DockerEndpoint(Path("/missing/docker"), Path("/missing/docker.sock")).validate()
