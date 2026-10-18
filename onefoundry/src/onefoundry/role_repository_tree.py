"""Bounded, stable pagination of a saved Git tree."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from onefoundry import role_repository_file as file
from onefoundry.gateway_errors import NotFound
from onefoundry.model_secrets import SecretStoreError

if TYPE_CHECKING:
    from sqlalchemy import Connection

    from onefoundry.role_tool_admission import RunContext
    from onefoundry.settings import Settings


def _local_entries(
    source: dict[str, Any], path: str, settings: Settings, *, limit: int | None = None
) -> list[dict[str, Any]]:
    if (
        source["kind"] != "local"
        or source["root_id"] != "workspace"
        or not isinstance(source["relative_path"], str)
        or not isinstance(source["branch"], str)
        or not isinstance(source["revision"], str)
        or file.SHA40.fullmatch(source["revision"]) is None
        or not settings.local_source_root
    ):
        raise file.RepositoryReadError("saved local source unavailable")
    deadline = time.monotonic() + settings.local_source_git_timeout_seconds
    with file._admit_repo(Path(settings.local_source_root), source["relative_path"]) as repo:
        if file._head(repo, deadline) != (source["branch"], source["revision"]):
            raise file.RepositoryReadError(
                "saved source branch or revision drifted", code="source_revision_drifted"
            )
        tree_ref = source["revision"] if path == "." else f"{source['revision']}:{path}"
        raw = file._git_read(
            repo,
            ("ls-tree", "-z", "-l", tree_ref),
            settings.live_text_max_output_bytes if limit is None else limit,
            deadline,
        )
        if file._head(repo, deadline) != (source["branch"], source["revision"]):
            raise file.RepositoryReadError(
                "saved source branch or revision drifted", code="source_revision_drifted"
            )
    entries: list[dict[str, Any]] = []
    for line in raw.split(b"\0"):
        if not line:
            continue
        try:
            metadata, name = line.split(b"\t", 1)
            mode, kind, sha, size = metadata.decode("ascii").split()
            entry_path = name.decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            raise file.RepositoryReadError("repository tree metadata invalid") from None
        full_path = entry_path if path == "." else f"{path}/{entry_path}"
        if not file._valid_path(full_path):
            continue
        if kind == "tree" and mode == "040000":
            entries.append({"path": full_path, "kind": "tree", "sha": sha, "size": None})
        elif kind == "blob" and mode in {"100644", "100755"}:
            entries.append({"path": full_path, "kind": "blob", "sha": sha, "size": int(size)})
    return entries


def _github_entries(
    connection: Connection,
    context: RunContext,
    source_id: str,
    source: dict[str, Any],
    path: str,
    settings: Settings,
) -> list[dict[str, Any]]:
    from onefoundry.repository_github import read_tree
    from onefoundry.source_github_file import _binding
    from onefoundry.source_store import SourceStore

    store = SourceStore(connection.engine, "repository-reader", settings)
    binding = _binding(store, context.project_id, UUID(source_id))
    if binding["revision"] != source["revision"]:
        raise file.RepositoryReadError("GitHub source revision changed", code="source_revision_drifted")
    try:
        credential = store._credential(context.project_id, UUID(str(binding["account_id"])))
    except (SecretStoreError, NotFound):
        raise file.RepositoryReadError(
            "GitHub account unavailable", code="source_account_unavailable"
        ) from None
    entries = read_tree(
        binding["owner"],
        binding["repo"],
        binding["revision"],
        path,
        credential,
        settings,
        deadline=time.monotonic() + settings.github_timeout_seconds,
    )
    current = _binding(store, context.project_id, UUID(source_id))
    if current != binding:
        code = (
            "source_revision_drifted" if current["revision"] != binding["revision"] else "source_unavailable"
        )
        raise file.RepositoryReadError("GitHub source or account changed during read", code=code)
    try:
        if store._credential(context.project_id, UUID(str(binding["account_id"]))) != credential:
            raise file.RepositoryReadError(
                "GitHub account changed during read", code="source_account_unavailable"
            )
    except (SecretStoreError, NotFound):
        raise file.RepositoryReadError(
            "GitHub account unavailable", code="source_account_unavailable"
        ) from None
    return entries


TREE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source_id": {"type": "string", "format": "uuid"},
        "directory": {"type": "string", "default": ""},
        "after": {"type": "string", "default": ""},
    },
    "required": ["source_id"],
    "additionalProperties": False,
}


class TreeArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_id: str
    directory: str = ""
    after: str = ""


def parse_repository_tree(arguments: object) -> dict[str, Any]:
    parsed = TreeArguments.model_validate(arguments)
    file._Arguments.canonical_uuid(parsed.source_id)
    if parsed.directory and not file._valid_path(parsed.directory):
        raise ValueError("invalid repository directory")
    if parsed.after and not file._valid_path(parsed.after):
        raise ValueError("invalid repository cursor")
    if parsed.after and parsed.directory and not parsed.after.startswith(parsed.directory + "/"):
        raise ValueError("repository cursor outside directory")
    return parsed.model_dump()


def repository_tree(
    connection: Connection, context: RunContext, arguments: dict[str, Any], settings: Settings
) -> dict[str, Any]:
    source_id, directory = arguments["source_id"], arguments["directory"]
    source = file._source(connection, context, source_id)
    if source is None:
        return {"source_id": source_id, "found": False}
    path = directory or "."
    entries = (
        _github_entries(connection, context, source_id, source, path, settings)
        if source["kind"] == "github"
        else _local_entries(source, path, settings, limit=settings.input_file_max_text_bytes)
    )
    with connection.engine.connect() as fresh:
        if file._source(fresh, context, source_id) != source:
            raise file.RepositoryReadError("saved source changed during read")
    entries.sort(key=lambda entry: entry["path"])
    remaining = [entry for entry in entries if entry["path"] > arguments["after"]]
    page = remaining[: context.max_items]
    has_more = len(remaining) > len(page)
    result = {
        "source_id": source_id,
        "found": True,
        "revision": source["revision"],
        "directory": directory,
        "entries": [{**entry, "kind": "directory" if entry["kind"] == "tree" else "file"} for entry in page],
        "has_more": has_more,
        "next_cursor": page[-1]["path"] if has_more else None,
    }
    validate_repository_tree(result)
    return result


def validate_repository_tree(result: dict[str, Any]) -> None:
    if result.get("found") is False:
        file.validate_repository_file(result)
        return
    if set(result) != {"source_id", "found", "revision", "directory", "entries", "has_more", "next_cursor"}:
        raise ValueError("repository tree result fields invalid")
    file._Arguments.canonical_uuid(result["source_id"])
    directory, entries = result["directory"], result["entries"]
    if (
        result["found"] is not True
        or not isinstance(result["revision"], str)
        or file.SHA40.fullmatch(result["revision"]) is None
        or not isinstance(directory, str)
        or (directory and not file._valid_path(directory))
        or not isinstance(entries, list)
        or not isinstance(result["has_more"], bool)
        or (result["has_more"] and not entries)
    ):
        raise ValueError("repository tree result invalid")
    previous = ""
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "kind", "sha", "size"}:
            raise ValueError("repository tree entry fields invalid")
        path = entry["path"]
        if (
            not isinstance(path, str)
            or not file._valid_path(path)
            or path <= previous
            or (directory and not path.startswith(directory + "/"))
            or entry["kind"] not in {"file", "directory"}
            or not isinstance(entry["sha"], str)
            or file.SHA40.fullmatch(entry["sha"]) is None
            or (entry["kind"] == "directory" and entry["size"] is not None)
            or (entry["kind"] == "file" and (type(entry["size"]) is not int or entry["size"] < 0))
        ):
            raise ValueError("repository tree entry invalid")
        previous = path
    if result["next_cursor"] != (previous if result["has_more"] else None):
        raise ValueError("repository tree cursor invalid")
