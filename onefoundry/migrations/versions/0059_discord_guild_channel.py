"""Bind an optional Discord server to the existing project channel."""

import sqlalchemy as sa
from alembic import op

revision = "0059_discord_guild_channel"
down_revision = "0058_project_chief_inbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("remote_channels", sa.Column("guild_id", sa.Text(), nullable=True))
    op.create_check_constraint(
        "remote_channels_discord_guild_only",
        "remote_channels",
        "guild_id IS NULL OR platform = 'discord'",
    )


def downgrade() -> None:
    op.drop_constraint("remote_channels_discord_guild_only", "remote_channels", type_="check")
    op.drop_column("remote_channels", "guild_id")
