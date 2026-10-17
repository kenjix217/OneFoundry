"""Permit one durable provider thread and turn binding after context preparation."""

import sqlalchemy as sa
from alembic import op

revision = "0066_team_provider_bindings"
down_revision = "0065_team_tool_calls"
branch_labels = None
depends_on = None


def upgrade() -> None:
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
            IF OLD.prompt_sha256 IS NULL THEN
              IF NEW.prompt_sha256 IS NULL OR
                 to_jsonb(NEW) - 'prompt_sha256' - 'context_message_ids' -
                 'context_dependency_ids' - 'context_omitted_count' IS DISTINCT FROM
                 to_jsonb(OLD) - 'prompt_sha256' - 'context_message_ids' -
                 'context_dependency_ids' - 'context_omitted_count' THEN
                RAISE EXCEPTION 'task session context preparation is invalid';
              END IF;
            ELSIF ROW(NEW.prompt_sha256,NEW.context_message_ids,NEW.context_dependency_ids,
                      NEW.context_omitted_count) IS DISTINCT FROM
                  ROW(OLD.prompt_sha256,OLD.context_message_ids,OLD.context_dependency_ids,
                      OLD.context_omitted_count) OR
                  (OLD.provider_thread_id IS NOT NULL AND
                   NEW.provider_thread_id IS DISTINCT FROM OLD.provider_thread_id) OR
                  (OLD.provider_turn_id IS NOT NULL AND
                   NEW.provider_turn_id IS DISTINCT FROM OLD.provider_turn_id) OR
                  (NEW.provider_turn_id IS NOT NULL AND NEW.provider_thread_id IS NULL) OR
                  to_jsonb(NEW) - 'provider_thread_id' - 'provider_turn_id' IS DISTINCT FROM
                  to_jsonb(OLD) - 'provider_thread_id' - 'provider_turn_id' THEN
              RAISE EXCEPTION 'task session provider binding is invalid';
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
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM task_session_attempts WHERE status='running' "
            "AND (provider_thread_id IS NOT NULL OR provider_turn_id IS NOT NULL))"
        )
    ):
        raise RuntimeError("active team provider bindings exist; downgrade would strand a turn")
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
