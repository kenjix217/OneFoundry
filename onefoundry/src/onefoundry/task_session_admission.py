"""Read-only team preflight and explicit operator cutover, with no model calls."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pydantic import Field
from sqlalchemy import text

from onefoundry.agent_conversation_policy import is_direct_conversation
from onefoundry.capability_schemas import CapabilityReport
from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.profile_hierarchy import chief_profile_id, lock_project
from onefoundry.provider_registry import provider_spec
from onefoundry.task_execution_capabilities import execution_capabilities
from onefoundry.task_session_store import TaskSessionStore
from onefoundry.task_session_tool_readiness import team_tool_readiness
from onefoundry.task_session_uncertainty import unresolved
from onefoundry.workflow_schemas import StrictModel

if TYPE_CHECKING:
    from onefoundry.settings import Settings
    from onefoundry.tool_gateway import ToolGateway


class TeamReadiness(StrictModel):
    work_order_id: UUID
    status: str
    execution_mode: str
    worker_enabled: bool
    provider: str | None
    ready_to_activate: bool
    ready_to_start: bool = False
    blockers: list[str]
    pending_approvals: int
    queued_attempts: int
    running_attempts: int
    confirmed_results: int
    next_step: str
    prompt_context_bytes_limit: int | None = None
    capability_notes: list[str] = Field(default_factory=list)
    capability_readiness: CapabilityReport | None = None


class TeamActivate(StrictModel):
    expected_version: int = Field(ge=1)


class TeamCancel(TeamActivate):
    client_action_id: UUID


def profile_blockers(profile: dict[str, Any], settings: Settings) -> list[str]:
    """Metadata admission is necessary; only live acceptance proves provider support."""
    problems: list[str] = []
    if not profile.get("instructions") or not profile.get("model") or not profile.get("model_present"):
        problems.append("Assign an agent with instructions and a current text model in Team.")
    provider = profile.get("provider")
    spec = provider_spec(str(provider))
    if spec is None or not spec.text_runtime_available:
        problems.append("The assigned model connection cannot run team tools on this route.")
    if not profile.get("catalog_valid"):
        problems.append("Refresh the assigned model account's catalog in Team.")
    if profile.get("auth_kind") != "none" and not profile.get("credential_ready"):
        problems.append("The assigned model account is not connected.")
    allowed = [item for item in profile["allowed_tools"] if item != "delegate_agent_v1"]
    _active, reason = team_tool_readiness(
        allowed,
        role_name=profile["name"],
        role_kind=profile["kind"],
        settings=settings,
    )
    if reason is not None:
        if "unregistered" in reason:
            from onefoundry.coordination_store import registered_tools

            unknown = sorted(set(allowed) - registered_tools())
            problems.append(
                f"Unregistered agent capabilities: {', '.join(unknown)}. Review the profile in Team."
            )
        else:
            problems.append(reason)
    return problems


def configured_profile(connection: Any, project_id: UUID, profile_id: UUID) -> dict[str, Any]:
    row = (
        connection.execute(
            text(
                "SELECT p.*,a.provider,a.auth_kind,a.catalog_valid,a.credential_ready,"
                "EXISTS(SELECT 1 FROM model_catalog c WHERE c.project_id=p.project_id "
                "AND c.account_id=p.model_account_id AND c.model_id=p.model AND "
                "c.category='text') AS model_present "
                "FROM profiles p LEFT JOIN model_accounts a ON a.project_id=p.project_id AND "
                "a.id=p.model_account_id "
                "WHERE p.project_id=:p AND p.id=:id"
            ),
            {"p": project_id, "id": profile_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise NotFound("agent profile not found in project")
    return dict(row)


class TaskSessionAdmission:
    def __init__(self, gateway: ToolGateway) -> None:
        self.gateway = gateway

    def inspect(self, project_id: UUID, order_id: UUID) -> TeamReadiness:
        with self.gateway.engine.connect() as connection:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            return self._inspect(connection, project_id, order_id)

    def _inspect(
        self, connection: Any, project_id: UUID, order_id: UUID, *, admit_draft: bool = False
    ) -> TeamReadiness:
        settings = self.gateway.live_text_settings
        if settings is None:
            raise Unavailable("team settings unavailable")
        order = (
            connection.execute(
                text(
                    "SELECT w.*,p.status AS project_status FROM work_orders w JOIN projects p ON "
                    "p.id=w.project_id "
                    "WHERE w.project_id=:p AND w.id=:o"
                ),
                {"p": project_id, "o": order_id},
            )
            .mappings()
            .one_or_none()
        )
        if order is None:
            raise NotFound("work order not found")
        profile = configured_profile(connection, project_id, order["profile_id"])
        blockers = profile_blockers(profile, settings)
        if (
            profile["id"] != chief_profile_id(connection, project_id)
            and not is_direct_conversation(connection, project_id, order_id, profile["id"])
        ) or order["parent_work_order_id"] is not None:
            blockers.append(
                "Start team execution from the root Chief of Staff work order. "
                "Specialist drafts need their parent team's assignment and dispatch."
            )
        if not settings.team_worker_enabled:
            blockers.insert(
                0, "The team worker is disabled. An operator must enable and verify it before work can run."
            )
        if order["project_status"] != "active":
            blockers.append("The project is not active.")
        paused = connection.scalar(
            text("SELECT paused FROM coordination_controls WHERE project_id=:p"), {"p": project_id}
        )
        if paused:
            blockers.append("Team execution is paused for this project.")
        draft_blocker = (
            "This is a saved draft. Mark ready admits it for planning; opening a task does not launch it."
        )
        if order["status"] == "draft" and not admit_draft:
            blockers.append(draft_blocker)
        elif order["status"] not in ("draft", "ready", "running"):
            blockers.append(
                "Completed. View the deliverable and acceptance record in Results and Progress."
                if order["status"] == "done"
                else "The deliverable awaits operator acceptance or return for rework."
                if order["status"] == "needs_review"
                else f"The work order is {order['status']}; review its saved reason before continuing."
            )
        if order["deliverable_kind"] in ("code_change", "external_action"):
            blockers.append(
                "This deliverable needs its separate execution or outbound owner and approval path."
            )
        counts = (
            connection.execute(
                text(
                    "SELECT count(*) FILTER(WHERE a.status='queued') AS queued,"
                    "count(*) FILTER(WHERE a.status='running') AS running FROM task_session_attempts a "
                    "WHERE a.project_id=:p AND a.root_work_order_id=:o"
                ),
                {"p": project_id, "o": order_id},
            )
            .mappings()
            .one()
        )
        pending = int(
            connection.scalar(
                text(
                    "SELECT count(*) FROM approval_requests WHERE project_id=:p AND work_order_id=:o "
                    "AND status='pending'"
                ),
                {"p": project_id, "o": order_id},
            )
        )
        if pending:
            blockers.append("An operator approval is pending. Resolve it in the approval inbox.")
        session_mode = connection.scalar(
            text(
                "SELECT execution_mode FROM task_sessions WHERE project_id=:p AND "
                "work_order_id=:o AND is_order_owner"
            ),
            {"p": project_id, "o": order_id},
        )
        mode = session_mode or "legacy"
        if unresolved(connection, project_id, root_id=order_id):
            blockers.append(
                "Review and acknowledge unknown outcomes before enabling or completing this team."
            )
        results = int(
            connection.scalar(
                text(
                    "SELECT count(*) FROM task_session_assignments WHERE project_id=:p AND "
                    "root_work_order_id=:o "
                    "AND status='satisfied'"
                ),
                {"p": project_id, "o": order_id},
            )
        )
        from onefoundry.task_session_media_wait import waiting_for_recording

        next_step = "Enable team execution for this order, then send or continue to queue a turn."
        if mode == "team":
            next_step = "Review the saved team plan, then send or continue with the selected agent if needed."
            if waiting_for_recording(connection, project_id, root_id=order_id):
                next_step = (
                    "An agent is waiting for your local recording. Save and verify the video in Results, "
                    "then explicitly continue that agent."
                )
        capabilities = execution_capabilities(
            connection, project_id, settings, profile_id=order["profile_id"], gateway=self.gateway
        )
        return TeamReadiness(
            work_order_id=order_id,
            status=order["status"],
            execution_mode=mode,
            worker_enabled=settings.team_worker_enabled,
            provider=profile.get("provider"),
            ready_to_activate=not blockers,
            ready_to_start=mode == "legacy" and not [item for item in blockers if item != draft_blocker],
            blockers=blockers,
            pending_approvals=pending,
            queued_attempts=counts["queued"],
            running_attempts=counts["running"],
            confirmed_results=results,
            next_step=(blockers[0] if blockers else next_step),
            prompt_context_bytes_limit=settings.onefoundry_max_context_bytes,
            capability_notes=capabilities["notes"],
            capability_readiness=CapabilityReport.model_validate(capabilities["readiness"]),
        )

    def activate(self, project_id: UUID, order_id: UUID, expected_version: int) -> dict[str, Any]:
        """Cut over this order only; preserve imported history and never enqueue work."""
        settings = self.gateway.live_text_settings
        if settings is None or self.gateway.actor_id != settings.operator_actor_id:
            raise Conflict("only the operator may enable team execution")
        with self.gateway.engine.begin() as connection:
            return self.activate_in_transaction(connection, project_id, order_id, expected_version)

    def activate_in_transaction(
        self, connection: Any, project_id: UUID, order_id: UUID, expected_version: int
    ) -> dict[str, Any]:
        """Use the same admission owner inside the explicit start transaction."""
        lock_project(connection, project_id)
        order = (
            connection.execute(
                text("SELECT version,profile_id FROM work_orders WHERE project_id=:p AND id=:o FOR UPDATE"),
                {"p": project_id, "o": order_id},
            )
            .mappings()
            .one_or_none()
        )
        if order is None:
            raise NotFound("work order not found")
        if order["version"] != expected_version:
            raise Conflict("work order changed; refresh before enabling team execution")
        readiness = self._inspect(connection, project_id, order_id)
        if readiness.blockers:
            raise Conflict("; ".join(readiness.blockers))
        busy = connection.scalar(
            text(
                "SELECT 1 FROM runs WHERE project_id=:p AND work_order_id=:o AND status='running' "
                "UNION ALL SELECT 1 FROM task_conversation_turns t JOIN task_conversations c "
                "ON c.project_id=t.project_id AND c.id=t.conversation_id WHERE c.project_id=:p "
                "AND c.work_order_id=:o AND t.status='running' LIMIT 1"
            ),
            {"p": project_id, "o": order_id},
        )
        if busy is not None:
            raise Conflict("wait for the current execution before enabling team mode")
        store = TaskSessionStore(self.gateway)
        session = store._existing(connection, project_id, order_id)
        if session is not None and session["execution_mode"] != "team":
            if session["profile_id"] != order["profile_id"]:
                raise Conflict("the saved session belongs to a different profile; create a separate task")
            connection.execute(
                text(
                    "UPDATE task_sessions SET execution_mode='team',status='open' "
                    "WHERE project_id=:p AND id=:s AND execution_mode='legacy'"
                ),
                {"p": project_id, "s": session["id"]},
            )
            self.gateway._event(
                connection,
                project_id,
                order_id,
                None,
                "task_session_team_enabled",
                {"session_id": str(session["id"]), "from_mode": "legacy", "queued_attempts": 0},
            )
        if session is None:
            profile_id = connection.scalar(
                text("SELECT profile_id FROM work_orders WHERE project_id=:p AND id=:o"),
                {"p": project_id, "o": order_id},
            )
            session = dict(
                connection.execute(
                    text(
                        "INSERT INTO task_sessions (id,project_id,actor_id,work_order_id, "
                        "root_work_order_id,"
                        "profile_id,kind,execution_mode,is_order_owner) "
                        "VALUES (:s,:p,:actor,:o,:o,:profile,'main','team',true) RETURNING *"
                    ),
                    {
                        "s": uuid4(),
                        "p": project_id,
                        "actor": self.gateway.actor_id,
                        "o": order_id,
                        "profile": profile_id,
                    },
                )
                .mappings()
                .one()
            )
            store._import_legacy(connection, project_id, order_id, session["id"], order_id)
            self.gateway._event(
                connection,
                project_id,
                order_id,
                None,
                "task_session_activated",
                {"session_id": str(session["id"]), "attempts_queued": 0},
            )
        session["execution_mode"] = "team"
        return store._read(connection, project_id, session)
