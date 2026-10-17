"""Project-scoped provider accounts, catalog, and profile binding."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012_model_gateway"
down_revision = "0011_run_audit_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "model_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("catalog_valid", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("credential_revision", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("credential_ready", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("refresh_generation", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("project_id", "id"),
        sa.CheckConstraint(
            "provider IN ('openai', 'anthropic', 'gemini')", name="model_accounts_provider_valid"
        ),
        sa.CheckConstraint("label ~ '[^[:space:]]'", name="model_accounts_label_nonblank"),
    )
    op.create_table(
        "model_catalog",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id", "account_id"],
            ["model_accounts.project_id", "model_accounts.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("project_id", "account_id", "model_id"),
    )
    op.add_column("profiles", sa.Column("model_account_id", postgresql.UUID(as_uuid=True)))
    op.create_foreign_key(
        "profiles_model_account_scope",
        "profiles",
        "model_accounts",
        ["project_id", "model_account_id"],
        ["project_id", "id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("profiles_model_account_scope", "profiles", type_="foreignkey")
    op.drop_column("profiles", "model_account_id")
    op.drop_table("model_catalog")
    op.drop_table("model_accounts")
