"""Fresh observations from the separately configured release worker."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import AwareDatetime, Field

from onefoundry.gateway_errors import Unavailable
from onefoundry.runner_spool import SpoolInvalid
from onefoundry.workflow_schemas import StrictModel

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


class ReleaseWorkerCapability(StrictModel):
    kind: Literal["extension_release_worker_v1"] = "extension_release_worker_v1"
    observed_at: AwareDatetime
    packaging: Literal[True] = True
    activation: Literal[True] = True
    installed_version: str = Field(pattern=r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
    installed_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    installed_image: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


def inspect(gateway: ToolGateway) -> dict[str, Any]:
    try:
        execution = gateway._execution_gateway()
        report = ReleaseWorkerCapability.model_validate_json(
            execution.spool._read_regular(execution.spool.root / "release_capabilities.json", 4096)
        )
        age = (datetime.now(UTC) - report.observed_at).total_seconds()
        if not 0 <= age <= execution.capability_max_age_seconds:
            return {"status": "unavailable", "reason": "release_worker_observation_expired"}
        return {"status": "ready", **report.model_dump(mode="json")}
    except (OSError, ValueError, SpoolInvalid, Unavailable):
        return {"status": "unavailable", "reason": "release_worker_not_connected"}
