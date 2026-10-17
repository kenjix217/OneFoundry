"""Operator-only abandonment of a stopped, unverified disposable coding attempt."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from pydantic import Field, ValidationError
from sqlalchemy import text

from onefoundry.external_code_schemas import ExternalCodeReceipt
from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.profile_hierarchy import lock_project
from onefoundry.runner_spool import SpoolInvalid
from onefoundry.task_session_uncertainty import UnknownAcknowledgement

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


class RepositoryAbandonment(UnknownAcknowledgement):
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    disposable_work_discarded: Literal[True]


def abandon(
    gateway: ToolGateway,
    project_id: UUID,
    run_id: UUID,
    attempt_id: UUID,
    data: RepositoryAbandonment,
) -> dict[str, Any]:
    settings = gateway.live_text_settings
    if settings is None or gateway.actor_id != settings.operator_actor_id:
        raise Conflict("only the operator can abandon an unverified coding attempt")
    with gateway.engine.begin() as connection:
        lock_project(connection, project_id)
        row = (
            connection.execute(
                text(
                    "SELECT a.*,s.root_work_order_id,s.id AS session_id FROM external_code_attempts a "
                    "JOIN runs r ON r.project_id=a.project_id AND r.id=a.run_id "
                    "JOIN task_sessions s ON s.project_id=r.project_id AND s.work_order_id=r.work_order_id "
                    "WHERE a.project_id=:p AND a.id=:a AND a.run_id=:r "
                    "AND a.fixture_id='repository_code_v1' FOR UPDATE OF a"
                ),
                {"p": project_id, "a": attempt_id, "r": run_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise NotFound("repository attempt not found in this project and run")
        result = {
            **data.model_dump(mode="json"),
            "attempt_id": str(attempt_id),
            "run_id": str(run_id),
            "status": "unknown",
        }
        prior = connection.scalar(
            text(
                "SELECT payload FROM events WHERE project_id=:p "
                "AND event_type='repository_code_abandoned' AND "
                "(payload->>'attempt_id'=:a OR payload->>'client_action_id'=:client) LIMIT 1"
            ),
            {"p": project_id, "a": str(attempt_id), "client": str(data.client_action_id)},
        )
        if prior is not None:
            if dict(prior) != result:
                raise Conflict("coding abandonment identity already has a different decision")
            return result
        if (
            row["status"] != "unknown"
            or row["request_sha256"] != data.request_sha256
            or row["receipt_sha256"] != data.receipt_sha256
        ):
            raise Conflict("refresh the exact unknown coding receipt before abandoning it")
        try:
            observed = gateway._execution_gateway().spool.read_receipt(attempt_id)
            receipt = ExternalCodeReceipt.model_validate(observed[0]) if observed is not None else None
        except (SpoolInvalid, ValidationError) as exc:
            raise Conflict("the saved coding receipt is invalid; reconcile the worker record first") from exc
        if observed is None or observed[1] != data.receipt_sha256:
            raise Conflict("a saved terminal worker receipt is required before abandonment")
        assert receipt is not None
        if (
            receipt.project_id != project_id
            or receipt.run_id != run_id
            or receipt.attempt_id != attempt_id
            or receipt.request_sha256 != data.request_sha256
            or receipt.target_id != "repository_code_v1"
            or receipt.status != "unknown"
            or receipt.commit is not None
            or receipt.artifacts
        ):
            raise Conflict("the receipt does not prove an unverified disposable attempt without a candidate")
        busy = connection.scalar(
            text(
                "SELECT 1 FROM task_session_attempts WHERE project_id=:p AND root_work_order_id=:root "
                "AND status IN ('queued','running') LIMIT 1"
            ),
            {"p": project_id, "root": row["root_work_order_id"]},
        )
        if busy is not None:
            raise Conflict("wait for active team work before recording coding abandonment")
        gateway._event(
            connection, project_id, row["root_work_order_id"], None, "repository_code_abandoned", result
        )
        return result
