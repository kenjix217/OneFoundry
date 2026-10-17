"""Seed a project-local Chief of Staff and profile reporting tree."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0028_profile_hierarchy"
down_revision = "0027_snapshot_codex_fixture"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("profiles", sa.Column("reports_to_profile_id", postgresql.UUID(as_uuid=True)))
    op.create_foreign_key(
        "profiles_reporting_parent_project_fk",
        "profiles",
        "profiles",
        ["project_id", "reports_to_profile_id"],
        ["project_id", "id"],
    )
    op.create_check_constraint(
        "profiles_no_self_report", "profiles", "reports_to_profile_id IS NULL OR reports_to_profile_id <> id"
    )
    op.create_index("profiles_project_parent_idx", "profiles", ["project_id", "reports_to_profile_id"])
    op.execute(
        "INSERT INTO profiles (id, project_id, actor_id, name, kind, model, instructions, "
        "memory_scope, allowed_tools, autonomy_level, configured) "
        "SELECT gen_random_uuid(), p.id, p.actor_id, 'Chief of Staff', 'builtin', NULL, "
        "'Coordinate work orders across the team. Clarify scope, sequence handoffs, and report decisions ' "
        "|| 'and evidence to the operator. Do not execute work or grant permissions outside the assigned ' "
        "|| 'tools and approval boundaries.', 'company', '[]'::jsonb, 'manual', false "
        "FROM projects p WHERE NOT EXISTS (SELECT 1 FROM profiles x WHERE x.project_id=p.id "
        "AND lower(x.name)='chief of staff')"
    )
    op.execute(
        "UPDATE profiles p SET reports_to_profile_id=c.id FROM profiles c "
        "WHERE c.project_id=p.project_id AND lower(c.name)='chief of staff' "
        "AND p.id<>c.id"
    )
    op.create_index(
        "profiles_single_operator_root_idx",
        "profiles",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("reports_to_profile_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("profiles_single_operator_root_idx", table_name="profiles")
    op.drop_index("profiles_project_parent_idx", table_name="profiles")
    op.drop_constraint("profiles_no_self_report", "profiles", type_="check")
    op.drop_constraint("profiles_reporting_parent_project_fk", "profiles", type_="foreignkey")
    op.drop_column("profiles", "reports_to_profile_id")
