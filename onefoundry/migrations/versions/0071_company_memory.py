"""Company source archives, reviewed replacements, and reversible withdrawal."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0071_company_memory"
down_revision = "0070_document_actions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_captures",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("client_capture_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=False),
        sa.Column("messages", postgresql.JSONB(), nullable=False),
        sa.Column("source_sha256", sa.Text(), nullable=False),
        sa.Column("payload_sha256", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.UniqueConstraint("project_id", "id", name="memory_captures_project_id_id_unique"),
        sa.UniqueConstraint(
            "project_id", "actor_id", "client_capture_id", name="memory_capture_request_unique"
        ),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="memory_captures_actor_nonblank"),
        sa.CheckConstraint("octet_length(source_ref) BETWEEN 1 AND 960", name="memory_captures_ref_bounded"),
        sa.CheckConstraint(
            "jsonb_typeof(messages) = 'array' AND jsonb_array_length(messages) BETWEEN 1 AND 16 "
            "AND octet_length(messages::text) <= 131072",
            name="memory_captures_messages_bounded",
        ),
        sa.CheckConstraint("source_sha256 ~ '^[0-9a-f]{64}$'", name="memory_captures_hash_valid"),
        sa.CheckConstraint("payload_sha256 ~ '^[0-9a-f]{64}$'", name="memory_capture_payload_hash_valid"),
    )
    op.add_column("run_results", sa.Column("source_capture_id", postgresql.UUID(as_uuid=True)))
    op.alter_column("run_results", "run_id", nullable=True)
    op.create_unique_constraint("run_results_capture_unique", "run_results", ["source_capture_id"])
    op.create_foreign_key(
        "run_results_capture_fk",
        "run_results",
        "memory_captures",
        ["project_id", "source_capture_id"],
        ["project_id", "id"],
    )
    op.drop_constraint("run_results_source_kind_valid", "run_results", type_="check")
    op.create_check_constraint(
        "run_results_source_kind_valid",
        "run_results",
        "(source_kind = 'operator_entered' AND run_id IS NOT NULL AND source_capture_id IS NULL "
        "AND source_sales_draft_id IS NULL AND source_runner_job_id IS NULL AND source_sha256 IS NULL) OR "
        "(source_kind = 'synthetic_sales' AND run_id IS NOT NULL AND source_capture_id IS NULL "
        "AND source_sales_draft_id IS NOT NULL AND source_runner_job_id IS NULL "
        "AND source_sha256 IS NOT NULL AND source_sha256 ~ '^[0-9a-f]{64}$') OR "
        "(source_kind = 'synthetic_qa' AND run_id IS NOT NULL AND source_capture_id IS NULL "
        "AND source_sales_draft_id IS NULL AND source_runner_job_id IS NOT NULL "
        "AND source_sha256 IS NOT NULL AND source_sha256 ~ '^[0-9a-f]{64}$') OR "
        "(source_kind = 'conversation_capture' AND run_id IS NULL AND source_capture_id IS NOT NULL "
        "AND source_sales_draft_id IS NULL AND source_runner_job_id IS NULL "
        "AND source_sha256 IS NOT NULL AND source_sha256 ~ '^[0-9a-f]{64}$')",
    )
    op.add_column("dreamcycle_candidates", sa.Column("replaces_memory_id", postgresql.UUID(as_uuid=True)))
    op.create_foreign_key(
        "dreamcycle_replaces_memory_fk",
        "dreamcycle_candidates",
        "memory_items",
        ["project_id", "replaces_memory_id"],
        ["project_id", "id"],
    )
    op.add_column("memory_items", sa.Column("state", sa.Text(), server_default="current", nullable=False))
    op.add_column("memory_items", sa.Column("superseded_by", postgresql.UUID(as_uuid=True)))
    op.add_column("memory_items", sa.Column("changed_at", sa.DateTime(timezone=True)))
    op.add_column("memory_items", sa.Column("changed_by_actor_id", sa.Text()))
    op.create_foreign_key(
        "memory_items_superseded_fk",
        "memory_items",
        "memory_items",
        ["project_id", "superseded_by"],
        ["project_id", "id"],
    )
    op.create_check_constraint(
        "memory_items_lifecycle_valid",
        "memory_items",
        "state IN ('current', 'withdrawn', 'superseded') AND "
        "((state = 'superseded' AND superseded_by IS NOT NULL AND superseded_by <> id) OR "
        "(state <> 'superseded' AND superseded_by IS NULL)) AND "
        "((changed_at IS NULL AND changed_by_actor_id IS NULL AND state = 'current') OR "
        "(changed_at IS NOT NULL AND changed_by_actor_id IS NOT NULL "
        "AND changed_by_actor_id ~ '[^[:space:]]'))",
    )
    op.add_column(
        "task_session_attempts",
        sa.Column(
            "context_memory", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
    )
    op.execute("""
        CREATE FUNCTION validate_memory_capture_result() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.source_kind = 'conversation_capture' AND NOT EXISTS (
            SELECT 1 FROM memory_captures s WHERE s.project_id = NEW.project_id
              AND s.id = NEW.source_capture_id AND s.source_sha256 = NEW.source_sha256
          ) THEN
            RAISE EXCEPTION 'capture result requires exact source lineage' USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END; $$;
        CREATE TRIGGER memory_capture_result_guard BEFORE INSERT ON run_results
          FOR EACH ROW EXECUTE FUNCTION validate_memory_capture_result();
    """)

    op.execute("""
        CREATE FUNCTION validate_memory_lifecycle() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF (NEW.id,NEW.project_id,NEW.actor_id,NEW.scope,NEW.text,NEW.source_result_id,
              NEW.source_candidate_id,NEW.created_at) IS DISTINCT FROM
             (OLD.id,OLD.project_id,OLD.actor_id,OLD.scope,OLD.text,OLD.source_result_id,
              OLD.source_candidate_id,OLD.created_at) THEN
            RAISE EXCEPTION 'published fact provenance is immutable' USING ERRCODE = '23514';
          END IF;
          IF NOT ((OLD.state='current' AND NEW.state IN ('withdrawn','superseded')) OR
                  (OLD.state='withdrawn' AND NEW.state='current')) THEN
            RAISE EXCEPTION 'invalid memory lifecycle transition' USING ERRCODE = '23514';
          END IF;
          IF NEW.state='superseded' AND NOT EXISTS (
            SELECT 1 FROM memory_items n JOIN dreamcycle_candidates c
              ON c.project_id=n.project_id AND c.id=n.source_candidate_id
            WHERE n.project_id=OLD.project_id AND n.id=NEW.superseded_by
              AND n.scope=OLD.scope AND n.state='current' AND c.status='approved'
              AND c.replaces_memory_id=OLD.id
          ) THEN
            RAISE EXCEPTION 'replacement requires matching reviewed fact' USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END; $$;
        CREATE TRIGGER memory_lifecycle_guard BEFORE UPDATE ON memory_items
          FOR EACH ROW EXECUTE FUNCTION validate_memory_lifecycle();
    """)

    op.create_check_constraint(
        "task_session_memory_snapshot_bounded",
        "task_session_attempts",
        "jsonb_typeof(context_memory)='array' AND jsonb_array_length(context_memory)<=20 "
        "AND octet_length(context_memory::text)<=131072",
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
          IF OLD.status='queued' AND NEW.context_memory IS DISTINCT FROM OLD.context_memory THEN
            RAISE EXCEPTION 'unprepared memory snapshot is immutable';
          END IF;
          IF OLD.status='running' AND NEW.status='running' THEN
            IF OLD.prompt_sha256 IS NULL THEN
              IF NEW.prompt_sha256 IS NULL OR
                 to_jsonb(NEW) - 'prompt_sha256' - 'context_message_ids' -
                 'context_dependency_ids' - 'context_omitted_count' - 'context_memory' IS DISTINCT FROM
                 to_jsonb(OLD) - 'prompt_sha256' - 'context_message_ids' -
                 'context_dependency_ids' - 'context_omitted_count' - 'context_memory' THEN
                RAISE EXCEPTION 'task session context preparation is invalid';
              END IF;
            ELSIF ROW(NEW.prompt_sha256,NEW.context_message_ids,NEW.context_dependency_ids,
                      NEW.context_omitted_count,NEW.context_memory) IS DISTINCT FROM
                  ROW(OLD.prompt_sha256,OLD.context_message_ids,OLD.context_dependency_ids,
                      OLD.context_omitted_count,OLD.context_memory) OR
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
                 NEW.context_omitted_count,NEW.context_memory) IS DISTINCT FROM
             ROW(OLD.prompt_sha256,OLD.context_message_ids,OLD.context_dependency_ids,
                 OLD.context_omitted_count,OLD.context_memory) THEN
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
    # Source archives and historical corrections cannot be discarded by a rollback.
    raise RuntimeError("company memory downgrade requires a separately reviewed data migration")
