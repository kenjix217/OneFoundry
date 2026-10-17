"""Pin the saved package-slot binding generation in run tool snapshots."""

import sqlalchemy as sa
from alembic import op

revision = "0091_extension_binding_pins"
down_revision = "0090_skill_prerequisites"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "extension_run_tools",
        sa.Column("binding_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("extension_run_tools", sa.Column("worker_image", sa.String(71)))
    op.create_check_constraint(
        "extension_run_tools_binding_version_nonnegative",
        "extension_run_tools",
        "binding_version >= 0",
    )
    op.create_check_constraint(
        "extension_run_tools_worker_image_digest",
        "extension_run_tools",
        "worker_image IS NULL OR worker_image ~ '^sha256:[0-9a-f]{64}$'",
    )


def downgrade() -> None:
    op.drop_constraint("extension_run_tools_worker_image_digest", "extension_run_tools")
    op.drop_constraint("extension_run_tools_binding_version_nonnegative", "extension_run_tools")
    op.drop_column("extension_run_tools", "worker_image")
    op.drop_column("extension_run_tools", "binding_version")
