"""Approval-bound document writes: one exact new file, written once and read back."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0051_document_writes"
down_revision = "0050_document_sources"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "document_writes",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("project_id", UUID, sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("source_id", UUID, nullable=False),
        sa.Column("work_order_id", UUID, nullable=False),
        sa.Column("run_id", UUID, nullable=False),
        sa.Column("approval_id", UUID, nullable=False),
        sa.Column("folder_id", sa.Text(), nullable=False),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("error_code", sa.Text()),
        sa.Column("provider_file_id", sa.Text()),
        sa.Column("readback", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("project_id", "approval_id", name="document_write_once"),
        sa.ForeignKeyConstraint(
            ["project_id", "source_id"], ["document_sources.project_id", "document_sources.id"]
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "work_order_id"], ["work_orders.project_id", "work_orders.id"]
        ),
        sa.ForeignKeyConstraint(["project_id", "run_id"], ["runs.project_id", "runs.id"]),
        sa.ForeignKeyConstraint(
            ["project_id", "approval_id"], ["approval_requests.project_id", "approval_requests.id"]
        ),
        sa.CheckConstraint("length(file_name) BETWEEN 1 AND 200", name="document_write_name_valid"),
        sa.CheckConstraint(
            "mime_type IN ('text/plain','text/markdown','text/csv')", name="document_write_mime_valid"
        ),
        sa.CheckConstraint(
            "octet_length(content) BETWEEN 1 AND 65536", name="document_write_content_bounded"
        ),
        sa.CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="document_write_digest_valid"),
        sa.CheckConstraint(
            "state IN ('pending','approved','rejected','dispatching','done','failed','unknown')",
            name="document_write_state_valid",
        ),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="document_write_actor_nonblank"),
    )


def downgrade() -> None:
    op.drop_table("document_writes")
