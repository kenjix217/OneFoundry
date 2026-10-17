"""A filtered coding view; the full pinned tree stays with trusted QA."""

from __future__ import annotations

import ast
import json
import os
import shutil
from pathlib import Path

from onefoundry.role_repository_file import _valid_path
from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.role_tool_admission import SENSITIVE_KEYS
from runner.local_source_stage import StageError

VIEW_NOTICE = ".onefoundry-source-view.json"


def validate_edit_scope(job: object, repo: Path) -> None:
    from onefoundry.repository_code_contract import RepositoryCodeManifest

    if not isinstance(job, RepositoryCodeManifest):
        raise TypeError("maintenance manifest required")
    for relative in job.target.allowed_paths:
        path = repo / relative
        if relative == VIEW_NOTICE or not _valid_path(relative):
            raise StageError("maintenance_edit_scope_private_path")
        if path.exists() and (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size > job.stage_limits.max_context_file_bytes
            or sensitive_source(relative, path.read_bytes())
        ):
            raise StageError("maintenance_edit_scope_protected_source")


def _sensitive_name(value: str) -> bool:
    name = value.lower().replace("_", "").replace("-", "")
    return any(name.endswith(key) or name.startswith(key) for key in SENSITIVE_KEYS)


def _literal(value: ast.AST | None) -> bool:
    if value is None:
        return False
    if isinstance(value, ast.Constant):
        return isinstance(value.value, (str, bytes)) and bool(value.value)
    if isinstance(value, ast.Call):
        return any(_literal(arg) for arg in value.args)
    if isinstance(value, ast.BinOp):
        return _literal(value.left) or _literal(value.right)
    if isinstance(value, ast.JoinedStr):
        return any(_literal(item) for item in value.values)
    return False


def sensitive_source(path: str, data: bytes) -> bool:
    """Reject credential data while permitting Python identifiers and types."""
    try:
        value = data.decode("utf-8")
    except UnicodeDecodeError:
        return True
    if "\x00" in value:
        return True
    if not path.endswith(".py"):
        return contains_sensitive_text(value)
    try:
        tree = ast.parse(value)
    except (SyntaxError, ValueError, RecursionError):
        return contains_sensitive_text(value)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            literal = (
                node.value.decode("utf-8", errors="replace") if isinstance(node.value, bytes) else node.value
            )
            if contains_sensitive_text(literal):
                return True
        if isinstance(node, ast.keyword) and node.arg and _sensitive_name(node.arg) and _literal(node.value):
            return True
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            names = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                (isinstance(name, ast.Name) and _sensitive_name(name.id))
                or (isinstance(name, ast.Attribute) and _sensitive_name(name.attr))
                for name in names
            ) and _literal(node.value):
                return True
        if isinstance(node, ast.arguments):
            defaults: list[ast.expr | None] = [None] * (
                len(node.posonlyargs + node.args) - len(node.defaults)
            )
            defaults.extend(node.defaults)
            pairs = zip(
                node.posonlyargs + node.args,
                defaults,
                strict=True,
            )
            for arg, default in [*pairs, *zip(node.kwonlyargs, node.kw_defaults, strict=True)]:
                if _sensitive_name(arg.arg) and _literal(default):
                    return True
        if isinstance(node, ast.Dict):
            for key, item in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and _sensitive_name(key.value)
                    and _literal(item)
                ):
                    return True
    # Comments can also contain credentials. Only inspect comment tokens, since
    # credential-shaped Python annotations have already been classified above.
    import io
    import tokenize

    try:
        for token in tokenize.generate_tokens(io.StringIO(value).readline):
            if token.type == tokenize.COMMENT and contains_sensitive_text(token.string):
                return True
    except (tokenize.TokenError, IndentationError):
        return True
    return False


def file_inventory(
    root: Path, *, max_files: int | None = None, max_bytes: int | None = None
) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    total = 0
    for directory, names, files in os.walk(root, followlinks=False):
        if any((Path(directory) / name).is_symlink() for name in names):
            raise ValueError("maintenance_source_symlink_directory")
        names[:] = [name for name in names if name != ".git"]
        for name in files:
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
                raise ValueError("maintenance_source_invalid_file")
            total += path.stat().st_size
            if (max_files is not None and len(result) >= max_files) or (
                max_bytes is not None and total > max_bytes
            ):
                raise ValueError("maintenance_source_inventory_limit")
            result[path.relative_to(root).as_posix()] = path.read_bytes()
    return result


def make_view(repo: Path, destination: Path, allowed_paths: list[str], max_file_bytes: int) -> list[str]:
    """Include safe text only; never copy Git, private or credential data."""
    destination.mkdir(mode=0o700)
    omitted: list[str] = []
    for relative, data in sorted(file_inventory(repo).items()):
        if relative == VIEW_NOTICE or not _valid_path(relative) or len(data) > max_file_bytes:
            omitted.append(relative)
            continue
        if sensitive_source(relative, data):
            omitted.append(relative)
            continue
        output = destination / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(repo / relative, output)
    if VIEW_NOTICE in allowed_paths or any(relative in omitted for relative in allowed_paths):
        raise ValueError("maintenance_edit_scope_contains_protected_source")
    (destination / VIEW_NOTICE).write_text(
        json.dumps(
            {
                "kind": "filtered_committed_source",
                "omitted_paths": omitted,
                "boundary": "Protected, binary and oversized files stay outside this process. "
                "Never recreate them.",
            }
        )
    )
    return omitted


def copy_changes(job: object, plain: Path, repo: Path, before: dict[str, bytes]) -> None:
    """Compare the full inventory, including deletions, before copying scoped text."""
    from onefoundry.repository_code_contract import RepositoryCodeManifest

    if not isinstance(job, RepositoryCodeManifest):
        raise TypeError("maintenance manifest required")
    after = file_inventory(plain, max_files=job.stage_limits.max_files, max_bytes=job.stage_limits.max_bytes)
    changed = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
    if not changed <= set(job.target.allowed_paths):
        raise ValueError("maintenance_candidate_outside_scope")
    for relative in sorted(changed):
        data = after.get(relative)
        target = repo / relative
        if data is None:
            target.unlink()
            continue
        if len(data) > job.stage_limits.max_context_file_bytes or sensitive_source(relative, data):
            raise ValueError("maintenance_candidate_protected_content")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
