"""Add project-scoped OneFoundry organization evidence without changing legacy orders."""

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0037_onefoundry_organizations"
down_revision = "0036_unknown_live_restart"
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
    op.create_table(
        "onefoundry_organizations",
        *_identity(),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("organization_type", sa.Text(), nullable=False, server_default="school_district"),
        sa.Column("canonical_domain", sa.Text()),
        sa.Column("identity_reviewed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("qualification", sa.Text(), nullable=False, server_default="unreviewed"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        *_scope("onefoundry_organizations"),
        sa.CheckConstraint("length(trim(display_name)) BETWEEN 1 AND 160", name="onefoundry_org_name_valid"),
        sa.CheckConstraint("organization_type = 'school_district'", name="onefoundry_org_type_valid"),
        sa.CheckConstraint(
            "qualification IN ('unreviewed','qualified','declined')",
            name="onefoundry_org_qualification_valid",
        ),
        sa.CheckConstraint("revision >= 1", name="onefoundry_org_revision_valid"),
        sa.CheckConstraint(
            "NOT identity_reviewed OR canonical_domain IS NOT NULL", name="onefoundry_org_reviewed_domain"
        ),
    )
    op.create_index(
        "onefoundry_org_canonical_unique",
        "onefoundry_organizations",
        ["project_id", "canonical_domain"],
        unique=True,
        postgresql_where=sa.text("identity_reviewed"),
    )
    op.create_table(
        "onefoundry_workflows",
        *_identity(),
        sa.Column("organization_id", UUID, nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="new"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        *_scope("onefoundry_workflows"),
        sa.ForeignKeyConstraint(
            ["project_id", "organization_id"],
            ["onefoundry_organizations.project_id", "onefoundry_organizations.id"],
        ),
        sa.UniqueConstraint("project_id", "organization_id", name="onefoundry_workflow_org_unique"),
        sa.UniqueConstraint("project_id", "organization_id", "id", name="onefoundry_workflow_org_id_unique"),
        sa.CheckConstraint(
            "status IN ('new','review_needed','research_reviewed','blocked','unknown')",
            name="onefoundry_workflow_status_valid",
        ),
        sa.CheckConstraint("revision >= 1", name="onefoundry_workflow_revision_valid"),
    )
    op.create_table(
        "onefoundry_sources",
        *_identity(),
        sa.Column("organization_id", UUID, nullable=False),
        sa.Column("source_kind", sa.Text(), nullable=False),
        sa.Column("official_url", sa.Text()),
        sa.Column("excerpt", sa.Text()),
        sa.Column("input_file_id", UUID),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("superseded_by", UUID),
        *_scope("onefoundry_sources"),
        sa.UniqueConstraint("project_id", "organization_id", "id", name="onefoundry_source_org_id_unique"),
        sa.ForeignKeyConstraint(
            ["project_id", "organization_id"],
            ["onefoundry_organizations.project_id", "onefoundry_organizations.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "input_file_id"], ["input_files.project_id", "input_files.id"]
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "organization_id", "superseded_by"],
            ["onefoundry_sources.project_id", "onefoundry_sources.organization_id", "onefoundry_sources.id"],
        ),
        sa.CheckConstraint(
            "(source_kind='public_excerpt' AND official_url ~ '^https://[^/[:space:]]+' "
            "AND length(trim(excerpt)) BETWEEN 1 AND 8192 AND input_file_id IS NULL) OR "
            "(source_kind='input_file' AND input_file_id IS NOT NULL "
            "AND official_url IS NULL AND excerpt IS NULL)",
            name="onefoundry_source_evidence_valid",
        ),
        sa.CheckConstraint("digest ~ '^[0-9a-f]{64}$'", name="onefoundry_source_digest_valid"),
        sa.CheckConstraint("revision >= 1", name="onefoundry_source_revision_valid"),
    )
    op.create_table(
        "onefoundry_claims",
        *_identity(),
        sa.Column("organization_id", UUID, nullable=False),
        sa.Column("source_id", UUID, nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("claim_text", sa.Text(), nullable=False),
        sa.Column("locator", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("review_state", sa.Text(), nullable=False, server_default="candidate"),
        sa.Column("verification_state", sa.Text(), nullable=False, server_default="not_run"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("superseded_by", UUID),
        *_scope("onefoundry_claims"),
        sa.UniqueConstraint("project_id", "organization_id", "id", name="onefoundry_claim_org_id_unique"),
        sa.ForeignKeyConstraint(
            ["project_id", "organization_id"],
            ["onefoundry_organizations.project_id", "onefoundry_organizations.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "organization_id", "source_id"],
            ["onefoundry_sources.project_id", "onefoundry_sources.organization_id", "onefoundry_sources.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "organization_id", "superseded_by"],
            ["onefoundry_claims.project_id", "onefoundry_claims.organization_id", "onefoundry_claims.id"],
        ),
        sa.CheckConstraint("length(trim(claim_text)) BETWEEN 1 AND 2000", name="onefoundry_claim_text_valid"),
        sa.CheckConstraint("length(trim(locator)) BETWEEN 1 AND 500", name="onefoundry_claim_locator_valid"),
        sa.CheckConstraint(
            "review_state IN ('candidate','approved','rejected')", name="onefoundry_claim_review_valid"
        ),
        sa.CheckConstraint(
            "verification_state IN ('not_run','verified','failed','unknown')",
            name="onefoundry_claim_verification_valid",
        ),
        sa.CheckConstraint("revision >= 1 AND source_revision >= 1", name="onefoundry_claim_revision_valid"),
    )
    op.create_table(
        "onefoundry_work_order_subjects",
        *_identity(),
        sa.Column("organization_id", UUID, nullable=False),
        sa.Column("workflow_id", UUID, nullable=False),
        sa.Column("work_order_id", UUID, nullable=False),
        sa.Column("subject_type", sa.Text(), nullable=False),
        sa.Column("step_key", sa.Text(), nullable=False),
        sa.Column("transition_revision", sa.Integer(), nullable=False),
        *_scope("onefoundry_work_order_subjects"),
        sa.ForeignKeyConstraint(
            ["project_id", "organization_id"],
            ["onefoundry_organizations.project_id", "onefoundry_organizations.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "organization_id", "workflow_id"],
            [
                "onefoundry_workflows.project_id",
                "onefoundry_workflows.organization_id",
                "onefoundry_workflows.id",
            ],
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "work_order_id"], ["work_orders.project_id", "work_orders.id"]
        ),
        sa.UniqueConstraint(
            "project_id",
            "workflow_id",
            "step_key",
            "transition_revision",
            name="onefoundry_order_step_unique",
        ),
        sa.CheckConstraint("subject_type = 'organization'", name="onefoundry_order_subject_type_valid"),
        sa.CheckConstraint("transition_revision >= 1", name="onefoundry_order_transition_valid"),
    )


def downgrade() -> None:
    for name in (
        "onefoundry_work_order_subjects",
        "onefoundry_claims",
        "onefoundry_sources",
        "onefoundry_workflows",
    ):
        op.drop_table(name)
    op.drop_index("onefoundry_org_canonical_unique", table_name="onefoundry_organizations")
    op.drop_table("onefoundry_organizations")
