"""One saved operator message starts or safely continues a canonical Chief job."""

import hashlib
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from sqlalchemy import text

from onefoundry.agent_conversation_policy import is_direct_conversation
from onefoundry.chief_workspace_controls import require_operator, root_order
from onefoundry.chief_workspace_schemas import ChiefMessage, ChiefReceipt
from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.operator_message_contract import initial_work_order_instructions
from onefoundry.profile_hierarchy import chief_profile_id, lock_project
from onefoundry.task_session_admission import TaskSessionAdmission, configured_profile, profile_blockers
from onefoundry.task_session_schemas import SessionMessageCreate
from onefoundry.task_session_store import TaskSessionStore
from onefoundry.task_session_uncertainty import unresolved
from onefoundry.work_order_readiness import mark_ready
from onefoundry.workflow_schemas import WorkOrderCreate

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


def fingerprint(data: ChiefMessage) -> str:
    # Preserve exact replay of requests recorded before agent selection was added.
    excluded = {"profile_id"} if data.profile_id is None else set()
    return hashlib.sha256(data.model_dump_json(exclude=excluded).encode()).hexdigest()


def saved_receipt(connection: Any, project_id: UUID, client_id: UUID) -> dict[str, Any] | None:
    row = connection.scalar(
        text(
            "SELECT payload FROM events WHERE project_id=:p AND event_type='chief_workspace_message' "
            "AND payload->>'client_message_id'=:client LIMIT 1"
        ),
        {"p": project_id, "client": str(client_id)},
    )
    return dict(row) if row is not None else None


def lookup(gateway: "ToolGateway", project_id: UUID, client_id: UUID) -> ChiefReceipt:
    with gateway.engine.connect() as connection:
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        saved = saved_receipt(connection, project_id, client_id)
        if saved is None:
            raise NotFound("no saved Chief message exists for this request")
        return ChiefReceipt.model_validate(saved["receipt"])


def inspection_required(connection: Any, project_id: UUID, order: dict[str, Any]) -> bool:
    """Do not let a conversational continuation acknowledge or repeat an uncertain effect."""
    root = order["id"]
    if order["status"] == "unknown" or unresolved(connection, project_id, root_id=root):
        return True
    pending = connection.scalar(
        text(
            "WITH RECURSIVE orders AS (SELECT id FROM work_orders WHERE project_id=:p AND id=:o "
            "UNION ALL SELECT w.id FROM work_orders w JOIN orders r ON "
            "w.parent_work_order_id=r.id WHERE w.project_id=:p) "
            "SELECT 1 FROM approval_requests a JOIN orders r ON r.id=a.work_order_id "
            "WHERE a.project_id=:p AND a.status='pending' LIMIT 1"
        ),
        {"p": project_id, "o": root},
    )
    calls = connection.scalar(
        text(
            "SELECT 1 FROM task_session_tool_calls t JOIN task_sessions s ON "
            "s.project_id=t.project_id AND s.id=t.session_id "
            "JOIN task_session_attempts a ON a.project_id=t.project_id AND a.id=t.attempt_id "
            "WHERE t.project_id=:p AND s.root_work_order_id=:o AND t.status='started' "
            "AND a.status NOT IN ('queued','running') LIMIT 1"
        ),
        {"p": project_id, "o": root},
    )
    return (
        pending is not None
        or calls is not None
        or order["deliverable_kind"] in ("code_change", "external_action")
    )


def prepare_session(
    gateway: "ToolGateway",
    connection: Any,
    project_id: UUID,
    order: dict[str, Any],
    store: TaskSessionStore,
    *,
    discussion: bool,
    deferred: bool,
) -> dict[str, Any]:
    session = store._ensure(connection, project_id, order["id"])
    if session["profile_id"] != order["profile_id"]:
        raise Conflict("historical conversation belongs to another agent; inspect its history in Details")
    if deferred:
        return session
    if discussion:
        # Inspection tools are separately admitted by purpose; no work-state or effect is reset.
        connection.execute(
            text(
                "UPDATE task_sessions SET execution_mode='team',status=CASE WHEN "
                "status='closed' THEN 'open' ELSE status END WHERE project_id=:p AND "
                "id=:s"
            ),
            {"p": project_id, "s": session["id"]},
        )
    else:
        if order["status"] == "draft":
            order = mark_ready(gateway, connection, project_id, order["id"])
        elif order["status"] not in ("ready", "running"):
            connection.execute(
                text(
                    "UPDATE work_orders SET "
                    "status='running',block_reason=NULL,blocked_from_status=NULL WHERE "
                    "project_id=:p AND id=:o"
                ),
                {"p": project_id, "o": order["id"]},
            )
            gateway._event(
                connection,
                project_id,
                order["id"],
                None,
                "chief_work_resumed",
                {
                    "previous_status": order["status"],
                    "session_id": str(session["id"]),
                    "evidence_preserved": True,
                },
            )
        if session["execution_mode"] == "legacy":
            version = connection.scalar(
                text("SELECT version FROM work_orders WHERE project_id=:p AND id=:o"),
                {"p": project_id, "o": order["id"]},
            )
            TaskSessionAdmission(gateway).activate_in_transaction(
                connection, project_id, order["id"], version
            )
        connection.execute(
            text("UPDATE task_sessions SET status='open' WHERE project_id=:p AND id=:s AND status='closed'"),
            {"p": project_id, "s": session["id"]},
        )
    return store._existing(connection, project_id, order["id"]) or session


def send(gateway: "ToolGateway", project_id: UUID, data: ChiefMessage) -> tuple[ChiefReceipt, bool]:
    require_operator(gateway)
    settings = gateway.live_text_settings
    assert settings is not None
    store = TaskSessionStore(gateway)
    with gateway.engine.begin() as connection:
        lock_project(connection, project_id)
        prior = saved_receipt(connection, project_id, data.client_message_id)
        if prior is not None:
            if prior["request_sha256"] != fingerprint(data):
                raise Conflict("message identity belongs to a different Chief request")
            return ChiefReceipt.model_validate(prior["receipt"]), False
        # A legacy message ID cannot be adopted by the command owner.
        if (
            connection.scalar(
                text("SELECT 1 FROM task_session_messages WHERE project_id=:p AND client_message_id=:client"),
                {"p": project_id, "client": data.client_message_id},
            )
            is not None
        ):
            raise Conflict("message identity is already owned by another operation")
        chief = chief_profile_id(connection, project_id)
        if data.order_id is None:
            profile_id = data.profile_id or chief
            configured_profile(connection, project_id, profile_id)
            order = gateway.create_work_order_in_transaction(
                connection,
                project_id,
                WorkOrderCreate(
                    title=data.body.splitlines()[0][:120],
                    instructions=initial_work_order_instructions(data.body),
                    profile_id=profile_id,
                    deliverable_kind="text_reply",
                ),
            )
            connection.execute(
                text(
                    "INSERT INTO chief_conversation_controls "
                    "(project_id,work_order_id,actor_id,completion_policy) VALUES "
                    "(:p,:o,:actor,:policy)"
                ),
                {
                    "p": project_id,
                    "o": order["id"],
                    "actor": gateway.actor_id,
                    "policy": "chief_verified" if profile_id == chief else "operator_review",
                },
            )
            if profile_id != chief:
                gateway._event(
                    connection,
                    project_id,
                    order["id"],
                    None,
                    "direct_agent_conversation_created",
                    {"profile_id": str(profile_id)},
                )
        else:
            order = root_order(connection, project_id, data.order_id)
            controls = (
                connection.execute(
                    text(
                        "SELECT trashed,archived FROM chief_conversation_controls WHERE "
                        "project_id=:p AND work_order_id=:o"
                    ),
                    {"p": project_id, "o": order["id"]},
                )
                .mappings()
                .one_or_none()
            )
            if controls is not None and controls["trashed"]:
                raise Conflict("restore this conversation from Trash before sending a message")
            if controls is not None and controls["archived"]:
                raise Conflict("restore this conversation from Archived before sending a message")
            if order["version"] != data.expected_version:
                raise Conflict("conversation changed; refresh before sending")
        if order["profile_id"] != chief and not is_direct_conversation(
            connection, project_id, order["id"], order["profile_id"]
        ):
            raise Conflict("this legacy job belongs to another agent; its history is available in Details")
        policy = connection.scalar(
            text(
                "SELECT completion_policy FROM chief_conversation_controls WHERE "
                "project_id=:p AND work_order_id=:o"
            ),
            {"p": project_id, "o": order["id"]},
        )
        discussion = inspection_required(connection, project_id, order) or (
            order["status"] in ("needs_review", "done") and policy != "chief_verified"
        )
        blockers = profile_blockers(configured_profile(connection, project_id, order["profile_id"]), settings)
        if not settings.team_worker_enabled:
            blockers.insert(
                0,
                "Enable the team worker in Settings before Chief can run."
                if order["profile_id"] == chief
                else "Enable the team worker in Settings before the selected agent can run.",
            )
        if connection.scalar(
            text("SELECT paused FROM coordination_controls WHERE project_id=:p"), {"p": project_id}
        ):
            blockers.append("Project execution is paused in Settings.")
        session = prepare_session(
            gateway, connection, project_id, order, store, discussion=discussion, deferred=bool(blockers)
        )
        purpose: Literal["work", "discussion"] = "discussion" if discussion else "work"
        receipt, _created = store._save_message(
            connection,
            project_id,
            session,
            SessionMessageCreate(
                client_message_id=data.client_message_id,
                expected_sequence=data.expected_sequence,
                body=data.body,
            ),
            purpose=purpose,
            defer=bool(blockers),
        )
        # Sending direction resumes future turns, without canceling an active bounded turn.
        connection.execute(
            text(
                "UPDATE chief_conversation_controls SET paused=false,version=version+1 "
                "WHERE project_id=:p AND work_order_id=:o AND paused"
            ),
            {"p": project_id, "o": order["id"]},
        )
        saved = ChiefReceipt(
            order_id=order["id"],
            session_id=session["id"],
            message_id=receipt["message_id"],
            attempt_id=receipt["attempt_id"],
            sequence=receipt["sequence"],
            disposition="awaiting_setup" if blockers else "discussion" if discussion else receipt["state"],
            detail=" ".join(blockers)
            if blockers
            else (
                "Chief can inspect and discuss the saved outcome. Work effects remain held "
                "until their owner resolves the outcome or approval."
                if order["profile_id"] == chief
                else "The agent can discuss the saved context. Work effects remain held "
                "until their owner resolves the outcome or approval."
            )
            if discussion
            else None,
        )
        gateway._event(
            connection,
            project_id,
            order["id"],
            None,
            "chief_workspace_message",
            {
                "client_message_id": str(data.client_message_id),
                "message_id": str(saved.message_id),
                "request_sha256": fingerprint(data),
                "purpose": purpose,
                "receipt": saved.model_dump(mode="json"),
            },
        )
        return saved, True
