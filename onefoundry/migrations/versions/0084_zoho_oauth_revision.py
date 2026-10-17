"""Bind protected OAuth changes independently of approved mailbox identity."""

import sqlalchemy as sa
from alembic import op

revision = "0084_zoho_oauth_revision"
down_revision = "0083_capability_setup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "zoho_mail_connections",
        sa.Column("credential_revision", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_check_constraint(
        "zoho_credential_revision_positive", "zoho_mail_connections", "credential_revision >= 1"
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT EXISTS(SELECT 1 FROM zoho_mail_connections)")):
        raise RuntimeError("Zoho connections exist; restore a checkpoint to downgrade")
    op.drop_constraint("zoho_credential_revision_positive", "zoho_mail_connections", type_="check")
    op.drop_column("zoho_mail_connections", "credential_revision")
