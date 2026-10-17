"""Pin automatic-attempt limits and quiet hours on immutable rule versions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0068_coordination_rule_limits"
down_revision = "0067_team_limits_and_retry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "coordination_rule_versions",
        sa.Column("max_model_attempts", sa.Integer(), nullable=False, server_default="10"),
    )
    op.add_column("coordination_rule_versions", sa.Column("quiet_hours", JSONB))
    op.create_check_constraint(
        "coordination_rule_model_limit_valid",
        "coordination_rule_versions",
        "max_model_attempts BETWEEN 0 AND 1000",
    )
    op.add_column("coordination_intents", sa.Column("model_admitted_at", sa.DateTime(timezone=True)))
    op.execute(
        "UPDATE coordination_intents SET model_admitted_at=updated_at "
        "WHERE action_kind='auto_start' AND "
        "(state IN ('leased','done','unknown') OR decision->>'run_id' IS NOT NULL)"
    )
    op.create_index(
        "coordination_rule_model_admissions",
        "coordination_intents",
        ["project_id", "rule_id", "model_admitted_at"],
    )
    op.execute(
        """CREATE FUNCTION guard_coordination_model_admission() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.model_admitted_at IS NOT NULL AND
             NEW.model_admitted_at IS DISTINCT FROM OLD.model_admitted_at THEN
            RAISE EXCEPTION 'coordination model admission is immutable';
          END IF;
          RETURN NEW;
        END; $$"""
    )
    op.execute(
        "CREATE TRIGGER coordination_model_admission_immutable BEFORE UPDATE ON coordination_intents "
        "FOR EACH ROW EXECUTE FUNCTION guard_coordination_model_admission()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER coordination_model_admission_immutable ON coordination_intents")
    op.execute("DROP FUNCTION guard_coordination_model_admission()")
    op.drop_index("coordination_rule_model_admissions", table_name="coordination_intents")
    op.drop_column("coordination_intents", "model_admitted_at")
    op.drop_constraint("coordination_rule_model_limit_valid", "coordination_rule_versions", type_="check")
    op.drop_column("coordination_rule_versions", "quiet_hours")
    op.drop_column("coordination_rule_versions", "max_model_attempts")
