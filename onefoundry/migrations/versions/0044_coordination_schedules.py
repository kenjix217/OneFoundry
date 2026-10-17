"""Scheduled coordination rules: a schedule on the rule version, source checks."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0044_coordination_schedules"
down_revision = "0043_coordination_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("coordination_rule_versions", sa.Column("schedule", postgresql.JSONB()))
    op.create_check_constraint(
        "coordination_rule_source_valid",
        "coordination_rule_versions",
        "source IN ('internal','schedule','email','webhook','channel','agent_finding','monitor')",
    )
    op.create_check_constraint(
        "coordination_rule_schedule_matches_source",
        "coordination_rule_versions",
        "(source='schedule') = (schedule IS NOT NULL) AND "
        "(schedule IS NULL OR (jsonb_typeof(schedule)='object' AND pg_column_size(schedule) <= 512))",
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM coordination_rule_versions WHERE source='schedule')")
    ):
        raise RuntimeError("schedule rules exist; downgrade would discard them")
    op.drop_constraint(
        "coordination_rule_schedule_matches_source", "coordination_rule_versions", type_="check"
    )
    op.drop_constraint("coordination_rule_source_valid", "coordination_rule_versions", type_="check")
    op.drop_column("coordination_rule_versions", "schedule")
