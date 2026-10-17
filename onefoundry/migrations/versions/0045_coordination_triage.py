"""Chief of Staff triage for unmatched findings and external events."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0045_coordination_triage"
down_revision = "0044_coordination_schedules"
branch_labels = None
depends_on = None

RULE_INTENT = (
    "(action_kind='rule_work_order' AND step_key='rule_work_order' AND signal_id IS NOT NULL "
    "AND rule_id IS NOT NULL AND rule_version >= 1 AND target_profile_id IS NOT NULL "
    "AND autonomy IN ('draft','ready') AND chain_depth BETWEEN 1 AND 30 "
    "AND chain_depth_limit BETWEEN 1 AND 30)"
)
OUTREACH_INTENT = (
    "(action_kind='onefoundry_draft_outreach' AND step_key='draft_outreach' "
    "AND workflow_id IS NOT NULL AND organization_id IS NOT NULL AND transition_revision >= 1)"
)
TRIAGE_INTENT = (
    "(action_kind='chief_triage' AND step_key='chief_triage' AND signal_id IS NOT NULL "
    "AND rule_id IS NULL AND chain_depth BETWEEN 1 AND 30 AND chain_depth_limit BETWEEN 1 AND 30)"
)


def upgrade() -> None:
    op.drop_constraint("coordination_signal_state_valid", "coordination_signals", type_="check")
    op.create_check_constraint(
        "coordination_signal_state_valid",
        "coordination_signals",
        "state IN ('new','matched','ignored','blocked','triage')",
    )
    op.drop_constraint("coordination_intent_action_valid", "coordination_intents", type_="check")
    op.create_check_constraint(
        "coordination_intent_action_valid",
        "coordination_intents",
        f"{OUTREACH_INTENT} OR {RULE_INTENT} OR {TRIAGE_INTENT}",
    )
    op.add_column("coordination_intents", sa.Column("decision", postgresql.JSONB()))
    op.add_column("coordination_intents", sa.Column("conversation_id", postgresql.UUID(as_uuid=True)))
    op.create_check_constraint(
        "coordination_intent_decision_bounded",
        "coordination_intents",
        "decision IS NULL OR (jsonb_typeof(decision)='object' AND pg_column_size(decision) <= 4096)",
    )
    op.create_index(
        "coordination_intent_triage_once",
        "coordination_intents",
        ["project_id", "signal_id"],
        unique=True,
        postgresql_where=sa.text("action_kind='chief_triage'"),
    )
    op.add_column("coordination_controls", sa.Column("triage_work_order_id", postgresql.UUID(as_uuid=True)))
    op.create_foreign_key(
        "coordination_controls_triage_order",
        "coordination_controls",
        "work_orders",
        ["project_id", "triage_work_order_id"],
        ["project_id", "id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM coordination_intents WHERE action_kind='chief_triage')")
    ):
        raise RuntimeError("triage intents exist; downgrade would discard them")
    op.drop_constraint("coordination_controls_triage_order", "coordination_controls", type_="foreignkey")
    op.drop_column("coordination_controls", "triage_work_order_id")
    op.drop_index("coordination_intent_triage_once", table_name="coordination_intents")
    op.drop_constraint("coordination_intent_decision_bounded", "coordination_intents", type_="check")
    op.drop_column("coordination_intents", "conversation_id")
    op.drop_column("coordination_intents", "decision")
    op.drop_constraint("coordination_intent_action_valid", "coordination_intents", type_="check")
    op.create_check_constraint(
        "coordination_intent_action_valid", "coordination_intents", f"{OUTREACH_INTENT} OR {RULE_INTENT}"
    )
    op.execute("UPDATE coordination_signals SET state='blocked' WHERE state='triage'")
    op.drop_constraint("coordination_signal_state_valid", "coordination_signals", type_="check")
    op.create_check_constraint(
        "coordination_signal_state_valid",
        "coordination_signals",
        "state IN ('new','matched','ignored','blocked')",
    )
