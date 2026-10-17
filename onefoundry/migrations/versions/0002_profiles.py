"""Project role profiles.

Revision ID: 0002_profiles
Revises: 0001_base
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_profiles"
down_revision = "0001_base"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("memory_scope", sa.Text(), nullable=False),
        sa.Column("allowed_tools", postgresql.JSONB(), nullable=False),
        sa.Column("autonomy_level", sa.Text(), nullable=False),
        sa.Column("configured", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.CheckConstraint("name ~ '[^[:space:]]'", name="profiles_name_nonblank"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="profiles_actor_nonblank"),
        sa.CheckConstraint("kind IN ('builtin', 'custom')", name="profiles_kind_valid"),
        sa.CheckConstraint("memory_scope IN ('company', 'project')", name="profiles_scope_valid"),
        sa.CheckConstraint(
            "autonomy_level IN ('manual', 'supervised', 'bounded')",
            name="profiles_autonomy_valid",
        ),
        sa.CheckConstraint("jsonb_typeof(allowed_tools) = 'array'", name="profiles_tools_array"),
        sa.CheckConstraint(
            "configured = (coalesce(model ~ '[^[:space:]]', false) AND instructions ~ '[^[:space:]]')",
            name="profiles_configuration_valid",
        ),
    )
    op.create_index(
        "profiles_project_name_unique", "profiles", ["project_id", sa.text("lower(name)")], unique=True
    )


def downgrade() -> None:
    op.drop_index("profiles_project_name_unique", table_name="profiles")
    op.drop_table("profiles")
