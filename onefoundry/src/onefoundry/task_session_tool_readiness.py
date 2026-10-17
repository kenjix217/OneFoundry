"""Team-specific activation for bounded repository reads; legacy restrictions remain."""

from onefoundry.live_tool_readiness import live_tool_readiness
from onefoundry.settings import Settings


def team_tool_readiness(
    allowed_tools: list[str], *, role_name: str, role_kind: str, settings: Settings
) -> tuple[bool, str | None]:
    repository_tools = {
        "repository_file_v1",
        "repository_tree_v1",
        "repository_file_range_v1",
        "coding_artifact_text_v1",
    }
    repository_requested = bool(repository_tools.intersection(allowed_tools))
    if repository_requested and not settings.team_repository_read_enabled:
        return False, "Team repository reading is disabled; the operator must approve source access first."
    browser_requested = "browser_static_inspect_v1" in allowed_tools
    if browser_requested and not settings.browser_static_enabled:
        return False, "Static browser inspection is disabled in this deployment."
    if browser_requested and (role_kind != "builtin" or role_name not in {"Researcher", "QA"}):
        return False, "Static browser inspection is unavailable to this role."
    remaining = [
        tool for tool in allowed_tools if tool not in repository_tools | {"browser_static_inspect_v1"}
    ]
    active, reason = live_tool_readiness(
        remaining,
        provider="openai",
        api_key_present=True,
        role_name=role_name,
        role_kind=role_kind,
        has_visuals=False,
        settings=settings,
    )
    return (
        (False, reason) if reason is not None else (active or repository_requested or browser_requested, None)
    )
