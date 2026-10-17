"""Project-scoped SMTP accounts and approval-bound outbound messages."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0052_outbound_smtp"
down_revision = "0051_document_writes"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "smtp_accounts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("project_id", UUID, sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("security", sa.Text(), nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("from_address", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="unverified"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("last_error_code", sa.Text()),
        sa.Column("last_probe_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("project_id", "id", name="smtp_accounts_project_id_id_unique"),
        sa.UniqueConstraint("project_id", "label", name="smtp_accounts_project_label_unique"),
        sa.CheckConstraint("security IN ('ssl','starttls')", name="smtp_account_security_valid"),
        sa.CheckConstraint("port BETWEEN 1 AND 65535", name="smtp_account_port_valid"),
        sa.CheckConstraint("status IN ('unverified','ready','failed')", name="smtp_account_status_valid"),
        sa.CheckConstraint("NOT enabled OR status='ready'", name="smtp_account_enabled_ready"),
        sa.CheckConstraint("version >= 1", name="smtp_account_version_valid"),
    )
    op.create_table(
        "outbound_emails",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("project_id", UUID, sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("account_id", UUID, nullable=False),
        sa.Column("account_version", sa.Integer(), nullable=False),
        sa.Column("work_order_id", UUID, nullable=False),
        sa.Column("run_id", UUID, nullable=False),
        sa.Column("approval_id", UUID, nullable=False),
        sa.Column("to_address", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("reason_code", sa.Text()),
        sa.Column("attempted_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("project_id", "approval_id", name="outbound_email_approval_once"),
        sa.ForeignKeyConstraint(
            ["project_id", "account_id"], ["smtp_accounts.project_id", "smtp_accounts.id"]
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "work_order_id"], ["work_orders.project_id", "work_orders.id"]
        ),
        sa.ForeignKeyConstraint(["project_id", "run_id"], ["runs.project_id", "runs.id"]),
        sa.ForeignKeyConstraint(
            ["project_id", "approval_id"], ["approval_requests.project_id", "approval_requests.id"]
        ),
        sa.CheckConstraint(
            "state IN ('pending','approved','rejected','dispatching','accepted','failed','unknown')",
            name="outbound_email_state_valid",
        ),
        sa.CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="outbound_email_digest_valid"),
        sa.CheckConstraint("account_version >= 1", name="outbound_email_account_version_valid"),
        sa.CheckConstraint("octet_length(body) BETWEEN 1 AND 16000", name="outbound_email_body_bounded"),
    )


def downgrade() -> None:
    op.drop_table("outbound_emails")
    op.drop_table("smtp_accounts")
