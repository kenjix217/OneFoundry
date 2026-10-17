"""Bounded synthetic coding jobs, artifacts, and exact merge approvals."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_synthetic_code"
down_revision = "0007_synthetic_text"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "approval_requests_project_id_id_unique", "approval_requests", ["project_id", "id"]
    )
    op.create_unique_constraint(
        "approval_requests_project_run_id_unique", "approval_requests", ["project_id", "run_id", "id"]
    )
    op.drop_constraint("runs_mode_valid", "runs", type_="check")
    op.create_check_constraint(
        "runs_mode_valid", "runs", "mode IN ('manual_planning', 'synthetic_text', 'synthetic_code')"
    )
    op.create_table(
        "runner_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=True),
        sa.Column("source_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("branch", sa.Text(), nullable=True),
        sa.Column("commit", sa.Text(), nullable=True),
        sa.Column("baseline_commit", sa.Text(), nullable=True),
        sa.Column("merged_commit", sa.Text(), nullable=True),
        sa.Column("observed_head_commit", sa.Text(), nullable=True),
        sa.Column("diff_sha256", sa.Text(), nullable=True),
        sa.Column("tests", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("receipt_sha256", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["project_id", "run_id"], ["runs.project_id", "runs.id"]),
        sa.ForeignKeyConstraint(
            ["project_id", "run_id", "source_job_id"],
            ["runner_jobs.project_id", "runner_jobs.run_id", "runner_jobs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "run_id", "approval_id"],
            ["approval_requests.project_id", "approval_requests.run_id", "approval_requests.id"],
        ),
        sa.UniqueConstraint("project_id", "id", name="runner_jobs_project_id_id_unique"),
        sa.UniqueConstraint("project_id", "run_id", "id", name="runner_jobs_project_run_id_unique"),
        sa.UniqueConstraint("project_id", "run_id", "stage", name="runner_jobs_run_stage_unique"),
        sa.CheckConstraint("stage IN ('engineer','qa','merge')", name="runner_jobs_stage_valid"),
        sa.CheckConstraint("status IN ('queued','done','failed','unknown')", name="runner_jobs_status_valid"),
        sa.CheckConstraint(
            "diff_sha256 IS NULL OR diff_sha256 ~ '^[0-9a-f]{64}$'", name="runner_jobs_diff_digest_valid"
        ),
        sa.CheckConstraint(
            "receipt_sha256 IS NULL OR receipt_sha256 ~ '^[0-9a-f]{64}$'",
            name="runner_jobs_receipt_digest_valid",
        ),
    )
    op.create_table(
        "runner_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["project_id", "run_id"], ["runs.project_id", "runs.id"]),
        sa.ForeignKeyConstraint(
            ["project_id", "run_id", "job_id"],
            ["runner_jobs.project_id", "runner_jobs.run_id", "runner_jobs.id"],
        ),
        sa.UniqueConstraint("project_id", "job_id", "kind", name="runner_artifacts_job_kind_unique"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="runner_artifacts_digest_valid"),
        sa.CheckConstraint("byte_size >= 0 AND byte_size <= 65536", name="runner_artifacts_size_valid"),
    )
    op.create_table(
        "synthetic_merge_bindings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approval_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("branch", sa.Text(), nullable=False),
        sa.Column("commit", sa.Text(), nullable=False),
        sa.Column("baseline_commit", sa.Text(), nullable=False),
        sa.Column("diff_sha256", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["project_id", "run_id"], ["runs.project_id", "runs.id"]),
        sa.ForeignKeyConstraint(
            ["project_id", "run_id", "approval_id"],
            ["approval_requests.project_id", "approval_requests.run_id", "approval_requests.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "run_id", "source_job_id"],
            ["runner_jobs.project_id", "runner_jobs.run_id", "runner_jobs.id"],
        ),
        sa.UniqueConstraint("project_id", "run_id", name="synthetic_merge_run_unique"),
        sa.UniqueConstraint("project_id", "approval_id", name="synthetic_merge_approval_unique"),
        sa.CheckConstraint("diff_sha256 ~ '^[0-9a-f]{64}$'", name="synthetic_merge_diff_digest_valid"),
    )


def downgrade() -> None:
    op.drop_table("synthetic_merge_bindings")
    op.drop_table("runner_artifacts")
    op.drop_table("runner_jobs")
    op.drop_constraint("approval_requests_project_run_id_unique", "approval_requests", type_="unique")
    op.drop_constraint("approval_requests_project_id_id_unique", "approval_requests", type_="unique")
    op.drop_constraint("runs_mode_valid", "runs", type_="check")
    op.create_check_constraint("runs_mode_valid", "runs", "mode IN ('manual_planning', 'synthetic_text')")
