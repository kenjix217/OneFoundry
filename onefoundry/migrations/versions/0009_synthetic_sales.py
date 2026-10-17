"""Local Sales draft and one-shot Mailpit delivery binding."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_synthetic_sales"
down_revision = "0008_synthetic_code"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sales_drafts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("source_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("recipient", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("body_sha256", sa.Text(), nullable=False),
        sa.Column("delivery_status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["project_id", "source_run_id"], ["runs.project_id", "runs.id"]),
        sa.ForeignKeyConstraint(["project_id", "run_id"], ["runs.project_id", "runs.id"]),
        sa.ForeignKeyConstraint(
            ["project_id", "run_id", "approval_id"],
            ["approval_requests.project_id", "approval_requests.run_id", "approval_requests.id"],
        ),
        sa.UniqueConstraint("project_id", "source_run_id", name="sales_drafts_source_unique"),
        sa.UniqueConstraint("project_id", "run_id", name="sales_drafts_run_unique"),
        sa.UniqueConstraint("project_id", "approval_id", name="sales_drafts_approval_unique"),
        sa.CheckConstraint("source_run_id = run_id", name="sales_drafts_same_run"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="sales_drafts_actor_nonblank"),
        sa.CheckConstraint(
            "recipient = 'sales-preview@agentpod.invalid'", name="sales_drafts_recipient_fixed"
        ),
        sa.CheckConstraint("octet_length(subject) BETWEEN 1 AND 200", name="sales_drafts_subject_bounded"),
        sa.CheckConstraint("octet_length(body) BETWEEN 1 AND 16000", name="sales_drafts_body_bounded"),
        sa.CheckConstraint("body_sha256 ~ '^[0-9a-f]{64}$'", name="sales_drafts_digest_valid"),
        sa.CheckConstraint(
            "delivery_status IN ('pending', 'rejected', 'unknown', 'delivered')",
            name="sales_drafts_delivery_valid",
        ),
    )


def downgrade() -> None:
    op.drop_table("sales_drafts")
