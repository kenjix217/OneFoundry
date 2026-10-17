"""Allow validated connector IDs from the bundled provider directory."""

import sqlalchemy as sa
from alembic import op

revision = "0018_provider_directory"
down_revision = "0017_project_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("model_accounts_provider_valid", "model_accounts", type_="check")
    op.create_check_constraint(
        "model_accounts_provider_valid",
        "model_accounts",
        "provider ~ '^[a-z][a-z0-9_]{0,63}$'",
    )


def downgrade() -> None:
    count = op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM model_accounts WHERE provider NOT IN "
            "('openai', 'anthropic', 'gemini', 'grok', 'ollama')"
        )
    )
    if count:
        raise RuntimeError("remove additional provider accounts before downgrading")
    op.drop_constraint("model_accounts_provider_valid", "model_accounts", type_="check")
    op.create_check_constraint(
        "model_accounts_provider_valid",
        "model_accounts",
        "provider IN ('openai', 'anthropic', 'gemini', 'grok', 'ollama')",
    )
