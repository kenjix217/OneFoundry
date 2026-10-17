"""Microsoft and Google accounts, and mailboxes read through their mail APIs."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0049_connector_accounts"
down_revision = "0048_agent_proposals"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "connector_accounts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("project_id", UUID, sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column("tenant", sa.Text(), nullable=False, server_default="common"),
        sa.Column("features", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("last_error_code", sa.Text()),
        sa.Column("granted_scopes", sa.Text(), nullable=False, server_default=""),
        sa.Column("connected_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("project_id", "id", name="connector_accounts_project_id_id_unique"),
        sa.UniqueConstraint("project_id", "label", name="connector_account_label_unique"),
        sa.CheckConstraint("provider IN ('microsoft','google')", name="connector_account_provider_valid"),
        sa.CheckConstraint("length(btrim(label)) BETWEEN 1 AND 80", name="connector_account_label_valid"),
        sa.CheckConstraint("client_id ~ '^[A-Za-z0-9._-]{8,200}$'", name="connector_account_client_valid"),
        sa.CheckConstraint("tenant ~ '^[A-Za-z0-9.-]{1,100}$'", name="connector_account_tenant_valid"),
        sa.CheckConstraint(
            "jsonb_typeof(features)='array' AND jsonb_array_length(features) <= 3",
            name="connector_account_features_array",
        ),
        sa.CheckConstraint(
            "status IN ('pending','connected','failed')", name="connector_account_status_valid"
        ),
        sa.CheckConstraint("version >= 1", name="connector_account_version_valid"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="connector_account_actor_nonblank"),
    )
    op.drop_constraint("mail_account_protocol_valid", "mail_accounts", type_="check")
    op.create_check_constraint(
        "mail_account_protocol_valid", "mail_accounts", "protocol IN ('imap','gmail','graph')"
    )
    for column in ("host", "port", "username"):
        op.alter_column("mail_accounts", column, nullable=True)
    op.add_column("mail_accounts", sa.Column("connector_account_id", UUID))
    op.add_column("mail_accounts", sa.Column("cursor", sa.Text()))
    op.create_foreign_key(
        "mail_accounts_connector_same_project",
        "mail_accounts",
        "connector_accounts",
        ["project_id", "connector_account_id"],
        ["project_id", "id"],
    )
    op.create_check_constraint(
        "mail_account_protocol_fields",
        "mail_accounts",
        "(protocol='imap' AND host IS NOT NULL AND port IS NOT NULL AND username IS NOT NULL "
        "AND connector_account_id IS NULL) OR "
        "(protocol<>'imap' AND connector_account_id IS NOT NULL)",
    )
    op.create_check_constraint(
        "mail_account_cursor_bounded", "mail_accounts", "cursor IS NULL OR length(cursor) <= 4000"
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS (SELECT 1 FROM mail_accounts WHERE protocol<>'imap')")):
        raise RuntimeError("API mailboxes exist; downgrade would discard them")
    op.drop_constraint("mail_account_cursor_bounded", "mail_accounts", type_="check")
    op.drop_constraint("mail_account_protocol_fields", "mail_accounts", type_="check")
    op.drop_constraint("mail_accounts_connector_same_project", "mail_accounts", type_="foreignkey")
    op.drop_column("mail_accounts", "cursor")
    op.drop_column("mail_accounts", "connector_account_id")
    for column in ("host", "port", "username"):
        op.alter_column("mail_accounts", column, nullable=False)
    op.drop_constraint("mail_account_protocol_valid", "mail_accounts", type_="check")
    op.create_check_constraint("mail_account_protocol_valid", "mail_accounts", "protocol IN ('imap')")
    op.drop_table("connector_accounts")
