"""DreamCycle source intake and lifecycle, inside Tool Gateway transactions."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from onefoundry import memory_store
from onefoundry.company_memory_schemas import CaptureCreate, MemoryTransition
from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.memory_policy import candidate_id
from onefoundry.memory_schemas import CandidateReview
from onefoundry.profile_hierarchy import lock_project

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def owned_memory(
    connection: Connection, project_id: UUID, memory_id: UUID, *, lock: bool = True
) -> dict[str, Any]:
    row = (
        connection.execute(
            text(
                "SELECT * FROM memory_items WHERE project_id=:p AND id=:id FOR UPDATE"
                if lock
                else "SELECT * FROM memory_items WHERE project_id=:p AND id=:id"
            ),
            {"p": project_id, "id": memory_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise NotFound("memory item not found in this project")
    return dict(row)


def replacement(
    connection: Connection, project_id: UUID, memory_id: UUID, scope: str, *, lock: bool = True
) -> dict[str, Any]:
    row = owned_memory(connection, project_id, memory_id, lock=lock)
    if row["state"] != "current" or row["scope"] != scope:
        raise Conflict("replacement requires current memory in the same project and scope")
    return row


class CompanyMemory:
    def __init__(self, gateway: ToolGateway) -> None:
        self.gateway = gateway

    def capture(
        self,
        project_id: UUID,
        data: CaptureCreate,
        *,
        project_lock: Callable[[Connection, UUID], None] = lock_project,
    ) -> dict[str, Any]:
        source_id, result_id = uuid4(), uuid4()
        messages = [message.model_dump(mode="json") for message in data.messages]
        digest = hashlib.sha256(json.dumps(messages, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        payload_digest = hashlib.sha256(data.model_dump_json().encode()).hexdigest()
        proposal_id = candidate_id(result_id, data.summary)
        with self.gateway.engine.begin() as connection:
            project_lock(connection, project_id)
            existing = (
                connection.execute(
                    text(
                        "SELECT id,payload_sha256 FROM memory_captures WHERE project_id=:p "
                        "AND actor_id=:actor AND client_capture_id=:request"
                    ),
                    {"p": project_id, "actor": self.gateway.actor_id, "request": data.client_capture_id},
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if existing["payload_sha256"] != payload_digest:
                    raise Conflict("capture ID was already used for different content")
                return self._capture_receipt(connection, project_id, existing["id"])
            if data.replaces_memory_id is not None:
                # The project lock already serializes review/lifecycle changes. A
                # draft-only capture login never needs UPDATE permission on facts.
                replacement(connection, project_id, data.replaces_memory_id, data.scope, lock=False)
            connection.execute(
                text(
                    "INSERT INTO memory_captures (id,project_id,actor_id,client_capture_id,source_ref,"
                    "messages,source_sha256,payload_sha256) "
                    "VALUES (:id,:p,:actor,:request,:ref,:messages,:digest,:payload_digest)"
                ).bindparams(bindparam("messages", type_=JSONB)),
                {
                    "id": source_id,
                    "p": project_id,
                    "actor": self.gateway.actor_id,
                    "ref": data.source_ref,
                    "messages": messages,
                    "digest": digest,
                    "request": data.client_capture_id,
                    "payload_digest": payload_digest,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO run_results (id,project_id,actor_id,summary,scope,source_kind,"
                    "source_capture_id,source_sha256) VALUES "
                    "(:id,:p,:actor,:summary,:scope,'conversation_capture',:source,:digest)"
                ),
                {
                    "id": result_id,
                    "p": project_id,
                    "actor": self.gateway.actor_id,
                    "summary": data.summary,
                    "scope": data.scope,
                    "source": source_id,
                    "digest": digest,
                },
            )
            candidate = memory_store.insert_candidate(
                connection,
                candidate_id=proposal_id,
                project_id=project_id,
                actor_id=self.gateway.actor_id,
                source_result_id=result_id,
                summary=data.summary,
                scope=data.scope,
                replaces_memory_id=data.replaces_memory_id,
            )
            self.gateway._event(
                connection,
                project_id,
                None,
                None,
                "company_memory_captured",
                {
                    "source_id": str(source_id),
                    "source_sha256": digest,
                    "result_id": str(result_id),
                    "candidate_id": str(proposal_id),
                    "scope": data.scope,
                    "message_count": len(messages),
                },
            )
            return {
                "source_id": source_id,
                "source_sha256": digest,
                "result": memory_store.get_result(connection, project_id, result_id),
                "candidate": candidate,
            }

    @staticmethod
    def _capture_receipt(connection: Connection, project_id: UUID, source_id: UUID) -> dict[str, Any]:
        result = (
            connection.execute(
                text(
                    "SELECT id,source_sha256 FROM run_results "
                    "WHERE project_id=:p AND source_capture_id=:source"
                ),
                {"p": project_id, "source": source_id},
            )
            .mappings()
            .one()
        )
        return {
            "source_id": source_id,
            "source_sha256": result["source_sha256"],
            "result": memory_store.get_result(connection, project_id, result["id"]),
            "candidate": memory_store.get_candidate_by_result(connection, project_id, result["id"]),
        }

    def source(self, project_id: UUID, source_id: UUID) -> dict[str, Any]:
        with self.gateway.engine.connect() as connection:
            self.gateway._require_project(connection, project_id)
            row = (
                connection.execute(
                    text("SELECT * FROM memory_captures WHERE project_id=:p AND id=:id"),
                    {"p": project_id, "id": source_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise NotFound("captured source not found in this project")
            return dict(row)

    def history(self, project_id: UUID, limit: int) -> list[dict[str, Any]]:
        if not 1 <= limit <= 50:
            raise ValueError("invalid history limit")
        with self.gateway.engine.connect() as connection:
            self.gateway._require_project(connection, project_id)
            rows = connection.execute(
                text(
                    "SELECT * FROM memory_items WHERE project_id=:p AND state<>'current' "
                    "ORDER BY changed_at DESC,id DESC LIMIT :limit"
                ),
                {"p": project_id, "limit": limit},
            ).mappings()
            return [{**dict(row), "score": 0} for row in rows]

    def review(self, project_id: UUID, proposal_id: UUID, data: CandidateReview) -> dict[str, Any]:
        from onefoundry.memory_embedding_index import index_publication

        with self.gateway.engine.begin() as connection:
            lock_project(connection, project_id)
            proposal = memory_store.get_candidate_for_update(connection, project_id, proposal_id)
            if proposal is None:
                raise NotFound("candidate not found")
            if proposal["status"] != "pending":
                raise Conflict("candidate already reviewed")
            old_id = proposal["replaces_memory_id"]
            if data.decision == "approved" and old_id is not None:
                replacement(connection, project_id, old_id, str(proposal["scope"]))
            row = memory_store.review_candidate(
                connection, project_id, proposal_id, self.gateway.actor_id, data.decision
            )
            memory_id = uuid4() if data.decision == "approved" else None
            if memory_id is not None:
                memory_store.publish_memory(
                    connection,
                    memory_id=memory_id,
                    project_id=project_id,
                    actor_id=self.gateway.actor_id,
                    scope=str(proposal["scope"]),
                    summary=str(proposal["summary"]),
                    result_id=proposal["source_result_id"],
                    candidate_id=proposal_id,
                )
                index_publication(self.gateway, connection, project_id, memory_id, str(proposal["summary"]))
                if old_id is not None:
                    connection.execute(
                        text(
                            "UPDATE memory_items SET state='superseded',superseded_by=:new,"
                            "changed_at=clock_timestamp(),changed_by_actor_id=:actor "
                            "WHERE project_id=:p AND id=:id"
                        ),
                        {"p": project_id, "id": old_id, "new": memory_id, "actor": self.gateway.actor_id},
                    )
            result = memory_store.get_result(connection, project_id, proposal["source_result_id"])
            if result is None:
                raise NotFound("result not found")
            run_id = result["run_id"]
            work_order_id = (
                None
                if run_id is None
                else connection.scalar(
                    text("SELECT work_order_id FROM runs WHERE project_id=:p AND id=:run"),
                    {"p": project_id, "run": run_id},
                )
            )
            self.gateway._event(
                connection,
                project_id,
                work_order_id,
                run_id,
                "dreamcycle_candidate_reviewed",
                {
                    "candidate_id": str(proposal_id),
                    "result_id": str(proposal["source_result_id"]),
                    "decision": data.decision,
                    "memory_id": str(memory_id) if memory_id else None,
                    "replaces_memory_id": str(old_id) if old_id else None,
                },
            )
            return row

    def transition(self, project_id: UUID, memory_id: UUID, data: MemoryTransition) -> dict[str, Any]:
        from onefoundry.memory_embedding_index import index_publication

        with self.gateway.engine.begin() as connection:
            lock_project(connection, project_id)
            old = owned_memory(connection, project_id, memory_id)
            expected, next_state = (
                ("current", "withdrawn") if data.action == "withdraw" else ("withdrawn", "current")
            )
            if old["state"] != expected:
                raise Conflict(f"{data.action} requires {expected} memory")
            row = (
                connection.execute(
                    text(
                        "UPDATE memory_items SET state=:state,changed_at=clock_timestamp(),"
                        "changed_by_actor_id=:actor WHERE project_id=:p AND id=:id RETURNING *"
                    ),
                    {"state": next_state, "actor": self.gateway.actor_id, "p": project_id, "id": memory_id},
                )
                .mappings()
                .one()
            )
            if data.action == "restore":
                index_publication(self.gateway, connection, project_id, memory_id, str(old["text"]))
            self.gateway._event(
                connection,
                project_id,
                None,
                None,
                "company_memory_state_changed",
                {"memory_id": str(memory_id), "from": expected, "to": next_state, "reason": data.reason},
            )
            return {**dict(row), "score": 0}

    def read(
        self, project_id: UUID, memory_id: UUID, *, scope: str | None = None, include_history: bool = False
    ) -> dict[str, Any]:
        with self.gateway.engine.connect() as connection:
            self.gateway._require_project(connection, project_id)
            visible = (
                "(project_id=:p AND scope='project')"
                if scope == "project"
                else "scope='company'"
                if scope == "company"
                else "(project_id=:p OR scope='company')"
            )
            current = "" if include_history else " AND state='current'"
            row = (
                connection.execute(
                    text(f"SELECT * FROM memory_items WHERE id=:id AND {visible}{current}"),  # noqa: S608
                    {"p": project_id, "id": memory_id},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise NotFound("memory item not found in the permitted scope")
            return {**dict(row), "score": 0}
