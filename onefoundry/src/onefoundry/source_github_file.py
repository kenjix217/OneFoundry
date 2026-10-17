"""Project/account binding and revocation checks for a saved GitHub file read."""

from __future__ import annotations

from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from onefoundry.gateway_errors import NotFound
from onefoundry.model_secrets import SecretStoreError
from onefoundry.repository_github import read_blob
from onefoundry.role_repository_file import RepositoryReadError
from onefoundry.source_github import GitHubUnavailable, _get

if TYPE_CHECKING:
    from onefoundry.source_store import SourceStore


def inspect(store: SourceStore, project_id: UUID, source_id: UUID, deadline: float) -> str | None:
    try:
        binding = _binding(store, project_id, source_id)
        credential = store._credential(project_id, UUID(str(binding["account_id"])))
        remaining = deadline - monotonic()
        if remaining <= 0:
            return "source_probe_timed_out"
        from onefoundry.source_github import REPO_PART, SHA

        if any(
            not isinstance(binding[key], str) or not pattern.fullmatch(binding[key])
            for key, pattern in (("owner", REPO_PART), ("repo", REPO_PART), ("revision", SHA))
        ):
            return "source_unavailable"
        value = _get(
            f"/repos/{binding['owner']}/{binding['repo']}/git/commits/{binding['revision']}",
            credential,
            store.settings,
            timeout_seconds=min(remaining, store.settings.github_timeout_seconds),
        )
        if monotonic() > deadline:
            return "source_probe_timed_out"
        if not isinstance(value, dict) or value.get("sha") != binding["revision"]:
            return "source_revision_drifted"
        if _binding(store, project_id, source_id) != binding:
            return "source_revision_drifted"
    except (RepositoryReadError, SecretStoreError, NotFound):
        return "source_account_unavailable"
    except GitHubUnavailable:
        return "source_dependency_unavailable"
    return None


def _binding(store: SourceStore, project_id: UUID, source_id: UUID) -> dict[str, Any]:
    with store.engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT s.account_id,s.owner,s.repo,s.revision,s.created_at,"
                    "a.created_at AS account_created "
                    "FROM project_sources s JOIN github_accounts a ON a.id=s.account_id "
                    "AND a.project_id=s.project_id WHERE s.project_id=:p AND s.id=:id AND s.kind='github'"
                ),
                {"p": str(project_id), "id": str(source_id)},
            )
            .mappings()
            .one_or_none()
        )
    if row is None:
        raise RepositoryReadError("GitHub source or account unavailable", code="source_account_unavailable")
    return dict(row)


def read(
    store: SourceStore,
    project_id: UUID,
    source_id: UUID,
    revision: str,
    path: str,
    *,
    limit: int,
    deadline: float,
) -> bytes:
    binding = _binding(store, project_id, source_id)
    if binding["revision"] != revision:
        raise RepositoryReadError("GitHub source revision changed")
    try:
        credential = store._credential(project_id, UUID(str(binding["account_id"])))
    except (SecretStoreError, NotFound):
        raise RepositoryReadError("GitHub account unavailable", code="source_account_unavailable") from None
    data = read_blob(
        binding["owner"],
        binding["repo"],
        revision,
        path,
        credential,
        store.settings,
        limit=limit,
        deadline=deadline,
    )
    if _binding(store, project_id, source_id) != binding:
        raise RepositoryReadError("GitHub source or account changed during read")
    try:
        if store._credential(project_id, UUID(str(binding["account_id"]))) != credential:
            raise RepositoryReadError("GitHub account changed during read")
    except (SecretStoreError, NotFound):
        raise RepositoryReadError("GitHub account unavailable", code="source_account_unavailable") from None
    return data
