"""Long operator messages keep their content and explicit bounds."""

from uuid import uuid4

import pytest

from onefoundry.chief_workspace_schemas import ChiefMessage
from onefoundry.operator_message_contract import MAX_OPERATOR_MESSAGE_CHARS, initial_work_order_instructions
from onefoundry.task_session_schemas import SessionMessageCreate
from onefoundry.task_session_start import TeamStart
from onefoundry.workflow_schemas import StrictModel


@pytest.mark.parametrize("model", [ChiefMessage, SessionMessageCreate, TeamStart])
def test_operator_message_accepts_more_than_old_composer_limit(model: type[StrictModel]) -> None:
    body = "Long direction: " + "verify every line. " * 1100 + "done"
    values = {"body": body, "client_message_id": uuid4()}
    if model is SessionMessageCreate:
        values["expected_sequence"] = 0
    if model is TeamStart:
        values["expected_version"] = 1
    assert model.model_validate(values).model_dump()["body"] == body


def test_long_initial_brief_points_to_full_saved_message() -> None:
    long_body = "Read these details: " + "evidence " * 2000
    assert initial_work_order_instructions(long_body) != long_body
    assert "first saved message" in initial_work_order_instructions(long_body)
    assert initial_work_order_instructions("Short direction") == "Short direction"


def test_message_hard_bound_remains_explicit() -> None:
    with pytest.raises(ValueError):
        ChiefMessage.model_validate(
            {"client_message_id": uuid4(), "body": "x" * (MAX_OPERATOR_MESSAGE_CHARS + 1)}
        )
