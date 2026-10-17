"""Queue bounded Chief follow-up for status changes in assigned child work."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0057_chief_followup"
down_revision = "0056_turn_assignments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("task_turn_source_valid", "task_conversation_turns", type_="check")
    op.create_check_constraint(
        "task_turn_source_valid",
        "task_conversation_turns",
        "(source_kind IN ('operator_ui','system_triage','system_followup') "
        "AND remote_inbound_id IS NULL) OR "
        "(source_kind IN ('discord','wechat') AND remote_inbound_id IS NOT NULL)",
    )
    op.create_table(
        "chief_followup_intents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_work_order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("child_work_order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("child_version", sa.Integer(), nullable=False),
        sa.Column("child_status", sa.Text(), nullable=False),
        sa.Column("source_decision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("error_code", sa.Text()),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True)),
        sa.Column("turn_id", postgresql.UUID(as_uuid=True)),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint(
            "project_id", "child_work_order_id", "child_version", name="chief_followup_child_version_unique"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(
            ["project_id", "parent_work_order_id"], ["work_orders.project_id", "work_orders.id"]
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "child_work_order_id"], ["work_orders.project_id", "work_orders.id"]
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "source_decision_id"],
            ["task_automation_decisions.project_id", "task_automation_decisions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "conversation_id"], ["task_conversations.project_id", "task_conversations.id"]
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "turn_id"], ["task_conversation_turns.project_id", "task_conversation_turns.id"]
        ),
        sa.CheckConstraint("child_version >= 1", name="chief_followup_version_valid"),
        sa.CheckConstraint(
            "child_status IN ('done','needs_review','failed','unknown','blocked')",
            name="chief_followup_status_valid",
        ),
        sa.CheckConstraint(
            "state IN ('pending','leased','done','blocked','unknown')", name="chief_followup_state_valid"
        ),
    )
    op.create_index("chief_followup_pending", "chief_followup_intents", ["state", "created_at", "id"])


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM chief_followup_intents)")):
        raise RuntimeError("Chief follow-up intents exist; downgrade would lose their outcomes")
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM task_conversation_turns WHERE source_kind='system_followup')")
    ):
        raise RuntimeError("Chief follow-up turns exist; downgrade would lose their source identity")
    op.drop_index("chief_followup_pending", table_name="chief_followup_intents")
    op.drop_table("chief_followup_intents")
    op.drop_constraint("task_turn_source_valid", "task_conversation_turns", type_="check")
    op.create_check_constraint(
        "task_turn_source_valid",
        "task_conversation_turns",
        "(source_kind IN ('operator_ui','system_triage') AND remote_inbound_id IS NULL) OR "
        "(source_kind IN ('discord','wechat') AND remote_inbound_id IS NOT NULL)",
    )
