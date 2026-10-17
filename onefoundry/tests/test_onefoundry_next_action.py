"""Records mode never advertises a future provider or draft action."""

import pytest

from onefoundry.onefoundry_next_action import OneFoundryActionSnapshot, next_action


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({}, "add_source"),
        ({"has_source": True, "has_candidate": True}, "review_claims"),
        ({"has_source": True, "has_approved_fresh_claim": True}, "qualify"),
        ({"has_source": True, "has_stale_evidence": True}, "review_source"),
        ({"qualification": "declined"}, "none"),
        ({"workflow_status": "blocked"}, "review_block"),
        ({"workflow_status": "unknown"}, "reconcile"),
        ({"mode": "off"}, "none"),
    ],
)
def test_next_action(changes: dict[str, object], expected: str) -> None:
    values: dict[str, object] = {
        "mode": "records",
        "workflow_status": "new",
        "workflow_revision": 1,
        "qualification": "unreviewed",
        "has_source": False,
        "has_candidate": False,
        "has_approved_fresh_claim": False,
        "has_stale_evidence": False,
    }
    values.update(changes)
    action = next_action(OneFoundryActionSnapshot.model_validate(values))
    assert action.action_key == expected
    assert action.source_revision is None


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"has_source": True}, "research"),
        (
            {"workflow_status": "research_requested", "has_source": True, "linked_order_status": "ready"},
            "start_research",
        ),
        (
            {"workflow_status": "research_requested", "has_source": True, "linked_order_status": "running"},
            "none",
        ),
        (
            {"workflow_status": "research_reviewed", "has_source": True, "has_approved_fresh_claim": True},
            "qualify",
        ),
        (
            {
                "workflow_status": "research_reviewed",
                "qualification": "qualified",
                "has_source": True,
                "has_approved_fresh_claim": True,
            },
            "draft",
        ),
        (
            {
                "workflow_status": "draft_requested",
                "qualification": "qualified",
                "has_source": True,
                "has_approved_fresh_claim": True,
                "linked_order_status": "ready",
            },
            "start_draft",
        ),
        (
            {
                "workflow_status": "draft_ready",
                "qualification": "qualified",
                "has_source": True,
                "has_approved_fresh_claim": True,
            },
            "review_draft",
        ),
        (
            {"workflow_status": "research_requested", "has_source": True, "linked_order_status": "failed"},
            "review_block",
        ),
        (
            {"workflow_status": "research_requested", "has_source": True, "linked_order_status": "unknown"},
            "reconcile",
        ),
    ],
)
def test_manual_next_action(changes: dict[str, object], expected: str) -> None:
    values: dict[str, object] = {
        "mode": "manual",
        "workflow_status": "new",
        "workflow_revision": 1,
        "qualification": "unreviewed",
        "has_source": False,
        "has_candidate": False,
        "has_approved_fresh_claim": False,
        "has_stale_evidence": False,
    }
    values.update(changes)
    assert next_action(OneFoundryActionSnapshot.model_validate(values)).action_key == expected
