"""Content-free diagnostics for a rejected legacy agent command."""

from typing import Any

from pydantic import ValidationError

from onefoundry.workflow_schemas import StrictModel


class DecisionValidationIssue(StrictModel):
    field: str
    code: str


def validation_errors(raw: str) -> list[dict[str, str]]:
    # Import at the call boundary: the command owner also uses this diagnostic reader.
    from onefoundry.task_automation import AgentDecision

    try:
        AgentDecision.model_validate_json(raw)
    except ValidationError as error:
        schema = AgentDecision.model_json_schema()
        models: list[dict[str, Any]] = [schema, *schema.get("$defs", {}).values()]
        fields = {key for model in models for key in model.get("properties", {})}
        return [
            {
                "field": ".".join(
                    str(part) if isinstance(part, int) or part in fields else "unsupported_field"
                    for part in issue["loc"]
                )
                or "proposal",
                "code": issue["type"],
            }
            for issue in error.errors(include_input=False, include_context=False, include_url=False)
        ]
    return []


def rejected_action_reply() -> str:
    return (
        "The agent's proposed action was rejected. No team assignment, rule, or project change "
        "was saved by this reply. Use Start work for team execution; review Reply details for "
        "the proposal's validation issues."
    )
