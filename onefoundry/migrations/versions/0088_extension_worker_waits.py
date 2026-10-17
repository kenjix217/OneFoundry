"""Record a known worker approval wait separately from an uncertain effect."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0088_extension_worker_waits"
down_revision = "0087_extension_bindings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "extension_invocations_project_id_run_id_request_sha256_key", "extension_invocations", type_="unique"
    )
    op.drop_constraint("extension_invocations_status_check", "extension_invocations", type_="check")
    op.create_check_constraint(
        "extension_invocations_status_check",
        "extension_invocations",
        "status IN ('approval_required','ready','running','waiting','done','failed','unknown','cancelled')",
    )
    op.add_column("extension_invocations", sa.Column("waiting_invocation_id", postgresql.UUID(as_uuid=True)))
    op.add_column("extension_invocations", sa.Column("worker_resume_from", sa.String(64)))
    op.create_foreign_key(
        "extension_invocations_waiting_fk",
        "extension_invocations",
        "extension_invocations",
        ["project_id", "waiting_invocation_id"],
        ["project_id", "id"],
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM extension_invocations WHERE waiting_invocation_id IS NOT NULL)")
    ):
        raise RuntimeError("worker wait evidence exists; downgrade is unsafe")
    op.drop_constraint("extension_invocations_waiting_fk", "extension_invocations", type_="foreignkey")
    op.drop_column("extension_invocations", "waiting_invocation_id")
    op.drop_column("extension_invocations", "worker_resume_from")
    op.create_unique_constraint(
        "extension_invocations_project_id_run_id_request_sha256_key",
        "extension_invocations",
        ["project_id", "run_id", "request_sha256"],
    )
    op.drop_constraint("extension_invocations_status_check", "extension_invocations", type_="check")
    op.create_check_constraint(
        "extension_invocations_status_check",
        "extension_invocations",
        "status IN ('approval_required','ready','running','done','failed','unknown','cancelled')",
    )
