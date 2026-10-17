"""The installed Agents SDK owns the offline specialist loop and handoff."""

from uuid import uuid4

import pytest
from agents.models.interface import ModelTracing
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call

from onefoundry.scripted_text_sdk import run_research_to_gtm


def profile(instructions: str = "Use only local text") -> dict[str, object]:
    return {"instructions": instructions, "model": "configured-but-unused", "allowed_tools": ["web_search"]}


def test_real_sdk_runner_handoff_is_offline_and_bounded() -> None:
    memory_id = uuid4()
    researcher_model = ScriptedModel(
        [
            ModelStep(
                output=[
                    assistant_message("Synthetic Researcher result"),
                    function_call("transfer_to_gtm", {}, call_id="handoff-1"),
                ]
            )
        ]
    )
    gtm_model = ScriptedModel([ModelStep(output=[assistant_message("Synthetic GTM draft")])])
    result = run_research_to_gtm(
        title="Local launch",
        work_instructions="Prepare a draft",
        researcher=profile("Research locally"),
        gtm=profile("Draft locally"),
        source_memories=[{"id": memory_id, "text": "Approved local memory"}],
        researcher_model=researcher_model,
        gtm_model=gtm_model,
    )
    assert result.researcher_text == "Synthetic Researcher result"
    assert result.gtm_text == "Synthetic GTM draft"
    assert result.researcher_calls == result.gtm_calls == 1
    assert researcher_model.remaining_steps == gtm_model.remaining_steps == 0
    first_call = researcher_model.first_call
    second_call = gtm_model.first_call
    assert first_call is not None and second_call is not None
    assert first_call.tools == [] and second_call.tools == []
    assert first_call.tracing == ModelTracing.DISABLED
    assert second_call.tracing == ModelTracing.DISABLED
    assert "Approved local memory" in str(first_call.input)
    assert str(memory_id) in str(first_call.input)
    assert "Research locally" in str(first_call.system_instructions)
    assert "Draft locally" in str(second_call.system_instructions)


def test_sdk_failure_and_unsafe_input_do_not_produce_result() -> None:
    failing_model = ScriptedModel([ModelStep.raise_error(RuntimeError("scripted model failed"))])
    with pytest.raises(RuntimeError, match="scripted model failed"):
        run_research_to_gtm(
            title="Local launch",
            work_instructions="Prepare a draft",
            researcher=profile(),
            gtm=profile(),
            source_memories=[],
            researcher_model=failing_model,
        )
    assert len(failing_model.calls) == 1
    with pytest.raises(ValueError, match="secret"):
        run_research_to_gtm(
            title="Local launch",
            work_instructions="Prepare a draft",
            researcher=profile(),
            gtm=profile(),
            source_memories=[{"id": uuid4(), "text": "api_key=unsafe"}],
        )
    unsafe_output = ScriptedModel(
        [
            ModelStep(
                output=[
                    assistant_message("api_key=unsafe"),
                    function_call("transfer_to_gtm", {}, call_id="handoff-unsafe"),
                ]
            )
        ]
    )
    with pytest.raises(ValueError, match="secret"):
        run_research_to_gtm(
            title="Local launch",
            work_instructions="Prepare a draft",
            researcher=profile(),
            gtm=profile(),
            source_memories=[],
            researcher_model=unsafe_output,
        )
