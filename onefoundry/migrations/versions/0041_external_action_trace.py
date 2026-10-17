"""Permit a bounded action trace on external coding attempts."""

import sqlalchemy as sa
from alembic import op

revision = "0041_external_action_trace"
down_revision = "0040_work_order_threads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("external_code_artifact_kind_valid", "external_code_artifacts", type_="check")
    op.create_check_constraint(
        "external_code_artifact_kind_valid",
        "external_code_artifacts",
        "kind IN ('diff','log','trace')",
    )


def downgrade() -> None:
    exists = op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM external_code_artifacts WHERE kind='trace')")
    )
    if exists:
        raise RuntimeError("saved action traces exist; downgrade would discard evidence")
    op.drop_constraint("external_code_artifact_kind_valid", "external_code_artifacts", type_="check")
    op.create_check_constraint(
        "external_code_artifact_kind_valid", "external_code_artifacts", "kind IN ('diff','log')"
    )
