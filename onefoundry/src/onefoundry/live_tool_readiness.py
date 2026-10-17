"""Decide whether a live text run can actually execute its requested role tools."""

from onefoundry.role_tool_admission import LIVE_SDK_EXCLUDED_IDS, MEDIA_TOOL_IDS, READ_TOOLS, WRITE_TOOL_IDS
from onefoundry.role_web_tools import WEB_TOOLS
from onefoundry.settings import Settings


def live_tool_readiness(
    allowed_tools: list[str],
    *,
    provider: str,
    api_key_present: bool,
    role_name: str,
    role_kind: str,
    has_visuals: bool,
    settings: Settings,
) -> tuple[bool, str | None]:
    """Return active state and a preflight failure before any run is recorded."""
    if not allowed_tools:
        return False, None
    requested = set(allowed_tools)
    if "request_video_capture_v1" in requested and not settings.media_capture_enabled:
        return False, "operator-selected local video recording is disabled in this deployment"
    if requested & MEDIA_TOOL_IDS and not settings.video_edit_enabled:
        return False, "local video editing is disabled in this deployment"
    registered = set(READ_TOOLS) | WRITE_TOOL_IDS | MEDIA_TOOL_IDS | set(WEB_TOOLS) | LIVE_SDK_EXCLUDED_IDS
    if requested - registered:
        return False, "profile requests unregistered Tool Gateway IDs; edit its allowed tools"
    admitted = (set(READ_TOOLS) | WRITE_TOOL_IDS | MEDIA_TOOL_IDS) - LIVE_SDK_EXCLUDED_IDS
    if settings.public_web_enabled and role_kind == "builtin" and role_name == "Researcher":
        admitted.update(
            tool_id
            for tool_id in WEB_TOOLS
            if tool_id != "public_web_search_v1" or settings.public_web_search_endpoint
        )
    if not requested & admitted:
        return False, "profile requests tools unavailable to this live text role"
    if requested - admitted:
        return False, "profile requests tools unavailable to this live text role"
    if provider != "openai" or not api_key_present:
        return (
            False,
            "this model connection cannot execute requested OneFoundry role tools; no provider call was made",
        )
    if has_visuals:
        return False, "live text cannot use role tools with visual inputs"
    return True, None
