"""Immutable, fake-only GitHub source snapshot evidence."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0026_github_fixture_snapshots"
down_revision = "0024_source_bound_attempt"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "github_fixture_source_snapshots",
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("saved_revision", sa.Text(), nullable=False),
        sa.Column("source_tree", sa.Text(), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("total_bytes", sa.Integer(), nullable=False),
        sa.Column("snapshot_path", sa.Text(), nullable=False),
        sa.Column("snapshot_sha256", sa.Text(), nullable=False),
        sa.Column("snapshot_byte_size", sa.Integer(), nullable=False),
        sa.Column("request_sha256", sa.Text(), nullable=False),
        sa.Column("receipt_sha256", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.UniqueConstraint("project_id", "approval_id", name="github_fixture_snapshot_approval_unique"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="github_fixture_snapshot_actor_valid"),
        sa.CheckConstraint(
            "saved_revision ~ '^[0-9a-f]{40}$'", name="github_fixture_snapshot_revision_valid"
        ),
        sa.CheckConstraint("source_tree ~ '^[0-9a-f]{40}$'", name="github_fixture_snapshot_tree_valid"),
        sa.CheckConstraint("snapshot_sha256 ~ '^[0-9a-f]{64}$'", name="github_fixture_snapshot_sha_valid"),
        sa.CheckConstraint("request_sha256 ~ '^[0-9a-f]{64}$'", name="github_fixture_snapshot_request_valid"),
        sa.CheckConstraint("receipt_sha256 ~ '^[0-9a-f]{64}$'", name="github_fixture_snapshot_receipt_valid"),
        sa.CheckConstraint(
            "snapshot_path = 'snapshots/' || snapshot_sha256 || '.json'",
            name="github_fixture_snapshot_path_valid",
        ),
        sa.CheckConstraint("file_count BETWEEN 0 AND 1000", name="github_fixture_snapshot_files_valid"),
        sa.CheckConstraint("total_bytes BETWEEN 0 AND 8388608", name="github_fixture_snapshot_bytes_valid"),
        sa.CheckConstraint(
            "snapshot_byte_size BETWEEN 1 AND 16777216", name="github_fixture_snapshot_bundle_valid"
        ),
    )
    op.execute(
        "CREATE TRIGGER github_fixture_snapshot_immutable BEFORE UPDATE OR DELETE "
        "ON github_fixture_source_snapshots FOR EACH ROW EXECUTE FUNCTION reject_event_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER github_fixture_snapshot_immutable ON github_fixture_source_snapshots")
    op.drop_table("github_fixture_source_snapshots")
