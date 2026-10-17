"""Project-scoped Zoho setup and exact approval-bound mailbox writes."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0082_zoho_mail"
down_revision = "0081_repository_code_target"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "zoho_mail_connections",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("project_id", uuid, sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="needs_credentials"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("mail_account_id", sa.Text()),
        sa.Column("from_address", sa.Text()),
        sa.Column("last_error_code", sa.Text()),
        sa.Column("last_probe_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("project_id", "id"),
        sa.UniqueConstraint("project_id", "label"),
        sa.CheckConstraint("status IN ('needs_credentials','unverified','ready','failed')"),
        sa.CheckConstraint(
            "NOT enabled OR (status='ready' AND mail_account_id IS NOT NULL AND from_address IS NOT NULL)"
        ),
        sa.CheckConstraint("version >= 1 AND length(label) BETWEEN 1 AND 80"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'"),
    )
    op.create_table(
        "zoho_mail_actions",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("project_id", uuid, sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("connection_id", uuid, nullable=False),
        sa.Column("connection_version", sa.Integer(), nullable=False),
        sa.Column("work_order_id", uuid, nullable=False),
        sa.Column("run_id", uuid, nullable=False),
        sa.Column("approval_id", uuid, nullable=False),
        sa.Column("proposal", postgresql.JSONB(), nullable=False),
        sa.Column("proposal_digest", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("error_code", sa.Text()),
        sa.Column("message_id", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("project_id", "approval_id"),
        sa.ForeignKeyConstraint(
            ["project_id", "connection_id"], ["zoho_mail_connections.project_id", "zoho_mail_connections.id"]
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "work_order_id"], ["work_orders.project_id", "work_orders.id"]
        ),
        sa.ForeignKeyConstraint(["project_id", "run_id"], ["runs.project_id", "runs.id"]),
        sa.ForeignKeyConstraint(
            ["project_id", "approval_id"], ["approval_requests.project_id", "approval_requests.id"]
        ),
        sa.CheckConstraint(
            "state IN ('pending','approved','rejected','dispatching','accepted','failed','unknown')"
        ),
        sa.CheckConstraint("proposal_digest ~ '^[0-9a-f]{64}$' AND octet_length(proposal::text) <= 16000"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'"),
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS(SELECT 1 FROM zoho_mail_connections)")):
        raise RuntimeError("Zoho connections exist; restore a checkpoint to downgrade")
    op.drop_table("zoho_mail_actions")
    op.drop_table("zoho_mail_connections")
