"""Project-scoped repository source bindings and read-only GitHub accounts."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0023_project_sources"
down_revision = "0022_project_brief"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "github_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.UniqueConstraint("project_id", "id", name="github_accounts_scope_unique"),
        sa.CheckConstraint("length(label) BETWEEN 1 AND 120", name="github_accounts_label_valid"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="github_accounts_actor_valid"),
    )
    op.create_table(
        "project_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("root_id", sa.Text()),
        sa.Column("relative_path", sa.Text()),
        sa.Column("account_id", postgresql.UUID(as_uuid=True)),
        sa.Column("owner", sa.Text()),
        sa.Column("repo", sa.Text()),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("revision", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(
            ["project_id", "account_id"], ["github_accounts.project_id", "github_accounts.id"]
        ),
        sa.CheckConstraint(
            "(kind='local' AND root_id IS NOT NULL AND relative_path IS NOT NULL "
            "AND account_id IS NULL AND owner IS NULL AND repo IS NULL) OR "
            "(kind='github' AND root_id IS NULL AND relative_path IS NULL "
            "AND account_id IS NOT NULL AND owner IS NOT NULL AND repo IS NOT NULL)",
            name="project_sources_kind_valid",
        ),
        sa.CheckConstraint("revision ~ '^[0-9a-f]{40}$'", name="project_sources_revision_valid"),
        sa.CheckConstraint("length(branch) BETWEEN 1 AND 200", name="project_sources_branch_valid"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="project_sources_actor_valid"),
    )
    op.create_index("project_sources_project_idx", "project_sources", ["project_id"])


def downgrade() -> None:
    op.drop_index("project_sources_project_idx", table_name="project_sources")
    op.drop_table("project_sources")
    op.drop_table("github_accounts")
