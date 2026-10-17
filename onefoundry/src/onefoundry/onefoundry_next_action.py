"""Deterministic action projection for the active business mode."""

from typing import Literal

from pydantic import Field

from onefoundry.workflow_schemas import StrictModel

ActionKey = Literal[
    "add_source",
    "research",
    "start_research",
    "review_claims",
    "qualify",
    "draft",
    "start_draft",
    "review_draft",
    "reconcile",
    "review_block",
    "review_source",
    "pending_draft",
    "none",
]


class OneFoundryActionSnapshot(StrictModel):
    mode: Literal["off", "records", "manual", "event", "inbox"]
    workflow_status: Literal[
        "new",
        "review_needed",
        "research_reviewed",
        "research_requested",
        "draft_requested",
        "draft_ready",
        "blocked",
        "unknown",
    ]
    workflow_revision: int = Field(ge=1)
    qualification: Literal["unreviewed", "qualified", "declined"]
    source_revision: int | None = None
    has_source: bool
    has_candidate: bool
    has_approved_fresh_claim: bool
    has_stale_evidence: bool
    linked_order_status: str | None = None
    intent_state: str | None = None


class OneFoundryNextAction(StrictModel):
    action_key: ActionKey
    label: str
    reason: str
    workflow_revision: int
    source_revision: int | None


def next_action(snapshot: OneFoundryActionSnapshot) -> OneFoundryNextAction:
    key: ActionKey = "none"
    label, reason = "No action available", "This record has no available action in the current mode."
    if snapshot.mode in {"records", "manual", "event", "inbox"}:
        if snapshot.workflow_status == "unknown" or snapshot.linked_order_status == "unknown":
            key, label, reason = (
                "reconcile",
                "Reconcile",
                "The prior outcome is unknown. Review it before continuing.",
            )
        elif snapshot.linked_order_status == "failed":
            key, label, reason = (
                "review_block",
                "Review failed run",
                "Inspect the saved run failure before continuing.",
            )
        elif snapshot.workflow_status == "blocked":
            key, label, reason = "review_block", "Review block", "Resolve the saved block before continuing."
        elif snapshot.mode in {"event", "inbox"} and snapshot.intent_state in {"blocked", "unknown"}:
            key, label, reason = (
                "review_block",
                "Review continuation",
                "Inspect the saved intent before continuing.",
            )
        elif snapshot.qualification == "declined":
            reason = "This district was declined."
        elif snapshot.has_stale_evidence:
            key, label, reason = "review_source", "Review source", "Evidence changed or was superseded."
        elif snapshot.has_candidate:
            key, label, reason = (
                "review_claims",
                "Review claims",
                "A proposed claim needs an operator decision.",
            )
        elif snapshot.has_approved_fresh_claim and snapshot.qualification == "unreviewed":
            key, label, reason = "qualify", "Qualify or decline", "Reviewed evidence is ready for a decision."
        elif (
            snapshot.mode in {"manual", "event", "inbox"} and snapshot.workflow_status == "research_requested"
        ):
            if snapshot.linked_order_status == "ready":
                key, label, reason = "start_research", "Start research", "The linked order is ready."
            else:
                reason = "Waiting for saved research run output."
        elif snapshot.mode in {"manual", "event", "inbox"} and snapshot.workflow_status == "draft_requested":
            if snapshot.linked_order_status == "ready":
                key, label, reason = "start_draft", "Start draft run", "The linked order is ready."
            else:
                reason = "Waiting for saved draft run output."
        elif snapshot.mode in {"manual", "event", "inbox"} and snapshot.workflow_status == "draft_ready":
            key, label, reason = "review_draft", "Review draft", "Review the cited saved draft."
        elif snapshot.mode in {"event", "inbox"} and snapshot.intent_state in {"pending", "leased"}:
            key, label, reason = (
                "pending_draft",
                "Draft order pending",
                "The coordinator has not confirmed a ready draft order yet.",
            )
        elif (
            snapshot.mode in {"manual", "event", "inbox"}
            and snapshot.has_approved_fresh_claim
            and snapshot.qualification == "qualified"
        ):
            key, label, reason = "draft", "Draft outreach", "Reviewed current evidence supports a draft."
        elif not snapshot.has_source:
            key, label, reason = (
                "add_source",
                "Add source",
                "Add an observed source before proposing a claim.",
            )
        elif not snapshot.has_approved_fresh_claim:
            if snapshot.mode in {"manual", "event", "inbox"}:
                key, label, reason = "research", "Research district", "Start a linked research order."
            else:
                key, label, reason = (
                    "review_source",
                    "Review source",
                    "Propose a claim from the observed source.",
                )
        else:
            reason = "This district is qualified. Research and drafts are not available in records mode."
    return OneFoundryNextAction(
        action_key=key,
        label=label,
        reason=reason,
        workflow_revision=snapshot.workflow_revision,
        source_revision=snapshot.source_revision,
    )
