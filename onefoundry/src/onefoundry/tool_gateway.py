"""Single transaction owner for manual work, events, and approval decisions.

These methods only record local operator state. They never call a model or execute
an approved proposal.
"""

# All interpolated SQL fragments below are module constants; request values use binds.
# ruff: noqa: S608

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import Engine, bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError

from onefoundry import coordination_store, memory_store, run_audit, text_role_store, work_order_completion
from onefoundry.external_code_schemas import AdapterId
from onefoundry.gateway_errors import Conflict as Conflict
from onefoundry.gateway_errors import NotFound as NotFound
from onefoundry.gateway_errors import Unavailable as Unavailable
from onefoundry.memory_policy import candidate_id, work_memory_query
from onefoundry.memory_schemas import CandidateReview, ResultCreate
from onefoundry.runner_spool import RunnerSpool
from onefoundry.scripted_text_sdk import ScriptedTextResult, run_research_to_gtm
from onefoundry.workflow_schemas import (
    ApprovalCreate,
    ApprovalDecision,
    FinishCreate,
    HandoffCreate,
    WorkOrderAssign,
    WorkOrderBlock,
    WorkOrderCreate,
    WorkOrderReview,
    WorkOrderUnblock,
)

if TYPE_CHECKING:
    from onefoundry.artifact_workspace import ArtifactWorkspace
    from onefoundry.browser_static_fixtures import StaticFixtureInput
    from onefoundry.browser_static_jobs import BrowserStaticJobStore
    from onefoundry.company_memory_schemas import CaptureCreate, MemoryTransition
    from onefoundry.coordination_schemas import RuleCreate, RuleEnabled, RuleRevise
    from onefoundry.document_store import DocumentStore
    from onefoundry.execution_gateway import ExecutionGateway
    from onefoundry.gateway_extension_store import ExtensionStore
    from onefoundry.input_file_store import InputFileStore
    from onefoundry.live_text_schemas import LiveTextRestart
    from onefoundry.live_text_store import LiveTextStore
    from onefoundry.project_chief_inbox import ProjectChiefInbox
    from onefoundry.sales_store import SalesStore
    from onefoundry.settings import Settings
    from onefoundry.skill_schemas import SkillBind, SkillCreate, SkillRename, SkillVersionCreate
    from onefoundry.skill_store import SkillStore
    from onefoundry.source_store import SourceStore
    from onefoundry.synthetic_code_store import SyntheticCodeStore
    from onefoundry.task_conversation_schemas import NewConversation, TurnSend
    from onefoundry.task_conversation_store import TaskConversationStore
    from onefoundry.task_session_schemas import SessionContinueCreate, SessionMessageCreate
    from onefoundry.task_session_store import TaskSessionStore
    from onefoundry.zoho_mail_store import ZohoMailStore


class SyntheticRunFailure(Exception):
    pass


class SyntheticPersistenceUnknown(Exception):
    pass


WORK_ORDER_COLUMNS = (
    "id, project_id, title, instructions, profile_id, deliverable_kind, acceptance_checks, "
    "parent_work_order_id, source_run_id, origin_signal_id, chain_depth, status, version, block_reason, "
    "blocked_from_status, created_at"
)
RUN_COLUMNS = (
    "r.id, r.project_id, r.work_order_id, r.profile_id, "
    "r.task_title_snapshot AS work_order_title, "
    "r.task_instructions_snapshot AS work_order_instructions, "
    "s.profile_model, "
    "r.status, r.mode, r.tokens_used, r.cost_usd, r.outcome_reason, r.started_at, r.finished_at"
)
RUN_SNAPSHOT_JOIN = (
    "LEFT JOIN LATERAL (SELECT profile_model FROM run_profile_snapshots "
    "WHERE project_id=r.project_id AND run_id=r.id "
    "ORDER BY transition_index DESC LIMIT 1) s ON true "
)
APPROVAL_COLUMNS = (
    "id, project_id, work_order_id, run_id, action_type, target, content, "
    "proposal_digest, status, created_at, decided_at"
)
EVENT_COLUMNS = "id, project_id, work_order_id, run_id, actor_id, event_type, payload, created_at"


class ToolGateway:
    def _zoho_mail(self) -> "ZohoMailStore":
        from onefoundry.zoho_mail_store import ZohoMailStore

        if self.live_text_settings is None:
            raise Unavailable("Zoho mail settings are unavailable")
        return ZohoMailStore(self, self.live_text_settings)

    def __init__(
        self,
        engine: Engine,
        actor_id: str,
        runner_spool_root: str | None = None,
        runner_limits: dict[str, int] | None = None,
        runner_receipt_wait_seconds: int | None = None,
        sales_mailpit_host: str | None = None,
        sales_mailpit_smtp_port: int | None = None,
        sales_mailpit_http_port: int | None = None,
        live_text_settings: "Settings | None" = None,
    ) -> None:
        self.engine = engine
        self.actor_id = actor_id
        self.runner_spool = RunnerSpool(runner_spool_root) if runner_spool_root is not None else None
        self.runner_limits = runner_limits
        self.runner_receipt_wait_seconds = runner_receipt_wait_seconds
        self.sales_mailpit_host = sales_mailpit_host
        self.sales_mailpit_smtp_port = sales_mailpit_smtp_port
        self.sales_mailpit_http_port = sales_mailpit_http_port
        self.live_text_settings = live_text_settings
        self.browser_static_jobs: BrowserStaticJobStore | None = None
        self.input_file_store: InputFileStore | None = None
        self.document_store: DocumentStore | None = None
        self._task_conversations_reconciled = False

    def _browser_jobs(self) -> "BrowserStaticJobStore":
        if self.browser_static_jobs is None:
            raise Unavailable("static browser inspection is not configured")
        return self.browser_static_jobs

    def create_browser_static_fixture(
        self, project_id: UUID, run_id: UUID, data: "StaticFixtureInput"
    ) -> dict[str, Any]:
        return self._browser_jobs().fixtures.create(project_id, run_id, data)

    def get_browser_static_fixture(self, project_id: UUID, run_id: UUID, fixture_id: UUID) -> dict[str, Any]:
        return self._browser_jobs().fixtures.get(project_id, run_id, fixture_id)

    def _skill_store(self) -> "SkillStore":
        from onefoundry.skill_store import SkillStore

        return SkillStore(self)

    def _extensions(self) -> "ExtensionStore":
        from onefoundry.gateway_extension_store import ExtensionStore

        return ExtensionStore(self)

    def extension_catalog(self, project_id: UUID) -> dict[str, Any]:
        return self._extensions().catalog(project_id)

    def extension_packages(self, project_id: UUID) -> list[dict[str, Any]]:
        return self._extensions().list_packages(project_id)

    def extension_package(self, project_id: UUID, package_id: UUID) -> dict[str, Any]:
        return self._extensions().get_package(project_id, package_id)

    def create_extension_package(self, project_id: UUID, data: Any) -> dict[str, Any]:
        return self._extensions().create_package(project_id, data)

    def review_extension_package(
        self, project_id: UUID, package_id: UUID, action: str, data: Any
    ) -> dict[str, Any]:
        return self._extensions().request_package_change(project_id, package_id, action, data)

    def extension_connections(self, project_id: UUID) -> list[dict[str, Any]]:
        return self._extensions().list_connections(project_id)

    def create_extension_connection(self, project_id: UUID, data: Any) -> dict[str, Any]:
        return self._extensions().create_connection(project_id, data)

    def update_extension_connection(self, project_id: UUID, connection_id: UUID, data: Any) -> dict[str, Any]:
        return self._extensions().update_connection(project_id, connection_id, data)

    def extension_drafts(self, project_id: UUID) -> list[dict[str, Any]]:
        return self._extensions().list_drafts(project_id)

    def create_extension_draft(self, project_id: UUID, data: Any) -> dict[str, Any]:
        return self._extensions().create_draft(project_id, data)

    def decide_extension_draft(self, project_id: UUID, draft_id: UUID, data: Any) -> dict[str, Any]:
        return self._extensions().decide_draft(project_id, draft_id, data)

    def check_github_source_snapshot(
        self, source_store: "SourceStore", project_id: UUID, source_id: UUID
    ) -> dict[str, Any]:
        from onefoundry.github_source_tool import check_snapshot

        return check_snapshot(self, source_store, project_id, source_id)

    def propose_github_stage(self, project_id: UUID, run_id: UUID, source_id: UUID) -> dict[str, Any]:
        from onefoundry.github_stage_proposal import propose

        return propose(self, project_id, run_id, source_id)

    def github_stage_status(self, project_id: UUID, approval_id: UUID) -> dict[str, Any]:
        from onefoundry.github_stage_dispatch import read_status

        return read_status(self, project_id, approval_id)

    def dispatch_github_stage(self, project_id: UUID, approval_id: UUID) -> dict[str, Any]:
        from onefoundry.github_stage_dispatch import dispatch

        return dispatch(self, project_id, approval_id)

    def propose_github_fixture_fetch(
        self, project_id: UUID, run_id: UUID, source_id: UUID, idempotency_key: UUID
    ) -> dict[str, Any]:
        from onefoundry.github_fixture_fetch_proposal import propose

        return propose(self, project_id, run_id, source_id, idempotency_key)

    def github_fixture_fetch_status(self, project_id: UUID, approval_id: UUID) -> dict[str, Any]:
        from onefoundry.github_fixture_fetch_dispatch import read_status

        return read_status(self, project_id, approval_id)

    def dispatch_github_fixture_fetch(self, project_id: UUID, approval_id: UUID) -> dict[str, Any]:
        from onefoundry.github_fixture_fetch_dispatch import dispatch

        return dispatch(self, project_id, approval_id)

    def propose_github_fixture_snapshot(
        self, project_id: UUID, run_id: UUID, source_id: UUID, idempotency_key: UUID
    ) -> dict[str, Any]:
        from onefoundry.github_fixture_snapshot_proposal import propose

        return propose(self, project_id, run_id, source_id, idempotency_key)

    def github_fixture_snapshot_status(self, project_id: UUID, approval_id: UUID) -> dict[str, Any]:
        from onefoundry.github_fixture_snapshot_dispatch import read_status

        return read_status(self, project_id, approval_id)

    def dispatch_github_fixture_snapshot(self, project_id: UUID, approval_id: UUID) -> dict[str, Any]:
        from onefoundry.github_fixture_snapshot_dispatch import dispatch

        return dispatch(self, project_id, approval_id)

    def transition_project(
        self, project_id: UUID, expected_version: int, action: str, confirm_name: str | None = None
    ) -> dict[str, Any]:
        from onefoundry.project_lifecycle import ProjectLifecycleStore

        return ProjectLifecycleStore(self).transition(project_id, expected_version, action, confirm_name)

    def list_skills(self, project_id: UUID) -> list[dict[str, Any]]:
        return self._skill_store().list_skills(project_id)

    def create_skill(self, project_id: UUID, data: "SkillCreate") -> dict[str, Any]:
        return self._skill_store().create_skill(project_id, data)

    def rename_skill(self, project_id: UUID, skill_id: UUID, data: "SkillRename") -> dict[str, Any]:
        return self._skill_store().rename_skill(project_id, skill_id, data)

    def list_skill_versions(self, project_id: UUID, skill_id: UUID) -> list[dict[str, Any]]:
        return self._skill_store().list_versions(project_id, skill_id)

    def get_skill_version(self, project_id: UUID, skill_id: UUID, version: int) -> dict[str, Any]:
        return self._skill_store().get_version(project_id, skill_id, version)

    def add_skill_version(
        self, project_id: UUID, skill_id: UUID, data: "SkillVersionCreate"
    ) -> dict[str, Any]:
        return self._skill_store().add_version(project_id, skill_id, data)

    def list_profile_skills(self, project_id: UUID, profile_id: UUID) -> list[dict[str, Any]]:
        return self._skill_store().list_bindings(project_id, profile_id)

    def bind_profile_skill(
        self, project_id: UUID, profile_id: UUID, skill_id: UUID, data: "SkillBind"
    ) -> dict[str, Any]:
        return self._skill_store().bind(project_id, profile_id, skill_id, data)

    def unbind_profile_skill(
        self, project_id: UUID, profile_id: UUID, skill_id: UUID, expected_binding_id: UUID
    ) -> None:
        self._skill_store().unbind(project_id, profile_id, skill_id, expected_binding_id)

    def _live_text_store(self) -> "LiveTextStore":
        from onefoundry.live_text_store import LiveTextStore
        from onefoundry.model_secrets import ModelSecretStore

        if self.live_text_settings is None:
            raise Unavailable("live text settings unavailable")
        return LiveTextStore(
            self, self.live_text_settings, ModelSecretStore(self.live_text_settings.model_secret_root)
        )

    def run_live_text(
        self, project_id: UUID, order_id: UUID, restart: "LiveTextRestart | None" = None
    ) -> dict[str, Any]:
        try:
            return self._live_text_store().run(project_id, order_id, restart=restart)
        except Conflict as error:
            from onefoundry.onefoundry_budget import record_denial

            record_denial(self, project_id, None, order_id, str(error))
            raise

    def live_text_output(self, project_id: UUID, run_id: UUID) -> dict[str, Any]:
        return self._live_text_store().get_output(project_id, run_id)

    def reconcile_live_text(self) -> None:
        self._live_text_store().reconcile_interrupted()

    def _task_conversation_store(self) -> "TaskConversationStore":
        from onefoundry.model_secrets import ModelSecretStore
        from onefoundry.task_conversation_store import TaskConversationStore

        if self.live_text_settings is None:
            raise Unavailable("conversation settings unavailable")
        return TaskConversationStore(
            self, self.live_text_settings, ModelSecretStore(self.live_text_settings.model_secret_root)
        )

    def _task_session_store(self) -> "TaskSessionStore":
        from onefoundry.task_session_store import TaskSessionStore

        return TaskSessionStore(self)

    def open_task_session(self, project_id: UUID, order_id: UUID) -> tuple[dict[str, Any], bool]:
        return self._task_session_store().open(project_id, order_id)

    def get_task_session(self, project_id: UUID, order_id: UUID) -> dict[str, Any]:
        return self._task_session_store().get(project_id, order_id)

    def get_task_session_by_id(
        self, project_id: UUID, root_order_id: UUID, session_id: UUID
    ) -> dict[str, Any]:
        return self._task_session_store().get_by_id(project_id, root_order_id, session_id)

    def send_task_session_message(
        self,
        project_id: UUID,
        order_id: UUID,
        data: "SessionMessageCreate",
        *,
        session_id: UUID | None = None,
    ) -> tuple[dict[str, Any], bool]:
        return self._task_session_store().send(project_id, order_id, data, session_id=session_id)

    def continue_task_session(
        self,
        project_id: UUID,
        order_id: UUID,
        data: "SessionContinueCreate",
        *,
        session_id: UUID | None = None,
    ) -> tuple[dict[str, Any], bool]:
        return self._task_session_store().continue_work(project_id, order_id, data, session_id=session_id)

    def get_task_conversation(self, project_id: UUID, order_id: UUID) -> dict[str, Any]:
        return self._task_conversation_store().get(project_id, order_id)

    def list_task_conversations(self, project_id: UUID, order_id: UUID) -> list[dict[str, Any]]:
        return self._task_conversation_store().list_for_order(project_id, order_id)

    def get_task_conversation_by_id(
        self, project_id: UUID, order_id: UUID, conversation_id: UUID, before_sequence: int | None = None
    ) -> dict[str, Any]:
        return self._task_conversation_store().get_by_id(
            project_id, order_id, conversation_id, before_sequence
        )

    def lookup_task_conversation_turn(
        self, project_id: UUID, order_id: UUID, client_turn_id: UUID
    ) -> dict[str, Any]:
        return self._task_conversation_store().lookup_turn(project_id, order_id, client_turn_id)

    def open_task_conversation(self, project_id: UUID, order_id: UUID) -> tuple[dict[str, Any], bool]:
        self._require_task_conversations_reconciled()
        return self._task_conversation_store().open(project_id, order_id)

    def new_task_conversation(
        self, project_id: UUID, order_id: UUID, data: "NewConversation"
    ) -> tuple[dict[str, Any], bool]:
        self._require_task_conversations_reconciled()
        return self._task_conversation_store().new(project_id, order_id, data)

    def acknowledge_unknown_task_conversation_turn(
        self, project_id: UUID, order_id: UUID, turn_id: UUID
    ) -> dict[str, Any]:
        self._require_task_conversations_reconciled()
        return self._task_conversation_store().acknowledge_unknown(project_id, order_id, turn_id)

    def send_task_conversation(
        self,
        project_id: UUID,
        order_id: UUID,
        data: "TurnSend",
        *,
        source_kind: Literal[
            "operator_ui", "discord", "wechat", "system_triage", "system_followup"
        ] = "operator_ui",
        remote_inbound_id: UUID | None = None,
    ) -> tuple[dict[str, Any], bool]:
        self._require_task_conversations_reconciled()
        return self._task_conversation_store().send(
            project_id, order_id, data, source_kind=source_kind, remote_inbound_id=remote_inbound_id
        )

    def reconcile_task_conversations(self) -> None:
        self._task_conversation_store().reconcile_interrupted()
        self._task_conversations_reconciled = True

    def _project_chief_inbox(self) -> "ProjectChiefInbox":
        from onefoundry.project_chief_inbox import ProjectChiefInbox

        if self.live_text_settings is None:
            raise Unavailable("Chief conversation settings unavailable")
        return ProjectChiefInbox(self, self.live_text_settings)

    def list_project_chief_turns(self, project_id: UUID) -> list[dict[str, Any]]:
        return self._project_chief_inbox().list_turns(project_id)

    def enqueue_project_chief_turn(
        self,
        project_id: UUID,
        client_turn_id: UUID,
        message: str,
        *,
        source: Literal["operator_ui", "discord", "wechat"] = "operator_ui",
        remote_inbound_id: UUID | None = None,
    ) -> dict[str, Any]:
        self._require_task_conversations_reconciled()
        return self._project_chief_inbox().enqueue(
            project_id, client_turn_id, message, source=source, remote_inbound_id=remote_inbound_id
        )

    def process_project_chief_turn(self) -> dict[str, Any] | None:
        self._require_task_conversations_reconciled()
        return self._project_chief_inbox().process_one()

    def reconcile_project_chief_turns(self) -> None:
        self._project_chief_inbox().reconcile_interrupted()

    def _require_task_conversations_reconciled(self) -> None:
        if not self._task_conversations_reconciled:
            raise Unavailable("conversation reconciliation is pending; check readiness and retry")

    def _sales_store(self) -> "SalesStore":
        from onefoundry.sales_store import SalesStore

        if (
            self.sales_mailpit_host is None
            or self.sales_mailpit_smtp_port is None
            or self.sales_mailpit_http_port is None
        ):
            raise Unavailable("local Mailpit configuration unavailable")
        return SalesStore(
            self, self.sales_mailpit_host, self.sales_mailpit_smtp_port, self.sales_mailpit_http_port
        )

    def sales_eligibility(self, project_id: UUID, run_id: UUID) -> dict[str, Any]:
        return self._sales_store().eligibility(project_id, run_id)

    def create_sales_draft(self, project_id: UUID, run_id: UUID) -> dict[str, Any]:
        return self._sales_store().create(project_id, run_id)

    def get_sales_draft(self, project_id: UUID, run_id: UUID) -> dict[str, Any]:
        return self._sales_store().get(project_id, run_id)

    def propose_synthetic_candidate(
        self, project_id: UUID, run_id: UUID, scope: str
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        from onefoundry.synthetic_memory import propose_synthetic_candidate

        return propose_synthetic_candidate(self, project_id, run_id, scope)

    def _code_store(self) -> "SyntheticCodeStore":
        from onefoundry.synthetic_code_store import SyntheticCodeStore

        if (
            self.runner_spool is None
            or self.runner_limits is None
            or self.runner_receipt_wait_seconds is None
        ):
            raise Unavailable("synthetic runner configuration unavailable")
        return SyntheticCodeStore(
            self, self.runner_spool, self.runner_limits, self.runner_receipt_wait_seconds
        )

    def synthetic_code_eligibility(self, project_id: UUID, order_id: UUID) -> dict[str, Any]:
        return self._code_store().eligibility(project_id, order_id)

    def _execution_gateway(self) -> "ExecutionGateway":
        from onefoundry.execution_gateway import ExecutionGateway

        if (
            self.runner_spool is None
            or self.runner_limits is None
            or self.runner_receipt_wait_seconds is None
            or self.live_text_settings is None
        ):
            raise Unavailable("external coding configuration unavailable")
        return ExecutionGateway(
            self,
            RunnerSpool(str(self.runner_spool.root / "external")),
            self.runner_limits,
            self.runner_receipt_wait_seconds,
            self.live_text_settings.external_code_enabled,
            self.live_text_settings.external_code_capability_max_age_seconds,
        )

    def _artifact_workspace(self) -> "ArtifactWorkspace":
        from onefoundry.artifact_workspace import ArtifactWorkspace

        if self.live_text_settings is None:
            raise Unavailable("artifact workspace configuration unavailable")
        return ArtifactWorkspace(self.live_text_settings.artifact_workspace_root)

    def workspace_artifacts(self, project_id: UUID, run_id: UUID) -> list[dict[str, Any]]:
        from onefoundry.workspace_artifact_readback import list_artifacts

        return list_artifacts(self, project_id, run_id)

    def workspace_artifact(self, project_id: UUID, run_id: UUID, artifact_id: UUID) -> dict[str, Any]:
        from onefoundry.workspace_artifact_readback import preview

        return preview(self, project_id, run_id, artifact_id)

    def workspace_artifact_bytes(self, project_id: UUID, run_id: UUID, artifact_id: UUID) -> bytes:
        from onefoundry.workspace_artifact_readback import binary

        return binary(self, project_id, run_id, artifact_id)

    def external_code_eligibility(self, project_id: UUID, order_id: UUID) -> dict[str, Any]:
        return self._execution_gateway().eligibility(project_id, order_id)

    def launch_external_code(self, project_id: UUID, order_id: UUID, adapter_id: AdapterId) -> dict[str, Any]:
        return self._execution_gateway().launch(project_id, order_id, adapter_id)

    def launch_snapshot_codex_fixture(
        self, project_id: UUID, order_id: UUID, snapshot_attempt_id: UUID, idempotency_key: UUID
    ) -> dict[str, Any]:
        return self._execution_gateway().launch_snapshot_codex(
            project_id, order_id, snapshot_attempt_id, idempotency_key
        )

    def snapshot_codex_fixture_eligibility(self, project_id: UUID, order_id: UUID) -> dict[str, Any]:
        return self._execution_gateway().snapshot_codex_eligibility(project_id, order_id)

    def offline_source_probe_eligibility(self, project_id: UUID, order_id: UUID) -> dict[str, Any]:
        return self._execution_gateway().offline_probe_eligibility(project_id, order_id)

    def launch_offline_source_probe(
        self, project_id: UUID, order_id: UUID, source_id: UUID
    ) -> dict[str, Any]:
        return self._execution_gateway().launch(
            project_id, order_id, "offline_probe", "local_source_probe_v1", source_id
        )

    def external_code_detail(self, project_id: UUID, run_id: UUID) -> dict[str, Any]:
        return self._execution_gateway().detail(project_id, run_id)

    def external_code_progress(
        self, project_id: UUID, run_id: UUID, attempt_id: UUID, after: int
    ) -> dict[str, Any]:
        return self._execution_gateway().progress(project_id, run_id, attempt_id, after)

    def reconcile_external_code(self, project_id: UUID, run_id: UUID) -> dict[str, Any]:
        return self._execution_gateway().reconcile(project_id, run_id)

    def external_code_artifact(self, project_id: UUID, run_id: UUID, artifact_id: UUID) -> dict[str, Any]:
        return self._execution_gateway().preview(project_id, run_id, artifact_id)

    def launch_synthetic_code(self, project_id: UUID, order_id: UUID) -> dict[str, Any]:
        return self._code_store().launch(project_id, order_id)

    def synthetic_code_detail(self, project_id: UUID, run_id: UUID) -> dict[str, Any]:
        return self._code_store().detail(project_id, run_id)

    def reconcile_synthetic_code(self, project_id: UUID, run_id: UUID) -> dict[str, Any]:
        return self._code_store().reconcile(project_id, run_id)

    def synthetic_code_artifact(self, project_id: UUID, run_id: UUID, artifact_id: UUID) -> dict[str, Any]:
        return self._code_store().preview(project_id, run_id, artifact_id)

    def request_synthetic_merge(self, project_id: UUID, run_id: UUID) -> dict[str, Any]:
        return self._code_store().request_merge_approval(project_id, run_id)

    def coordination_status(self, project_id: UUID) -> dict[str, Any]:
        with self.engine.connect() as connection:
            self._require_project(connection, project_id)
            return coordination_store.status(connection, self.live_text_settings, project_id)

    def set_coordination_paused(
        self, project_id: UUID, paused: bool, expected_version: int
    ) -> dict[str, Any]:
        with self.engine.begin() as connection:
            self._require_project(connection, project_id)
            coordination_store.set_paused(self, connection, project_id, paused, expected_version)
            return coordination_store.status(connection, self.live_text_settings, project_id)

    def set_agent_rule_access(self, project_id: UUID, enabled: bool, expected_version: int) -> dict[str, Any]:
        from onefoundry.profile_hierarchy import lock_project

        with self.engine.begin() as connection:
            lock_project(connection, project_id)
            coordination_store.set_agent_rules_enabled(
                self, connection, project_id, enabled, expected_version
            )
            return coordination_store.status(connection, self.live_text_settings, project_id)

    def set_profile_tool_ceiling(
        self, project_id: UUID, tools: list[str], expected_version: int
    ) -> dict[str, Any]:
        from onefoundry.coordination_profiles import set_ceiling

        with self.engine.begin() as connection:
            self._require_project(connection, project_id)
            set_ceiling(self, connection, project_id, tools, expected_version)
            return coordination_store.status(connection, self.live_text_settings, project_id)

    def list_coordination_rules(self, project_id: UUID) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            self._require_project(connection, project_id)
            return coordination_store.list_rules(connection, project_id)

    def create_coordination_rule(self, project_id: UUID, data: "RuleCreate") -> dict[str, Any]:
        with self.engine.begin() as connection:
            self._require_project(connection, project_id)
            return coordination_store.create_rule(self, connection, project_id, data)

    def revise_coordination_rule(self, project_id: UUID, rule_id: UUID, data: "RuleRevise") -> dict[str, Any]:
        with self.engine.begin() as connection:
            self._require_project(connection, project_id)
            return coordination_store.revise_rule(self, connection, project_id, rule_id, data)

    def set_coordination_rule_enabled(
        self, project_id: UUID, rule_id: UUID, data: "RuleEnabled"
    ) -> dict[str, Any]:
        with self.engine.begin() as connection:
            self._require_project(connection, project_id)
            return coordination_store.set_rule_enabled(self, connection, project_id, rule_id, data)

    def list_coordination_signals(self, project_id: UUID, limit: int) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            self._require_project(connection, project_id)
            return coordination_store.list_signals(connection, project_id, limit)

    def _require_project(self, connection: Any, project_id: UUID) -> None:
        if connection.scalar(text("SELECT 1 FROM projects WHERE id = :id"), {"id": project_id}) is None:
            raise NotFound("project not found")

    def onefoundry_create_organization(self, project_id: UUID, data: Any) -> dict[str, Any]:
        from onefoundry.onefoundry_organization_store import create_organization

        return create_organization(self, project_id, data)

    def onefoundry_statement_create(self, project_id: UUID, data: Any) -> dict[str, Any]:
        from onefoundry.onefoundry_company_statements import create_statement

        return create_statement(self, project_id, data)

    def onefoundry_statement_decide(
        self, project_id: UUID, statement_id: UUID, decision: str, data: Any
    ) -> dict[str, Any]:
        from onefoundry.onefoundry_company_statements import decide_statement

        return decide_statement(self, project_id, statement_id, decision, data)

    def onefoundry_linked_order(
        self, project_id: UUID, organization_id: UUID, step: Literal["research", "draft"], data: Any
    ) -> dict[str, Any]:
        from onefoundry.onefoundry_workflow import linked_order

        try:
            return linked_order(self, project_id, organization_id, step, data)
        except Conflict as error:
            from onefoundry.onefoundry_budget import record_denial

            record_denial(self, project_id, organization_id, None, str(error))
            raise

    def onefoundry_candidates_from_run(
        self, project_id: UUID, organization_id: UUID, run_id: UUID
    ) -> list[dict[str, Any]]:
        from onefoundry.onefoundry_research_candidates import from_run

        return from_run(self, project_id, organization_id, run_id)

    def onefoundry_save_draft(self, project_id: UUID, organization_id: UUID, data: Any) -> dict[str, Any]:
        from onefoundry.onefoundry_draft import save_draft

        return save_draft(self, project_id, organization_id, data)

    def onefoundry_create_source(self, project_id: UUID, organization_id: UUID, data: Any) -> dict[str, Any]:
        from onefoundry.onefoundry_organization_store import create_source

        return create_source(self, project_id, organization_id, data)

    def onefoundry_create_claim(self, project_id: UUID, organization_id: UUID, data: Any) -> dict[str, Any]:
        from onefoundry.onefoundry_claim_store import create_claim

        return create_claim(self, project_id, organization_id, data)

    def onefoundry_review_claim(self, project_id: UUID, claim_id: UUID, data: Any) -> dict[str, Any]:
        from onefoundry.onefoundry_claim_store import review_claim

        return review_claim(self, project_id, claim_id, data)

    def onefoundry_qualify(self, project_id: UUID, organization_id: UUID, data: Any) -> dict[str, Any]:
        from onefoundry.onefoundry_organization_store import qualify

        return qualify(self, project_id, organization_id, data)

    def available_role_tools(self, project_id: UUID, run_id: UUID, profile_id: UUID) -> list[dict[str, Any]]:
        from onefoundry.role_tool_admission import available_tools

        return available_tools(self, project_id, run_id, profile_id)

    def invoke_role_tool(
        self,
        project_id: UUID,
        run_id: UUID,
        profile_id: UUID,
        call_id: str,
        tool_id: str,
        arguments: object,
        *,
        tool_version: int,
    ) -> dict[str, Any]:
        from onefoundry.role_tool_admission import invoke_tool

        return invoke_tool(
            self,
            project_id,
            run_id,
            profile_id,
            call_id,
            tool_id,
            arguments,
            tool_version=tool_version,
        )

    def _event(
        self,
        connection: Any,
        project_id: UUID,
        work_order_id: UUID | None,
        run_id: UUID | None,
        event_type: str,
        payload: dict[str, Any],
        actor_id: str | None = None,
    ) -> None:
        statement = text(
            "INSERT INTO events (id, project_id, work_order_id, run_id, actor_id, "
            "event_type, payload, created_at) "
            "VALUES (:id, :project_id, :work_order_id, :run_id, :actor_id, "
            ":event_type, :payload, clock_timestamp())"
        ).bindparams(bindparam("payload", type_=JSONB))
        connection.execute(
            statement,
            {
                "id": uuid4(),
                "project_id": project_id,
                "work_order_id": work_order_id,
                "run_id": run_id,
                "actor_id": actor_id or self.actor_id,
                "event_type": event_type,
                "payload": payload,
            },
        )

    def list_work_orders(self, project_id: UUID) -> list[dict[str, Any]]:
        from onefoundry.work_order_origin import origins

        with self.engine.connect() as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                text(
                    f"SELECT {WORK_ORDER_COLUMNS} FROM work_orders "
                    "WHERE project_id = :project_id ORDER BY created_at DESC, id DESC"
                ),
                {"project_id": project_id},
            ).mappings()
            orders = [dict(row) for row in rows]
            causes = origins(connection, project_id, [row["id"] for row in orders])
            return [{**row, "triggered_by": causes.get(row["id"])} for row in orders]

    def list_work_order_events(self, project_id: UUID, order_id: UUID) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            self._require_project(connection, project_id)
            exists = connection.scalar(
                text("SELECT 1 FROM work_orders WHERE project_id=:project_id AND id=:order_id"),
                {"project_id": project_id, "order_id": order_id},
            )
            if exists is None:
                raise NotFound("work order not found")
            rows = (
                connection.execute(
                    text(
                        f"SELECT {EVENT_COLUMNS} FROM events "
                        "WHERE project_id=:project_id AND work_order_id=:order_id "
                        "ORDER BY created_at,id LIMIT 1001"
                    ),
                    {"project_id": project_id, "order_id": order_id},
                )
                .mappings()
                .all()
            )
            if len(rows) > 1000:
                raise Conflict("work order activity exceeds supported read limit")
            return [dict(row) for row in rows]

    def create_work_order(self, project_id: UUID, data: WorkOrderCreate) -> dict[str, Any]:
        with self.engine.begin() as connection:
            return self.create_work_order_in_transaction(connection, project_id, data)

    def create_work_order_in_transaction(
        self, connection: Any, project_id: UUID, data: WorkOrderCreate
    ) -> dict[str, Any]:
        """Reuse order validation/events inside an atomic command transaction."""
        order_id = uuid4()
        self._require_project(connection, project_id)
        if (
            connection.scalar(
                text("SELECT 1 FROM profiles WHERE project_id = :project_id AND id = :id"),
                {"project_id": project_id, "id": data.profile_id},
            )
            is None
        ):
            raise NotFound("profile not found in project")
        row = (
            connection.execute(
                text(
                    "INSERT INTO work_orders "
                    "(id, project_id, actor_id, title, instructions, profile_id, "
                    "deliverable_kind, acceptance_checks, status) "
                    "VALUES (:id, :project_id, :actor_id, :title, :instructions, :profile_id, "
                    ":deliverable_kind, :acceptance_checks, 'draft') "
                    f"RETURNING {WORK_ORDER_COLUMNS}"
                ).bindparams(bindparam("acceptance_checks", type_=JSONB)),
                {
                    "id": order_id,
                    "project_id": project_id,
                    "actor_id": self.actor_id,
                    "title": data.title,
                    "instructions": data.instructions,
                    "profile_id": data.profile_id,
                    "deliverable_kind": data.deliverable_kind,
                    "acceptance_checks": data.acceptance_checks,
                },
            )
            .mappings()
            .one()
        )
        self._event(
            connection,
            project_id,
            order_id,
            None,
            "work_order_created",
            {
                "status": "draft",
                "profile_id": str(data.profile_id),
                "deliverable_kind": data.deliverable_kind,
            },
        )
        return dict(row)

    def settle_work_order(
        self, connection: Any, project_id: UUID, order_id: UUID, run_id: UUID, outcome: str
    ) -> str:
        return work_order_completion.settle(self, connection, project_id, order_id, run_id, outcome)

    def reconcile_external_action(
        self, connection: Any, project_id: UUID, order_id: UUID, run_id: UUID | None, *, confirmed: bool
    ) -> str:
        return work_order_completion.external_action_result(
            self, connection, project_id, order_id, run_id, confirmed=confirmed
        )

    def block_unavailable_capability(self, project_id: UUID, order_id: UUID, reason: str) -> None:
        with self.engine.begin() as connection:
            order = (
                connection.execute(
                    text("SELECT status FROM work_orders WHERE project_id=:p AND id=:o FOR UPDATE"),
                    {"p": project_id, "o": order_id},
                )
                .mappings()
                .one_or_none()
            )
            if order is None:
                raise NotFound("work order not found")
            if order["status"] != "ready":
                raise Conflict("work order changed during capability check")
            connection.execute(
                text(
                    "UPDATE work_orders SET status='blocked',block_reason=:reason,"
                    "blocked_from_status='ready' WHERE project_id=:p AND id=:o"
                ),
                {"reason": reason, "p": project_id, "o": order_id},
            )
            self._event(
                connection,
                project_id,
                order_id,
                None,
                "work_order_blocked",
                {"prior_status": "ready", "reason": reason, "source": "capability_preflight"},
            )
            coordination_store.emit_work_order_signal(self, connection, project_id, order_id, "blocked")

    def review_work_order(
        self,
        project_id: UUID,
        order_id: UUID,
        action: Literal["accept", "return", "reopen"],
        data: WorkOrderReview,
    ) -> dict[str, Any]:
        with self.engine.begin() as connection:
            order, run_id = self._lock_control_order(connection, project_id, order_id)
            self._check_version(order, data.expected_version)
            if action in {"accept", "return"} and order["status"] != "needs_review":
                raise Conflict("work order is not awaiting review")
            if action == "reopen":
                if order["status"] != "done":
                    raise Conflict("only a done work order can be reopened")
                latest_run_id = connection.scalar(
                    text(
                        "SELECT id FROM runs WHERE project_id=:p AND work_order_id=:o "
                        "ORDER BY started_at DESC,id DESC LIMIT 1"
                    ),
                    {"p": project_id, "o": order_id},
                )
                current = work_order_completion.evidence(connection, project_id, order_id, latest_run_id)
                if current["evidence"]:
                    raise Conflict("work order has recorded deliverable evidence")
            target = "done" if action == "accept" else "ready"
            from onefoundry.task_session_review import review as review_team

            review_team(self, connection, project_id, order_id, action, data.reason)
            saved = (
                connection.execute(
                    text(
                        "UPDATE work_orders SET status=:status WHERE project_id=:p AND id=:o "
                        f"RETURNING {WORK_ORDER_COLUMNS}"
                    ),
                    {"status": target, "p": project_id, "o": order_id},
                )
                .mappings()
                .one()
            )
            self._event(
                connection,
                project_id,
                order_id,
                run_id,
                f"work_order_{action}ed" if action != "return" else "work_order_returned",
                {"from_status": order["status"], "status": target, "reason": data.reason},
            )
            if target == "done":
                coordination_store.emit_work_order_signal(
                    self, connection, project_id, order_id, "done", run_id
                )
            return dict(saved)

    def _lock_control_order(
        self, connection: Any, project_id: UUID, order_id: UUID
    ) -> tuple[dict[str, Any], UUID | None]:
        # Manual actions lock run then order. Discover the run without a lock,
        # then take locks in that same order to avoid block/finish deadlocks.
        run_id = connection.scalar(
            text(
                "SELECT id FROM runs WHERE project_id=:project_id AND work_order_id=:id "
                "AND status='running' ORDER BY started_at DESC,id DESC LIMIT 1"
            ),
            {"project_id": project_id, "id": order_id},
        )
        if run_id is not None:
            connection.execute(
                text("SELECT id FROM runs WHERE project_id=:project_id AND id=:id FOR UPDATE"),
                {"project_id": project_id, "id": run_id},
            ).one()
        order = (
            connection.execute(
                text(
                    f"SELECT {WORK_ORDER_COLUMNS} FROM work_orders "
                    "WHERE project_id=:project_id AND id=:id FOR UPDATE"
                ),
                {"project_id": project_id, "id": order_id},
            )
            .mappings()
            .one_or_none()
        )
        if order is None:
            raise NotFound("work order not found")
        return dict(order), run_id

    @staticmethod
    def _check_version(order: dict[str, Any], expected_version: int) -> None:
        if order["version"] != expected_version:
            raise Conflict("work order changed; refresh and retry")

    def assign_work_order(self, project_id: UUID, order_id: UUID, data: WorkOrderAssign) -> dict[str, Any]:
        with self.engine.begin() as connection:
            order, _ = self._lock_control_order(connection, project_id, order_id)
            self._check_version(order, data.expected_version)
            if order["status"] not in {"draft", "ready"}:
                raise Conflict("assignment requires a draft or ready work order")
            if order["profile_id"] == data.profile_id:
                raise Conflict("profile already assigned")
            if connection.scalar(
                text(
                    "SELECT 1 FROM task_conversation_turns t JOIN task_conversations c "
                    "ON c.project_id=t.project_id AND c.id=t.conversation_id "
                    "WHERE c.project_id=:project_id AND c.work_order_id=:order_id "
                    "AND t.status='running' LIMIT 1"
                ),
                {"project_id": project_id, "order_id": order_id},
            ):
                raise Conflict("agent response is pending; refresh the conversation before reassigning")
            if (
                connection.scalar(
                    text("SELECT 1 FROM profiles WHERE project_id=:project_id AND id=:id"),
                    {"project_id": project_id, "id": data.profile_id},
                )
                is None
            ):
                raise NotFound("profile not found in project")
            changed = (
                connection.execute(
                    text(
                        "UPDATE work_orders SET profile_id=:profile_id "
                        "WHERE project_id=:project_id AND id=:id "
                        f"RETURNING {WORK_ORDER_COLUMNS}"
                    ),
                    {"profile_id": data.profile_id, "project_id": project_id, "id": order_id},
                )
                .mappings()
                .one()
            )
            self._event(
                connection,
                project_id,
                order_id,
                None,
                "work_order_assigned",
                {"from_profile_id": str(order["profile_id"]), "to_profile_id": str(data.profile_id)},
            )
            return dict(changed)

    def block_work_order(self, project_id: UUID, order_id: UUID, data: WorkOrderBlock) -> dict[str, Any]:
        with self.engine.begin() as connection:
            order, run_id = self._lock_control_order(connection, project_id, order_id)
            self._check_version(order, data.expected_version)
            if order["status"] not in {"draft", "ready", "running"}:
                raise Conflict("only draft, ready, or manual running work can be blocked")
            if order["status"] == "running":
                run = (
                    connection.execute(
                        text("SELECT status, mode FROM runs WHERE project_id=:project_id AND id=:id"),
                        {"project_id": project_id, "id": run_id},
                    )
                    .mappings()
                    .one_or_none()
                    if run_id is not None
                    else None
                )
                if run is None or run["status"] != "running" or run["mode"] != "manual_planning":
                    raise Conflict("in-flight execution cannot be blocked")
            changed = (
                connection.execute(
                    text(
                        "UPDATE work_orders SET status='blocked', block_reason=:reason, "
                        "blocked_from_status=:prior WHERE project_id=:project_id AND id=:id "
                        f"RETURNING {WORK_ORDER_COLUMNS}"
                    ),
                    {
                        "reason": data.reason,
                        "prior": order["status"],
                        "project_id": project_id,
                        "id": order_id,
                    },
                )
                .mappings()
                .one()
            )
            self._event(
                connection,
                project_id,
                order_id,
                run_id,
                "work_order_blocked",
                {"prior_status": order["status"], "reason": data.reason},
            )
            coordination_store.emit_work_order_signal(
                self, connection, project_id, order_id, "blocked", run_id
            )
            return dict(changed)

    def unblock_work_order(self, project_id: UUID, order_id: UUID, data: WorkOrderUnblock) -> dict[str, Any]:
        with self.engine.begin() as connection:
            order, run_id = self._lock_control_order(connection, project_id, order_id)
            self._check_version(order, data.expected_version)
            if order["status"] != "blocked":
                raise Conflict("work order is not blocked")
            prior = order["blocked_from_status"]
            if prior == "running":
                run = (
                    connection.execute(
                        text("SELECT status, mode FROM runs WHERE project_id=:project_id AND id=:id"),
                        {"project_id": project_id, "id": run_id},
                    )
                    .mappings()
                    .one_or_none()
                    if run_id is not None
                    else None
                )
                if run is None or run["status"] != "running" or run["mode"] != "manual_planning":
                    raise Conflict("manual run cannot be resumed")
            changed = (
                connection.execute(
                    text(
                        "UPDATE work_orders SET status=:prior, block_reason=NULL, blocked_from_status=NULL "
                        "WHERE project_id=:project_id AND id=:id "
                        f"RETURNING {WORK_ORDER_COLUMNS}"
                    ),
                    {"prior": prior, "project_id": project_id, "id": order_id},
                )
                .mappings()
                .one()
            )
            self._event(
                connection,
                project_id,
                order_id,
                run_id,
                "work_order_unblocked",
                {"restored_status": prior},
            )
            return dict(changed)

    @staticmethod
    def _require_unblocked_order(connection: Any, project_id: UUID, order_id: UUID) -> None:
        status = connection.scalar(
            text("SELECT status FROM work_orders WHERE project_id=:project_id AND id=:id FOR UPDATE"),
            {"project_id": project_id, "id": order_id},
        )
        if status == "blocked":
            raise Conflict("work order is blocked")

    def mark_ready(self, project_id: UUID, order_id: UUID) -> dict[str, Any]:
        from onefoundry.work_order_readiness import mark_ready

        with self.engine.begin() as connection:
            return mark_ready(self, connection, project_id, order_id)

    def start_run(self, project_id: UUID, order_id: UUID) -> dict[str, Any]:
        run_id = uuid4()
        with self.engine.begin() as connection:
            order = (
                connection.execute(
                    text(
                        "SELECT status, profile_id FROM work_orders "
                        "WHERE project_id = :project_id AND id = :id FOR UPDATE"
                    ),
                    {"project_id": project_id, "id": order_id},
                )
                .mappings()
                .one_or_none()
            )
            if order is None:
                raise NotFound("work order not found")
            if order["status"] != "ready":
                raise Conflict("work order is not ready")
            run_audit.insert_run(
                connection,
                project_id,
                run_id,
                order_id,
                order["profile_id"],
                self.actor_id,
                "manual_planning",
            )
            connection.execute(
                text("UPDATE work_orders SET status = 'running' WHERE project_id = :project_id AND id = :id"),
                {"project_id": project_id, "id": order_id},
            )
            self._event(
                connection,
                project_id,
                order_id,
                run_id,
                "run_started",
                {"status": "running", "profile_id": str(order["profile_id"]), "mode": "manual_planning"},
            )
            return self._run_row(connection, project_id, run_id)

    def _run_row(self, connection: Any, project_id: UUID, run_id: UUID) -> dict[str, Any]:
        row = (
            connection.execute(
                text(
                    f"SELECT {RUN_COLUMNS} FROM runs r "
                    + RUN_SNAPSHOT_JOIN
                    + "WHERE r.project_id = :project_id AND r.id = :id"
                ),
                {"project_id": project_id, "id": run_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise NotFound("run not found")
        return dict(row)

    def list_runs(self, project_id: UUID, query: str) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            self._require_project(connection, project_id)
            rows = connection.execute(
                text(
                    f"SELECT {RUN_COLUMNS} FROM runs r "
                    + RUN_SNAPSHOT_JOIN
                    + "WHERE r.project_id = :project_id AND "
                    "(:query = '' OR r.task_title_snapshot ILIKE :pattern "
                    "OR r.task_instructions_snapshot ILIKE :pattern "
                    "OR EXISTS (SELECT 1 FROM events e WHERE e.project_id = r.project_id "
                    "AND e.work_order_id = r.work_order_id AND e.event_type ILIKE :pattern)) "
                    "ORDER BY r.started_at DESC, r.id DESC"
                ),
                {"project_id": project_id, "query": query, "pattern": f"%{query}%"},
            ).mappings()
            return [dict(row) for row in rows]

    def get_run(self, project_id: UUID, run_id: UUID) -> dict[str, Any]:
        from onefoundry.work_order_origin import origins

        with self.engine.connect() as connection:
            run = self._run_row(connection, project_id, run_id)
            attempts = connection.scalar(
                text(
                    "SELECT count(*) FROM runs WHERE project_id=:project_id AND work_order_id=:work_order_id"
                ),
                {"project_id": project_id, "work_order_id": run["work_order_id"]},
            )
            scope = (
                "AND (run_id=:run_id OR (run_id IS NULL AND created_at>=:started_at "
                "AND created_at<=:finished_at)) "
                if attempts and attempts > 1
                else ""
            )
            events = connection.execute(
                text(
                    f"SELECT {EVENT_COLUMNS} FROM events "
                    "WHERE project_id = :project_id AND work_order_id = :work_order_id "
                    + scope
                    + "ORDER BY created_at, id"
                ),
                {
                    "project_id": project_id,
                    "work_order_id": run["work_order_id"],
                    "run_id": run_id,
                    "started_at": run["started_at"],
                    "finished_at": run["finished_at"] or datetime.now(UTC),
                },
            ).mappings()
            run["events"] = [dict(event) for event in events]
            run["profile_snapshots"] = run_audit.list_profiles(connection, project_id, run_id)
            run["skill_snapshots"] = run_audit.list_skills(connection, project_id, run_id)
            run["triggered_by"] = origins(connection, project_id, [run["work_order_id"]]).get(
                run["work_order_id"]
            )
            return run

    def handoff(self, project_id: UUID, run_id: UUID, data: HandoffCreate) -> dict[str, Any]:
        with self.engine.begin() as connection:
            run = (
                connection.execute(
                    text(
                        "SELECT work_order_id, profile_id, status, mode FROM runs "
                        "WHERE project_id = :project_id AND id = :id FOR UPDATE"
                    ),
                    {"project_id": project_id, "id": run_id},
                )
                .mappings()
                .one_or_none()
            )
            if run is None:
                raise NotFound("run not found")
            if run["status"] != "running" or run["mode"] != "manual_planning":
                raise Conflict("manual handoff requires a running manual run")
            self._require_unblocked_order(connection, project_id, run["work_order_id"])
            if run["profile_id"] == data.profile_id:
                raise Conflict("profile already assigned")
            if (
                connection.scalar(
                    text("SELECT 1 FROM profiles WHERE project_id = :project_id AND id = :id"),
                    {"project_id": project_id, "id": data.profile_id},
                )
                is None
            ):
                raise NotFound("profile not found in project")
            connection.execute(
                text("UPDATE runs SET profile_id = :profile_id WHERE project_id = :project_id AND id = :id"),
                {"profile_id": data.profile_id, "project_id": project_id, "id": run_id},
            )
            run_audit.record_handoff(
                connection,
                project_id,
                run_id,
                data.profile_id,
                self.actor_id,
                extension_worker_image=self.live_text_settings.extension_worker_image
                if self.live_text_settings
                else None,
            )
            connection.execute(
                text(
                    "UPDATE work_orders SET profile_id = :profile_id "
                    "WHERE project_id = :project_id AND id = :id"
                ),
                {"profile_id": data.profile_id, "project_id": project_id, "id": run["work_order_id"]},
            )
            self._event(
                connection,
                project_id,
                run["work_order_id"],
                run_id,
                "run_handed_off",
                {"from_profile_id": str(run["profile_id"]), "to_profile_id": str(data.profile_id)},
            )
            return self._run_row(connection, project_id, run_id)

    def finish(self, project_id: UUID, run_id: UUID, data: FinishCreate) -> dict[str, Any]:
        with self.engine.begin() as connection:
            run = (
                connection.execute(
                    text(
                        "SELECT work_order_id, status, mode FROM runs "
                        "WHERE project_id = :project_id AND id = :id FOR UPDATE"
                    ),
                    {"project_id": project_id, "id": run_id},
                )
                .mappings()
                .one_or_none()
            )
            if run is None:
                raise NotFound("run not found")
            if run["status"] != "running" or run["mode"] != "manual_planning":
                raise Conflict("manual finish requires a running manual run")
            self._require_unblocked_order(connection, project_id, run["work_order_id"])
            pending = connection.scalar(
                text(
                    "SELECT 1 FROM approval_requests WHERE project_id = :project_id "
                    "AND run_id = :id AND status = 'pending' LIMIT 1"
                ),
                {"project_id": project_id, "id": run_id},
            )
            if pending is not None:
                raise Conflict("pending approvals must be decided before finish")
            connection.execute(
                text(
                    "UPDATE runs SET status = :outcome, outcome_reason = :reason, "
                    "finished_at = clock_timestamp() "
                    "WHERE project_id = :project_id AND id = :id"
                ),
                {
                    "outcome": data.outcome,
                    "reason": data.reason,
                    "project_id": project_id,
                    "id": run_id,
                },
            )
            self.settle_work_order(connection, project_id, run["work_order_id"], run_id, data.outcome)
            self._event(
                connection,
                project_id,
                run["work_order_id"],
                run_id,
                "run_finished",
                {"outcome": data.outcome},
            )
            return self._run_row(connection, project_id, run_id)

    def request_approval(self, project_id: UUID, run_id: UUID, data: ApprovalCreate) -> dict[str, Any]:
        proposal = {
            "action_type": data.action_type,
            "target": data.target,
            "content": data.content,
        }
        proposal_bytes = json.dumps(
            proposal, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        digest = hashlib.sha256(proposal_bytes).hexdigest()
        approval_id = uuid4()
        with self.engine.begin() as connection:
            run = (
                connection.execute(
                    text(
                        "SELECT work_order_id, status, mode FROM runs "
                        "WHERE project_id = :project_id AND id = :id FOR UPDATE"
                    ),
                    {"project_id": project_id, "id": run_id},
                )
                .mappings()
                .one_or_none()
            )
            if run is None:
                raise NotFound("run not found")
            if run["status"] != "running" or run["mode"] != "manual_planning":
                raise Conflict("approval request requires a running manual run")
            self._require_unblocked_order(connection, project_id, run["work_order_id"])
            row = (
                connection.execute(
                    text(
                        "INSERT INTO approval_requests "
                        "(id, project_id, actor_id, work_order_id, run_id, action_type, target, "
                        "content, proposal_digest, status) "
                        "VALUES (:id, :project_id, :actor_id, :work_order_id, :run_id, :action_type, "
                        ":target, :content, :proposal_digest, 'pending') "
                        f"RETURNING {APPROVAL_COLUMNS}"
                    ),
                    {
                        "id": approval_id,
                        "project_id": project_id,
                        "actor_id": self.actor_id,
                        "work_order_id": run["work_order_id"],
                        "run_id": run_id,
                        "action_type": data.action_type,
                        "target": data.target,
                        "content": data.content,
                        "proposal_digest": digest,
                    },
                )
                .mappings()
                .one()
            )
            self._event(
                connection,
                project_id,
                run["work_order_id"],
                run_id,
                "approval_requested",
                {
                    "approval_id": str(approval_id),
                    "action_type": data.action_type,
                    "proposal_digest": digest,
                    "status": "pending",
                },
            )
            return dict(row)

    def list_approvals(self, project_id: UUID, status: str | None) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            self._require_project(connection, project_id)
            binding = (
                ", EXISTS (SELECT 1 FROM synthetic_merge_bindings smb WHERE "
                "smb.project_id = approval_requests.project_id AND "
                "smb.approval_id = approval_requests.id) AS synthetic_merge, "
                "EXISTS (SELECT 1 FROM sales_drafts sd WHERE "
                "sd.project_id = approval_requests.project_id AND "
                "sd.approval_id = approval_requests.id) AS synthetic_sales, "
                "EXISTS (SELECT 1 FROM outbound_emails oe WHERE "
                "oe.project_id = approval_requests.project_id AND "
                "oe.approval_id = approval_requests.id) AS outbound_email, "
                "(SELECT z.proposal->'message'->>'operation' FROM zoho_mail_actions z WHERE "
                "z.project_id=approval_requests.project_id AND z.approval_id=approval_requests.id) "
                "AS zoho_mail_operation, "
                "EXISTS (SELECT 1 FROM events e WHERE "
                "e.project_id=approval_requests.project_id AND "
                "e.run_id=approval_requests.run_id AND "
                "e.event_type='github_stage_proposed' AND "
                "e.payload->>'approval_id'=approval_requests.id::text AND "
                "e.payload->>'proposal_digest'=approval_requests.proposal_digest) AS github_stage, "
                "EXISTS (SELECT 1 FROM events e WHERE "
                "e.project_id=approval_requests.project_id AND "
                "e.run_id=approval_requests.run_id AND "
                "e.event_type='github_fixture_fetch_proposed' AND "
                "e.payload->>'approval_id'=approval_requests.id::text AND "
                "e.payload->>'proposal_digest'=approval_requests.proposal_digest) AS github_fetch, "
                "EXISTS (SELECT 1 FROM events e WHERE "
                "e.project_id=approval_requests.project_id AND "
                "e.run_id=approval_requests.run_id AND "
                "e.event_type='github_fixture_snapshot_proposed' AND "
                "e.payload->>'approval_id'=approval_requests.id::text AND "
                "e.payload->>'proposal_digest'=approval_requests.proposal_digest) AS github_snapshot, "
                "EXISTS (SELECT 1 FROM events e WHERE "
                "e.project_id=approval_requests.project_id AND "
                "e.run_id=approval_requests.run_id AND "
                "e.event_type='github_pr_comment_proposed' AND "
                "e.payload->>'approval_id'=approval_requests.id::text AND "
                "e.payload->>'proposal_digest'=approval_requests.proposal_digest) AS github_comment "
            )
            if status is None:
                statement = text(
                    f"SELECT {APPROVAL_COLUMNS}{binding} FROM approval_requests "
                    "WHERE project_id = :project_id ORDER BY created_at DESC, id DESC"
                )
                params: dict[str, Any] = {"project_id": project_id}
            else:
                statement = text(
                    f"SELECT {APPROVAL_COLUMNS}{binding} FROM approval_requests "
                    "WHERE project_id = :project_id AND status = :status "
                    "ORDER BY created_at DESC, id DESC"
                )
                params = {"project_id": project_id, "status": status}
            rows = connection.execute(
                statement,
                params,
            ).mappings()
            return [dict(row) for row in rows]

    def decide_approval(self, project_id: UUID, approval_id: UUID, data: ApprovalDecision) -> dict[str, Any]:
        code: SyntheticCodeStore | None = None
        approved_job: tuple[UUID, dict[str, Any]] | None = None
        sales_delivery = False
        outbound_email_bound = False
        outbound_email_id: UUID | None = None
        zoho_mail_action_id: UUID | None = None
        zoho_mail_operation: str | None = None
        with self.engine.begin() as connection:
            from onefoundry.capability_setup import lock_decision_project as lock_setup_project
            from onefoundry.coordination_profiles import lock_decision_project as lock_profile_project
            from onefoundry.gateway_extension_review import lock_review_project

            lock_setup_project(connection, project_id, approval_id)
            if data.decision == "approved":
                lock_profile_project(connection, project_id, approval_id)
            lock_review_project(self, connection, project_id, approval_id)
            approval = (
                connection.execute(
                    text(
                        f"SELECT {APPROVAL_COLUMNS} FROM approval_requests "
                        "WHERE project_id = :project_id AND id = :id FOR UPDATE"
                    ),
                    {"project_id": project_id, "id": approval_id},
                )
                .mappings()
                .one_or_none()
            )
            if approval is None:
                raise NotFound("approval request not found")
            if approval["status"] != "pending":
                raise Conflict("approval request already decided")
            if data.decision == "approved":
                from onefoundry.company_knowledge_proposal import validate_pending as validate_knowledge
                from onefoundry.repository_code_gateway import validate_pending as validate_maintenance
                from onefoundry.self_extension_activation import validate_pending as validate_activation

                validate_activation(self, connection, project_id, approval)
                validate_maintenance(self, connection, project_id, approval)
                validate_knowledge(self, connection, project_id, approval)
            self._require_unblocked_order(connection, project_id, approval["work_order_id"])
            from onefoundry.role_github_pr import pending_source_current

            pr_current = pending_source_current(connection, project_id, approval)
            if pr_current is False and data.decision == "approved":
                raise Conflict("saved GitHub PR source changed or proposal expired")
            from onefoundry.github_pr_comment_proposal import (
                pending_source_current as comment_source_current,
            )

            comment_current = comment_source_current(connection, project_id, approval)
            if comment_current is False and data.decision == "approved":
                raise Conflict("saved GitHub PR comment source changed or proposal expired")
            from onefoundry.github_stage_proposal import bound_source_state

            github_binding = bound_source_state(
                connection, project_id, approval, lock=data.decision == "approved"
            )
            github_stage = github_binding is not None
            if github_binding is not None and data.decision == "approved" and not github_binding[1]:
                raise Conflict("saved GitHub source changed; stage proposal was not approved")
            from onefoundry.github_fixture_fetch_contract import expiry
            from onefoundry.github_fixture_fetch_proposal import bound_intent

            fetch_binding = bound_intent(connection, project_id, approval, lock=data.decision == "approved")
            github_fetch = fetch_binding is not None
            if fetch_binding is not None and data.decision == "approved":
                if not fetch_binding[1]:
                    raise Conflict("saved GitHub source changed; fixture fetch was not approved")
                if datetime.now(UTC) >= expiry(fetch_binding[0].expires_at):
                    raise Conflict("fixture fetch proposal expired")
            from onefoundry.github_fixture_snapshot_proposal import bound_intent as bound_snapshot_intent

            snapshot_binding = bound_snapshot_intent(
                connection, project_id, approval, lock=data.decision == "approved"
            )
            github_snapshot = snapshot_binding is not None
            if snapshot_binding is not None and data.decision == "approved":
                if not snapshot_binding[1]:
                    raise Conflict("saved GitHub source changed; fixture snapshot was not approved")
                if datetime.now(UTC) >= expiry(snapshot_binding[0].expires_at):
                    raise Conflict("fixture snapshot proposal expired")
            row = (
                connection.execute(
                    text(
                        "UPDATE approval_requests SET status = :decision, decided_at = clock_timestamp() "
                        "WHERE project_id = :project_id AND id = :id "
                        f"RETURNING {APPROVAL_COLUMNS}"
                    ),
                    {"decision": data.decision, "project_id": project_id, "id": approval_id},
                )
                .mappings()
                .one()
            )
            self._event(
                connection,
                project_id,
                approval["work_order_id"],
                approval["run_id"],
                "approval_decided",
                {
                    "approval_id": str(approval["id"]),
                    "action_type": approval["action_type"],
                    "proposal_digest": approval["proposal_digest"],
                    "decision": data.decision,
                },
            )
            from onefoundry.coordination_profiles import apply_decision as apply_profile_decision

            apply_profile_decision(self, connection, project_id, dict(approval), data.decision)
            from onefoundry.gateway_extension_review import apply_decision as apply_extension_decision

            apply_extension_decision(self, connection, project_id, dict(approval), data.decision)
            from onefoundry.capability_setup import apply_decision as apply_setup_decision

            apply_setup_decision(self, connection, project_id, dict(approval), data.decision)
            from onefoundry.document_writes import apply_decision as apply_write_decision

            apply_write_decision(self, connection, project_id, dict(approval), data.decision)
            from onefoundry.document_action_store import apply_decision as apply_document_action_decision

            apply_document_action_decision(self, connection, project_id, dict(approval), data.decision)
            from onefoundry.smtp_outbound import apply_decision as apply_email_decision

            outbound_email_bound, outbound_email_id = apply_email_decision(
                self, connection, project_id, dict(approval), data.decision
            )
            from onefoundry.zoho_mail_actions import apply_decision as apply_zoho_decision

            zoho_mail_operation = connection.scalar(
                text(
                    "SELECT proposal->'message'->>'operation' FROM zoho_mail_actions "
                    "WHERE project_id=:p AND approval_id=:a"
                ),
                {"p": project_id, "a": approval_id},
            )
            if zoho_mail_operation is not None and self.live_text_settings is None:
                raise Unavailable("Zoho mail settings are unavailable")
            if self.live_text_settings is not None:
                zoho_mail_action_id = apply_zoho_decision(
                    self._zoho_mail(), connection, project_id, dict(approval), data.decision
                )
            coordination_store.emit_approval_signal(
                self, connection, project_id, dict(approval), data.decision
            )
            synthetic_merge = (
                connection.scalar(
                    text(
                        "SELECT 1 FROM synthetic_merge_bindings WHERE project_id=:project_id "
                        "AND approval_id=:approval_id"
                    ),
                    {"project_id": project_id, "approval_id": approval_id},
                )
                is not None
            )
            if synthetic_merge and data.decision == "approved":
                code = self._code_store()
                approved_job = code.approved_merge_job(
                    connection, project_id, approval_id, approval["run_id"]
                )
                if approved_job is None:
                    raise Conflict("merge binding missing")
                self._event(
                    connection,
                    project_id,
                    approval["work_order_id"],
                    approval["run_id"],
                    "synthetic_merge_queued",
                    {"approval_id": str(approval_id), "job_id": str(approved_job[0])},
                )
            synthetic_sales = connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM sales_drafts WHERE "
                    "project_id=:project_id AND approval_id=:approval_id)"
                ),
                {"project_id": project_id, "approval_id": approval_id},
            )
            if synthetic_sales:
                sales_delivery = self._sales_store().decision(
                    connection, project_id, dict(approval), data.decision
                )
            result = {
                **dict(row),
                "synthetic_merge": synthetic_merge,
                "synthetic_sales": synthetic_sales,
                "outbound_email": outbound_email_bound,
                "zoho_mail_operation": zoho_mail_operation,
                "github_stage": github_stage,
                "github_fetch": github_fetch,
                "github_snapshot": github_snapshot,
            }
        if approved_job is not None and code is not None:
            code._dispatch(project_id, approval["run_id"], approved_job[0], approved_job[1])
        if sales_delivery:
            self._sales_store().deliver_once(project_id, approval_id)
        if outbound_email_id is not None and self.live_text_settings is not None:
            from onefoundry.smtp_accounts import SmtpAccounts
            from onefoundry.smtp_dispatch import dispatch_once

            dispatch_once(self, SmtpAccounts(self, self.live_text_settings), project_id, outbound_email_id)
        if zoho_mail_action_id is not None:
            from onefoundry.zoho_mail_dispatch import dispatch_once as dispatch_zoho_once

            dispatch_zoho_once(self._zoho_mail(), project_id, zoho_mail_action_id)
        if data.decision == "approved" and approval["action_type"] == "deploy":
            from onefoundry.self_extension_activation import dispatch as dispatch_extension

            try:
                bound = json.loads(approval["content"])
            except ValueError:
                bound = {}
            if isinstance(bound, dict) and bound.get("kind") == "extension_activation_proposal_v1":
                dispatch_extension(self, project_id, approval_id)
        if data.decision == "approved" and approval["action_type"] == "other":
            from onefoundry.company_knowledge_proposal import apply_decision as apply_knowledge_decision

            apply_knowledge_decision(self, project_id, dict(approval))
        if data.decision == "rejected":
            from onefoundry.gateway_extension_wait_terminal import reject_callback_approval

            reject_callback_approval(self, project_id, approval_id)
        return result

    def list_results(self, project_id: UUID) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            self._require_project(connection, project_id)
            return memory_store.list_results(connection, project_id)

    def create_result(self, project_id: UUID, run_id: UUID, data: ResultCreate) -> dict[str, Any]:
        result_id = uuid4()
        with self.engine.begin() as connection:
            run = (
                connection.execute(
                    text(
                        "SELECT work_order_id, status, mode FROM runs "
                        "WHERE project_id = :project_id AND id = :id FOR UPDATE"
                    ),
                    {"project_id": project_id, "id": run_id},
                )
                .mappings()
                .one_or_none()
            )
            if run is None:
                raise NotFound("run not found")
            if run["status"] != "done" or run["mode"] != "manual_planning":
                raise Conflict("operator result requires a completed successful manual run")
            existing = connection.scalar(
                text("SELECT 1 FROM run_results WHERE project_id = :project_id AND run_id = :run_id"),
                {"project_id": project_id, "run_id": run_id},
            )
            if existing is not None:
                raise Conflict("run result already recorded")
            row = memory_store.insert_result(
                connection,
                result_id=result_id,
                project_id=project_id,
                actor_id=self.actor_id,
                run_id=run_id,
                summary=data.summary,
                scope=data.scope,
            )
            self._event(
                connection,
                project_id,
                run["work_order_id"],
                run_id,
                "run_result_recorded",
                {"result_id": str(result_id), "scope": data.scope, "source_kind": "operator_entered"},
            )
            return row

    def list_candidates(self, project_id: UUID, status: str | None) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            self._require_project(connection, project_id)
            return memory_store.list_candidates(connection, project_id, status)

    def create_candidate(self, project_id: UUID, result_id: UUID) -> dict[str, Any]:
        with self.engine.begin() as connection:
            result = memory_store.get_result(connection, project_id, result_id)
            if result is None:
                raise NotFound("result not found")
            # Results are immutable to the runtime role. Lock the parent run to
            # serialize candidate creation for this result.
            connection.execute(
                text("SELECT id FROM runs WHERE project_id = :project_id AND id = :id FOR UPDATE"),
                {"project_id": project_id, "id": result["run_id"]},
            )
            existing = connection.scalar(
                text(
                    "SELECT 1 FROM dreamcycle_candidates "
                    "WHERE project_id = :project_id AND source_result_id = :result_id"
                ),
                {"project_id": project_id, "result_id": result_id},
            )
            if existing is not None:
                raise Conflict("candidate already exists for result")
            proposal_id = candidate_id(result_id, str(result["summary"]))
            row = memory_store.insert_candidate(
                connection,
                candidate_id=proposal_id,
                project_id=project_id,
                actor_id=self.actor_id,
                source_result_id=result_id,
                summary=str(result["summary"]),
                scope=str(result["scope"]),
            )
            run = (
                connection.execute(
                    text("SELECT work_order_id FROM runs WHERE project_id = :project_id AND id = :id"),
                    {"project_id": project_id, "id": result["run_id"]},
                )
                .mappings()
                .one()
            )
            self._event(
                connection,
                project_id,
                run["work_order_id"],
                result["run_id"],
                "dreamcycle_candidate_created",
                {"candidate_id": str(proposal_id), "result_id": str(result_id), "scope": result["scope"]},
            )
            return row

    def review_candidate(self, project_id: UUID, proposal_id: UUID, data: CandidateReview) -> dict[str, Any]:
        from onefoundry.company_memory import CompanyMemory

        return CompanyMemory(self).review(project_id, proposal_id, data)

    def capture_company_memory(self, project_id: UUID, data: "CaptureCreate") -> dict[str, Any]:
        from onefoundry.company_memory import CompanyMemory

        return CompanyMemory(self).capture(project_id, data)

    def read_memory_source(self, project_id: UUID, source_id: UUID) -> dict[str, Any]:
        from onefoundry.company_memory import CompanyMemory

        return CompanyMemory(self).source(project_id, source_id)

    def memory_history(self, project_id: UUID, limit: int) -> list[dict[str, Any]]:
        from onefoundry.company_memory import CompanyMemory

        return CompanyMemory(self).history(project_id, limit)

    def transition_memory(
        self, project_id: UUID, memory_id: UUID, data: "MemoryTransition"
    ) -> dict[str, Any]:
        from onefoundry.company_memory import CompanyMemory

        return CompanyMemory(self).transition(project_id, memory_id, data)

    def read_memory(
        self, project_id: UUID, memory_id: UUID, *, scope: str | None = None, include_history: bool = False
    ) -> dict[str, Any]:
        from onefoundry.company_memory import CompanyMemory

        return CompanyMemory(self).read(project_id, memory_id, scope=scope, include_history=include_history)

    def retrieve_memory(
        self, project_id: UUID, query: str, limit: int, scope: str | None = None
    ) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            self._require_project(connection, project_id)
            return memory_store.retrieve_memory(
                connection, project_id, query, limit, scope, self.live_text_settings
            )

    @staticmethod
    def _text_role_eligibility(
        connection: Any, project_id: UUID, order: dict[str, Any], *, lock_profiles: bool = False
    ) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]:
        researcher = text_role_store.get_profile(
            connection, project_id, order["profile_id"], lock=lock_profiles
        )
        gtm = text_role_store.get_gtm_profile(connection, project_id, lock=lock_profiles)
        reason: str | None = None
        if order["status"] != "ready":
            reason = "work order is not ready"
        elif researcher is None or researcher["kind"] != "builtin" or researcher["name"] != "Researcher":
            reason = "assign the built-in Researcher profile"
        elif gtm is None:
            reason = "built-in GTM profile is missing"
        eligibility = {
            "eligible": reason is None,
            "reason": reason,
            "fixture": "research_to_gtm",
            "researcher_profile_id": researcher["id"]
            if researcher is not None and researcher["name"] == "Researcher"
            else None,
            "gtm_profile_id": gtm["id"] if gtm is not None else None,
        }
        return eligibility, researcher, gtm

    def synthetic_text_eligibility(self, project_id: UUID, order_id: UUID) -> dict[str, Any]:
        with self.engine.connect() as connection:
            self._require_project(connection, project_id)
            order = text_role_store.get_order(connection, project_id, order_id, lock=False)
            if order is None:
                raise NotFound("work order not found")
            eligibility, _, _ = self._text_role_eligibility(connection, project_id, order)
            return eligibility

    def start_synthetic_text(self, project_id: UUID, order_id: UUID) -> dict[str, Any]:
        run_id = uuid4()
        with self.engine.begin() as connection:
            order = text_role_store.get_order(connection, project_id, order_id, lock=True)
            if order is None:
                raise NotFound("work order not found")
            eligibility, researcher, gtm = self._text_role_eligibility(
                connection, project_id, order, lock_profiles=True
            )
            if not eligibility["eligible"] or researcher is None or gtm is None:
                raise Conflict(str(eligibility["reason"]))
            from onefoundry.onefoundry_budget import linked_workflow, reserve

            workflow_id = linked_workflow(connection, project_id, order_id)
            if workflow_id is not None:
                reserve(connection, self, project_id, workflow_id, f"model_attempt:{run_id}", "model_attempt")
            run_audit.insert_run(
                connection,
                project_id,
                run_id,
                order_id,
                researcher["id"],
                self.actor_id,
                "synthetic_text",
                extension_worker_image=self.live_text_settings.extension_worker_image
                if self.live_text_settings
                else None,
            )
            connection.execute(
                text("UPDATE work_orders SET status = 'running' WHERE project_id = :project_id AND id = :id"),
                {"project_id": project_id, "id": order_id},
            )
            self._event(
                connection,
                project_id,
                order_id,
                run_id,
                "synthetic_text_started",
                {
                    "status": "running",
                    "mode": "synthetic_text",
                    "fixture": "research_to_gtm",
                    "profile_id": str(researcher["id"]),
                    "source_kind": "synthetic_scripted_sdk",
                    "tools_active": False,
                    "tokens_used": 0,
                    "cost_usd": 0,
                },
            )
            memory_scope = (
                "project" if "project" in {researcher["memory_scope"], gtm["memory_scope"]} else None
            )
            memory_query = work_memory_query(str(order["title"]), str(order["instructions"]))
            memories = memory_store.retrieve_memory(
                connection, project_id, memory_query, 5, memory_scope, self.live_text_settings
            )
            return {
                "run_id": run_id,
                "order": order,
                "researcher": researcher,
                "gtm": gtm,
                "memories": memories,
            }

    def complete_synthetic_text(
        self,
        project_id: UUID,
        run_id: UUID,
        prepared: dict[str, Any],
        result: ScriptedTextResult,
    ) -> dict[str, Any]:
        researcher = prepared["researcher"]
        gtm = prepared["gtm"]
        memories = prepared["memories"]
        with self.engine.begin() as connection:
            run = (
                connection.execute(
                    text(
                        "SELECT work_order_id, profile_id, status, mode FROM runs "
                        "WHERE project_id = :project_id AND id = :id FOR UPDATE"
                    ),
                    {"project_id": project_id, "id": run_id},
                )
                .mappings()
                .one_or_none()
            )
            if run is None:
                raise NotFound("run not found")
            if (
                run["status"] != "running"
                or run["mode"] != "synthetic_text"
                or run["profile_id"] != researcher["id"]
            ):
                raise Conflict("synthetic run state changed")
            self._record_text_output(
                connection,
                project_id,
                run_id,
                run["work_order_id"],
                researcher,
                "Researcher",
                result.researcher_text,
                memories,
            )
            connection.execute(
                text("UPDATE runs SET profile_id = :gtm_id WHERE project_id = :project_id AND id = :id"),
                {"gtm_id": gtm["id"], "project_id": project_id, "id": run_id},
            )
            run_audit.record_handoff(
                connection,
                project_id,
                run_id,
                gtm["id"],
                f"profile:{researcher['id']}",
                used_profile=gtm,
                extension_worker_image=self.live_text_settings.extension_worker_image
                if self.live_text_settings
                else None,
            )
            connection.execute(
                text(
                    "UPDATE work_orders SET profile_id = :gtm_id WHERE project_id = :project_id AND id = :id"
                ),
                {"gtm_id": gtm["id"], "project_id": project_id, "id": run["work_order_id"]},
            )
            self._event(
                connection,
                project_id,
                run["work_order_id"],
                run_id,
                "synthetic_text_handed_off",
                {
                    "from_profile_id": str(researcher["id"]),
                    "to_profile_id": str(gtm["id"]),
                    "source_kind": "synthetic_scripted_sdk",
                },
                actor_id=f"profile:{researcher['id']}",
            )
            self._record_text_output(
                connection,
                project_id,
                run_id,
                run["work_order_id"],
                gtm,
                "GTM",
                result.gtm_text,
                memories,
            )
            connection.execute(
                text(
                    "UPDATE runs SET status = 'done', finished_at = clock_timestamp() "
                    "WHERE project_id = :project_id AND id = :id"
                ),
                {"project_id": project_id, "id": run_id},
            )
            self.settle_work_order(connection, project_id, run["work_order_id"], run_id, "done")
            self._event(
                connection,
                project_id,
                run["work_order_id"],
                run_id,
                "synthetic_text_finished",
                {"status": "done", "source_kind": "synthetic_scripted_sdk", "tokens_used": 0, "cost_usd": 0},
                actor_id=f"profile:{gtm['id']}",
            )
            from onefoundry.onefoundry_budget import linked_workflow, settle

            if linked_workflow(connection, project_id, run["work_order_id"]) is not None:
                reservation_id = connection.scalar(
                    text(
                        "SELECT id FROM onefoundry_budget_reservations WHERE project_id=:p "
                        "AND operation_key=:key"
                    ),
                    {"p": project_id, "key": f"model_attempt:{run_id}"},
                )
                if reservation_id is not None:
                    settle(connection, self, project_id, reservation_id, run_id, "settled", 0, 0.0)
            return {
                "run": self._run_row(connection, project_id, run_id),
                "outputs": text_role_store.list_outputs(connection, project_id, run_id),
            }

    def _record_text_output(
        self,
        connection: Any,
        project_id: UUID,
        run_id: UUID,
        order_id: UUID,
        profile: dict[str, Any],
        role: str,
        body: str,
        memories: list[dict[str, Any]],
    ) -> None:
        output_id = uuid4()
        actor = f"profile:{profile['id']}"
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        text_role_store.insert_output(
            connection,
            output_id=output_id,
            project_id=project_id,
            run_id=run_id,
            profile_id=profile["id"],
            actor_id=actor,
            role=role,
            configured_model=profile["model"],
            body=body,
            digest=digest,
        )
        for memory in memories:
            text_role_store.insert_source(
                connection,
                source_id=uuid4(),
                project_id=project_id,
                actor_id=actor,
                output_id=output_id,
                source_project_id=memory["project_id"],
                memory_item_id=memory["id"],
            )
        self._event(
            connection,
            project_id,
            order_id,
            run_id,
            "synthetic_text_output_recorded",
            {
                "output_id": str(output_id),
                "profile_id": str(profile["id"]),
                "role": role,
                "source_kind": "synthetic_scripted_sdk",
                "text_sha256": digest,
                "citation_count": len(memories),
                "tokens_used": 0,
                "cost_usd": 0,
            },
            actor_id=actor,
        )

    def fail_synthetic_text(self, project_id: UUID, run_id: UUID, *, unknown: bool) -> None:
        status = "unknown" if unknown else "failed"
        with self.engine.begin() as connection:
            run = (
                connection.execute(
                    text(
                        "SELECT work_order_id, profile_id, status, mode FROM runs "
                        "WHERE project_id = :project_id AND id = :id FOR UPDATE"
                    ),
                    {"project_id": project_id, "id": run_id},
                )
                .mappings()
                .one_or_none()
            )
            if run is None:
                raise NotFound("run not found")
            if run["status"] != "running" or run["mode"] != "synthetic_text":
                raise Conflict("synthetic run is not running")
            connection.execute(
                text(
                    "UPDATE runs SET status = :status, outcome_reason = :reason, "
                    "finished_at = clock_timestamp() WHERE project_id = :project_id AND id = :id"
                ),
                {
                    "status": status,
                    "reason": "scripted SDK completion uncertain"
                    if unknown
                    else "scripted SDK execution failed",
                    "project_id": project_id,
                    "id": run_id,
                },
            )
            self.settle_work_order(connection, project_id, run["work_order_id"], run_id, status)
            self._event(
                connection,
                project_id,
                run["work_order_id"],
                run_id,
                "synthetic_text_failed",
                {"status": status, "source_kind": "synthetic_scripted_sdk", "tokens_used": 0, "cost_usd": 0},
                actor_id=f"profile:{run['profile_id']}",
            )
            from onefoundry.onefoundry_budget import linked_workflow, settle

            if linked_workflow(connection, project_id, run["work_order_id"]) is not None:
                reservation_id = connection.scalar(
                    text(
                        "SELECT id FROM onefoundry_budget_reservations WHERE project_id=:p "
                        "AND operation_key=:key"
                    ),
                    {"p": project_id, "key": f"model_attempt:{run_id}"},
                )
                if reservation_id is not None:
                    settle(
                        connection,
                        self,
                        project_id,
                        reservation_id,
                        run_id,
                        "unknown" if unknown else "settled",
                        None,
                        None,
                    )

    def list_text_outputs(self, project_id: UUID, run_id: UUID) -> list[dict[str, Any]]:
        with self.engine.connect() as connection:
            self._run_row(connection, project_id, run_id)
            return text_role_store.list_outputs(connection, project_id, run_id)

    def read_synthetic_text(self, project_id: UUID, run_id: UUID) -> dict[str, Any]:
        with self.engine.connect() as connection:
            run = self._run_row(connection, project_id, run_id)
            return {"run": run, "outputs": text_role_store.list_outputs(connection, project_id, run_id)}

    def run_synthetic_text(self, project_id: UUID, order_id: UUID) -> dict[str, Any]:
        """Mediate the one allowed SDK fixture and its atomic owner state."""
        prepared = self.start_synthetic_text(project_id, order_id)
        run_id = prepared["run_id"]
        try:
            result = run_research_to_gtm(
                title=prepared["order"]["title"],
                work_instructions=prepared["order"]["instructions"],
                researcher=prepared["researcher"],
                gtm=prepared["gtm"],
                source_memories=prepared["memories"],
            )
        except Exception:
            self.fail_synthetic_text(project_id, run_id, unknown=False)
            raise SyntheticRunFailure("scripted SDK run failed") from None
        try:
            return self.complete_synthetic_text(project_id, run_id, prepared, result)
        except (SQLAlchemyError, Conflict):
            try:
                observed = self.read_synthetic_text(project_id, run_id)
            except SQLAlchemyError:
                raise SyntheticPersistenceUnknown("synthetic result persistence uncertain") from None
            if observed["run"]["status"] == "done" and len(observed["outputs"]) == 2:
                return observed
            if observed["run"]["status"] == "running":
                self.fail_synthetic_text(project_id, run_id, unknown=True)
            raise SyntheticPersistenceUnknown("synthetic result persistence uncertain") from None
