"""Exact approval-bound existing-file, cell, and sharing proposals."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0070_document_actions"
down_revision = "0069_connector_signout"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "document_actions",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("project_id", uuid, sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("source_id", uuid, nullable=False),
        sa.Column("work_order_id", uuid, nullable=False),
        sa.Column("run_id", uuid, nullable=False),
        sa.Column("approval_id", uuid, nullable=False),
        sa.Column("file_id", sa.Text(), nullable=False),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("proposal", postgresql.JSONB(), nullable=False),
        sa.Column("proposal_digest", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("error_code", sa.Text()),
        sa.Column("readback", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("project_id", "approval_id"),
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
        sa.CheckConstraint("operation IN ('replace_file','edit_cells','share_file')"),
        sa.CheckConstraint(
            "state IN ('pending','approved','rejected','dispatching','done','failed','unknown')"
        ),
        sa.CheckConstraint("proposal_digest ~ '^[0-9a-f]{64}$'"),
        sa.CheckConstraint("octet_length(proposal::text) <= 160000"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'"),
    )
    op.create_index("document_action_target", "document_actions", ["project_id", "file_id", "state"])


def downgrade() -> None:
    op.drop_table("document_actions")
