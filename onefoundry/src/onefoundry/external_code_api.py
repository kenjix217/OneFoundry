"""HTTP boundary for a delegated coding attempt."""

from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import Field

from onefoundry.external_code_schemas import (
    ExternalCodeArtifactPreview,
    ExternalCodeDetail,
    ExternalCodeEligibility,
    ExternalCodeLaunch,
    ExternalCodeProgress,
    OfflineSourceProbeEligibility,
    OfflineSourceProbeLaunch,
    SnapshotCodexFixtureEligibility,
    SnapshotCodexFixtureLaunch,
)
from onefoundry.repository_code_abandon import RepositoryAbandonment, abandon
from onefoundry.repository_code_contract import RepositoryCodeLaunch, RepositoryCodeTarget
from onefoundry.tool_gateway import ToolGateway
from onefoundry.workflow_api import execute
from onefoundry.workflow_schemas import StrictModel


class MaintenanceAcquire(StrictModel):
    proposal_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


def make_external_code_router(gateway: ToolGateway) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}")

    @router.post("/runs/{run_id}/repository-code/{attempt_id}/abandon")
    def abandon_repository(
        project_id: UUID, run_id: UUID, attempt_id: UUID, data: RepositoryAbandonment
    ) -> dict[str, object]:
        return execute(lambda: abandon(gateway, project_id, run_id, attempt_id, data))

    @router.post("/capability-setup/{approval_id}/quiesce")
    def setup_quiesce(project_id: UUID, approval_id: UUID, data: MaintenanceAcquire) -> dict[str, bool]:
        from onefoundry.capability_setup_maintenance import acquire

        return execute(lambda: {"acquired": acquire(gateway, project_id, approval_id, data.proposal_digest)})

    @router.post("/capability-setup/{approval_id}/register")
    def setup_register(project_id: UUID, approval_id: UUID, data: MaintenanceAcquire) -> dict[str, object]:
        from onefoundry.capability_setup_host import register

        return execute(lambda: register(gateway, project_id, approval_id, data.proposal_digest))

    @router.post("/capability-setup/{approval_id}/release")
    def setup_release(project_id: UUID, approval_id: UUID) -> dict[str, bool]:
        from onefoundry.capability_setup_maintenance import release

        return execute(lambda: {"released": release(gateway, project_id, approval_id)})

    @router.post("/extension-activation/{approval_id}/quiesce")
    def quiesce(project_id: UUID, approval_id: UUID, data: MaintenanceAcquire) -> dict[str, bool]:
        from onefoundry.runtime_maintenance import acquire

        return execute(lambda: {"acquired": acquire(gateway, project_id, approval_id, data.proposal_digest)})

    @router.post("/extension-activation/{approval_id}/release")
    def release_maintenance(project_id: UUID, approval_id: UUID) -> dict[str, bool]:
        from onefoundry.runtime_maintenance import release

        return execute(lambda: {"released": release(gateway, project_id, approval_id)})

    @router.post("/extension-activation/{approval_id}/configure")
    def configure_activation(project_id: UUID, approval_id: UUID) -> dict[str, bool]:
        from onefoundry.extension_tool_grants import configure

        return execute(lambda: {"configured": configure(gateway, project_id, approval_id)})

    @router.get("/extension-activation/preflight")
    def activation_preflight(project_id: UUID) -> dict[str, object]:
        # Installation stops the whole local app. Report global activity as
        # counts only; another project's active work also prevents a restart.
        from onefoundry.runtime_maintenance import preflight

        return execute(lambda: preflight(gateway, project_id))

    @router.get("/work-orders/{order_id}/extensions")
    def extension_status(project_id: UUID, order_id: UUID) -> list[dict[str, object]]:
        from onefoundry.self_extension_continuation import status

        return execute(lambda: status(gateway, project_id, order_id))

    @router.post("/runs/{run_id}/repository-code/target-proposals")
    def propose_target(project_id: UUID, run_id: UUID, data: RepositoryCodeTarget) -> dict[str, object]:
        from onefoundry.repository_code_gateway import propose

        return execute(lambda: propose(gateway, project_id, run_id, data))

    @router.post(
        "/work-orders/{order_id}/repository-code/runs", response_model=ExternalCodeDetail, status_code=201
    )
    def launch_repository(project_id: UUID, order_id: UUID, data: RepositoryCodeLaunch) -> ExternalCodeDetail:
        return execute(
            lambda: ExternalCodeDetail.model_validate(
                gateway._execution_gateway().launch_repository_code(project_id, order_id, data)
            )
        )

    @router.get("/work-orders/{order_id}/external-code/eligibility", response_model=ExternalCodeEligibility)
    def eligibility(project_id: UUID, order_id: UUID) -> ExternalCodeEligibility:
        return execute(
            lambda: ExternalCodeEligibility.model_validate(
                gateway.external_code_eligibility(project_id, order_id)
            )
        )

    @router.post(
        "/work-orders/{order_id}/external-code/runs", response_model=ExternalCodeDetail, status_code=201
    )
    def launch(project_id: UUID, order_id: UUID, data: ExternalCodeLaunch) -> ExternalCodeDetail:
        return execute(
            lambda: ExternalCodeDetail.model_validate(
                gateway.launch_external_code(project_id, order_id, data.adapter_id)
            )
        )

    @router.get(
        "/work-orders/{order_id}/offline-source-probe/eligibility",
        response_model=OfflineSourceProbeEligibility,
    )
    def offline_probe_eligibility(project_id: UUID, order_id: UUID) -> OfflineSourceProbeEligibility:
        return execute(
            lambda: OfflineSourceProbeEligibility.model_validate(
                gateway.offline_source_probe_eligibility(project_id, order_id)
            )
        )

    @router.post(
        "/work-orders/{order_id}/offline-source-probe/runs",
        response_model=ExternalCodeDetail,
        status_code=201,
    )
    def launch_offline_probe(
        project_id: UUID, order_id: UUID, data: OfflineSourceProbeLaunch
    ) -> ExternalCodeDetail:
        return execute(
            lambda: ExternalCodeDetail.model_validate(
                gateway.launch_offline_source_probe(project_id, order_id, data.source_id)
            )
        )

    @router.post(
        "/work-orders/{order_id}/github-snapshot-codex/runs",
        response_model=ExternalCodeDetail,
        status_code=201,
    )
    def launch_snapshot_codex(
        project_id: UUID, order_id: UUID, data: SnapshotCodexFixtureLaunch
    ) -> ExternalCodeDetail:
        return execute(
            lambda: ExternalCodeDetail.model_validate(
                gateway.launch_snapshot_codex_fixture(
                    project_id, order_id, data.snapshot_attempt_id, data.idempotency_key
                )
            )
        )

    @router.get(
        "/work-orders/{order_id}/github-snapshot-codex/eligibility",
        response_model=SnapshotCodexFixtureEligibility,
    )
    def snapshot_codex_eligibility(project_id: UUID, order_id: UUID) -> SnapshotCodexFixtureEligibility:
        return execute(
            lambda: SnapshotCodexFixtureEligibility.model_validate(
                gateway.snapshot_codex_fixture_eligibility(project_id, order_id)
            )
        )

    @router.get("/runs/{run_id}/external-code", response_model=ExternalCodeDetail)
    def detail(project_id: UUID, run_id: UUID) -> ExternalCodeDetail:
        return execute(
            lambda: ExternalCodeDetail.model_validate(gateway.external_code_detail(project_id, run_id))
        )

    @router.get(
        "/runs/{run_id}/external-code/attempts/{attempt_id}/progress",
        response_model=ExternalCodeProgress,
    )
    def progress(
        project_id: UUID, run_id: UUID, attempt_id: UUID, after: int = Query(default=0, ge=0, le=24)
    ) -> ExternalCodeProgress:
        return execute(
            lambda: ExternalCodeProgress.model_validate(
                gateway.external_code_progress(project_id, run_id, attempt_id, after)
            )
        )

    @router.post("/runs/{run_id}/external-code/reconcile", response_model=ExternalCodeDetail)
    def reconcile(project_id: UUID, run_id: UUID) -> ExternalCodeDetail:
        return execute(
            lambda: ExternalCodeDetail.model_validate(gateway.reconcile_external_code(project_id, run_id))
        )

    @router.get(
        "/runs/{run_id}/external-code/artifacts/{artifact_id}", response_model=ExternalCodeArtifactPreview
    )
    def preview(project_id: UUID, run_id: UUID, artifact_id: UUID) -> ExternalCodeArtifactPreview:
        return execute(
            lambda: ExternalCodeArtifactPreview.model_validate(
                gateway.external_code_artifact(project_id, run_id, artifact_id)
            )
        )

    return router
