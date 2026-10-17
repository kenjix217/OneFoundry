"""Offline text fixture that uses the OpenAI Agents SDK loop and handoff.

No provider client, hosted tool, web search, or outbound operation is wired here.
The SDK's deterministic ScriptedModel supplies both specialist turns.
"""

from dataclasses import dataclass
from typing import Any

from agents import Agent, ItemHelpers, RunConfig, Runner, set_tracing_disabled
from agents.items import HandoffCallItem, HandoffOutputItem, MessageOutputItem
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call

from onefoundry.workflow_schemas import local_text

set_tracing_disabled(True)


@dataclass(frozen=True)
class ScriptedTextResult:
    researcher_text: str
    gtm_text: str
    researcher_calls: int
    gtm_calls: int


def run_research_to_gtm(
    *,
    title: str,
    work_instructions: str,
    researcher: dict[str, Any],
    gtm: dict[str, Any],
    source_memories: list[dict[str, Any]],
    researcher_model: ScriptedModel | None = None,
    gtm_model: ScriptedModel | None = None,
) -> ScriptedTextResult:
    """Exercise one SDK Researcher→GTM handoff with fully local model steps."""
    safe_title = local_text(title)
    safe_work = local_text(work_instructions)
    research_instructions = local_text(str(researcher["instructions"]), allow_empty=True)
    gtm_instructions = local_text(str(gtm["instructions"]), allow_empty=True)
    citation_text = ", ".join(str(item["id"]) for item in source_memories) or "none"
    memory_context = (
        "\n".join(f"- {item['id']}: {local_text(str(item['text']))}" for item in source_memories) or "none"
    )
    research_fixture = (
        f"Synthetic Researcher fixture for {safe_title}. "
        f"Approved memory citations: {citation_text}. "
        "No factual claim, web search, or provider inference was generated."
    )
    gtm_fixture = (
        f"Synthetic GTM draft for {safe_title}. Approved memory citations: {citation_text}. "
        "This local placeholder needs operator review before any use."
    )
    scripted_researcher = researcher_model or ScriptedModel(
        [
            ModelStep(
                output=[
                    assistant_message(research_fixture),
                    function_call("transfer_to_gtm", {}, call_id="synthetic-handoff"),
                ]
            )
        ]
    )
    scripted_gtm = gtm_model or ScriptedModel([ModelStep(output=[assistant_message(gtm_fixture)])])
    gtm_agent = Agent(
        name="GTM",
        instructions=f"{gtm_instructions}\nLocal synthetic text fixture. No external tools.",
        model=scripted_gtm,
        tools=[],
    )
    researcher_agent = Agent(
        name="Researcher",
        instructions=f"{research_instructions}\nLocal synthetic text fixture. No external tools.",
        model=scripted_researcher,
        tools=[],
        handoffs=[gtm_agent],
    )
    result = Runner.run_sync(
        researcher_agent,
        f"Work order: {safe_title}\nInstructions: {safe_work}\n"
        f"Approved project-visible memory:\n{memory_context}",
        run_config=RunConfig(tracing_disabled=True, trace_include_sensitive_data=False),
        max_turns=3,
    )
    messages = [
        (item.agent.name, ItemHelpers.text_message_output(item))
        for item in result.new_items
        if isinstance(item, MessageOutputItem)
    ]
    handoff_calls = sum(isinstance(item, HandoffCallItem) for item in result.new_items)
    handoff_outputs = sum(isinstance(item, HandoffOutputItem) for item in result.new_items)
    if (
        [name for name, _ in messages] != ["Researcher", "GTM"]
        or handoff_calls != 1
        or handoff_outputs != 1
        or result.last_agent.name != "GTM"
        or scripted_researcher.remaining_steps != 0
        or scripted_gtm.remaining_steps != 0
    ):
        raise RuntimeError("scripted SDK handoff incomplete")
    researcher_text = local_text(messages[0][1])
    gtm_text = local_text(messages[1][1])
    if len(researcher_text.encode("utf-8")) > 16000 or len(gtm_text.encode("utf-8")) > 16000:
        raise RuntimeError("scripted SDK output exceeds bound")
    return ScriptedTextResult(
        researcher_text, gtm_text, len(scripted_researcher.calls), len(scripted_gtm.calls)
    )
