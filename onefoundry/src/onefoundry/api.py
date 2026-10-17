"""Step-2 HTTP routes; persistence stays in ProjectStore."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.project_plan import ProjectPlanOut, ProjectPlanUpdate
from onefoundry.projects import ProjectStore
from onefoundry.schemas import (
    ProfileCreate,
    ProfileOut,
    ProfilePatch,
    ProjectBrief,
    ProjectCreate,
    ProjectOut,
)


def make_router(store: ProjectStore) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/projects", response_model=list[ProjectOut])
    def list_projects(status: Literal["active", "archived", "trashed"] = "active") -> list[ProjectOut]:
        try:
            return [ProjectOut.model_validate(row) for row in store.list_projects(status)]
        except SQLAlchemyError as exc:
            raise HTTPException(503, "database unavailable") from exc

    @router.post("/projects", response_model=ProjectOut, status_code=201)
    def create_project(data: ProjectCreate) -> ProjectOut:
        try:
            return ProjectOut.model_validate(store.create_project(data))
        except SQLAlchemyError as exc:
            raise HTTPException(503, "database unavailable") from exc

    @router.get("/projects/{project_id}", response_model=ProjectOut)
    def get_project(project_id: UUID) -> ProjectOut:
        try:
            row = store.get_project(project_id)
        except SQLAlchemyError as exc:
            raise HTTPException(503, "database unavailable") from exc
        if row is None:
            raise HTTPException(404, "project not found")
        return ProjectOut.model_validate(row)

    @router.patch("/projects/{project_id}/brief", response_model=ProjectOut)
    def update_brief(project_id: UUID, data: ProjectBrief) -> ProjectOut:
        try:
            return ProjectOut.model_validate(store.update_brief(project_id, data))
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except SQLAlchemyError as exc:
            raise HTTPException(503, "database unavailable") from exc

    @router.get("/projects/{project_id}/plan", response_model=ProjectPlanOut)
    def get_plan(project_id: UUID) -> ProjectPlanOut:
        try:
            return ProjectPlanOut.model_validate(store.get_plan(project_id))
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except SQLAlchemyError as exc:
            raise HTTPException(503, "database unavailable") from exc

    @router.patch("/projects/{project_id}/plan", response_model=ProjectPlanOut)
    def update_plan(project_id: UUID, data: ProjectPlanUpdate) -> ProjectPlanOut:
        try:
            return ProjectPlanOut.model_validate(store.update_plan(project_id, data))
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except SQLAlchemyError as exc:
            raise HTTPException(503, "database unavailable") from exc

    @router.post("/projects/{project_id}/profiles/starter", response_model=list[ProfileOut])
    def fill_starter_profiles(project_id: UUID) -> list[ProfileOut]:
        try:
            return [ProfileOut.model_validate(row) for row in store.fill_starter_instructions(project_id)]
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except SQLAlchemyError as exc:
            raise HTTPException(503, "database unavailable") from exc

    @router.get("/projects/{project_id}/profiles", response_model=list[ProfileOut])
    def list_profiles(project_id: UUID) -> list[ProfileOut]:
        try:
            if store.get_project(project_id) is None:
                raise HTTPException(404, "project not found")
            return [ProfileOut.model_validate(row) for row in store.list_profiles(project_id)]
        except SQLAlchemyError as exc:
            raise HTTPException(503, "database unavailable") from exc

    @router.post("/projects/{project_id}/profiles", response_model=ProfileOut, status_code=201)
    def create_profile(project_id: UUID, data: ProfileCreate) -> ProfileOut:
        try:
            if store.get_project(project_id) is None:
                raise HTTPException(404, "project not found")
            return ProfileOut.model_validate(store.create_profile(project_id, data))
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except IntegrityError as exc:
            if getattr(exc.orig, "sqlstate", None) == "23505":
                raise HTTPException(409, "profile name already exists") from exc
            raise HTTPException(503, "database unavailable") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except SQLAlchemyError as exc:
            raise HTTPException(503, "database unavailable") from exc

    @router.patch("/projects/{project_id}/profiles/{profile_id}", response_model=ProfileOut)
    def update_profile(project_id: UUID, profile_id: UUID, data: ProfilePatch) -> ProfileOut:
        try:
            row = store.update_profile(project_id, profile_id, data)
        except NotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except SQLAlchemyError as exc:
            raise HTTPException(503, "database unavailable") from exc
        if row is None:
            raise HTTPException(404, "profile not found")
        return ProfileOut.model_validate(row)

    return router
