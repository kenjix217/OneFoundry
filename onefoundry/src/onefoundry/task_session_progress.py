"""Persisted evidence checkpoints for the automatic Chief no-progress guard."""

from typing import Any
from uuid import UUID

from sqlalchemy import text


def decisions_without_progress(connection: Any, project_id: UUID, root_id: UUID, main_id: UUID) -> int:
    """Novel confirmed result bytes or an explicit operator turn start a new window.

    Repeated artifact/QA receipts with fresh storage IDs are the same evidence.
    Text reports use their saved content digest. This measures recorded evidence,
    not the semantic quality of a model claim; acceptance still belongs to its owner.
    """
    count = connection.scalar(
        text(
            "WITH evidence AS (SELECT m.created_at, CASE "
            "WHEN a.acceptance_kind='artifact' THEN 'artifact:'||(m.body::jsonb->'artifact'->>'sha256') "
            "WHEN a.acceptance_kind='qa_review' AND "
            "jsonb_array_length(m.body::jsonb->'reviewed_artifacts')>0 THEN "
            "'qa:'||(m.body::jsonb->>'verdict')||':'|| "
            "(SELECT string_agg(x->>'sha256',',' ORDER BY x->>'sha256') "
            "FROM jsonb_array_elements(m.body::jsonb->'reviewed_artifacts') x) "
            "ELSE 'text:'||a.result_sha256 END AS evidence_key "
            "FROM task_session_assignments a JOIN task_session_messages m "
            "ON m.project_id=a.project_id AND m.id=a.result_message_id "
            "WHERE a.project_id=:p AND a.root_work_order_id=:root AND a.status='satisfied'), "
            "first_evidence AS (SELECT min(created_at) AS first_seen FROM evidence GROUP BY evidence_key), "
            "checkpoint AS (SELECT greatest((SELECT max(first_seen) FROM first_evidence), "
            "(SELECT max(created_at) FROM task_session_attempts WHERE project_id=:p AND session_id=:main "
            "AND (source_message_id IS NOT NULL OR client_attempt_id IS NOT NULL))) AS at) "
            "SELECT count(DISTINCT a.id) FROM task_session_attempts a "
            "JOIN task_session_decision_causes c ON c.project_id=a.project_id AND c.dispatch_attempt_id=a.id "
            "CROSS JOIN checkpoint WHERE a.project_id=:p AND a.session_id=:main "
            "AND c.root_work_order_id=:root AND a.source_message_id IS NULL "
            "AND a.client_attempt_id IS NULL AND a.retry_of_attempt_id IS NULL "
            "AND (checkpoint.at IS NULL OR a.created_at>checkpoint.at)"
        ),
        {"p": project_id, "root": root_id, "main": main_id},
    )
    return int(count or 0)
