"""External coding attempts remain separate from the synthetic fixture."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0021_external_code"
down_revision = "0020_grok_account"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("runs_mode_valid", "runs", type_="check")
    op.create_check_constraint(
        "runs_mode_valid",
        "runs",
        "mode IN ('manual_planning','synthetic_text','synthetic_code','live_text','external_code')",
    )
    op.drop_constraint("runs_cost_valid", "runs", type_="check")
    op.create_check_constraint(
        "runs_cost_valid",
        "runs",
        "(mode = 'live_text' AND ((status = 'done' AND tokens_used >= 0 AND "
        "(cost_usd IS NULL OR cost_usd >= 0)) OR "
        "(status <> 'done' AND tokens_used IS NULL AND cost_usd IS NULL))) OR "
        "(mode = 'external_code' AND "
        "((status = 'done' AND (tokens_used IS NULL OR tokens_used >= 0) AND "
        "(cost_usd IS NULL OR cost_usd >= 0)) OR "
        "(status <> 'done' AND tokens_used IS NULL AND cost_usd IS NULL))) OR "
        "(mode NOT IN ('live_text','external_code') AND tokens_used = 0 AND cost_usd = 0)",
    )
    op.create_table(
        "external_code_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("adapter_id", sa.Text(), nullable=False),
        sa.Column("fixture_id", sa.Text(), nullable=False),
        sa.Column("request_sha256", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text()),
        sa.Column("provider_session_id", sa.Text()),
        sa.Column("baseline_commit", sa.Text()),
        sa.Column("branch", sa.Text()),
        sa.Column("commit", sa.Text()),
        sa.Column("diff_sha256", sa.Text()),
        sa.Column("tests", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("receipt_sha256", sa.Text()),
        sa.Column("tokens_used", sa.Integer()),
        sa.Column("cost_usd", sa.Numeric(20, 10)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["project_id", "run_id"], ["runs.project_id", "runs.id"]),
        sa.UniqueConstraint("project_id", "run_id", name="external_code_attempts_run_unique"),
        sa.UniqueConstraint("project_id", "run_id", "id", name="external_code_attempts_scope_unique"),
        sa.CheckConstraint("adapter_id IN ('codex_cli','claude_code')", name="external_code_adapter_valid"),
        sa.CheckConstraint("fixture_id = 'engineer_qa_v1'", name="external_code_fixture_valid"),
        sa.CheckConstraint(
            "status IN ('queued','done','failed','unknown')", name="external_code_status_valid"
        ),
        sa.CheckConstraint("request_sha256 ~ '^[0-9a-f]{64}$'", name="external_code_request_digest_valid"),
        sa.CheckConstraint(
            "baseline_commit IS NULL OR baseline_commit ~ '^[0-9a-f]{40}$'",
            name="external_code_baseline_valid",
        ),
        sa.CheckConstraint("commit IS NULL OR commit ~ '^[0-9a-f]{40}$'", name="external_code_commit_valid"),
        sa.CheckConstraint(
            "diff_sha256 IS NULL OR diff_sha256 ~ '^[0-9a-f]{64}$'", name="external_code_diff_valid"
        ),
        sa.CheckConstraint(
            "receipt_sha256 IS NULL OR receipt_sha256 ~ '^[0-9a-f]{64}$'", name="external_code_receipt_valid"
        ),
        sa.CheckConstraint("tokens_used IS NULL OR tokens_used >= 0", name="external_code_tokens_valid"),
        sa.CheckConstraint("cost_usd IS NULL OR cost_usd >= 0", name="external_code_cost_valid"),
        sa.CheckConstraint("jsonb_typeof(tests) = 'array'", name="external_code_tests_array"),
        sa.CheckConstraint(
            "provider_session_id IS NULL OR length(provider_session_id) BETWEEN 1 AND 200",
            name="external_code_session_valid",
        ),
    )
    op.execute("""
        CREATE FUNCTION protect_external_code_attempt() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF ROW(NEW.id, NEW.project_id, NEW.run_id, NEW.actor_id, NEW.adapter_id,
                 NEW.fixture_id, NEW.request_sha256, NEW.created_at)
             IS DISTINCT FROM
             ROW(OLD.id, OLD.project_id, OLD.run_id, OLD.actor_id, OLD.adapter_id,
                 OLD.fixture_id, OLD.request_sha256, OLD.created_at) THEN
            RAISE EXCEPTION 'external coding attempt identity is immutable';
          END IF;
          IF OLD.status <> 'queued' OR
             (OLD.provider_session_id IS NOT NULL AND
              NEW.provider_session_id IS DISTINCT FROM OLD.provider_session_id) OR
             (OLD.baseline_commit IS NOT NULL AND
              NEW.baseline_commit IS DISTINCT FROM OLD.baseline_commit) THEN
            RAISE EXCEPTION 'external coding attempt result is immutable';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER external_code_attempt_protect BEFORE UPDATE ON external_code_attempts "
        "FOR EACH ROW EXECUTE FUNCTION protect_external_code_attempt()"
    )
    op.execute(
        "CREATE TRIGGER external_code_attempt_no_delete BEFORE DELETE ON external_code_attempts "
        "FOR EACH ROW EXECUTE FUNCTION reject_event_mutation()"
    )
    op.create_table(
        "external_code_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "run_id", "attempt_id"],
            [
                "external_code_attempts.project_id",
                "external_code_attempts.run_id",
                "external_code_attempts.id",
            ],
        ),
        sa.UniqueConstraint("project_id", "attempt_id", "kind", name="external_code_artifact_kind_unique"),
        sa.CheckConstraint("kind IN ('diff','log')", name="external_code_artifact_kind_valid"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="external_code_artifact_digest_valid"),
        sa.CheckConstraint("byte_size BETWEEN 0 AND 65536", name="external_code_artifact_size_valid"),
    )
    op.execute(
        "CREATE TRIGGER external_code_artifacts_immutable BEFORE UPDATE OR DELETE ON external_code_artifacts "
        "FOR EACH ROW EXECUTE FUNCTION reject_event_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER external_code_artifacts_immutable ON external_code_artifacts")
    op.drop_table("external_code_artifacts")
    op.execute("DROP TRIGGER external_code_attempt_no_delete ON external_code_attempts")
    op.execute("DROP TRIGGER external_code_attempt_protect ON external_code_attempts")
    op.execute("DROP FUNCTION protect_external_code_attempt()")
    op.drop_table("external_code_attempts")
    op.drop_constraint("runs_cost_valid", "runs", type_="check")
    op.create_check_constraint(
        "runs_cost_valid",
        "runs",
        "(mode = 'live_text' AND ((status = 'done' AND tokens_used >= 0 AND "
        "(cost_usd IS NULL OR cost_usd >= 0)) OR "
        "(status <> 'done' AND tokens_used IS NULL AND cost_usd IS NULL))) OR "
        "(mode <> 'live_text' AND tokens_used = 0 AND cost_usd = 0)",
    )
    op.drop_constraint("runs_mode_valid", "runs", type_="check")
    op.create_check_constraint(
        "runs_mode_valid", "runs", "mode IN ('manual_planning','synthetic_text','synthetic_code','live_text')"
    )
