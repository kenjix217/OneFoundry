"""Rules may start work automatically in auto mode, through a separate launch intent."""

import sqlalchemy as sa
from alembic import op

revision = "0047_coordination_auto_start"
down_revision = "0046_mail_accounts"
branch_labels = None
depends_on = None

RULE_INTENT = (
    "(action_kind='rule_work_order' AND step_key='rule_work_order' AND signal_id IS NOT NULL "
    "AND rule_id IS NOT NULL AND rule_version >= 1 AND target_profile_id IS NOT NULL "
    "AND autonomy IN ('draft','ready','auto_start') AND chain_depth BETWEEN 1 AND 30 "
    "AND chain_depth_limit BETWEEN 1 AND 30)"
)
OLD_RULE_INTENT = RULE_INTENT.replace("'draft','ready','auto_start'", "'draft','ready'")
OUTREACH_INTENT = (
    "(action_kind='onefoundry_draft_outreach' AND step_key='draft_outreach' "
    "AND workflow_id IS NOT NULL AND organization_id IS NOT NULL AND transition_revision >= 1)"
)
TRIAGE_INTENT = (
    "(action_kind='chief_triage' AND step_key='chief_triage' AND signal_id IS NOT NULL "
    "AND rule_id IS NULL AND chain_depth BETWEEN 1 AND 30 AND chain_depth_limit BETWEEN 1 AND 30)"
)
LAUNCH_INTENT = (
    "(action_kind='auto_start' AND step_key='auto_start' AND signal_id IS NOT NULL "
    "AND rule_id IS NOT NULL AND work_order_id IS NOT NULL)"
)


def upgrade() -> None:
    op.drop_constraint("coordination_rule_autonomy_valid", "coordination_rule_versions", type_="check")
    op.create_check_constraint(
        "coordination_rule_autonomy_valid",
        "coordination_rule_versions",
        "autonomy IN ('draft','ready','auto_start')",
    )
    op.drop_constraint("coordination_intent_action_valid", "coordination_intents", type_="check")
    op.create_check_constraint(
        "coordination_intent_action_valid",
        "coordination_intents",
        f"{OUTREACH_INTENT} OR {RULE_INTENT} OR {TRIAGE_INTENT} OR {LAUNCH_INTENT}",
    )
    op.drop_constraint("coordination_intent_rule_once", "coordination_intents", type_="unique")
    op.create_index(
        "coordination_intent_rule_once",
        "coordination_intents",
        ["project_id", "signal_id", "rule_id"],
        unique=True,
        postgresql_where=sa.text("action_kind='rule_work_order'"),
    )
    op.create_index(
        "coordination_intent_launch_once",
        "coordination_intents",
        ["project_id", "work_order_id"],
        unique=True,
        postgresql_where=sa.text("action_kind='auto_start'"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM coordination_intents WHERE action_kind='auto_start' "
            "OR autonomy='auto_start') OR EXISTS (SELECT 1 FROM coordination_rule_versions "
            "WHERE autonomy='auto_start')"
        )
    ):
        raise RuntimeError("auto-start rules or launches exist; downgrade would discard them")
    op.drop_index("coordination_intent_launch_once", table_name="coordination_intents")
    op.drop_index("coordination_intent_rule_once", table_name="coordination_intents")
    op.create_unique_constraint(
        "coordination_intent_rule_once", "coordination_intents", ["project_id", "signal_id", "rule_id"]
    )
    op.drop_constraint("coordination_intent_action_valid", "coordination_intents", type_="check")
    op.create_check_constraint(
        "coordination_intent_action_valid",
        "coordination_intents",
        f"{OUTREACH_INTENT} OR {OLD_RULE_INTENT} OR {TRIAGE_INTENT}",
    )
    op.drop_constraint("coordination_rule_autonomy_valid", "coordination_rule_versions", type_="check")
    op.create_check_constraint(
        "coordination_rule_autonomy_valid", "coordination_rule_versions", "autonomy IN ('draft','ready')"
    )
