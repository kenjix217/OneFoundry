"""Allow a protected account-runtime credential for Codex."""

import sqlalchemy as sa
from alembic import op

revision = "0019_codex_account"
down_revision = "0018_provider_directory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("model_accounts_transport_valid", "model_accounts", type_="check")
    op.create_check_constraint(
        "model_accounts_transport_valid",
        "model_accounts",
        "(provider = 'ollama' AND auth_kind = 'none' AND endpoint IS NOT NULL "
        "AND credential_ready = false) OR "
        "(provider = 'openai_codex_runtime' AND auth_kind = 'account_runtime' AND endpoint IS NULL) OR "
        "(provider NOT IN ('ollama','openai_codex_runtime') AND auth_kind = 'api_key' AND endpoint IS NULL)",
    )
    for table in ("live_text_run_bindings", "live_text_outputs"):
        op.drop_constraint(f"{table}_provider_valid", table, type_="check")
        op.create_check_constraint(f"{table}_provider_valid", table, "provider ~ '^[a-z][a-z0-9_]{0,63}$'")


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM model_accounts WHERE auth_kind='account_runtime'")):
        raise RuntimeError("remove Codex account connections before downgrading")
    queries = {
        "live_text_run_bindings": sa.text(
            "SELECT count(*) FROM live_text_run_bindings WHERE provider NOT IN ('openai','grok','ollama')"
        ),
        "live_text_outputs": sa.text(
            "SELECT count(*) FROM live_text_outputs WHERE provider NOT IN ('openai','grok','ollama')"
        ),
    }
    for table, query in queries.items():
        if op.get_bind().scalar(query):
            raise RuntimeError("remove added-provider runs before downgrading")
        op.drop_constraint(f"{table}_provider_valid", table, type_="check")
        op.create_check_constraint(f"{table}_provider_valid", table, "provider IN ('openai','grok','ollama')")
    op.drop_constraint("model_accounts_transport_valid", "model_accounts", type_="check")
    op.create_check_constraint(
        "model_accounts_transport_valid",
        "model_accounts",
        "(provider = 'ollama' AND auth_kind = 'none' AND endpoint IS NOT NULL "
        "AND credential_ready = false) OR "
        "(provider <> 'ollama' AND auth_kind = 'api_key' AND endpoint IS NULL)",
    )
