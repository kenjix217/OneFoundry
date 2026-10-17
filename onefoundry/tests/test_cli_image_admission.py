"""Shared boundary regressions for both pinned coding CLI admissions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from runner import claude_cli_admission, cli_image_admission, codex_cli_admission
from runner.cli_image_admission import AdmissionError, AdmissionSpec, DockerEndpoint


@pytest.mark.parametrize("spec", [codex_cli_admission.SPEC, claude_cli_admission.SPEC])
def test_swap_boundary_mismatch_fails_before_start_and_cleans_container(spec: AdmissionSpec) -> None:
    calls: list[tuple[str, ...]] = []
    state = {
        "HostConfig": {
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "Binds": None,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "PidsLimit": 32,
            "Memory": spec.memory_bytes,
            "MemorySwap": -1,
            "NanoCpus": 1_000_000_000,
            "Tmpfs": {
                spec.home: f"rw,nosuid,nodev,size={spec.tmpfs_bytes}",
                "/tmp": f"rw,nosuid,nodev,size={spec.tmpfs_bytes}",  # noqa: S108
            },
        },
        "Config": {"User": "1000:1000"},
        "Mounts": [],
        "Image": "sha256:fixture",
    }

    def fake_docker(endpoint: DockerEndpoint, *args: str, timeout: int = 180) -> str:
        calls.append(args)
        if args[0] == "inspect":
            return json.dumps([state])
        return ""

    with pytest.raises(AdmissionError, match="container_boundary_invalid"):
        cli_image_admission.offline_container(
            DockerEndpoint(Path("/docker"), Path("/socket")),
            "sha256:fixture",
            spec,
            entrypoint=None,
            args=("--version",),
            call=fake_docker,
        )
    create = calls[0]
    assert f"--memory-swap={spec.memory_bytes}" in create
    assert [call[0] for call in calls] == ["create", "inspect", "rm"]
    assert calls[-1] == ("rm", "--force", create[create.index("--name") + 1])
