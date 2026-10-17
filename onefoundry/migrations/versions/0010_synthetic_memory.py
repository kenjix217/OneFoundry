"""Exact synthetic Sales and QA source links for reviewed memory intake."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_synthetic_memory"
down_revision = "0009_synthetic_sales"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("run_results", sa.Column("source_sales_draft_id", postgresql.UUID(as_uuid=True)))
    op.add_column("run_results", sa.Column("source_runner_job_id", postgresql.UUID(as_uuid=True)))
    op.add_column("run_results", sa.Column("source_sha256", sa.Text()))
    op.create_foreign_key(
        "run_results_sales_draft_fk", "run_results", "sales_drafts", ["source_sales_draft_id"], ["id"]
    )
    op.create_foreign_key(
        "run_results_qa_job_fk",
        "run_results",
        "runner_jobs",
        ["project_id", "run_id", "source_runner_job_id"],
        ["project_id", "run_id", "id"],
    )
    op.drop_constraint("run_results_source_kind_valid", "run_results", type_="check")
    op.create_check_constraint(
        "run_results_source_kind_valid",
        "run_results",
        "(source_kind = 'operator_entered' AND source_sales_draft_id IS NULL "
        "AND source_runner_job_id IS NULL AND source_sha256 IS NULL) OR "
        "(source_kind = 'synthetic_sales' AND source_sales_draft_id IS NOT NULL "
        "AND source_runner_job_id IS NULL AND source_sha256 IS NOT NULL "
        "AND source_sha256 ~ '^[0-9a-f]{64}$') OR "
        "(source_kind = 'synthetic_qa' AND source_sales_draft_id IS NULL "
        "AND source_runner_job_id IS NOT NULL AND source_sha256 IS NOT NULL "
        "AND source_sha256 ~ '^[0-9a-f]{64}$')",
    )
    op.execute(
        """
        CREATE FUNCTION validate_synthetic_memory_result() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.source_kind = 'synthetic_sales' AND NOT EXISTS (
            SELECT 1 FROM sales_drafts d
            JOIN runs r ON r.project_id = d.project_id AND r.id = d.run_id
            JOIN approval_requests a ON a.project_id = d.project_id AND a.id = d.approval_id
            WHERE d.project_id = NEW.project_id AND d.run_id = NEW.run_id
              AND d.source_run_id = NEW.run_id AND d.id = NEW.source_sales_draft_id
              AND d.delivery_status = 'delivered' AND d.sent_at IS NOT NULL
              AND d.body_sha256 = NEW.source_sha256 AND a.status = 'approved'
              AND r.mode = 'synthetic_text' AND r.status = 'done'
              AND EXISTS (
                SELECT 1 FROM text_outputs t WHERE t.project_id = d.project_id
                  AND t.run_id = d.run_id AND t.role = 'GTM'
              )
          ) THEN
            RAISE EXCEPTION 'synthetic Sales result requires delivered draft and exact source'
              USING ERRCODE = '23514';
          ELSIF NEW.source_kind = 'synthetic_qa' AND NOT EXISTS (
            SELECT 1 FROM runner_jobs j
            JOIN runner_jobs e ON e.project_id = j.project_id AND e.run_id = j.run_id
              AND e.id = j.source_job_id
            JOIN runs r ON r.project_id = j.project_id AND r.id = j.run_id
            WHERE j.project_id = NEW.project_id AND j.run_id = NEW.run_id
              AND j.id = NEW.source_runner_job_id AND j.stage = 'qa' AND j.status = 'done'
              AND e.stage = 'engineer' AND e.status = 'done'
              AND j.receipt_sha256 = NEW.source_sha256
              AND j.commit IS NOT NULL AND j.diff_sha256 IS NOT NULL
              AND jsonb_array_length(j.tests) > 0
              AND NOT EXISTS (
                SELECT 1 FROM jsonb_array_elements(j.tests) AS t(value)
                WHERE t.value->>'status' IS DISTINCT FROM 'passed'
                  OR t.value->>'exit_code' IS DISTINCT FROM '0'
              )
              AND r.mode = 'synthetic_code' AND r.status = 'done'
          ) THEN
            RAISE EXCEPTION 'synthetic QA result requires verified completed QA source'
              USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER synthetic_memory_result_guard BEFORE INSERT ON run_results "
        "FOR EACH ROW EXECUTE FUNCTION validate_synthetic_memory_result()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER synthetic_memory_result_guard ON run_results")
    op.execute("DROP FUNCTION validate_synthetic_memory_result()")
    op.drop_constraint("run_results_source_kind_valid", "run_results", type_="check")
    op.create_check_constraint(
        "run_results_source_kind_valid", "run_results", "source_kind = 'operator_entered'"
    )
    op.drop_constraint("run_results_qa_job_fk", "run_results", type_="foreignkey")
    op.drop_constraint("run_results_sales_draft_fk", "run_results", type_="foreignkey")
    op.drop_column("run_results", "source_sha256")
    op.drop_column("run_results", "source_runner_job_id")
    op.drop_column("run_results", "source_sales_draft_id")
