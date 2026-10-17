"""Watched OneDrive/SharePoint and Google Drive folders that feed the input-file pipeline."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0050_document_sources"
down_revision = "0049_connector_accounts"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "document_sources",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("project_id", UUID, sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("connector_account_id", UUID, nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("folder_ref", sa.Text(), nullable=False),
        sa.Column("folder_id", sa.Text()),
        sa.Column("folder_name", sa.Text()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.Text(), nullable=False, server_default="unverified"),
        sa.Column("last_error_code", sa.Text()),
        sa.Column("cursor", sa.Text()),
        sa.Column("last_probe_at", sa.DateTime(timezone=True)),
        sa.Column("last_poll_at", sa.DateTime(timezone=True)),
        sa.Column("poll_lease_until", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("project_id", "id", name="document_sources_project_id_id_unique"),
        sa.UniqueConstraint("project_id", "label", name="document_source_label_unique"),
        sa.ForeignKeyConstraint(
            ["project_id", "connector_account_id"], ["connector_accounts.project_id", "connector_accounts.id"]
        ),
        sa.CheckConstraint("provider IN ('onedrive','gdrive')", name="document_source_provider_valid"),
        sa.CheckConstraint("length(btrim(label)) BETWEEN 1 AND 80", name="document_source_label_valid"),
        sa.CheckConstraint("length(folder_ref) BETWEEN 1 AND 400", name="document_source_folder_ref_valid"),
        sa.CheckConstraint("status IN ('unverified','ready','failed')", name="document_source_status_valid"),
        sa.CheckConstraint("NOT enabled OR status='ready'", name="document_source_enabled_requires_ready"),
        sa.CheckConstraint("cursor IS NULL OR length(cursor) <= 4000", name="document_source_cursor_bounded"),
        sa.CheckConstraint("version >= 1", name="document_source_version_valid"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="document_source_actor_nonblank"),
    )
    op.create_table(
        "document_files",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("source_id", UUID, nullable=False),
        sa.Column("provider_file_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("revision", sa.Text(), nullable=False),
        sa.Column("input_file_id", UUID),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("project_id", "source_id", "provider_file_id", name="document_file_once"),
        sa.ForeignKeyConstraint(
            ["project_id", "source_id"], ["document_sources.project_id", "document_sources.id"]
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "input_file_id"], ["input_files.project_id", "input_files.id"]
        ),
        sa.CheckConstraint("length(provider_file_id) BETWEEN 1 AND 300", name="document_file_id_valid"),
        sa.CheckConstraint("length(revision) BETWEEN 1 AND 300", name="document_file_revision_valid"),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="document_file_actor_nonblank"),
    )


def downgrade() -> None:
    op.drop_table("document_files")
    op.drop_table("document_sources")
