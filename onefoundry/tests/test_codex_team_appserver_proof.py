"""Real Codex control-path admission with an offline provider fixture."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from runner import codex_cli_admission, codex_team_appserver_proof
from runner import docker_egress_proof as egress
from runner.codex_cli_admission import DockerEndpoint


def test_rejects_unpinned_image_before_docker() -> None:
    with pytest.raises(egress.ProofError, match="team_appserver_image_unpinned"):
        codex_team_appserver_proof.prove_team_appserver_turn("latest", "latest")


@pytest.mark.integration
@pytest.mark.parametrize("request_path", ["/v1/responses", "/backend-api/codex/responses"])
def test_actual_codex_appserver_resume_and_gateway_callback(request_path: str) -> None:
    executable = shutil.which("docker")
    assert executable is not None
    fixture_tag = os.environ["ONEFOUNDRY_EGRESS_TEST_IMAGE"]
    fixture_image = egress._inspect("image", fixture_tag)["Id"]
    assert isinstance(fixture_image, str)
    admitted = codex_cli_admission.build_and_prove(
        DockerEndpoint(Path(executable).resolve(), Path("/var/run/docker.sock"))
    )
    result = codex_team_appserver_proof.prove_team_appserver_turn(
        admitted["image_id"], fixture_image, request_path=request_path
    )
    assert result == {
        "thread_started": True,
        "turn_completed": True,
        "dynamic_calls": 2,
        "provider_requests": 4,
        "resumed_thread": True,
        "network_isolated": True,
        "provider_tool_names": ["onefoundry"],
        "provider_tool_types": ["namespace"],
        "broker_credential_absent_from_child": True,
        "native_prompt_removed": True,
    }
