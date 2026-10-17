"""Durable browser inspection intent and terminal receipt metadata."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0033_browser_static_jobs"
down_revision = "0032_browser_static_fixtures"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "browser_static_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("fixture_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("idempotency_key", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_sha256", sa.Text(), nullable=False),
        sa.Column("binding_digest", sa.Text(), nullable=False),
        sa.Column("source_sha256", sa.Text(), nullable=False),
        sa.Column("image_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text()),
        sa.Column("receipt_sha256", sa.Text()),
        sa.Column("title", sa.Text()),
        sa.Column("inspection_text", sa.Text()),
        sa.Column("blocked_request_count", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["project_id", "run_id", "fixture_id"],
            [
                "browser_static_fixtures.project_id",
                "browser_static_fixtures.run_id",
                "browser_static_fixtures.id",
            ],
        ),
        sa.UniqueConstraint(
            "project_id", "run_id", "idempotency_key", name="browser_static_jobs_idempotency_unique"
        ),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="browser_static_jobs_actor"),
        sa.CheckConstraint("request_sha256 ~ '^[0-9a-f]{64}$'", name="browser_static_jobs_request_hash"),
        sa.CheckConstraint("binding_digest ~ '^[0-9a-f]{64}$'", name="browser_static_jobs_binding_hash"),
        sa.CheckConstraint("source_sha256 ~ '^[0-9a-f]{64}$'", name="browser_static_jobs_source_hash"),
        sa.CheckConstraint("image_id ~ '^sha256:[0-9a-f]{64}$'", name="browser_static_jobs_image_id"),
        sa.CheckConstraint(
            "(status IN ('pending','queued') AND reason_code IS NULL AND receipt_sha256 IS NULL "
            "AND finished_at IS NULL) OR "
            "(status IN ('done','failed','unknown') AND reason_code IS NOT NULL "
            "AND finished_at IS NOT NULL)",
            name="browser_static_jobs_state",
        ),
        sa.CheckConstraint(
            "reason_code IS NULL OR reason_code ~ '^[a-z][a-z0-9_]{0,159}$'",
            name="browser_static_jobs_reason",
        ),
        sa.CheckConstraint(
            "receipt_sha256 IS NULL OR receipt_sha256 ~ '^[0-9a-f]{64}$'",
            name="browser_static_jobs_receipt_hash",
        ),
        sa.CheckConstraint(
            "(status='done' AND title IS NOT NULL AND inspection_text IS NOT NULL "
            "AND blocked_request_count BETWEEN 0 AND 1000 AND length(title) <= 128 "
            "AND length(inspection_text) <= 4096) OR "
            "(status<>'done' AND title IS NULL AND inspection_text IS NULL "
            "AND blocked_request_count IS NULL)",
            name="browser_static_jobs_evidence_bounded",
        ),
    )
    op.execute("""
        CREATE FUNCTION protect_browser_static_job() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF ROW(NEW.id,NEW.project_id,NEW.run_id,NEW.fixture_id,NEW.actor_id,
                 NEW.idempotency_key,NEW.request_sha256,NEW.binding_digest,
                 NEW.source_sha256,NEW.image_id,NEW.created_at)
             IS DISTINCT FROM
             ROW(OLD.id,OLD.project_id,OLD.run_id,OLD.fixture_id,OLD.actor_id,
                 OLD.idempotency_key,OLD.request_sha256,OLD.binding_digest,
                 OLD.source_sha256,OLD.image_id,OLD.created_at) THEN
            RAISE EXCEPTION 'browser job identity is immutable';
          END IF;
          IF NOT ((OLD.status='pending' AND NEW.status IN ('queued','unknown')) OR
                  (OLD.status='queued' AND NEW.status IN ('done','failed','unknown'))) THEN
            RAISE EXCEPTION 'browser job transition is invalid';
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER browser_static_jobs_protect BEFORE UPDATE ON browser_static_jobs "
        "FOR EACH ROW EXECUTE FUNCTION protect_browser_static_job()"
    )
    op.execute(
        "CREATE TRIGGER browser_static_jobs_no_delete BEFORE DELETE ON browser_static_jobs "
        "FOR EACH ROW EXECUTE FUNCTION reject_event_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER browser_static_jobs_no_delete ON browser_static_jobs")
    op.execute("DROP TRIGGER browser_static_jobs_protect ON browser_static_jobs")
    op.execute("DROP FUNCTION protect_browser_static_job()")
    op.drop_table("browser_static_jobs")
