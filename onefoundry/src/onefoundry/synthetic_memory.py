"""Operator-triggered DreamCycle intake from completed synthetic owner results.

The gateway owns the transaction and event call. This module never promotes a
candidate: the existing one-shot review path is the only publication owner.
"""

import hashlib
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, text

from onefoundry import memory_store
from onefoundry.memory_policy import candidate_id, candidate_summary
from onefoundry.tool_gateway import Conflict, NotFound, ToolGateway


def _sales_source(connection: Connection, project_id: UUID, run_id: UUID) -> dict[str, Any]:
    row = (
        connection.execute(
            text(
                "SELECT d.id, d.project_id, d.run_id, d.source_run_id, d.actor_id, d.subject, "
                "d.body, d.body_sha256, d.delivery_status, d.sent_at, d.approval_id "
                "FROM sales_drafts d WHERE d.project_id=:project_id AND d.run_id=:run_id"
            ),
            {"project_id": project_id, "run_id": run_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise Conflict("completed Sales draft is required")
    source = dict(row)
    if source["delivery_status"] != "delivered" or source["sent_at"] is None:
        raise Conflict("confirmed Mailpit delivery is required before Sales memory review")
    if hashlib.sha256(str(source["body"]).encode("utf-8")).hexdigest() != source["body_sha256"]:
        raise Conflict("Sales draft digest does not match saved body")
    gtm = connection.execute(
        text(
            "SELECT 1 FROM text_outputs WHERE project_id=:project_id AND run_id=:run_id "
            "AND role='GTM' AND source_kind='synthetic_scripted_sdk'"
        ),
        {"project_id": project_id, "run_id": run_id},
    ).scalar_one_or_none()
    if source["source_run_id"] != run_id or gtm is None:
        raise Conflict("Researcher to GTM source output is required")
    approval = connection.execute(
        text("SELECT status FROM approval_requests WHERE project_id=:project_id AND id=:approval_id"),
        {"project_id": project_id, "approval_id": source["approval_id"]},
    ).scalar_one_or_none()
    if approval != "approved":
        raise Conflict("approved Sales delivery is required")
    return source


def _qa_source(connection: Connection, project_id: UUID, run_id: UUID) -> dict[str, Any]:
    row = (
        connection.execute(
            text(
                "SELECT j.id, j.project_id, j.run_id, j.actor_id, j.status, j.receipt_sha256, "
                "j.commit, j.diff_sha256, j.tests FROM runner_jobs j "
                "JOIN runner_jobs e ON e.project_id=j.project_id AND e.run_id=j.run_id "
                "AND e.id=j.source_job_id AND e.stage='engineer' AND e.status='done' "
                "WHERE j.project_id=:project_id AND j.run_id=:run_id AND j.stage='qa'"
            ),
            {"project_id": project_id, "run_id": run_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None or row["status"] != "done" or not row["receipt_sha256"]:
        raise Conflict("verified completed QA receipt is required")
    source = dict(row)
    if not source["commit"] or not source["diff_sha256"]:
        raise Conflict("QA receipt is missing its exact commit or diff digest")
    tests = source["tests"]
    if (
        not isinstance(tests, list)
        or not tests
        or any(
            not isinstance(test, dict) or test.get("status") != "passed" or test.get("exit_code") != 0
            for test in tests
        )
    ):
        raise Conflict("QA receipt must include passing tests")
    return source


def _source_summary(mode: str, source: dict[str, Any]) -> str:
    if mode == "synthetic_text":
        return candidate_summary(
            f"Synthetic Researcher to GTM to Sales draft: {source['subject']}\n{source['body']}"
        )
    tests = source["tests"]
    names = ", ".join(str(test["name"]) for test in tests)
    return candidate_summary(
        "Synthetic Engineer to QA result: verified QA receipt with passing tests "
        f"({names}). Commit {source['commit']}; diff SHA-256 {source['diff_sha256']}."
    )


def propose_synthetic_candidate(
    gateway: ToolGateway, project_id: UUID, run_id: UUID, scope: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Create one result and pending candidate atomically for a completed run.

    A repeat request returns the same rows. A conflicting source or scope fails.
    No source text or result summary enters the append-only event payload.
    """
    if scope not in {"project", "company"}:
        raise ValueError("invalid memory scope")
    with gateway.engine.begin() as connection:
        run = (
            connection.execute(
                text(
                    "SELECT id, work_order_id, status, mode FROM runs "
                    "WHERE project_id=:project_id AND id=:run_id FOR UPDATE"
                ),
                {"project_id": project_id, "run_id": run_id},
            )
            .mappings()
            .one_or_none()
        )
        if run is None:
            raise NotFound("run not found")
        if run["status"] != "done" or run["mode"] not in {"synthetic_text", "synthetic_code"}:
            raise Conflict("completed synthetic Sales or Engineer to QA run is required")
        mode = str(run["mode"])
        source = (
            _sales_source(connection, project_id, run_id)
            if mode == "synthetic_text"
            else _qa_source(connection, project_id, run_id)
        )
        try:
            summary = _source_summary(mode, source)
        except ValueError as exc:
            raise Conflict(f"synthetic source cannot be staged for memory: {exc}") from exc
        source_kind = "synthetic_sales" if mode == "synthetic_text" else "synthetic_qa"
        source_column = "source_sales_draft_id" if mode == "synthetic_text" else "source_runner_job_id"
        existing = memory_store.get_result_by_run(connection, project_id, run_id)
        if existing is not None:
            if (
                existing["source_kind"] != source_kind
                or existing["scope"] != scope
                or existing["summary"] != summary
                or existing[source_column] != source["id"]
                or existing["source_sha256"]
                != (source["body_sha256"] if mode == "synthetic_text" else source["receipt_sha256"])
            ):
                raise Conflict("run already has a different memory result")
            result = existing
        else:
            result_id = uuid4()
            result = memory_store.insert_synthetic_result(
                connection,
                result_id=result_id,
                project_id=project_id,
                actor_id=gateway.actor_id,
                run_id=run_id,
                summary=summary,
                scope=scope,
                source_kind=source_kind,
                source_id=source["id"],
                source_sha256=(
                    source["body_sha256"] if mode == "synthetic_text" else source["receipt_sha256"]
                ),
            )
            gateway._event(
                connection,
                project_id,
                run["work_order_id"],
                run_id,
                "run_result_recorded",
                {
                    "result_id": str(result_id),
                    "scope": scope,
                    "source_kind": source_kind,
                    "source_id": str(source["id"]),
                },
            )
        candidate = memory_store.get_candidate_by_result(connection, project_id, result["id"])
        if candidate is None:
            proposal_id = candidate_id(result["id"], summary)
            candidate = memory_store.insert_candidate(
                connection,
                candidate_id=proposal_id,
                project_id=project_id,
                actor_id=gateway.actor_id,
                source_result_id=result["id"],
                summary=summary,
                scope=scope,
            )
            gateway._event(
                connection,
                project_id,
                run["work_order_id"],
                run_id,
                "dreamcycle_candidate_created",
                {"candidate_id": str(proposal_id), "result_id": str(result["id"]), "scope": scope},
            )
        return result, candidate
