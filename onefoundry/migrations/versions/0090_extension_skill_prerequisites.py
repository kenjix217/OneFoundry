"""Allow built-in tool prerequisites and pin extension descriptor identities."""

import sqlalchemy as sa
from alembic import op

revision = "0090_skill_prerequisites"
down_revision = "0089_extension_webhooks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("extension_skill_prerequisites", "package_id", nullable=True)
    op.add_column("extension_skill_prerequisites", sa.Column("package_sha256", sa.String(64)))
    op.add_column("extension_skill_prerequisites", sa.Column("descriptor_sha256", sa.String(64)))
    op.create_check_constraint(
        "extension_prerequisite_hash_pair",
        "extension_skill_prerequisites",
        "(package_sha256 IS NULL) = (descriptor_sha256 IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("extension_prerequisite_hash_pair", "extension_skill_prerequisites")
    op.drop_column("extension_skill_prerequisites", "descriptor_sha256")
    op.drop_column("extension_skill_prerequisites", "package_sha256")
    op.alter_column("extension_skill_prerequisites", "package_id", nullable=False)
