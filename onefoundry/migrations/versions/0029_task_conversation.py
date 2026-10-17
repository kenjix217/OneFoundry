"""Explicit operator to profile conversations for one work order."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0029_task_conversation"
down_revision = "0028_profile_hierarchy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("work_order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "work_order_id"], ["work_orders.project_id", "work_orders.id"]
        ),
        sa.ForeignKeyConstraint(["project_id", "profile_id"], ["profiles.project_id", "profiles.id"]),
        sa.UniqueConstraint("project_id", "id", name="task_conversations_project_id_id_unique"),
        sa.UniqueConstraint(
            "project_id", "work_order_id", "profile_id", name="task_conversations_order_profile_unique"
        ),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="task_conversations_actor_nonblank"),
    )
    op.create_table(
        "task_conversation_turns",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_turn_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("operator_text", sa.Text(), nullable=False),
        sa.Column("operator_sha256", sa.Text(), nullable=False),
        sa.Column("prompt_sha256", sa.Text(), nullable=False),
        sa.Column("assistant_text", sa.Text()),
        sa.Column("assistant_sha256", sa.Text()),
        sa.Column("assistant_actor_id", sa.Text()),
        sa.Column("reason", sa.Text()),
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("credential_revision", sa.Integer(), nullable=False),
        sa.Column("provider_response_id", sa.Text()),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("cost_usd", sa.Numeric(20, 10)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["project_id", "conversation_id"], ["task_conversations.project_id", "task_conversations.id"]
        ),
        sa.UniqueConstraint("project_id", "conversation_id", "sequence", name="task_turn_sequence_unique"),
        sa.UniqueConstraint(
            "project_id", "conversation_id", "client_turn_id", name="task_turn_client_unique"
        ),
        sa.UniqueConstraint("project_id", "client_turn_id", name="task_turn_project_client_unique"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="task_turn_actor_nonblank"),
        sa.CheckConstraint("sequence > 0", name="task_turn_sequence_positive"),
        sa.CheckConstraint("status IN ('running','done','failed','unknown')", name="task_turn_status_valid"),
        sa.CheckConstraint("operator_text ~ '[^[:space:]]'", name="task_turn_operator_nonblank"),
        sa.CheckConstraint("operator_sha256 ~ '^[0-9a-f]{64}$'", name="task_turn_operator_digest"),
        sa.CheckConstraint("prompt_sha256 ~ '^[0-9a-f]{64}$'", name="task_turn_prompt_digest"),
        sa.CheckConstraint(
            "assistant_sha256 IS NULL OR assistant_sha256 ~ '^[0-9a-f]{64}$'",
            name="task_turn_assistant_digest",
        ),
        sa.CheckConstraint(
            "(status='done' AND assistant_text IS NOT NULL AND assistant_sha256 IS NOT NULL "
            "AND assistant_actor_id IS NOT NULL "
            "AND input_tokens >= 0 "
            "AND output_tokens >= 0 AND finished_at IS NOT NULL) OR "
            "(status<>'done' AND assistant_text IS NULL AND assistant_sha256 IS NULL)",
            name="task_turn_outcome_valid",
        ),
    )
    op.create_index(
        "task_turns_project_conversation_idx",
        "task_conversation_turns",
        ["project_id", "conversation_id", "sequence"],
    )
    op.execute(
        """CREATE FUNCTION guard_task_conversation_turn() RETURNS trigger LANGUAGE plpgsql AS $$
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
    op.execute(
        "CREATE TRIGGER task_conversation_turn_finalize BEFORE UPDATE ON task_conversation_turns "
        "FOR EACH ROW EXECUTE FUNCTION guard_task_conversation_turn()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER task_conversation_turn_finalize ON task_conversation_turns")
    op.execute("DROP FUNCTION guard_task_conversation_turn()")
    op.drop_index("task_turns_project_conversation_idx", table_name="task_conversation_turns")
    op.drop_table("task_conversation_turns")
    op.drop_table("task_conversations")
