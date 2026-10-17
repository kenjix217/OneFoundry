"""Thin HTTP entrypoint and database readiness probe."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from onefoundry.api import make_router
from onefoundry.artifact_workspace import ArtifactInvalid, ArtifactWorkspace
from onefoundry.browser_static_fixture_api import make_browser_static_fixture_router
from onefoundry.browser_static_fixtures import StaticFixturePolicy, StaticFixtureStore
from onefoundry.browser_static_job_api import make_browser_static_job_router
from onefoundry.browser_static_jobs import BrowserStaticJobStore
from onefoundry.browser_target import BrowserTargetLimits
from onefoundry.chief_workspace_api import make_chief_workspace_router
from onefoundry.connector_api import make_connector_router
from onefoundry.connector_store import ConnectorStore
from onefoundry.coordination_api import make_coordination_router
from onefoundry.coordination_launcher import CoordinationLauncher
from onefoundry.document_api import make_document_router
from onefoundry.document_store import DocumentStore
from onefoundry.external_code_api import make_external_code_router
from onefoundry.gateway_extension_api import make_extension_router
from onefoundry.gateway_extension_portability_api import make_extension_portability_router
from onefoundry.gateway_extension_runtime_api import make_extension_runtime_router
from onefoundry.gateway_extension_webhook_api import make_extension_webhook_router
from onefoundry.gateway_extension_worker_runtime import ExtensionWorkerRuntime
from onefoundry.github_pr_comment_api import make_github_pr_comment_router
from onefoundry.github_tool_api import make_github_tool_router
from onefoundry.infra_monitor import make_infra_router
from onefoundry.input_file_api import make_input_file_router
from onefoundry.input_file_bytes import InputFileBytes
from onefoundry.input_file_store import InputFileStore
from onefoundry.live_text_api import make_live_text_router
from onefoundry.mail_api import make_mail_router
from onefoundry.mail_store import MailStore
from onefoundry.media_capture_api import make_media_capture_router
from onefoundry.memory_api import make_memory_router
from onefoundry.model_gateway import ModelGateway, make_model_router
from onefoundry.model_secrets import ModelSecretStore, SecretStoreError
from onefoundry.onefoundry_capability_api import make_onefoundry_capability_router
from onefoundry.onefoundry_inbox_api import make_onefoundry_inbox_router
from onefoundry.onefoundry_organization_api import make_onefoundry_organization_router
from onefoundry.project_chief_api import make_project_chief_router
from onefoundry.project_lifecycle import ActiveProjectWriteMiddleware
from onefoundry.project_lifecycle_api import make_project_lifecycle_router
from onefoundry.projects import ProjectStore
from onefoundry.remote_channel_api import make_remote_channel_router
from onefoundry.remote_channel_core import RemoteChannelCore
from onefoundry.remote_channel_runtime import RemoteChannelRuntime
from onefoundry.remote_channel_runtime_api import make_remote_channel_runtime_router
from onefoundry.sales_api import make_sales_router
from onefoundry.security import WriteOriginMiddleware
from onefoundry.settings import Settings
from onefoundry.skill_api import make_skill_router
from onefoundry.smtp_accounts import SmtpAccounts
from onefoundry.smtp_api import make_smtp_router
from onefoundry.source_api import make_source_router
from onefoundry.source_store import SourceStore
from onefoundry.synthetic_code_api import make_synthetic_code_router
from onefoundry.synthetic_memory_api import make_synthetic_memory_router
from onefoundry.task_conversation_api import make_task_conversation_router
from onefoundry.task_session_api import make_task_session_router
from onefoundry.text_role_api import make_text_role_router
from onefoundry.tool_gateway import ToolGateway
from onefoundry.video_edit_api import make_video_edit_router
from onefoundry.workflow_api import make_workflow_router
from onefoundry.workspace_artifact_api import make_workspace_artifact_router
from onefoundry.zoho_mail_api import make_zoho_mail_router
from runner.browser_static_job_spool import BrowserStaticJobSpool


def create_app(settings: Settings, engine: Engine | None = None) -> FastAPI:
    owned_engine = engine is None
    db = engine or create_engine(settings.database_url, pool_pre_ping=True)
    if settings.video_edit_enabled:
        InputFileBytes(settings.video_edit_asset_root)
    model_secrets = ModelSecretStore(settings.model_secret_root)
    model_gateway = ModelGateway(db, settings.operator_actor_id, model_secrets, settings)
    source_store = SourceStore(db, settings.operator_actor_id, settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            try:
                source_store.prepare_secrets()
                mail_store.prepare_secrets()
                smtp_accounts.prepare_secrets()
                connector_store.prepare_secrets()
                zoho_mail_store.prepare_secrets()
                source_store.reconcile_orphan_secrets()
                remote_channels.prepare_secrets()
                model_gateway.reconcile_orphan_secrets()
                gateway.reconcile_live_text()
                gateway.reconcile_task_conversations()
                gateway.reconcile_project_chief_turns()
                from onefoundry.zoho_mail_dispatch import reconcile_interrupted as reconcile_zoho_mail

                reconcile_zoho_mail(zoho_mail_store)
                launcher.gateway.reconcile_task_conversations()
            except SQLAlchemyError:
                pass  # Readiness reports schema or database failure; no files are removed.
            await remote_channel_runtime.start()
            await launcher.start()
            await extension_worker_runtime.start()
            yield
        finally:
            await extension_worker_runtime.stop()
            await launcher.stop()
            await remote_channel_runtime.stop()
            if owned_engine:
                db.dispose()

    app = FastAPI(title="OneFoundry", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def redact_validation_input(_request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {key: value for key, value in error.items() if key not in {"input", "ctx"}}
                    for error in exc.errors()
                ]
            },
        )

    from onefoundry.runtime_maintenance import RuntimeMaintenance
    from onefoundry.runtime_maintenance_http import attach_write_guards

    maintenance = RuntimeMaintenance(
        db,
        Path(settings.runner_spool_root) / "external",
        settings.repository_code_policy.maintenance_lock_seconds,
    )

    def include_router(router: APIRouter) -> None:
        attach_write_guards(router, maintenance)
        app.include_router(router)

    app.add_middleware(WriteOriginMiddleware, public_port=settings.app_public_port)
    app.add_middleware(ActiveProjectWriteMiddleware, engine=db)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    include_router(make_router(ProjectStore(db, settings.operator_actor_id, model_secrets)))
    include_router(make_onefoundry_capability_router(db))
    include_router(make_model_router(model_gateway))
    include_router(make_source_router(source_store))
    gateway = ToolGateway(
        db,
        settings.operator_actor_id,
        settings.runner_spool_root,
        settings.runner_limits,
        settings.runner_receipt_wait_seconds,
        settings.sales_mailpit_host,
        settings.sales_mailpit_smtp_port,
        settings.sales_mailpit_http_port,
        settings,
    )
    connector_store = ConnectorStore(gateway, settings)
    extension_worker_runtime = ExtensionWorkerRuntime(gateway)
    mail_store = MailStore(gateway, settings, connectors=connector_store)
    smtp_accounts = SmtpAccounts(gateway, settings)
    zoho_mail_store = gateway._zoho_mail()
    launcher_gateway = ToolGateway(
        db,
        settings.coordination_launcher_actor_id,
        runner_spool_root=settings.runner_spool_root,
        runner_limits=settings.runner_limits,
        runner_receipt_wait_seconds=settings.runner_receipt_wait_seconds,
        live_text_settings=settings,
    )
    launcher_gateway.input_file_store = InputFileStore(launcher_gateway, settings)
    launcher_connectors = ConnectorStore(launcher_gateway, settings)
    launcher = CoordinationLauncher(
        launcher_gateway,
        settings,
        MailStore(launcher_gateway, settings, connectors=launcher_connectors),
        DocumentStore(launcher_gateway, settings, launcher_connectors),
        maintenance=maintenance,
    )
    if settings.browser_static_enabled:
        runner_root = Path(settings.runner_spool_root)
        if runner_root.is_symlink() or not runner_root.is_dir():
            raise ValueError("browser runner spool root is unavailable")
        browser_root = Path(settings.browser_static_spool_root)
        browser_root.mkdir(mode=0o700, exist_ok=True)
        browser_spool = BrowserStaticJobSpool(browser_root)
        browser_policy = StaticFixturePolicy(
            BrowserTargetLimits(
                settings.browser_static_max_url_chars,
                settings.browser_static_max_viewport_width,
                settings.browser_static_max_viewport_height,
                settings.browser_static_max_page_bytes,
                settings.browser_static_max_timeout_seconds,
                settings.browser_static_max_lifetime_seconds,
            ),
            settings.browser_static_max_fixture_bytes,
        )
        gateway.browser_static_jobs = BrowserStaticJobStore(
            gateway,
            StaticFixtureStore(gateway, browser_policy),
            browser_spool,
            settings.browser_static_image_id,
            settings.browser_static_claim_timeout_seconds,
        )
        app.state.browser_static_jobs = gateway.browser_static_jobs
        include_router(make_browser_static_fixture_router(gateway))
        include_router(make_browser_static_job_router(gateway))
    remote_channels = RemoteChannelCore(
        gateway, settings.model_secret_root, settings.remote_identity_challenge_seconds
    )
    app.state.remote_channels = remote_channels
    remote_channel_runtime = RemoteChannelRuntime(remote_channels, settings, maintenance=maintenance)
    app.state.remote_channel_runtime = remote_channel_runtime
    include_router(make_workflow_router(gateway))
    if settings.onefoundry_business_mode in {"records", "manual", "event", "inbox"}:
        include_router(make_onefoundry_organization_router(gateway))
    if settings.onefoundry_business_mode == "inbox":
        include_router(make_onefoundry_inbox_router(gateway))
    input_file_store = InputFileStore(gateway, settings)
    gateway.input_file_store = input_file_store
    include_router(make_input_file_router(input_file_store))
    include_router(make_github_tool_router(gateway, source_store))
    include_router(make_project_lifecycle_router(gateway))
    include_router(make_memory_router(gateway))
    include_router(make_text_role_router(gateway))
    include_router(make_live_text_router(gateway))
    include_router(make_task_conversation_router(gateway))
    include_router(make_task_session_router(gateway))
    include_router(make_chief_workspace_router(gateway))
    include_router(make_project_chief_router(gateway))
    include_router(make_remote_channel_router(remote_channels))
    include_router(make_remote_channel_runtime_router(remote_channel_runtime))
    include_router(make_skill_router(gateway))
    include_router(make_extension_portability_router(gateway))
    include_router(make_extension_router(gateway))
    include_router(make_extension_runtime_router(gateway))
    include_router(make_extension_webhook_router(gateway))
    include_router(make_coordination_router(gateway))
    include_router(make_mail_router(mail_store))
    include_router(make_smtp_router(smtp_accounts))
    include_router(make_zoho_mail_router(zoho_mail_store))
    include_router(make_connector_router(connector_store))
    include_router(make_document_router(DocumentStore(gateway, settings, connector_store)))
    include_router(make_synthetic_code_router(gateway))
    include_router(make_external_code_router(gateway))
    include_router(make_sales_router(gateway))
    include_router(make_synthetic_memory_router(gateway))
    include_router(make_workspace_artifact_router(gateway))
    include_router(make_media_capture_router(gateway))
    include_router(make_video_edit_router(gateway))
    include_router(make_github_pr_comment_router(gateway))
    include_router(
        make_infra_router(Path(settings.infra_snapshot_path), settings.infra_snapshot_max_age_seconds)
    )

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "live"}

    @app.get("/api/onefoundry/business-mode")
    def onefoundry_business_mode() -> dict[str, str]:
        return {"mode": settings.onefoundry_business_mode}

    @app.get("/health/ready")
    def ready() -> dict[str, str]:
        try:
            with db.connect() as connection:
                connection.execute(text("SELECT 1"))
                connection.execute(
                    text("SELECT id, status, version, trash_from_status FROM projects WHERE false")
                )
                connection.execute(text("SELECT id FROM events WHERE false"))
                if settings.video_edit_enabled:
                    connection.execute(text("SELECT id FROM video_edit_assets WHERE false"))
                    connection.execute(text("SELECT id FROM video_edit_jobs WHERE false"))
                if settings.onefoundry_business_mode in {"records", "manual", "event", "inbox"}:
                    connection.execute(text("SELECT id FROM onefoundry_organizations WHERE false"))
                    connection.execute(text("SELECT id FROM onefoundry_sources WHERE false"))
                    connection.execute(text("SELECT id FROM onefoundry_claims WHERE false"))
                    connection.execute(text("SELECT id FROM onefoundry_workflows WHERE false"))
                    connection.execute(text("SELECT id FROM onefoundry_work_order_subjects WHERE false"))
                if settings.onefoundry_business_mode in {"manual", "event", "inbox"}:
                    connection.execute(text("SELECT id FROM onefoundry_company_statements WHERE false"))
                    connection.execute(text("SELECT id FROM onefoundry_budget_snapshots WHERE false"))
                    connection.execute(text("SELECT id FROM onefoundry_budget_reservations WHERE false"))
                    connection.execute(text("SELECT id FROM onefoundry_context_packets WHERE false"))
                    connection.execute(text("SELECT id FROM onefoundry_drafts WHERE false"))
                connection.execute(text("SELECT id, action_kind FROM coordination_intents WHERE false"))
                connection.execute(text("SELECT id FROM coordination_signals WHERE false"))
                connection.execute(text("SELECT id FROM coordination_rules WHERE false"))
                connection.execute(text("SELECT id FROM coordination_rule_versions WHERE false"))
                connection.execute(text("SELECT project_id FROM coordination_controls WHERE false"))
                connection.execute(text("SELECT id FROM mail_accounts WHERE false"))
                connection.execute(text("SELECT id FROM smtp_accounts WHERE false"))
                connection.execute(
                    text("SELECT id,credential_revision FROM zoho_mail_connections WHERE false")
                )
                connection.execute(text("SELECT id FROM zoho_mail_actions WHERE false"))
                connection.execute(text("SELECT id FROM outbound_emails WHERE false"))
                connection.execute(text("SELECT id FROM coordination_profile_proposals WHERE false"))
                connection.execute(text("SELECT id FROM connector_accounts WHERE false"))
                connection.execute(text("SELECT id FROM document_sources WHERE false"))
                connection.execute(text("SELECT id FROM document_files WHERE false"))
                connection.execute(text("SELECT id FROM document_writes WHERE false"))
                connection.execute(text("SELECT id, model_account_id FROM profiles WHERE false"))
                connection.execute(text("SELECT id FROM model_accounts WHERE false"))
                connection.execute(text("SELECT id FROM model_catalog WHERE false"))
                connection.execute(text("SELECT id FROM live_text_run_bindings WHERE false"))
                connection.execute(text("SELECT id FROM skills WHERE false"))
                connection.execute(text("SELECT id FROM skill_versions WHERE false"))
                connection.execute(text("SELECT id FROM profile_skill_bindings WHERE false"))
                connection.execute(text("SELECT id FROM run_skill_snapshots WHERE false"))
                connection.execute(text("SELECT id FROM live_text_outputs WHERE false"))
                connection.execute(text("SELECT id FROM live_text_output_sources WHERE false"))
                connection.execute(text("SELECT id FROM task_conversations WHERE false"))
                connection.execute(text("SELECT id FROM task_conversation_turns WHERE false"))
                connection.execute(text("SELECT id,credential_revision FROM remote_channels WHERE false"))
                connection.execute(text("SELECT id,status FROM remote_inbound WHERE false"))
                connection.execute(text("SELECT id,status FROM remote_outbound WHERE false"))
                connection.execute(
                    text("SELECT id, version, block_reason, blocked_from_status FROM work_orders WHERE false")
                )
                connection.execute(
                    text("SELECT id, task_title_snapshot, task_instructions_snapshot FROM runs WHERE false")
                )
                connection.execute(text("SELECT id FROM run_profile_snapshots WHERE false"))
                connection.execute(text("SELECT id FROM approval_requests WHERE false"))
                if settings.browser_static_enabled:
                    connection.execute(text("SELECT id FROM browser_static_fixtures WHERE false"))
                    connection.execute(text("SELECT id FROM browser_static_jobs WHERE false"))
                connection.execute(
                    text(
                        "SELECT id, source_sales_draft_id, source_runner_job_id, source_sha256 "
                        "FROM run_results WHERE false"
                    )
                )
                connection.execute(text("SELECT id FROM dreamcycle_candidates WHERE false"))
                connection.execute(text("SELECT id FROM memory_items WHERE false"))
                connection.execute(text("SELECT id FROM text_outputs WHERE false"))
                connection.execute(text("SELECT id FROM text_output_sources WHERE false"))
                connection.execute(text("SELECT id FROM runner_jobs WHERE false"))
                connection.execute(text("SELECT id FROM runner_artifacts WHERE false"))
                connection.execute(
                    text(
                        "SELECT id,source_id,source_root_id,source_relative_path,source_branch,"
                        "source_revision FROM external_code_attempts WHERE false"
                    )
                )
                connection.execute(text("SELECT id FROM external_code_artifacts WHERE false"))
                connection.execute(text("SELECT id,status FROM workspace_artifacts WHERE false"))
                connection.execute(text("SELECT id FROM github_accounts WHERE false"))
                connection.execute(text("SELECT id FROM project_sources WHERE false"))
                connection.execute(text("SELECT id FROM synthetic_merge_bindings WHERE false"))
                connection.execute(text("SELECT id FROM sales_drafts WHERE false"))
        except SQLAlchemyError as exc:
            raise HTTPException(status_code=503, detail="database unavailable") from exc
        if not gateway._task_conversations_reconciled:
            try:
                gateway.reconcile_task_conversations()
            except SQLAlchemyError as exc:
                raise HTTPException(
                    status_code=503, detail="conversation reconciliation unavailable"
                ) from exc
        if not remote_channel_runtime.reconciled:
            raise HTTPException(status_code=503, detail="remote channel reconciliation unavailable")
        try:
            model_secrets.check_ready()
            source_store.secrets.check_ready()
            remote_channels.secrets.check_ready()
        except SecretStoreError:
            raise HTTPException(status_code=503, detail="model credential store unavailable") from None
        try:
            ArtifactWorkspace(settings.artifact_workspace_root).check_ready()
        except ArtifactInvalid:
            raise HTTPException(status_code=503, detail="artifact workspace unavailable") from None
        return {"status": "ready", "database": "ready"}

    frontend_dir = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if frontend_dir.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")

    return app


def app() -> FastAPI:
    return create_app(Settings())
