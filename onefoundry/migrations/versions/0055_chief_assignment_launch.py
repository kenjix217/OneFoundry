"""Allow Chief-assigned work orders to use the existing bounded auto-start launcher."""

import sqlalchemy as sa
from alembic import op

revision = "0055_chief_assignment_launch"
down_revision = "0054_project_plan"
branch_labels = None
depends_on = None

OUTREACH_INTENT = (
    "(action_kind='onefoundry_draft_outreach' AND step_key='draft_outreach' "
    "AND workflow_id IS NOT NULL AND organization_id IS NOT NULL AND transition_revision >= 1)"
)
RULE_INTENT = (
    "(action_kind='rule_work_order' AND step_key='rule_work_order' AND signal_id IS NOT NULL "
    "AND rule_id IS NOT NULL AND rule_version >= 1 AND target_profile_id IS NOT NULL "
    "AND autonomy IN ('draft','ready','auto_start') AND chain_depth BETWEEN 1 AND 30 "
    "AND chain_depth_limit BETWEEN 1 AND 30)"
)
TRIAGE_INTENT = (
    "(action_kind='chief_triage' AND step_key='chief_triage' AND signal_id IS NOT NULL "
    "AND rule_id IS NULL AND chain_depth BETWEEN 1 AND 30 AND chain_depth_limit BETWEEN 1 AND 30)"
)
RULE_LAUNCH = (
    "(action_kind='auto_start' AND step_key='auto_start' AND signal_id IS NOT NULL "
    "AND rule_id IS NOT NULL AND work_order_id IS NOT NULL)"
)
CHIEF_LAUNCH = (
    "(action_kind='auto_start' AND step_key='auto_start' AND signal_id IS NULL "
    "AND rule_id IS NULL AND work_order_id IS NOT NULL)"
)


def upgrade() -> None:
    op.drop_constraint("coordination_intent_action_valid", "coordination_intents", type_="check")
    op.create_check_constraint(
        "coordination_intent_action_valid",
        "coordination_intents",
        f"{OUTREACH_INTENT} OR {RULE_INTENT} OR {TRIAGE_INTENT} OR {RULE_LAUNCH} OR {CHIEF_LAUNCH}",
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM coordination_intents WHERE action_kind='auto_start' "
            "AND signal_id IS NULL AND rule_id IS NULL)"
        )
    ):
        raise RuntimeError("Chief assignment launches exist; downgrade would lose their source type")
    op.drop_constraint("coordination_intent_action_valid", "coordination_intents", type_="check")
    op.create_check_constraint(
        "coordination_intent_action_valid",
        "coordination_intents",
        f"{OUTREACH_INTENT} OR {RULE_INTENT} OR {TRIAGE_INTENT} OR {RULE_LAUNCH}",
    )
