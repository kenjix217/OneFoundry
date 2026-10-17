"""Admitted operator check of a saved GitHub source through the Tool Gateway."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.model_secrets import SecretStoreError
from onefoundry.source_github import GitHubUnavailable

if TYPE_CHECKING:
    from onefoundry.source_store import SourceStore
    from onefoundry.tool_gateway import ToolGateway


def _record(gateway: "ToolGateway", project_id: UUID, source_id: UUID, payload: dict[str, Any]) -> None:
    with gateway.engine.begin() as connection:
        gateway._require_project(connection, project_id)
        gateway._event(
            connection,
            project_id,
            None,
            None,
            "github_source_snapshot_attempt",
            {
                "source_id": str(source_id),
                **payload,
            },
        )


def _record_checked_if_current(gateway: "ToolGateway", snapshot: dict[str, Any]) -> None:
    """Read owner state again before recording a GitHub observation."""
    project_id = snapshot["project_id"]
    source_id = snapshot["source_id"]
    stale_reason: str | None = None
    with gateway.engine.begin() as connection:
        status = connection.scalar(text("SELECT status FROM projects WHERE id=:id"), {"id": project_id})
        if status is None:
            raise NotFound("project not found")
        if status != "active":
            stale_reason = "project_changed_during_check"
        if stale_reason is None:
            source = (
                connection.execute(
                    text(
                        "SELECT kind,account_id,owner,repo,branch,revision,created_at FROM project_sources "
                        "WHERE project_id=:project_id AND id=:source_id"
                    ),
                    {"project_id": project_id, "source_id": source_id},
                )
                .mappings()
                .one_or_none()
            )
            account = connection.scalar(
                text(
                    "SELECT created_at FROM github_accounts WHERE project_id=:project_id AND id=:account_id"
                ),
                {"project_id": project_id, "account_id": snapshot["account_id"]},
            )
            if (
                source is None
                or account is None
                or account != snapshot["account_created_at"]
                or any(
                    source[key] != expected
                    for key, expected in (
                        ("kind", "github"),
                        ("account_id", snapshot["account_id"]),
                        ("owner", snapshot["owner"]),
                        ("repo", snapshot["repo"]),
                        ("branch", snapshot["branch"]),
                        ("revision", snapshot["saved_revision"]),
                        ("created_at", snapshot["source_created_at"]),
                    )
                )
            ):
                stale_reason = "source_changed_during_check"
        payload = (
            {"status": "unknown", "reason": stale_reason}
            if stale_reason is not None
            else {
                "status": "checked",
                "saved_revision": snapshot["saved_revision"],
                "current_revision": snapshot["current_revision"],
                "matches_saved_revision": snapshot["matches_saved_revision"],
            }
        )
        gateway._event(
            connection,
            project_id,
            None,
            None,
            "github_source_snapshot_attempt",
            {"source_id": str(source_id), **payload},
        )
    if stale_reason is not None:
        raise Conflict("saved GitHub source or project changed during check; no current result was recorded")


def check_snapshot(
    gateway: "ToolGateway", source_store: "SourceStore", project_id: UUID, source_id: UUID
) -> dict[str, Any]:
    """Perform one explicit read; an agent profile cannot invoke this operator route."""
    with gateway.engine.connect() as connection:
        status = connection.scalar(text("SELECT status FROM projects WHERE id=:id"), {"id": project_id})
    if status is None:
        raise NotFound("project not found")
    if status != "active":
        raise Conflict("restore this project before checking GitHub")
    try:
        snapshot = source_store.github_source_snapshot(project_id, source_id)
    except NotFound:
        _record(gateway, project_id, source_id, {"status": "failed", "reason": "source_not_found"})
        raise
    except Conflict:
        _record(gateway, project_id, source_id, {"status": "failed", "reason": "source_check_conflict"})
        raise
    except ValueError:
        _record(gateway, project_id, source_id, {"status": "failed", "reason": "invalid_saved_source"})
        raise Conflict("saved GitHub source metadata is invalid") from None
    except (GitHubUnavailable, SecretStoreError, Unavailable):
        _record(gateway, project_id, source_id, {"status": "unknown", "reason": "github_check_unavailable"})
        raise Unavailable("GitHub source check unavailable; saved revision was not changed") from None
    except SQLAlchemyError:
        # Database failure may prevent recording an event; do not claim a checked result.
        raise Unavailable("GitHub source check state unavailable") from None
    snapshot["checked_at"] = datetime.now(UTC)
    _record_checked_if_current(gateway, snapshot)
    del snapshot["account_id"]
    del snapshot["source_created_at"]
    del snapshot["account_created_at"]
    return snapshot
