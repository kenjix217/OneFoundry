"""Record reversible project lifecycle transitions."""

import sqlalchemy as sa
from alembic import op

revision = "0017_project_lifecycle"
down_revision = "0016_skill_library"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("status", sa.Text(), nullable=False, server_default="active"))
    op.add_column("projects", sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("projects", sa.Column("trash_from_status", sa.Text(), nullable=True))
    op.create_check_constraint(
        "projects_status_valid", "projects", "status IN ('active', 'archived', 'trashed')"
    )
    op.create_check_constraint("projects_version_positive", "projects", "version > 0")
    op.create_check_constraint(
        "projects_trash_source_valid",
        "projects",
        "(status = 'trashed' AND trash_from_status IN ('active', 'archived')) OR "
        "(status <> 'trashed' AND trash_from_status IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("projects_trash_source_valid", "projects", type_="check")
    op.drop_constraint("projects_version_positive", "projects", type_="check")
    op.drop_constraint("projects_status_valid", "projects", type_="check")
    op.drop_column("projects", "trash_from_status")
    op.drop_column("projects", "version")
    op.drop_column("projects", "status")
