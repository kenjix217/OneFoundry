"""Add versioned company facts, pinned context, bounded manual workflow, and cited drafts."""

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0038_onefoundry_manual"
down_revision = "0037_onefoundry_organizations"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def _identity() -> list[Any]:
    return [
        sa.Column("id", UUID, primary_key=True),
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    ]


def _scope(name: str) -> list[sa.Constraint]:
    return [
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.UniqueConstraint("project_id", "id", name=f"{name}_project_id_id_unique"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name=f"{name}_actor_nonblank"),
    ]


def upgrade() -> None:
    op.add_column("onefoundry_workflows", sa.Column("blocked_reason", sa.Text()))
    op.create_check_constraint(
        "onefoundry_workflow_block_reason_valid",
        "onefoundry_workflows",
        "blocked_reason IS NULL OR blocked_reason IN ('budget_exceeded','usage_unknown')",
    )
    op.add_column("onefoundry_work_order_subjects", sa.Column("idempotency_key", UUID))
    op.create_unique_constraint(
        "onefoundry_order_idempotency_unique",
        "onefoundry_work_order_subjects",
        ["project_id", "workflow_id", "idempotency_key"],
    )
    op.create_unique_constraint(
        "onefoundry_order_one_subject", "onefoundry_work_order_subjects", ["project_id", "work_order_id"]
    )
    op.drop_constraint("onefoundry_workflow_status_valid", "onefoundry_workflows", type_="check")
    op.create_check_constraint(
        "onefoundry_workflow_status_valid",
        "onefoundry_workflows",
        "status IN ('new','review_needed','research_reviewed','research_requested',"
        "'draft_requested','draft_ready','blocked','unknown')",
    )
    op.create_table(
        "onefoundry_company_statements",
        *_identity(),
        sa.Column("statement_key", UUID, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("approved_by", sa.Text()),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("retired_by", sa.Text()),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        *_scope("onefoundry_company_statements"),
        sa.UniqueConstraint(
            "project_id", "statement_key", "version", name="onefoundry_statement_version_unique"
        ),
        sa.UniqueConstraint("project_id", "id", "version", name="onefoundry_statement_id_version_unique"),
        sa.CheckConstraint("version >= 1", name="onefoundry_statement_version_valid"),
        sa.CheckConstraint("length(trim(body)) BETWEEN 1 AND 2000", name="onefoundry_statement_body_valid"),
        sa.CheckConstraint(
            "state IN ('draft','approved','retired')", name="onefoundry_statement_state_valid"
        ),
        sa.CheckConstraint(
            "(state='draft' AND approved_by IS NULL AND retired_by IS NULL) OR "
            "(state='approved' AND approved_by IS NOT NULL AND retired_by IS NULL) OR "
            "(state='retired' AND approved_by IS NOT NULL AND retired_by IS NOT NULL)",
            name="onefoundry_statement_actors_valid",
        ),
    )
    op.create_table(
        "onefoundry_budget_snapshots",
        *_identity(),
        sa.Column("workflow_id", UUID, nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("linked_orders_limit", sa.Integer(), nullable=False),
        sa.Column("model_attempts_limit", sa.Integer(), nullable=False),
        sa.Column("public_web_calls_limit", sa.Integer(), nullable=False),
        sa.Column("context_bytes_limit", sa.Integer(), nullable=False),
        sa.Column("output_tokens_limit", sa.Integer(), nullable=False),
        sa.Column("elapsed_seconds_limit", sa.Integer(), nullable=False),
        *_scope("onefoundry_budget_snapshots"),
        sa.ForeignKeyConstraint(
            ["project_id", "workflow_id"], ["onefoundry_workflows.project_id", "onefoundry_workflows.id"]
        ),
        sa.UniqueConstraint("project_id", "workflow_id", name="onefoundry_budget_workflow_unique"),
        sa.CheckConstraint(
            "linked_orders_limit BETWEEN 1 AND 8 AND model_attempts_limit BETWEEN 0 AND 8 "
            "AND public_web_calls_limit BETWEEN 0 AND 20 AND context_bytes_limit BETWEEN 1024 AND 32000 "
            "AND output_tokens_limit BETWEEN 1 AND 8192 AND elapsed_seconds_limit BETWEEN 30 AND 3600",
            name="onefoundry_budget_limits_valid",
        ),
    )
    op.create_table(
        "onefoundry_budget_reservations",
        *_identity(),
        sa.Column("workflow_id", UUID, nullable=False),
        sa.Column("operation_key", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="reserved"),
        sa.Column("owner_id", UUID),
        sa.Column("observed_tokens", sa.Integer()),
        sa.Column("observed_cost_usd", sa.Numeric(12, 6)),
        *_scope("onefoundry_budget_reservations"),
        sa.ForeignKeyConstraint(
            ["project_id", "workflow_id"], ["onefoundry_workflows.project_id", "onefoundry_workflows.id"]
        ),
        sa.UniqueConstraint(
            "project_id", "workflow_id", "operation_key", name="onefoundry_budget_operation_unique"
        ),
        sa.CheckConstraint(
            "kind IN ('linked_order','model_attempt','public_web_call')", name="onefoundry_budget_kind_valid"
        ),
        sa.CheckConstraint("state IN ('reserved','settled','unknown')", name="onefoundry_budget_state_valid"),
    )
    op.create_table(
        "onefoundry_context_packets",
        *_identity(),
        sa.Column("organization_id", UUID, nullable=False),
        sa.Column("work_order_id", UUID, nullable=False),
        sa.Column("profile_id", UUID, nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("claim_ids", postgresql.JSONB(), nullable=False),
        sa.Column("statement_versions", postgresql.JSONB(), nullable=False),
        sa.Column("excluded", postgresql.JSONB(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        *_scope("onefoundry_context_packets"),
        sa.ForeignKeyConstraint(
            ["project_id", "organization_id"],
            ["onefoundry_organizations.project_id", "onefoundry_organizations.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "work_order_id"], ["work_orders.project_id", "work_orders.id"]
        ),
        sa.ForeignKeyConstraint(["project_id", "profile_id"], ["profiles.project_id", "profiles.id"]),
        sa.UniqueConstraint("project_id", "work_order_id", name="onefoundry_context_order_unique"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="onefoundry_context_hash_valid"),
    )
    op.create_table(
        "onefoundry_drafts",
        *_identity(),
        sa.Column("organization_id", UUID, nullable=False),
        sa.Column("context_packet_id", UUID, nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("segments", postgresql.JSONB(), nullable=False),
        *_scope("onefoundry_drafts"),
        sa.ForeignKeyConstraint(
            ["project_id", "organization_id"],
            ["onefoundry_organizations.project_id", "onefoundry_organizations.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "context_packet_id"],
            ["onefoundry_context_packets.project_id", "onefoundry_context_packets.id"],
        ),
        sa.UniqueConstraint(
            "project_id", "organization_id", "revision", name="onefoundry_draft_revision_unique"
        ),
        sa.CheckConstraint("revision >= 1", name="onefoundry_draft_revision_valid"),
    )


def downgrade() -> None:
    for name in (
        "onefoundry_drafts",
        "onefoundry_context_packets",
        "onefoundry_budget_reservations",
        "onefoundry_budget_snapshots",
        "onefoundry_company_statements",
    ):
        op.drop_table(name)
    op.drop_constraint(
        "onefoundry_order_idempotency_unique", "onefoundry_work_order_subjects", type_="unique"
    )
    op.drop_constraint("onefoundry_order_one_subject", "onefoundry_work_order_subjects", type_="unique")
    op.drop_column("onefoundry_work_order_subjects", "idempotency_key")
    op.drop_constraint("onefoundry_workflow_status_valid", "onefoundry_workflows", type_="check")
    op.drop_constraint("onefoundry_workflow_block_reason_valid", "onefoundry_workflows", type_="check")
    op.drop_column("onefoundry_workflows", "blocked_reason")
    op.create_check_constraint(
        "onefoundry_workflow_status_valid",
        "onefoundry_workflows",
        "status IN ('new','review_needed','research_reviewed','blocked','unknown')",
    )
