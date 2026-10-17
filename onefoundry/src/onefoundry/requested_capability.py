"""Registration and current admission are separate facts for requested tools."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import Field
from sqlalchemy import text

from onefoundry.capability_schemas import CapabilityBlocker, CapabilityCheck, capability_check
from onefoundry.gateway_errors import NotFound
from onefoundry.role_tool_admission import registered_tool_ids
from onefoundry.workflow_schemas import StrictModel

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


class InspectCapability(StrictModel):
    tool_id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    profile_id: UUID | None = None


def inspect(gateway: ToolGateway, project_id: UUID, profile_id: UUID, tool_id: str) -> CapabilityCheck:
    InspectCapability(tool_id=tool_id)
    with gateway.engine.connect() as connection:
        profile = (
            connection.execute(
                text("SELECT allowed_tools,name,kind FROM profiles WHERE project_id=:p AND id=:id"),
                {"p": project_id, "id": profile_id},
            )
            .mappings()
            .one_or_none()
        )
    if profile is None:
        raise NotFound("capability profile not found in this project")
    tools = profile["allowed_tools"]
    if tool_id not in registered_tool_ids():
        terms = set(tool_id.split("_")) - {"v1", "v2", "tool"}
        alternatives = sorted(item for item in registered_tool_ids() if terms.intersection(item.split("_")))
        alternative_note = (
            " Related registered tools to inspect before commissioning new code: "
            + ", ".join(alternatives)
            + ". Registration does not grant access or prove that an alternative satisfies this task."
            if alternatives
            else ""
        )
        return capability_check(
            f"tool:{tool_id}",
            "Requested tool registration",
            "role_tool_admission",
            [
                CapabilityBlocker(
                    kind="missing_implementation",
                    code="requested_tool_not_registered",
                    owner="role_tool_admission",
                    message="This exact tool has no registered implementation." + alternative_note,
                    next_step="Inspect alternatives, then commission an approved bounded implementation. "
                    "New tool permission requires operator configuration.",
                )
            ],
        )
    if tool_id not in tools:
        return capability_check(
            f"tool:{tool_id}",
            "Requested tool permission",
            "role_tool_admission",
            [
                CapabilityBlocker(
                    kind="permission_required",
                    code="requested_tool_not_granted",
                    owner="profiles",
                    message="The tool exists but this profile has no permission to use it.",
                    next_step="Request the exact profile grant; do not build another copy of the tool.",
                )
            ],
        )
    settings = gateway.live_text_settings
    from onefoundry.task_session_tool_readiness import team_tool_readiness

    active, reason = (
        team_tool_readiness(
            [tool_id], role_name=profile["name"], role_kind=profile["kind"], settings=settings
        )
        if settings is not None
        else (False, "Tool configuration is unavailable.")
    )
    if not active:
        disabled = settings is not None and (
            (
                tool_id
                in {
                    "repository_file_v1",
                    "repository_tree_v1",
                    "repository_file_range_v1",
                    "coding_artifact_text_v1",
                }
                and not settings.team_repository_read_enabled
            )
            or (tool_id == "browser_static_inspect_v1" and not settings.browser_static_enabled)
            or (tool_id.startswith("public_web_") and not settings.public_web_enabled)
            or (tool_id == "request_video_capture_v1" and not settings.media_capture_enabled)
            or (
                tool_id in {"edit_video_v1", "video_edit_status_v1", "cancel_video_edit_v1"}
                and not settings.video_edit_enabled
            )
        )
        return capability_check(
            f"tool:{tool_id}",
            "Requested tool admission",
            "task_session_tool_readiness",
            [
                CapabilityBlocker(
                    kind="disabled" if disabled else "dependency_unavailable",
                    code="requested_tool_runtime_unavailable",
                    owner="task_session_tool_readiness",
                    message=reason or "This registered tool is not available to the current runtime.",
                    next_step="Configure the existing owner and role, then verify current tool readiness.",
                )
            ],
        )
    return capability_check(f"tool:{tool_id}", "Requested tool admission", "task_session_tool_readiness", [])
