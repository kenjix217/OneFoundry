"""Record which public messages and dependency results were sent to a team turn."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0063_team_context_snapshot"
down_revision = "0062_team_assignments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task_session_attempts",
        sa.Column(
            "context_message_ids", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
    )
    op.add_column(
        "task_session_attempts",
        sa.Column(
            "context_dependency_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "task_session_attempts",
        sa.Column("context_omitted_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "task_session_attempts_context_messages_valid",
        "task_session_attempts",
        "jsonb_typeof(context_message_ids)='array'",
    )
    op.create_check_constraint(
        "task_session_attempts_context_dependencies_valid",
        "task_session_attempts",
        "jsonb_typeof(context_dependency_ids)='array'",
    )
    op.create_check_constraint(
        "task_session_attempts_context_omitted_valid",
        "task_session_attempts",
        "context_omitted_count>=0",
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
          IF OLD.status='running' AND NEW.status='running' THEN
            IF OLD.prompt_sha256 IS NOT NULL OR NEW.prompt_sha256 IS NULL OR
               to_jsonb(NEW) - 'prompt_sha256' - 'context_message_ids' -
               'context_dependency_ids' - 'context_omitted_count' IS DISTINCT FROM
               to_jsonb(OLD) - 'prompt_sha256' - 'context_message_ids' -
               'context_dependency_ids' - 'context_omitted_count' THEN
              RAISE EXCEPTION 'task session context preparation is invalid';
            END IF;
            RETURN NEW;
          END IF;
          IF OLD.status='running' AND NEW.status IN ('done','failed','unknown') AND
             ROW(NEW.prompt_sha256,NEW.context_message_ids,NEW.context_dependency_ids,
                 NEW.context_omitted_count) IS DISTINCT FROM
             ROW(OLD.prompt_sha256,OLD.context_message_ids,OLD.context_dependency_ids,
                 OLD.context_omitted_count) THEN
            RAISE EXCEPTION 'task session context snapshot is immutable';
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
    if connection.scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM task_session_attempts WHERE prompt_sha256 IS NOT NULL)")
    ):
        raise RuntimeError("team context snapshots exist; downgrade would discard input evidence")
    op.drop_constraint("task_session_attempts_context_omitted_valid", "task_session_attempts", type_="check")
    op.drop_constraint(
        "task_session_attempts_context_dependencies_valid", "task_session_attempts", type_="check"
    )
    op.drop_constraint("task_session_attempts_context_messages_valid", "task_session_attempts", type_="check")
    op.drop_column("task_session_attempts", "context_omitted_count")
    op.drop_column("task_session_attempts", "context_dependency_ids")
    op.drop_column("task_session_attempts", "context_message_ids")
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
