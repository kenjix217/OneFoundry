"""Project skill library HTTP routes; mutations flow through ToolGateway."""

from uuid import UUID

from fastapi import APIRouter

from onefoundry.skill_schemas import (
    SkillBind,
    SkillBindingOut,
    SkillCreate,
    SkillOut,
    SkillRename,
    SkillVersionCreate,
    SkillVersionOut,
)
from onefoundry.tool_gateway import ToolGateway
from onefoundry.workflow_api import execute


def make_skill_router(gateway: ToolGateway) -> APIRouter:
    router = APIRouter(prefix="/api/projects/{project_id}")

    @router.get("/skills", response_model=list[SkillOut])
    def list_skills(project_id: UUID) -> list[SkillOut]:
        return execute(lambda: [SkillOut.model_validate(x) for x in gateway.list_skills(project_id)])

    @router.post("/skills", response_model=SkillOut, status_code=201)
    def create_skill(project_id: UUID, data: SkillCreate) -> SkillOut:
        return execute(lambda: SkillOut.model_validate(gateway.create_skill(project_id, data)))

    @router.patch("/skills/{skill_id}", response_model=SkillOut)
    def rename_skill(project_id: UUID, skill_id: UUID, data: SkillRename) -> SkillOut:
        return execute(lambda: SkillOut.model_validate(gateway.rename_skill(project_id, skill_id, data)))

    @router.get("/skills/{skill_id}/versions", response_model=list[SkillVersionOut])
    def list_versions(project_id: UUID, skill_id: UUID) -> list[SkillVersionOut]:
        return execute(
            lambda: [
                SkillVersionOut.model_validate(x) for x in gateway.list_skill_versions(project_id, skill_id)
            ]
        )

    @router.post("/skills/{skill_id}/versions", response_model=SkillVersionOut, status_code=201)
    def add_version(project_id: UUID, skill_id: UUID, data: SkillVersionCreate) -> SkillVersionOut:
        return execute(
            lambda: SkillVersionOut.model_validate(gateway.add_skill_version(project_id, skill_id, data))
        )

    @router.get("/skills/{skill_id}/versions/{version}", response_model=SkillVersionOut)
    def get_version(project_id: UUID, skill_id: UUID, version: int) -> SkillVersionOut:
        return execute(
            lambda: SkillVersionOut.model_validate(gateway.get_skill_version(project_id, skill_id, version))
        )

    @router.get("/profiles/{profile_id}/skills", response_model=list[SkillBindingOut])
    def list_bindings(project_id: UUID, profile_id: UUID) -> list[SkillBindingOut]:
        return execute(
            lambda: [
                SkillBindingOut.model_validate(x) for x in gateway.list_profile_skills(project_id, profile_id)
            ]
        )

    @router.put("/profiles/{profile_id}/skills/{skill_id}", response_model=SkillBindingOut)
    def bind(project_id: UUID, profile_id: UUID, skill_id: UUID, data: SkillBind) -> SkillBindingOut:
        return execute(
            lambda: SkillBindingOut.model_validate(
                gateway.bind_profile_skill(project_id, profile_id, skill_id, data)
            )
        )

    @router.delete("/profiles/{profile_id}/skills/{skill_id}", status_code=204)
    def unbind(
        project_id: UUID,
        profile_id: UUID,
        skill_id: UUID,
        expected_binding_id: UUID,
    ) -> None:
        execute(lambda: gateway.unbind_profile_skill(project_id, profile_id, skill_id, expected_binding_id))

    return router
