"""A separate draft-only persistence identity delegates to the existing memory owner."""

from typing import Any
from uuid import UUID

from sqlalchemy import Connection, create_engine, text

from onefoundry.company_memory import CompanyMemory
from onefoundry.company_memory_schemas import CaptureCreate
from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.memory_plugin_capture_settings import MemoryCaptureDatabaseSettings
from onefoundry.memory_plugin_settings import MemoryPluginSettings
from onefoundry.settings import Settings
from onefoundry.tool_gateway import ToolGateway


def capture_project_lock(connection: Connection, project_id: UUID) -> None:
    status = connection.scalar(
        text("SELECT public.memory_capture_lock_project(:project_id)"), {"project_id": project_id}
    )
    if status is None:
        raise NotFound("project not found")
    if status != "active":
        raise Conflict("restore this project before changing its workspace")


class MemoryCaptureToolGateway(ToolGateway):
    def capture_company_memory(self, project_id: UUID, data: CaptureCreate) -> dict[str, Any]:
        return CompanyMemory(self).capture(project_id, data, project_lock=capture_project_lock)


def capture_gateways(policy: MemoryPluginSettings, settings: Settings) -> dict[str, ToolGateway]:
    grants = [grant for grant in policy.grants if grant.capture]
    if policy.auth_mode != "api_key" or not grants:
        return {}
    configured = MemoryCaptureDatabaseSettings()  # type: ignore[call-arg]
    if configured.db_user == settings.db_user:
        raise ValueError("capture DB identity must differ from the read-only login")
    url = settings.database_url.set(username=configured.db_user, password=configured.db_password)
    engine = create_engine(
        url,
        hide_parameters=True,
        pool_size=policy.max_in_flight,
        max_overflow=0,
        pool_timeout=policy.deadline_seconds,
        connect_args={
            "connect_timeout": policy.deadline_seconds,
            "options": f"-c statement_timeout={policy.deadline_seconds * 1000} "
            f"-c lock_timeout={policy.deadline_seconds * 1000}",
        },
    )
    with engine.connect() as connection:
        unsafe: bool = connection.exec_driver_sql(
            "SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls "
            "OR EXISTS(SELECT 1 FROM pg_auth_members WHERE member=pg_roles.oid) "
            "FROM pg_roles WHERE rolname=current_user"
        ).scalar_one()
        owns: bool = connection.exec_driver_sql(
            "SELECT EXISTS(SELECT 1 FROM pg_class WHERE relnamespace='public'::regnamespace"
            " AND relowner=(SELECT oid FROM pg_roles WHERE rolname=current_user))"
        ).scalar_one()
        if (
            unsafe
            or owns
            or connection.exec_driver_sql(
                "SELECT has_table_privilege(current_user,'memory_items','INSERT,UPDATE,DELETE')"
                " OR "
                "has_column_privilege(current_user,'dreamcycle_candidates','status','INSERT,UPDATE')"
                " OR has_any_column_privilege(current_user,'dreamcycle_candidates','UPDATE')"
                " OR has_table_privilege(current_user,'dreamcycle_candidates','DELETE')"
                " OR has_any_column_privilege(current_user,'run_results','UPDATE')"
                " OR has_table_privilege(current_user,'run_results','DELETE')"
                " OR has_any_column_privilege(current_user,'memory_captures','UPDATE')"
                " OR has_table_privilege(current_user,'memory_captures','DELETE')"
                " OR has_column_privilege(current_user,'memory_captures','messages','SELECT')"
                " OR has_column_privilege(current_user,'memory_captures','source_ref','SELECT')"
                " OR has_table_privilege(current_user,'events','SELECT,UPDATE,DELETE')"
                " OR has_schema_privilege(current_user,'public','CREATE')"
            ).scalar_one()
        ):
            raise ValueError("capture DB identity must have draft-only privileges")
        connection.exec_driver_sql("SELECT id,payload_sha256,source_sha256 FROM memory_captures LIMIT 0")
        connection.exec_driver_sql("SELECT id,summary,status FROM dreamcycle_candidates LIMIT 0")
        if not connection.exec_driver_sql(
            "SELECT has_function_privilege(current_user,'public.memory_capture_lock_project(uuid)','EXECUTE')"
        ).scalar_one():
            raise ValueError("capture DB identity needs the approved project lock helper")
    return {
        grant.actor_id: MemoryCaptureToolGateway(engine, grant.actor_id, live_text_settings=settings)
        for grant in grants
    }
