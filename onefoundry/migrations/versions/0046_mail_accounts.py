"""Read-only mailbox connections that turn new mail into coordination signals."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0046_mail_accounts"
down_revision = "0045_coordination_triage"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "mail_accounts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("project_id", UUID, sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("protocol", sa.Text(), nullable=False, server_default="imap"),
        sa.Column("host", sa.Text(), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("username", sa.Text(), nullable=False),
        sa.Column("folder", sa.Text(), nullable=False, server_default="INBOX"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.Text(), nullable=False, server_default="unverified"),
        sa.Column("last_error_code", sa.Text()),
        sa.Column("capabilities", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("uidvalidity", sa.BigInteger()),
        sa.Column("last_uid", sa.BigInteger()),
        sa.Column("last_probe_at", sa.DateTime(timezone=True)),
        sa.Column("last_poll_at", sa.DateTime(timezone=True)),
        sa.Column("poll_lease_until", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("project_id", "id", name="mail_accounts_project_id_id_unique"),
        sa.UniqueConstraint("project_id", "label", name="mail_account_label_unique"),
        sa.CheckConstraint("protocol IN ('imap')", name="mail_account_protocol_valid"),
        sa.CheckConstraint("length(btrim(label)) BETWEEN 1 AND 80", name="mail_account_label_valid"),
        sa.CheckConstraint("host ~ '^[A-Za-z0-9.-]{1,253}$'", name="mail_account_host_valid"),
        sa.CheckConstraint("port BETWEEN 1 AND 65535", name="mail_account_port_valid"),
        sa.CheckConstraint("length(username) BETWEEN 1 AND 320", name="mail_account_username_valid"),
        sa.CheckConstraint("length(folder) BETWEEN 1 AND 200", name="mail_account_folder_valid"),
        sa.CheckConstraint("status IN ('unverified','ready','failed')", name="mail_account_status_valid"),
        sa.CheckConstraint("NOT enabled OR status='ready'", name="mail_account_enabled_requires_ready"),
        sa.CheckConstraint("version >= 1", name="mail_account_version_valid"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="mail_account_actor_nonblank"),
    )
    op.drop_constraint("coordination_signal_source_valid", "coordination_signals", type_="check")
    op.create_check_constraint(
        "coordination_signal_source_valid",
        "coordination_signals",
        "source IN ('internal','schedule','email','webhook','channel','agent_finding','monitor','document')",
    )
    op.drop_constraint("coordination_rule_source_valid", "coordination_rule_versions", type_="check")
    op.create_check_constraint(
        "coordination_rule_source_valid",
        "coordination_rule_versions",
        "source IN ('internal','schedule','email','webhook','channel','agent_finding','monitor','document')",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM coordination_signals WHERE source='document')")):
        raise RuntimeError("document signals exist; downgrade would discard them")
    op.drop_constraint("coordination_rule_source_valid", "coordination_rule_versions", type_="check")
    op.create_check_constraint(
        "coordination_rule_source_valid",
        "coordination_rule_versions",
        "source IN ('internal','schedule','email','webhook','channel','agent_finding','monitor')",
    )
    op.drop_constraint("coordination_signal_source_valid", "coordination_signals", type_="check")
    op.create_check_constraint(
        "coordination_signal_source_valid",
        "coordination_signals",
        "source IN ('internal','schedule','email','webhook','channel','agent_finding','monitor')",
    )
    op.drop_table("mail_accounts")
