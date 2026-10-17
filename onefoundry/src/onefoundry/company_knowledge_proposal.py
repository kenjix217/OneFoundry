"""Source-verified knowledge excerpts proposed for operator review and capture."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pydantic import Field, field_validator
from sqlalchemy import text

from onefoundry.company_memory_schemas import CaptureCreate, public_text
from onefoundry.gateway_errors import Conflict
from onefoundry.memory_policy import MAX_SUMMARY_BYTES
from onefoundry.profile_hierarchy import lock_project
from onefoundry.repository_code_contract import digest
from onefoundry.source_store import verify_local
from onefoundry.workflow_schemas import StrictModel

if TYPE_CHECKING:
    from onefoundry.task_session_tools import TaskSessionTools
    from onefoundry.tool_gateway import ToolGateway


class KnowledgeCitation(StrictModel):
    source_id: UUID
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    path: str = Field(min_length=1, max_length=1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    excerpt: str = Field(min_length=1, max_length=2000)

    @field_validator("excerpt")
    @classmethod
    def verbatim_excerpt(cls, value: str) -> str:
        # Validate source text with the existing memory policy, but never apply
        # prose whitespace normalization to an exact repository citation.
        public_text(value)
        if len(value.encode("utf-8")) > MAX_SUMMARY_BYTES:
            raise ValueError("excerpt exceeds byte limit")
        return value


class KnowledgeDraft(StrictModel):
    summary: str = Field(min_length=1, max_length=4096)
    scope: str = Field(pattern=r"^(company|project)$")
    citations: list[KnowledgeCitation] = Field(min_length=1, max_length=4)

    _safe = field_validator("summary")(public_text)


def propose(
    tools: TaskSessionTools, scope: dict[str, Any], action_id: UUID, data: KnowledgeDraft
) -> dict[str, Any]:
    messages: list[dict[str, object]] = []
    with tools.gateway.engine.connect() as connection:
        prior = connection.scalar(
            text(
                "SELECT payload FROM events WHERE project_id=:p AND event_type='company"
                "_knowledge_proposed' AND payload->>'tool_call_id'=:id LIMIT 1"
            ),
            {"p": tools.project_id, "id": str(action_id)},
        )
        if prior is not None:
            return dict(prior)
        observed_at = connection.scalar(
            text("SELECT created_at FROM task_session_attempts WHERE project_id=:p AND id=:id"),
            {"p": tools.project_id, "id": tools.attempt_id},
        )
        if observed_at is None:
            raise Conflict("knowledge observation has no current task attempt")
    for index, citation in enumerate(data.citations):
        observed = tools.gateway.invoke_role_tool(
            tools.project_id,
            tools.attempt_id,
            scope["profile_id"],
            f"{action_id.hex}_{index}",
            "repository_file_v1",
            {"source_id": str(citation.source_id), "path": citation.path},
            tool_version=1,
        )
        result = observed.get("result", {})
        if (
            observed.get("status") != "done"
            or result.get("found") is not True
            or result.get("truncated") is not False
            or result.get("revision") != citation.revision
            or result.get("sha256") != citation.sha256
            or citation.excerpt not in result.get("text", "")
        ):
            raise Conflict("knowledge citation differs from the admitted committed source")
        messages.append(
            {
                "speaker": "assistant",
                "text": (
                    f"Source {citation.source_id}@{citation.revision}, {citation.path}, "
                    f"SHA-256 {citation.sha256}\n{citation.excerpt}"
                ),
                "sent_at": observed_at.isoformat(),
            }
        )
    citations_sha = digest([item.model_dump(mode="json") for item in data.citations])
    capture = CaptureCreate.model_validate(
        {
            "client_capture_id": str(action_id),
            "source_ref": f"repository-evidence:{citations_sha}",
            "authorized_company_content": True,
            "summary": data.summary,
            "scope": data.scope,
            "messages": messages,
        }
    )
    value = {
        "kind": "company_knowledge_capture_v1",
        "profile_id": str(scope["profile_id"]),
        "citations": [item.model_dump(mode="json") for item in data.citations],
        "capture": capture.model_dump(mode="json"),
    }
    content = json.dumps(value, sort_keys=True, separators=(",", ":"))
    target = "Create a reviewed company knowledge draft"
    proposal_digest = digest({"action_type": "other", "target": target, "content": content})
    with tools.gateway.engine.begin() as connection:
        lock_project(connection, tools.project_id)
        prior = connection.scalar(
            text(
                "SELECT payload FROM events WHERE project_id=:p AND event_type='company_knowledge_proposed' "
                "AND payload->>'tool_call_id'=:id LIMIT 1"
            ),
            {"p": tools.project_id, "id": str(action_id)},
        )
        if prior is not None:
            return dict(prior)
        approval_id = uuid4()
        connection.execute(
            text(
                "INSERT INTO approval_requests (id,project_id,actor_id,work_order_id,ru"
                "n_id,action_type,target,content,proposal_digest,status) "
                "VALUES (:id,:p,:actor,:order,:run,'other',:target,:content,:digest,'pending')"
            ),
            {
                "id": approval_id,
                "p": tools.project_id,
                "actor": f"profile:{scope['profile_id']}",
                "order": scope["work_order_id"],
                "run": tools.attempt_id,
                "target": target,
                "content": content,
                "digest": proposal_digest,
            },
        )
        payload = {
            "tool_call_id": str(action_id),
            "approval_id": str(approval_id),
            "status": "waiting_for_operator",
            "proposal_digest": proposal_digest,
        }
        tools.gateway._event(
            connection,
            tools.project_id,
            scope["work_order_id"],
            tools.attempt_id,
            "company_knowledge_proposed",
            payload,
        )
        tools.gateway._event(
            connection,
            tools.project_id,
            scope["work_order_id"],
            tools.attempt_id,
            "approval_requested",
            {
                "approval_id": str(approval_id),
                "action_type": "other",
                "proposal_digest": proposal_digest,
                "status": "pending",
            },
        )
        return payload


def validate_pending(
    gateway: ToolGateway, connection: Any, project_id: UUID, approval: Any
) -> CaptureCreate | None:
    try:
        value = json.loads(approval["content"])
    except ValueError:
        return None
    if not isinstance(value, dict) or value.get("kind") != "company_knowledge_capture_v1":
        return None
    if (
        approval["action_type"] != "other"
        or digest({"action_type": "other", "target": approval["target"], "content": approval["content"]})
        != approval["proposal_digest"]
    ):
        raise Conflict("company knowledge proposal identity changed")
    settings = gateway.live_text_settings
    if settings is None:
        raise Conflict("company knowledge source configuration unavailable")
    tools = connection.scalar(
        text("SELECT allowed_tools FROM profiles WHERE project_id=:p AND id=:id"),
        {"p": project_id, "id": UUID(value["profile_id"])},
    )
    if not tools or "repository_file_v1" not in tools or not settings.team_repository_read_enabled:
        raise Conflict("knowledge source permission was revoked")
    for raw in value["citations"]:
        citation = KnowledgeCitation.model_validate(raw)
        source = (
            connection.execute(
                text(
                    "SELECT relative_path,branch,revision FROM project_sources WHERE projec"
                    "t_id=:p AND id=:id AND kind='local'"
                ),
                {"p": project_id, "id": citation.source_id},
            )
            .mappings()
            .one_or_none()
        )
        if (
            source is None
            or source["revision"] != citation.revision
            or verify_local(settings, source["relative_path"]) != (source["branch"], source["revision"])
        ):
            raise Conflict("knowledge source changed before operator capture")
    return CaptureCreate.model_validate(value["capture"])


def apply_decision(gateway: ToolGateway, project_id: UUID, approval: dict[str, Any]) -> None:
    with gateway.engine.connect() as connection:
        status = connection.scalar(
            text("SELECT status FROM approval_requests WHERE project_id=:p AND id=:id"),
            {"p": project_id, "id": approval["id"]},
        )
        if status != "approved":
            raise Conflict("company knowledge capture requires an approved decision")
        capture = validate_pending(gateway, connection, project_id, approval)
    if capture is None:
        return
    result = gateway.capture_company_memory(project_id, capture)
    with gateway.engine.begin() as connection:
        lock_project(connection, project_id)
        prior = connection.scalar(
            text(
                "SELECT 1 FROM events WHERE project_id=:p AND event_type='company_knowledge_capture_applied' "
                "AND payload->>'approval_id'=:id LIMIT 1"
            ),
            {"p": project_id, "id": str(approval["id"])},
        )
        if prior is None:
            gateway._event(
                connection,
                project_id,
                approval["work_order_id"],
                None,
                "company_knowledge_capture_applied",
                {
                    "approval_id": str(approval["id"]),
                    "capture_id": str(result["source_id"]),
                    "status": "pending_memory_review",
                },
            )
