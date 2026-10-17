"""Private operator-approved static browser fixture owner."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0032_browser_static_fixtures"
down_revision = "0031_workspace_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "browser_static_fixtures",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("idempotency_key", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approved_url", sa.Text(), nullable=False),
        sa.Column("html", postgresql.BYTEA(), nullable=False),
        sa.Column("css", postgresql.BYTEA(), nullable=False),
        sa.Column("source_sha256", sa.Text(), nullable=False),
        sa.Column("proposal_digest", sa.Text(), nullable=False),
        sa.Column("viewport_width", sa.Integer(), nullable=False),
        sa.Column("viewport_height", sa.Integer(), nullable=False),
        sa.Column("max_page_bytes", sa.Integer(), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("lifetime_seconds", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id", "run_id"], ["runs.project_id", "runs.id"]),
        sa.ForeignKeyConstraint(
            ["project_id", "run_id", "approval_id"],
            ["approval_requests.project_id", "approval_requests.run_id", "approval_requests.id"],
        ),
        sa.UniqueConstraint("project_id", "approval_id", name="browser_static_fixture_approval_unique"),
        sa.UniqueConstraint("project_id", "run_id", "id", name="browser_static_fixture_scope_unique"),
        sa.UniqueConstraint(
            "project_id", "run_id", "idempotency_key", name="browser_static_fixture_idempotency_unique"
        ),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="browser_static_fixture_actor_nonblank"),
        sa.CheckConstraint(
            "length(approved_url) BETWEEN 1 AND 2048", name="browser_static_fixture_url_bounded"
        ),
        sa.CheckConstraint("source_sha256 ~ '^[0-9a-f]{64}$'", name="browser_static_fixture_source_hash"),
        sa.CheckConstraint("proposal_digest ~ '^[0-9a-f]{64}$'", name="browser_static_fixture_proposal_hash"),
        sa.CheckConstraint(
            "octet_length(html) > 0 AND octet_length(html)+octet_length(css)+15 <= max_page_bytes "
            "AND max_page_bytes BETWEEN 1 AND 262144",
            name="browser_static_fixture_content_bounded",
        ),
        sa.CheckConstraint("viewport_width BETWEEN 1 AND 1920", name="browser_static_fixture_width"),
        sa.CheckConstraint("viewport_height BETWEEN 1 AND 1200", name="browser_static_fixture_height"),
        sa.CheckConstraint("timeout_seconds BETWEEN 1 AND 30", name="browser_static_fixture_timeout"),
        sa.CheckConstraint("lifetime_seconds BETWEEN 1 AND 300", name="browser_static_fixture_lifetime"),
    )
    op.execute("""
        CREATE FUNCTION protect_browser_static_fixture() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION 'browser fixture proposal is immutable';
        END $$
    """)
    op.execute(
        "CREATE TRIGGER browser_static_fixture_protect BEFORE UPDATE ON browser_static_fixtures "
        "FOR EACH ROW EXECUTE FUNCTION protect_browser_static_fixture()"
    )
    op.execute(
        "CREATE TRIGGER browser_static_fixture_no_delete BEFORE DELETE ON browser_static_fixtures "
        "FOR EACH ROW EXECUTE FUNCTION reject_event_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER browser_static_fixture_no_delete ON browser_static_fixtures")
    op.execute("DROP TRIGGER browser_static_fixture_protect ON browser_static_fixtures")
    op.execute("DROP FUNCTION protect_browser_static_fixture()")
    op.drop_table("browser_static_fixtures")
