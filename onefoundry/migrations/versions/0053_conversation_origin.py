"""Retain the trusted origin of each project conversation turn."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0053_conversation_origin"
down_revision = "0052_outbound_smtp"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "coordination_controls",
        sa.Column("agent_rules_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "task_conversation_turns",
        sa.Column("source_kind", sa.Text(), nullable=False, server_default="operator_ui"),
    )
    op.add_column(
        "task_conversation_turns",
        sa.Column("remote_inbound_id", postgresql.UUID(as_uuid=True)),
    )
    op.create_foreign_key(
        "task_turn_remote_inbound_same_project",
        "task_conversation_turns",
        "remote_inbound",
        ["project_id", "remote_inbound_id"],
        ["project_id", "id"],
    )
    op.create_check_constraint(
        "task_turn_source_valid",
        "task_conversation_turns",
        "(source_kind IN ('operator_ui','system_triage') AND remote_inbound_id IS NULL) OR "
        "(source_kind IN ('discord','wechat') AND remote_inbound_id IS NOT NULL)",
    )
    # Historical Chief triage turns have an exact intent-to-conversation link.
    # The old finalize trigger bars updates to completed turns, so suspend it
    # only for this provenance correction inside the migration transaction.
    op.execute("ALTER TABLE task_conversation_turns DISABLE TRIGGER task_conversation_turn_finalize")
    op.execute(
        """UPDATE task_conversation_turns t SET source_kind='system_triage'
        FROM task_conversations c
        JOIN coordination_intents i ON i.project_id=c.project_id
          AND i.conversation_id=c.id AND i.action_kind='chief_triage'
        WHERE t.project_id=c.project_id AND t.conversation_id=c.id"""
    )
    op.execute("ALTER TABLE task_conversation_turns ENABLE TRIGGER task_conversation_turn_finalize")
    op.execute(
        """CREATE OR REPLACE FUNCTION guard_task_conversation_turn() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.status <> 'running' THEN
            RAISE EXCEPTION 'completed conversation turns are immutable';
          END IF;
          IF ROW(NEW.project_id, NEW.actor_id, NEW.conversation_id, NEW.client_turn_id,
                 NEW.sequence, NEW.operator_text, NEW.operator_sha256, NEW.prompt_sha256,
                 NEW.profile_id, NEW.account_id, NEW.provider, NEW.model_id,
                 NEW.credential_revision, NEW.created_at, NEW.source_kind, NEW.remote_inbound_id)
             IS DISTINCT FROM
             ROW(OLD.project_id, OLD.actor_id, OLD.conversation_id, OLD.client_turn_id,
                 OLD.sequence, OLD.operator_text, OLD.operator_sha256, OLD.prompt_sha256,
                 OLD.profile_id, OLD.account_id, OLD.provider, OLD.model_id,
                 OLD.credential_revision, OLD.created_at, OLD.source_kind, OLD.remote_inbound_id) THEN
            RAISE EXCEPTION 'conversation turn binding is immutable';
          END IF;
          IF NEW.status NOT IN ('done', 'failed', 'unknown') THEN
            RAISE EXCEPTION 'conversation turn transition is invalid';
          END IF;
          RETURN NEW;
        END; $$"""
    )
    op.create_table(
        "task_automation_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("turn_id", postgresql.UUID(as_uuid=True)),
        sa.Column("run_id", postgresql.UUID(as_uuid=True)),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("arguments_sha256", sa.Text(), nullable=False),
        sa.Column("rule_id", postgresql.UUID(as_uuid=True)),
        sa.Column("assigned_work_order_id", postgresql.UUID(as_uuid=True)),
        sa.Column("rule_version", sa.Integer()),
        sa.Column("project_version", sa.Integer()),
        sa.Column("error_code", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "turn_id"],
            ["task_conversation_turns.project_id", "task_conversation_turns.id"],
        ),
        sa.ForeignKeyConstraint(["project_id", "run_id"], ["runs.project_id", "runs.id"]),
        sa.ForeignKeyConstraint(
            ["project_id", "rule_id"], ["coordination_rules.project_id", "coordination_rules.id"]
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "assigned_work_order_id"], ["work_orders.project_id", "work_orders.id"]
        ),
        sa.UniqueConstraint("project_id", "turn_id", name="task_automation_decision_turn_unique"),
        sa.UniqueConstraint("project_id", "run_id", name="task_automation_decision_run_unique"),
        sa.CheckConstraint(
            "(turn_id IS NOT NULL) <> (run_id IS NOT NULL)", name="task_automation_decision_origin_valid"
        ),
        sa.CheckConstraint(
            "status IN ('none','clarify','saved','rejected','unavailable')",
            name="task_automation_decision_status_valid",
        ),
        sa.CheckConstraint(
            "arguments_sha256 ~ '^[0-9a-f]{64}$'", name="task_automation_decision_digest_valid"
        ),
    )


def downgrade() -> None:
    op.drop_table("task_automation_decisions")
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM task_conversation_turns WHERE source_kind<>'operator_ui')")
    ):
        raise RuntimeError("non-UI conversation turns exist; downgrade would lose source identity")
    op.execute(
        """CREATE OR REPLACE FUNCTION guard_task_conversation_turn() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.status <> 'running' THEN
            RAISE EXCEPTION 'completed conversation turns are immutable';
          END IF;
          IF ROW(NEW.project_id, NEW.actor_id, NEW.conversation_id, NEW.client_turn_id,
                 NEW.sequence, NEW.operator_text, NEW.operator_sha256, NEW.prompt_sha256,
                 NEW.profile_id, NEW.account_id, NEW.provider, NEW.model_id,
                 NEW.credential_revision, NEW.created_at)
             IS DISTINCT FROM
             ROW(OLD.project_id, OLD.actor_id, OLD.conversation_id, OLD.client_turn_id,
                 OLD.sequence, OLD.operator_text, OLD.operator_sha256, OLD.prompt_sha256,
                 OLD.profile_id, OLD.account_id, OLD.provider, OLD.model_id,
                 OLD.credential_revision, OLD.created_at) THEN
            RAISE EXCEPTION 'conversation turn binding is immutable';
          END IF;
          IF NEW.status NOT IN ('done', 'failed', 'unknown') THEN
            RAISE EXCEPTION 'conversation turn transition is invalid';
          END IF;
          RETURN NEW;
        END; $$"""
    )
    op.drop_constraint("task_turn_source_valid", "task_conversation_turns", type_="check")
    op.drop_constraint("task_turn_remote_inbound_same_project", "task_conversation_turns", type_="foreignkey")
    op.drop_column("task_conversation_turns", "remote_inbound_id")
    op.drop_column("task_conversation_turns", "source_kind")
    op.drop_column("coordination_controls", "agent_rules_enabled")
