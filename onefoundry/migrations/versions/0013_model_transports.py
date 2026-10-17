"""Add xAI and keyless local Ollama model accounts."""

import sqlalchemy as sa
from alembic import op

revision = "0013_model_transports"
down_revision = "0012_model_gateway"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "model_accounts", sa.Column("auth_kind", sa.Text(), nullable=False, server_default="api_key")
    )
    op.add_column("model_accounts", sa.Column("endpoint", sa.Text()))
    op.drop_constraint("model_accounts_provider_valid", "model_accounts", type_="check")
    op.create_check_constraint(
        "model_accounts_provider_valid",
        "model_accounts",
        "provider IN ('openai', 'anthropic', 'gemini', 'grok', 'ollama')",
    )
    op.create_check_constraint(
        "model_accounts_transport_valid",
        "model_accounts",
        "(provider = 'ollama' AND auth_kind = 'none' AND endpoint IS NOT NULL "
        "AND credential_ready = false) OR "
        "(provider <> 'ollama' AND auth_kind = 'api_key' AND endpoint IS NULL)",
    )


def downgrade() -> None:
    count = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM model_accounts WHERE provider IN ('grok', 'ollama')")
    )
    if count:
        raise RuntimeError("remove xAI and Ollama accounts before downgrading")
    op.drop_constraint("model_accounts_transport_valid", "model_accounts", type_="check")
    op.drop_constraint("model_accounts_provider_valid", "model_accounts", type_="check")
    op.create_check_constraint(
        "model_accounts_provider_valid",
        "model_accounts",
        "provider IN ('openai', 'anthropic', 'gemini')",
    )
    op.drop_column("model_accounts", "endpoint")
    op.drop_column("model_accounts", "auth_kind")
