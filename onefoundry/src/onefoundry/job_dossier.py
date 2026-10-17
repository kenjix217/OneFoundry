"""Bounded, current job facts for a work-order conversation.

This reads operational owners at send time. Reviewed memory is added by the
conversation owner separately and never substitutes for these records.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text

from onefoundry.work_order_completion import evidence


def _rows(connection: Any, statement: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(text(statement), params).mappings()]


def build(connection: Any, project_id: UUID, order_id: UUID) -> str:
    params = {"p": project_id, "o": order_id}
    order = _rows(
        connection,
        "SELECT status,deliverable_kind,acceptance_checks FROM work_orders WHERE project_id=:p AND id=:o",
        params,
    )[0]
    latest = connection.scalar(
        text(
            "SELECT id FROM runs WHERE project_id=:p AND work_order_id=:o "
            "ORDER BY started_at DESC,id DESC LIMIT 1"
        ),
        params,
    )
    matched = evidence(connection, project_id, order_id, latest)
    lines = [
        "Current saved job state (read at this turn; do not infer completion from a model reply):",
        f"Status: {order['status']}",
        f"Declared deliverable: {order['deliverable_kind']}",
        "Acceptance checks: " + ("; ".join(order["acceptance_checks"]) or "none declared"),
        "Missing checks: " + ("; ".join(matched["missing_checks"]) or "none"),
        "Operator accepted completion: " + ("yes" if matched["operator_accepted"] else "no"),
    ]
    runs = _rows(
        connection,
        "SELECT id,status,mode,outcome_reason FROM runs WHERE project_id=:p AND work_order_id=:o "
        "ORDER BY started_at DESC,id DESC LIMIT 6",
        params,
    )
    lines.append(
        "Runs: "
        + (
            "; ".join(
                f"{run['id']} {run['mode']} {run['status']} {run['outcome_reason'][:120]}" for run in runs
            )
            or "none recorded"
        )
    )
    results = _rows(
        connection,
        "SELECT r.id,r.summary,r.source_kind FROM run_results r JOIN runs x ON "
        "x.project_id=r.project_id AND x.id=r.run_id "
        "WHERE x.project_id=:p AND x.work_order_id=:o ORDER BY r.created_at DESC,r.id DESC LIMIT 6",
        params,
    )
    lines.append(
        "Saved results: "
        + (
            "; ".join(
                f"{row['id']} {str(row['source_kind'] or 'manual')} {row['summary'][:240]}" for row in results
            )
            or "none recorded"
        )
    )
    replies = _rows(
        connection,
        "SELECT o.id,o.text FROM live_text_outputs o JOIN runs r ON "
        "r.project_id=o.project_id AND r.id=o.run_id "
        "WHERE r.project_id=:p AND r.work_order_id=:o ORDER BY o.created_at DESC LIMIT 4",
        params,
    )
    replies += _rows(
        connection,
        "SELECT o.id,o.text FROM text_outputs o JOIN runs r ON "
        "r.project_id=o.project_id AND r.id=o.run_id "
        "WHERE r.project_id=:p AND r.work_order_id=:o ORDER BY o.created_at DESC LIMIT 4",
        params,
    )
    lines.append(
        "Saved model replies: "
        + ("; ".join(f"{row['id']} {row['text'][:240]}" for row in replies[:6]) or "none recorded")
    )
    handoffs = _rows(
        connection,
        "SELECT event_type,payload FROM events WHERE project_id=:p AND work_order_id=:o "
        "AND event_type IN ('run_handed_off','synthetic_text_handed_off','synthetic_code_handed_off') "
        "ORDER BY created_at DESC,id DESC LIMIT 6",
        params,
    )
    lines.append(
        "Handoffs: "
        + (
            "; ".join(
                f"{row['event_type']} to "
                f"{row['payload'].get('to_profile_id', row['payload'].get('to', 'unknown'))}"
                for row in handoffs
            )
            or "none recorded"
        )
    )
    children = _rows(
        connection,
        "SELECT o.id,o.title,o.status,p.name AS profile_name FROM work_orders o "
        "JOIN profiles p ON p.project_id=o.project_id AND p.id=o.profile_id "
        "WHERE o.project_id=:p AND o.parent_work_order_id=:o "
        "ORDER BY o.created_at DESC,o.id DESC LIMIT 8",
        params,
    )
    if children:
        lines.append("Child assignments and current evidence:")
    else:
        lines.append("Child assignments: none recorded")
    for child in children:
        child_params = {"p": project_id, "o": child["id"]}
        child_run = _rows(
            connection,
            "SELECT id,status FROM runs WHERE project_id=:p AND work_order_id=:o "
            "ORDER BY started_at DESC,id DESC LIMIT 1",
            child_params,
        )
        child_run_id = child_run[0]["id"] if child_run else None
        child_evidence = evidence(connection, project_id, child["id"], child_run_id)
        summary = (
            _rows(
                connection,
                "SELECT summary FROM run_results WHERE project_id=:p AND run_id=:r "
                "ORDER BY created_at DESC,id DESC LIMIT 1",
                {"p": project_id, "r": child_run_id},
            )
            if child_run_id
            else []
        )
        if not summary and child_run_id:
            summary = _rows(
                connection,
                "SELECT text AS summary FROM live_text_outputs WHERE project_id=:p AND run_id=:r "
                "ORDER BY created_at DESC,id DESC LIMIT 1",
                {"p": project_id, "r": child_run_id},
            )
        evidence_refs = (
            ", ".join(f"{item['source']}:{item['id']}" for item in child_evidence["evidence"]) or "none"
        )
        lines.append(
            f"- {child['id']} {child['title'][:120]} assigned to {child['profile_name']} "
            f"({child['status']}); latest run {child_run[0]['status'] if child_run else 'none'}; "
            f"result {str(summary[0]['summary'])[:240] if summary else 'none'}; "
            f"evidence {evidence_refs}; "
            f"missing {', '.join(child_evidence['missing_checks']) or 'none'}"
        )
    artifacts = _rows(
        connection,
        "SELECT a.id,a.relative_path AS path,a.sha256,a.status FROM workspace_artifacts a "
        "JOIN runs r ON r.project_id=a.project_id AND r.id=a.run_id "
        "WHERE r.project_id=:p AND r.work_order_id=:o "
        "ORDER BY a.created_at DESC,a.id DESC LIMIT 8",
        params,
    )
    artifacts += _rows(
        connection,
        "SELECT a.id,a.path,a.sha256,'recorded' AS status FROM external_code_artifacts a "
        "JOIN runs r ON r.project_id=a.project_id AND r.id=a.run_id "
        "WHERE r.project_id=:p AND r.work_order_id=:o "
        "ORDER BY a.created_at DESC,a.id DESC LIMIT 8",
        params,
    )
    artifacts += _rows(
        connection,
        "SELECT a.id,a.path,a.sha256,'recorded' AS status FROM runner_artifacts a "
        "JOIN runs r ON r.project_id=a.project_id AND r.id=a.run_id "
        "WHERE r.project_id=:p AND r.work_order_id=:o "
        "ORDER BY a.created_at DESC,a.id DESC LIMIT 8",
        params,
    )
    lines.append(
        "Artifacts (saved path and SHA-256): "
        + (
            "; ".join(
                f"{row['id']} {row['path'][:180]} {row['sha256']} ({row['status']})" for row in artifacts[:12]
            )
            or "none recorded"
        )
    )
    approvals = _rows(
        connection,
        "SELECT id,action_type,status FROM approval_requests WHERE project_id=:p AND work_order_id=:o "
        "ORDER BY created_at DESC,id DESC LIMIT 8",
        params,
    )
    lines.append(
        "Approvals: "
        + (
            "; ".join(f"{row['id']} {row['action_type']} {row['status']}" for row in approvals)
            or "none recorded"
        )
    )
    lines.append(
        "Deliverable evidence: "
        + ("; ".join(f"{row['source']} {row['id']}" for row in matched["evidence"]) or "none recorded")
    )
    return "\n".join(lines)
