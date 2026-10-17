"""Allow bounded specialist sessions under one work order and record dependencies."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0062_team_assignments"
down_revision = "0061_team_attempt_requests"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.drop_index("task_sessions_one_owner_per_order", table_name="task_sessions")
    op.add_column(
        "task_sessions", sa.Column("is_order_owner", sa.Boolean(), nullable=False, server_default=sa.true())
    )
    op.create_check_constraint(
        "task_sessions_owner_kind_valid",
        "task_sessions",
        "(kind='main' AND is_order_owner) OR kind='child' OR (kind='consultation' AND NOT is_order_owner)",
    )
    op.create_index(
        "task_sessions_one_owner_per_order",
        "task_sessions",
        ["project_id", "work_order_id"],
        unique=True,
        postgresql_where=sa.text("is_order_owner"),
    )
    op.create_table(
        "task_session_assignments",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("root_work_order_id", UUID, nullable=False),
        sa.Column("parent_session_id", UUID, nullable=False),
        sa.Column("child_session_id", UUID, nullable=False),
        sa.Column("source_attempt_id", UUID, nullable=False),
        sa.Column("client_action_id", UUID, nullable=False),
        sa.Column("payload_sha256", sa.Text(), nullable=False),
        sa.Column("plan_revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("expected_result", sa.Text(), nullable=False),
        sa.Column("acceptance_check", sa.Text(), nullable=False),
        sa.Column("acceptance_kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("result_message_id", UUID),
        sa.Column("result_action_id", UUID),
        sa.Column("result_sha256", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint(
            "project_id", "root_work_order_id", "id", name="task_session_assignments_root_id_unique"
        ),
        sa.UniqueConstraint("project_id", "child_session_id", name="task_session_assignments_child_unique"),
        sa.UniqueConstraint("project_id", "client_action_id", name="task_session_assignments_action_unique"),
        sa.UniqueConstraint(
            "project_id", "result_action_id", name="task_session_assignments_result_action_unique"
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "root_work_order_id", "parent_session_id"],
            ["task_sessions.project_id", "task_sessions.root_work_order_id", "task_sessions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "root_work_order_id", "child_session_id"],
            ["task_sessions.project_id", "task_sessions.root_work_order_id", "task_sessions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "source_attempt_id"],
            ["task_session_attempts.project_id", "task_session_attempts.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "child_session_id", "result_message_id"],
            [
                "task_session_messages.project_id",
                "task_session_messages.session_id",
                "task_session_messages.id",
            ],
        ),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="task_session_assignments_actor_valid"),
        sa.CheckConstraint("payload_sha256 ~ '^[0-9a-f]{64}$'", name="task_session_assignments_hash_valid"),
        sa.CheckConstraint("plan_revision > 0", name="task_session_assignments_revision_valid"),
        sa.CheckConstraint("objective ~ '[^[:space:]]'", name="task_session_assignments_objective_valid"),
        sa.CheckConstraint("expected_result ~ '[^[:space:]]'", name="task_session_assignments_result_valid"),
        sa.CheckConstraint("acceptance_check ~ '[^[:space:]]'", name="task_session_assignments_check_valid"),
        sa.CheckConstraint(
            "acceptance_kind IN ('text_report','artifact','operator_review')",
            name="task_session_assignments_acceptance_kind_valid",
        ),
        sa.CheckConstraint(
            "status IN ('pending','ready','active','satisfied','blocked','canceled')",
            name="task_session_assignments_status_valid",
        ),
        sa.CheckConstraint(
            "(status='satisfied') = (result_message_id IS NOT NULL)",
            name="task_session_assignments_satisfaction_valid",
        ),
        sa.CheckConstraint(
            "(result_action_id IS NULL) = (result_sha256 IS NULL)",
            name="task_session_assignments_result_identity_valid",
        ),
        sa.CheckConstraint(
            "result_sha256 IS NULL OR result_sha256 ~ '^[0-9a-f]{64}$'",
            name="task_session_assignments_result_hash_valid",
        ),
    )
    op.create_index(
        "task_session_assignments_parent_state",
        "task_session_assignments",
        ["project_id", "root_work_order_id", "parent_session_id", "status"],
    )
    op.create_table(
        "task_session_dependencies",
        sa.Column("project_id", UUID, nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("root_work_order_id", UUID, nullable=False),
        sa.Column("assignment_id", UUID, nullable=False),
        sa.Column("depends_on_id", UUID, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("project_id", "assignment_id", "depends_on_id"),
        sa.ForeignKeyConstraint(
            ["project_id", "root_work_order_id", "assignment_id"],
            [
                "task_session_assignments.project_id",
                "task_session_assignments.root_work_order_id",
                "task_session_assignments.id",
            ],
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "root_work_order_id", "depends_on_id"],
            [
                "task_session_assignments.project_id",
                "task_session_assignments.root_work_order_id",
                "task_session_assignments.id",
            ],
        ),
        sa.CheckConstraint("actor_id ~ '[^[:space:]]'", name="task_session_dependencies_actor_valid"),
        sa.CheckConstraint("assignment_id <> depends_on_id", name="task_session_dependencies_no_self"),
    )
    op.create_index(
        "task_session_dependencies_reverse",
        "task_session_dependencies",
        ["project_id", "root_work_order_id", "depends_on_id"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM task_session_assignments)")
    ) or connection.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM task_sessions WHERE NOT is_order_owner)")):
        raise RuntimeError("team assignments exist; downgrade would discard delegated work")
    op.drop_index("task_session_dependencies_reverse", table_name="task_session_dependencies")
    op.drop_table("task_session_dependencies")
    op.drop_index("task_session_assignments_parent_state", table_name="task_session_assignments")
    op.drop_table("task_session_assignments")
    op.drop_index("task_sessions_one_owner_per_order", table_name="task_sessions")
    op.drop_constraint("task_sessions_owner_kind_valid", "task_sessions", type_="check")
    op.drop_column("task_sessions", "is_order_owner")
    op.create_index(
        "task_sessions_one_owner_per_order",
        "task_sessions",
        ["project_id", "work_order_id"],
        unique=True,
        postgresql_where=sa.text("kind<>'consultation'"),
    )
