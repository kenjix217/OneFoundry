"""Give explicit team-session continuations a durable idempotency identity."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0061_team_attempt_requests"
down_revision = "0060_team_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "task_session_messages_session_id_unique",
        "task_session_messages",
        ["project_id", "session_id", "id"],
    )
    op.create_foreign_key(
        "task_session_attempts_source_session_fk",
        "task_session_attempts",
        "task_session_messages",
        ["project_id", "session_id", "source_message_id"],
        ["project_id", "session_id", "id"],
    )
    op.add_column(
        "task_sessions", sa.Column("execution_mode", sa.Text(), nullable=False, server_default="legacy")
    )
    op.create_check_constraint(
        "task_sessions_execution_mode_valid", "task_sessions", "execution_mode IN ('legacy','team')"
    )
    op.add_column(
        "task_session_attempts",
        sa.Column("client_attempt_id", postgresql.UUID(as_uuid=True)),
    )
    op.add_column("task_session_attempts", sa.Column("input_sequence", sa.Integer()))
    op.create_check_constraint(
        "task_session_attempts_input_sequence_valid",
        "task_session_attempts",
        "input_sequence IS NULL OR input_sequence >= 0",
    )
    op.create_unique_constraint(
        "task_session_attempts_client_unique",
        "task_session_attempts",
        ["project_id", "client_attempt_id"],
    )
    op.execute(
        """CREATE OR REPLACE FUNCTION guard_task_session_attempt() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.status NOT IN ('queued','running') THEN
            RAISE EXCEPTION 'completed task session attempts are immutable';
          END IF;
          IF ROW(NEW.project_id,NEW.actor_id,NEW.root_work_order_id,NEW.session_id,
                 NEW.source_message_id,NEW.client_attempt_id,NEW.created_at) IS DISTINCT FROM
             ROW(OLD.project_id,OLD.actor_id,OLD.root_work_order_id,OLD.session_id,
                 OLD.source_message_id,OLD.client_attempt_id,OLD.created_at) THEN
            RAISE EXCEPTION 'task session attempt binding is immutable';
          END IF;
          IF (OLD.status='queued' AND NEW.status='running' AND NEW.input_sequence IS NULL) OR
             (OLD.status='running' AND NEW.input_sequence IS DISTINCT FROM OLD.input_sequence) THEN
            RAISE EXCEPTION 'task session attempt input boundary is invalid';
          END IF;
          IF (OLD.status='queued' AND NEW.status NOT IN ('running','failed','unknown')) OR
             (OLD.status='running' AND NEW.status NOT IN ('done','failed','unknown')) THEN
            RAISE EXCEPTION 'task session attempt transition is invalid';
          END IF;
          RETURN NEW;
        END; $$"""
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM task_sessions WHERE execution_mode='team')")):
        raise RuntimeError("team sessions exist; downgrade would remove the execution boundary")
    if connection.scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM task_session_attempts WHERE client_attempt_id IS NOT NULL)")
    ):
        raise RuntimeError("team continuation identities exist; downgrade would discard saved requests")
    op.drop_constraint("task_session_attempts_client_unique", "task_session_attempts", type_="unique")
    op.drop_constraint("task_session_attempts_input_sequence_valid", "task_session_attempts", type_="check")
    op.drop_column("task_session_attempts", "input_sequence")
    op.drop_column("task_session_attempts", "client_attempt_id")
    op.drop_constraint("task_sessions_execution_mode_valid", "task_sessions", type_="check")
    op.drop_column("task_sessions", "execution_mode")
    op.drop_constraint("task_session_attempts_source_session_fk", "task_session_attempts", type_="foreignkey")
    op.drop_constraint("task_session_messages_session_id_unique", "task_session_messages", type_="unique")
    op.execute(
        """CREATE OR REPLACE FUNCTION guard_task_session_attempt() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.status NOT IN ('queued','running') THEN
            RAISE EXCEPTION 'completed task session attempts are immutable';
          END IF;
          IF ROW(NEW.project_id,NEW.actor_id,NEW.root_work_order_id,NEW.session_id,
                 NEW.source_message_id,NEW.created_at) IS DISTINCT FROM
             ROW(OLD.project_id,OLD.actor_id,OLD.root_work_order_id,OLD.session_id,
                 OLD.source_message_id,OLD.created_at) THEN
            RAISE EXCEPTION 'task session attempt binding is immutable';
          END IF;
          IF (OLD.status='queued' AND NEW.status NOT IN ('running','failed','unknown')) OR
             (OLD.status='running' AND NEW.status NOT IN ('done','failed','unknown')) THEN
            RAISE EXCEPTION 'task session attempt transition is invalid';
          END IF;
          RETURN NEW;
        END; $$"""
    )
