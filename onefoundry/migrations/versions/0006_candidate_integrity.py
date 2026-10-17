"""Bind candidates to immutable results and make review one-shot.

Revision ID: 0006_candidate_integrity
Revises: 0005_memory_dreamcycle
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_candidate_integrity"
down_revision = "0005_memory_dreamcycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    mismatched = connection.scalar(
        sa.text(
            "SELECT count(*) FROM dreamcycle_candidates c "
            "JOIN run_results r ON r.project_id = c.project_id AND r.id = c.source_result_id "
            "WHERE c.summary IS DISTINCT FROM r.summary OR c.scope IS DISTINCT FROM r.scope"
        )
    )
    if mismatched:
        raise RuntimeError("existing DreamCycle candidates must match source results before migration 0006")
    op.execute(
        """
        CREATE FUNCTION enforce_dreamcycle_candidate() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE source_summary text;
        DECLARE source_scope text;
        BEGIN
          IF TG_OP = 'INSERT' THEN
            SELECT r.summary, r.scope INTO source_summary, source_scope
            FROM run_results r
            WHERE r.project_id = NEW.project_id AND r.id = NEW.source_result_id;
            IF NOT FOUND OR NEW.summary IS DISTINCT FROM source_summary
                         OR NEW.scope IS DISTINCT FROM source_scope THEN
              RAISE EXCEPTION 'candidate must match source result'
                USING ERRCODE = '23514';
            END IF;
            IF NEW.status <> 'pending' THEN
              RAISE EXCEPTION 'candidate must begin pending'
                USING ERRCODE = '23514';
            END IF;
            RETURN NEW;
          END IF;

          IF NEW.id IS DISTINCT FROM OLD.id
             OR NEW.project_id IS DISTINCT FROM OLD.project_id
             OR NEW.actor_id IS DISTINCT FROM OLD.actor_id
             OR NEW.source_result_id IS DISTINCT FROM OLD.source_result_id
             OR NEW.summary IS DISTINCT FROM OLD.summary
             OR NEW.scope IS DISTINCT FROM OLD.scope
             OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'candidate source is immutable'
              USING ERRCODE = '23514';
          END IF;
          IF OLD.status <> 'pending' OR NEW.status NOT IN ('approved', 'rejected')
             OR NEW.reviewed_at IS NULL OR NEW.reviewed_by_actor_id IS NULL THEN
            RAISE EXCEPTION 'candidate decision is one-shot'
              USING ERRCODE = '23514';
          END IF;
          RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER dreamcycle_candidate_integrity "
        "BEFORE INSERT OR UPDATE ON dreamcycle_candidates "
        "FOR EACH ROW EXECUTE FUNCTION enforce_dreamcycle_candidate()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER dreamcycle_candidate_integrity ON dreamcycle_candidates")
    op.execute("DROP FUNCTION enforce_dreamcycle_candidate()")
