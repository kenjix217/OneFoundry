"""Bind external coding attempts to an immutable local source snapshot."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0024_source_bound_attempt"
down_revision = "0023_project_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION reject_project_source_update() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'saved repository sources are immutable';
        END $$
    """)
    op.execute(
        "CREATE TRIGGER project_source_no_update BEFORE UPDATE ON project_sources "
        "FOR EACH ROW EXECUTE FUNCTION reject_project_source_update()"
    )
    op.add_column("external_code_attempts", sa.Column("source_id", postgresql.UUID(as_uuid=True)))
    for name in (
        "source_root_id",
        "source_relative_path",
        "source_branch",
        "source_revision",
    ):
        op.add_column("external_code_attempts", sa.Column(name, sa.Text()))
    op.drop_constraint("external_code_fixture_valid", "external_code_attempts", type_="check")
    op.drop_constraint("external_code_adapter_valid", "external_code_attempts", type_="check")
    op.create_check_constraint(
        "external_code_adapter_valid",
        "external_code_attempts",
        "(fixture_id='engineer_qa_v1' AND adapter_id IN ('codex_cli','claude_code')) OR "
        "(fixture_id='local_source_probe_v1' AND adapter_id='offline_probe')",
    )
    op.create_check_constraint(
        "external_code_fixture_valid",
        "external_code_attempts",
        "fixture_id IN ('engineer_qa_v1','local_source_probe_v1')",
    )
    op.create_check_constraint(
        "external_code_source_snapshot_valid",
        "external_code_attempts",
        "(fixture_id='engineer_qa_v1' AND source_id IS NULL AND source_root_id IS NULL "
        "AND source_relative_path IS NULL AND source_branch IS NULL AND source_revision IS NULL) OR "
        "(fixture_id='local_source_probe_v1' AND source_id IS NOT NULL AND source_root_id='workspace' "
        "AND source_relative_path IS NOT NULL AND source_branch IS NOT NULL "
        "AND source_revision ~ '^[0-9a-f]{40}$')",
    )
    op.execute("""
        CREATE OR REPLACE FUNCTION protect_external_code_attempt() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF ROW(NEW.id, NEW.project_id, NEW.run_id, NEW.actor_id, NEW.adapter_id,
                 NEW.fixture_id, NEW.request_sha256, NEW.created_at,
                 NEW.source_id, NEW.source_root_id, NEW.source_relative_path,
                 NEW.source_branch, NEW.source_revision)
             IS DISTINCT FROM
             ROW(OLD.id, OLD.project_id, OLD.run_id, OLD.actor_id, OLD.adapter_id,
                 OLD.fixture_id, OLD.request_sha256, OLD.created_at,
                 OLD.source_id, OLD.source_root_id, OLD.source_relative_path,
                 OLD.source_branch, OLD.source_revision) THEN
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


def downgrade() -> None:
    op.execute("DROP TRIGGER project_source_no_update ON project_sources")
    op.execute("DROP FUNCTION reject_project_source_update()")
    op.execute("""
        CREATE OR REPLACE FUNCTION protect_external_code_attempt() RETURNS trigger LANGUAGE plpgsql AS $$
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
    op.drop_constraint("external_code_source_snapshot_valid", "external_code_attempts", type_="check")
    op.drop_constraint("external_code_adapter_valid", "external_code_attempts", type_="check")
    op.create_check_constraint(
        "external_code_adapter_valid",
        "external_code_attempts",
        "adapter_id IN ('codex_cli','claude_code')",
    )
    op.drop_constraint("external_code_fixture_valid", "external_code_attempts", type_="check")
    op.create_check_constraint(
        "external_code_fixture_valid", "external_code_attempts", "fixture_id='engineer_qa_v1'"
    )
    for name in (
        "source_revision",
        "source_branch",
        "source_relative_path",
        "source_root_id",
    ):
        op.drop_column("external_code_attempts", name)
    op.drop_column("external_code_attempts", "source_id")
