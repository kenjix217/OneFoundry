"""Bind one fake Codex coding attempt to an immutable v3 snapshot."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0027_snapshot_codex_fixture"
down_revision = "0026_github_fixture_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "github_fixture_snapshot_scope_unique",
        "github_fixture_source_snapshots",
        ["project_id", "attempt_id"],
    )
    op.add_column(
        "external_code_attempts",
        sa.Column("snapshot_attempt_id", postgresql.UUID(as_uuid=True)),
    )
    op.add_column(
        "external_code_attempts",
        sa.Column("idempotency_key", postgresql.UUID(as_uuid=True)),
    )
    op.add_column("external_code_attempts", sa.Column("intent_sha256", sa.Text()))
    op.add_column("external_code_attempts", sa.Column("snapshot_source_id", postgresql.UUID(as_uuid=True)))
    op.add_column("external_code_attempts", sa.Column("snapshot_account_id", postgresql.UUID(as_uuid=True)))
    op.add_column("external_code_attempts", sa.Column("snapshot_owner", sa.Text()))
    op.add_column("external_code_attempts", sa.Column("snapshot_repo", sa.Text()))
    op.add_column("external_code_attempts", sa.Column("snapshot_branch", sa.Text()))
    op.add_column("external_code_attempts", sa.Column("snapshot_revision", sa.Text()))
    op.add_column("external_code_attempts", sa.Column("snapshot_tree", sa.Text()))
    op.add_column("external_code_attempts", sa.Column("snapshot_path", sa.Text()))
    op.add_column("external_code_attempts", sa.Column("snapshot_sha256", sa.Text()))
    op.add_column("external_code_attempts", sa.Column("snapshot_byte_size", sa.Integer()))
    op.add_column("external_code_attempts", sa.Column("snapshot_request_sha256", sa.Text()))
    op.add_column("external_code_attempts", sa.Column("snapshot_receipt_sha256", sa.Text()))
    op.create_foreign_key(
        "external_code_snapshot_scope_fk",
        "external_code_attempts",
        "github_fixture_source_snapshots",
        ["project_id", "snapshot_attempt_id"],
        ["project_id", "attempt_id"],
    )
    op.create_unique_constraint(
        "external_code_idempotency_unique",
        "external_code_attempts",
        ["project_id", "idempotency_key"],
    )
    op.drop_constraint("external_code_fixture_valid", "external_code_attempts", type_="check")
    op.drop_constraint("external_code_adapter_valid", "external_code_attempts", type_="check")
    op.drop_constraint("external_code_source_snapshot_valid", "external_code_attempts", type_="check")
    op.create_check_constraint(
        "external_code_fixture_valid",
        "external_code_attempts",
        "fixture_id IN ('engineer_qa_v1','local_source_probe_v1','github_snapshot_codex_fixture_v1')",
    )
    op.create_check_constraint(
        "external_code_adapter_valid",
        "external_code_attempts",
        "(fixture_id='engineer_qa_v1' AND adapter_id IN ('codex_cli','claude_code')) OR "
        "(fixture_id='local_source_probe_v1' AND adapter_id='offline_probe') OR "
        "(fixture_id='github_snapshot_codex_fixture_v1' AND adapter_id='codex_cli')",
    )
    op.create_check_constraint(
        "external_code_source_snapshot_valid",
        "external_code_attempts",
        "(fixture_id='engineer_qa_v1' AND source_id IS NULL AND source_root_id IS NULL "
        "AND source_relative_path IS NULL AND source_branch IS NULL AND source_revision IS NULL "
        "AND snapshot_attempt_id IS NULL AND idempotency_key IS NULL AND intent_sha256 IS NULL "
        "AND snapshot_source_id IS NULL AND snapshot_account_id IS NULL "
        "AND snapshot_owner IS NULL AND snapshot_repo IS NULL AND snapshot_branch IS NULL "
        "AND snapshot_revision IS NULL AND snapshot_tree IS NULL AND snapshot_path IS NULL "
        "AND snapshot_sha256 IS NULL AND snapshot_byte_size IS NULL "
        "AND snapshot_request_sha256 IS NULL "
        "AND snapshot_receipt_sha256 IS NULL) OR "
        "(fixture_id='local_source_probe_v1' AND source_id IS NOT NULL "
        "AND source_root_id='workspace' AND source_relative_path IS NOT NULL "
        "AND source_branch IS NOT NULL AND source_revision ~ '^[0-9a-f]{40}$' "
        "AND snapshot_attempt_id IS NULL AND idempotency_key IS NULL AND intent_sha256 IS NULL "
        "AND snapshot_source_id IS NULL AND snapshot_account_id IS NULL "
        "AND snapshot_owner IS NULL AND snapshot_repo IS NULL AND snapshot_branch IS NULL "
        "AND snapshot_revision IS NULL AND snapshot_tree IS NULL AND snapshot_path IS NULL "
        "AND snapshot_sha256 IS NULL AND snapshot_byte_size IS NULL "
        "AND snapshot_request_sha256 IS NULL "
        "AND snapshot_receipt_sha256 IS NULL) OR "
        "(fixture_id='github_snapshot_codex_fixture_v1' AND source_id IS NULL "
        "AND source_root_id IS NULL AND source_relative_path IS NULL "
        "AND source_branch IS NULL AND source_revision IS NULL "
        "AND snapshot_attempt_id IS NOT NULL AND idempotency_key IS NOT NULL "
        "AND intent_sha256 IS NOT NULL AND intent_sha256 ~ '^[0-9a-f]{64}$' "
        "AND snapshot_source_id IS NOT NULL AND snapshot_account_id IS NOT NULL "
        "AND snapshot_owner IS NOT NULL AND snapshot_repo IS NOT NULL AND snapshot_branch IS NOT NULL "
        "AND snapshot_revision IS NOT NULL AND snapshot_revision ~ '^[0-9a-f]{40}$' "
        "AND snapshot_tree IS NOT NULL AND snapshot_tree ~ '^[0-9a-f]{40}$' "
        "AND snapshot_sha256 IS NOT NULL AND snapshot_sha256 ~ '^[0-9a-f]{64}$' "
        "AND snapshot_path IS NOT NULL "
        "AND snapshot_path = 'snapshots/' || snapshot_sha256 || '.json' "
        "AND snapshot_byte_size IS NOT NULL AND snapshot_byte_size BETWEEN 1 AND 16777216 "
        "AND snapshot_request_sha256 IS NOT NULL "
        "AND snapshot_request_sha256 ~ '^[0-9a-f]{64}$' "
        "AND snapshot_receipt_sha256 IS NOT NULL "
        "AND snapshot_receipt_sha256 ~ '^[0-9a-f]{64}$')",
    )
    op.execute("""
        CREATE OR REPLACE FUNCTION protect_external_code_attempt() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF ROW(NEW.id, NEW.project_id, NEW.run_id, NEW.actor_id, NEW.adapter_id,
                 NEW.fixture_id, NEW.request_sha256, NEW.created_at,
                 NEW.source_id, NEW.source_root_id, NEW.source_relative_path,
                 NEW.source_branch, NEW.source_revision, NEW.snapshot_attempt_id,
                 NEW.idempotency_key, NEW.intent_sha256, NEW.snapshot_source_id, NEW.snapshot_account_id,
                 NEW.snapshot_owner, NEW.snapshot_repo, NEW.snapshot_branch,
                 NEW.snapshot_revision, NEW.snapshot_tree, NEW.snapshot_path,
                 NEW.snapshot_sha256, NEW.snapshot_byte_size, NEW.snapshot_request_sha256,
                 NEW.snapshot_receipt_sha256)
             IS DISTINCT FROM
             ROW(OLD.id, OLD.project_id, OLD.run_id, OLD.actor_id, OLD.adapter_id,
                 OLD.fixture_id, OLD.request_sha256, OLD.created_at,
                 OLD.source_id, OLD.source_root_id, OLD.source_relative_path,
                 OLD.source_branch, OLD.source_revision, OLD.snapshot_attempt_id,
                 OLD.idempotency_key, OLD.intent_sha256, OLD.snapshot_source_id, OLD.snapshot_account_id,
                 OLD.snapshot_owner, OLD.snapshot_repo, OLD.snapshot_branch,
                 OLD.snapshot_revision, OLD.snapshot_tree, OLD.snapshot_path,
                 OLD.snapshot_sha256, OLD.snapshot_byte_size, OLD.snapshot_request_sha256,
                 OLD.snapshot_receipt_sha256) THEN
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
    op.drop_constraint("external_code_source_snapshot_valid", "external_code_attempts", type_="check")
    op.drop_constraint("external_code_adapter_valid", "external_code_attempts", type_="check")
    op.drop_constraint("external_code_fixture_valid", "external_code_attempts", type_="check")
    op.create_check_constraint(
        "external_code_fixture_valid",
        "external_code_attempts",
        "fixture_id IN ('engineer_qa_v1','local_source_probe_v1')",
    )
    op.create_check_constraint(
        "external_code_adapter_valid",
        "external_code_attempts",
        "(fixture_id='engineer_qa_v1' AND adapter_id IN ('codex_cli','claude_code')) OR "
        "(fixture_id='local_source_probe_v1' AND adapter_id='offline_probe')",
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
    op.drop_constraint("external_code_idempotency_unique", "external_code_attempts", type_="unique")
    op.drop_constraint("external_code_snapshot_scope_fk", "external_code_attempts", type_="foreignkey")
    for name in (
        "snapshot_receipt_sha256",
        "snapshot_request_sha256",
        "snapshot_byte_size",
        "snapshot_sha256",
        "snapshot_path",
        "snapshot_tree",
        "snapshot_revision",
        "snapshot_account_id",
        "snapshot_source_id",
        "snapshot_branch",
        "snapshot_repo",
        "snapshot_owner",
    ):
        op.drop_column("external_code_attempts", name)
    op.drop_column("external_code_attempts", "idempotency_key")
    op.drop_column("external_code_attempts", "intent_sha256")
    op.drop_column("external_code_attempts", "snapshot_attempt_id")
    op.drop_constraint(
        "github_fixture_snapshot_scope_unique", "github_fixture_source_snapshots", type_="unique"
    )
