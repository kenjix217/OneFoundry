"""Allow protected Grok subscription account credentials."""

import sqlalchemy as sa
from alembic import op

revision = "0020_grok_account"
down_revision = "0019_codex_account"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("model_accounts_transport_valid", "model_accounts", type_="check")
    op.create_check_constraint(
        "model_accounts_transport_valid",
        "model_accounts",
        "(provider = 'ollama' AND auth_kind = 'none' AND endpoint IS NOT NULL "
        "AND credential_ready = false) OR "
        "(provider IN ('openai_codex_runtime','xai') AND auth_kind = 'account_runtime' "
        "AND endpoint IS NULL) OR "
        "(provider NOT IN ('ollama','openai_codex_runtime','xai') "
        "AND auth_kind = 'api_key' AND endpoint IS NULL)",
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM model_accounts WHERE provider='xai'")):
        raise RuntimeError("remove Grok subscription account connections before downgrading")
    op.drop_constraint("model_accounts_transport_valid", "model_accounts", type_="check")
    op.create_check_constraint(
        "model_accounts_transport_valid",
        "model_accounts",
        "(provider = 'ollama' AND auth_kind = 'none' AND endpoint IS NOT NULL "
        "AND credential_ready = false) OR "
        "(provider = 'openai_codex_runtime' AND auth_kind = 'account_runtime' AND endpoint IS NULL) OR "
        "(provider NOT IN ('ollama','openai_codex_runtime') AND auth_kind = 'api_key' AND endpoint IS NULL)",
    )
