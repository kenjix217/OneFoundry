"""HTTP boundary for repository source registration."""

from collections.abc import Callable
from uuid import UUID

from fastapi import APIRouter, HTTPException
from sqlalchemy.exc import SQLAlchemyError

from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.model_secrets import SecretStoreError
from onefoundry.source_github import GitHubUnavailable
from onefoundry.source_schemas import (
    GitHubAccountCreate,
    GitHubAccountOut,
    GitHubRepoOut,
    GitHubSourceCreate,
    LocalRepositoryDiscovery,
    LocalSourceCreate,
    SourceOut,
    SourceRootOut,
)
from onefoundry.source_store import SourceStore


def _execute[T](action: Callable[[], T]) -> T:
    try:
        return action()
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from None
    except Unavailable as exc:
        raise HTTPException(503, str(exc)) from None
    except SecretStoreError:
        raise HTTPException(503, "GitHub credential store unavailable") from None
    except GitHubUnavailable:
        raise HTTPException(502, "GitHub repository discovery unavailable or credential rejected") from None
    except ValueError:
        raise HTTPException(422, "invalid repository source input") from None
    except SQLAlchemyError:
        raise HTTPException(503, "repository source store unavailable") from None


def make_source_router(store: SourceStore) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/source-roots", response_model=list[SourceRootOut])
    def roots() -> list[SourceRootOut]:
        return [SourceRootOut.model_validate(row) for row in store.roots()]

    @router.get("/source-roots/workspace/repositories", response_model=LocalRepositoryDiscovery)
    def discover_local() -> LocalRepositoryDiscovery:
        return _execute(lambda: LocalRepositoryDiscovery.model_validate(store.discover_local()))

    @router.get("/projects/{project_id}/sources", response_model=list[SourceOut])
    def sources(project_id: UUID) -> list[SourceOut]:
        return _execute(lambda: [SourceOut.model_validate(row) for row in store.list_sources(project_id)])

    @router.post("/projects/{project_id}/sources/local", response_model=SourceOut, status_code=201)
    def add_local(project_id: UUID, data: LocalSourceCreate) -> SourceOut:
        return _execute(lambda: SourceOut.model_validate(store.add_local(project_id, data)))

    @router.get("/projects/{project_id}/github/accounts", response_model=list[GitHubAccountOut])
    def accounts(project_id: UUID) -> list[GitHubAccountOut]:
        return _execute(
            lambda: [GitHubAccountOut.model_validate(row) for row in store.list_accounts(project_id)]
        )

    @router.post("/projects/{project_id}/github/accounts", response_model=GitHubAccountOut, status_code=201)
    def add_account(project_id: UUID, data: GitHubAccountCreate) -> GitHubAccountOut:
        return _execute(lambda: GitHubAccountOut.model_validate(store.add_account(project_id, data)))

    @router.get(
        "/projects/{project_id}/github/accounts/{account_id}/repos",
        response_model=list[GitHubRepoOut],
    )
    def repositories(project_id: UUID, account_id: UUID) -> list[GitHubRepoOut]:
        return _execute(
            lambda: [GitHubRepoOut.model_validate(row) for row in store.repositories(project_id, account_id)]
        )

    @router.post("/projects/{project_id}/sources/github", response_model=SourceOut, status_code=201)
    def add_github(project_id: UUID, data: GitHubSourceCreate) -> SourceOut:
        return _execute(lambda: SourceOut.model_validate(store.add_github(project_id, data)))

    @router.delete("/projects/{project_id}/sources/{source_id}", status_code=204)
    def remove_source(project_id: UUID, source_id: UUID) -> None:
        _execute(lambda: store.remove_source(project_id, source_id))

    @router.delete("/projects/{project_id}/github/accounts/{account_id}", status_code=204)
    def remove_account(project_id: UUID, account_id: UUID) -> None:
        _execute(lambda: store.remove_account(project_id, account_id))

    return router
