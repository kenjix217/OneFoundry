"""Persist project-local connection choices for package connection slots."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0087_extension_bindings"
down_revision = "0086_extension_invocations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.add_column("extension_drafts", sa.Column("target_skill_id", uuid))
    op.add_column("extension_drafts", sa.Column("expected_base_version", sa.Integer()))
    op.create_foreign_key(
        "extension_drafts_target_skill_fk",
        "extension_drafts",
        "skills",
        ["project_id", "target_skill_id"],
        ["project_id", "id"],
    )
    op.create_check_constraint(
        "extension_drafts_base_pair",
        "extension_drafts",
        "(target_skill_id IS NULL AND expected_base_version IS NULL) OR "
        "(target_skill_id IS NOT NULL AND expected_base_version >= 1)",
    )
    op.create_table(
        "extension_connection_bindings",
        sa.Column("project_id", uuid, nullable=False),
        sa.Column("package_id", uuid, nullable=False),
        sa.Column("slot", sa.String(64), nullable=False),
        sa.Column("connection_id", uuid, nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(
            ["project_id", "package_id"], ["extension_packages.project_id", "extension_packages.id"]
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "connection_id"],
            ["extension_connections.project_id", "extension_connections.id"],
        ),
        sa.PrimaryKeyConstraint("project_id", "package_id", "slot"),
        sa.CheckConstraint("slot ~ '^[a-z][a-z0-9_]{0,63}$'"),
        sa.CheckConstraint("version >= 1"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'"),
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM extension_connection_bindings) OR "
            "EXISTS(SELECT 1 FROM extension_drafts WHERE target_skill_id IS NOT NULL)"
        )
    ):
        raise RuntimeError("extension binding or skill revisions exist; restore a checkpoint to downgrade")
    op.drop_table("extension_connection_bindings")
    op.drop_constraint("extension_drafts_base_pair", "extension_drafts", type_="check")
    op.drop_constraint("extension_drafts_target_skill_fk", "extension_drafts", type_="foreignkey")
    op.drop_column("extension_drafts", "expected_base_version")
    op.drop_column("extension_drafts", "target_skill_id")
