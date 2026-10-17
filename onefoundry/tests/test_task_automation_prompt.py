"""A malformed action proposal must remain an honest, readable chat reply."""

import json

from onefoundry.task_automation import DECISION_INSTRUCTIONS, invalid_reply, parse_response


def test_invalid_team_proposal_keeps_reply_without_claiming_saved_actions() -> None:
    raw = json.dumps(
        {
            "reply": "I propose Researcher and QA for the next steps.",
            "decision": "delegate_team",
            "team": [
                {
                    "work_order": {
                        "title": "Review the product",
                        "deliverable": "A source-backed feature list",
                        "acceptance_checks": ["Evidence recorded"],
                    },
                    "autonomy": "manual",
                }
            ],
        }
    )
    assert parse_response(raw) is None
    assert invalid_reply(raw) == "I propose Researcher and QA for the next steps."
    assert "profile_id" in DECISION_INSTRUCTIONS
    assert "instructions" in DECISION_INSTRUCTIONS
    assert '"draft"' in DECISION_INSTRUCTIONS


def test_unstructured_reply_still_reads_as_chat() -> None:
    assert invalid_reply("I need the approved demo environment first.") == (
        "I need the approved demo environment first."
    )


def test_invalid_action_diagnostics_never_include_input_values_or_unknown_keys() -> None:
    import json

    from onefoundry.task_decision_validation import validation_errors

    errors = validation_errors(
        json.dumps(
            {
                "reply": "safe",
                "decision": "delegate_team",
                "team": [
                    {
                        "work_order": {"title": "secret-value", "token=do-not-store": "protected"},
                        "password=do-not-store": "protected",
                    }
                ],
            }
        )
    )
    assert errors
    saved = json.dumps(errors)
    assert "secret-value" not in saved and "do-not-store" not in saved and "protected" not in saved
    assert "unsupported_field" in saved and "instructions" in saved
    assert validation_errors('{"reply":"safe","decision":"none"}') == []
