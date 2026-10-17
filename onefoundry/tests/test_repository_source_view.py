"""Protected source never enters coding context; scope includes deleted files."""

import os
from pathlib import Path

import pytest

from runner.repository_source_view import (
    VIEW_NOTICE,
    copy_changes,
    file_inventory,
    make_view,
    sensitive_source,
)
from tests.test_repository_code import job_at


def test_reader_acceptance_fixture_imports_are_available_in_coding_view() -> None:
    import ast

    root = Path(__file__).resolve().parents[1]
    for name in ("test_repository_tree_extension.py", "test_repository_document_extension.py"):
        tree = ast.parse((root / "acceptance" / name).read_text())
        fixtures = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("tests.")
        ]
        assert fixtures
        for module in fixtures:
            relative = module.replace(".", "/") + ".py"
            assert not sensitive_source(relative, (root / relative).read_bytes())


@pytest.mark.parametrize(
    "code",
    [
        "def verify(token: str) -> bool:\n    return bool(token)\n",
        "token = load_from_store()\n",
        "def verify(*, password: str):\n    return password is not None\n",
    ],
)
def test_source_identifiers_are_safe(code: str) -> None:
    assert not sensitive_source("module.py", code.encode())


@pytest.mark.parametrize(
    "code",
    [
        'token = "fixture private value"\n',
        'DB_PASSWORD: str = "fixture private value"\n',
        'def verify(token="fixture private value"):\n    pass\n',
        'def verify(*, password="fixture private value"):\n    pass\n',
        'self.password = SecretStr("fixture private value")\n',
        'cfg = {"api_key": "fixture private value"}\n',
        "# token=fixture-private-value\n",
    ],
)
def test_credential_literals_and_comments_are_excluded(code: str) -> None:
    assert sensitive_source("module.py", code.encode())


def test_view_filters_private_binary_and_sensitive_files(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "safe.py").write_text("def use(token: str):\n    return bool(token)\n")
    (root / "private.py").write_text('password="fixture private value"\n')
    (root / ".env").write_text("fixture-only")
    (root / "asset.bin").write_bytes(b"\x00\xff")
    (root / ".git").mkdir()
    (root / ".git/config").write_text("fixture git credentials")
    view = tmp_path / "view"
    omitted = make_view(root, view, ["safe.py"], 1024)
    assert set(omitted) == {"private.py", ".env", "asset.bin"}
    assert set(file_inventory(view)) == {"safe.py", VIEW_NOTICE}
    with pytest.raises(ValueError, match="protected_source"):
        make_view(root, tmp_path / "denied", ["private.py"], 1024)


@pytest.mark.parametrize("effect", ["delete", "edit", "new", "symlink", "hardlink"])
def test_unapproved_changes_are_denied_including_deletion(tmp_path: Path, effect: str) -> None:
    job, _, _ = job_at(tmp_path)
    repo, view = tmp_path / "trusted", tmp_path / "view"
    repo.mkdir()
    (repo / "calculator.py").write_text("def add(a,b):\n    return a-b\n")
    (repo / "other.py").write_text("read_only = True\n")
    make_view(repo, view, job.target.allowed_paths, 1024)
    before = file_inventory(view)
    other = view / "other.py"
    if effect == "delete":
        other.unlink()
    elif effect == "edit":
        other.write_text("changed = True\n")
    elif effect == "new":
        (view / "new.py").write_text("changed = True\n")
    elif effect == "symlink":
        (view / "link.py").symlink_to(other)
    else:
        os.link(other, view / "link.py")
    with pytest.raises(ValueError):
        copy_changes(job, view, repo, before)
    assert (repo / "other.py").read_text() == "read_only = True\n"


def test_approved_change_preserves_excluded_source(tmp_path: Path) -> None:
    job, _, _ = job_at(tmp_path)
    repo, view = tmp_path / "trusted", tmp_path / "view"
    repo.mkdir()
    (repo / "calculator.py").write_text("def add(a,b):\n    return a-b\n")
    protected = b'password="fixture private value"\n'
    (repo / "private.py").write_bytes(protected)
    make_view(repo, view, job.target.allowed_paths, 1024)
    before = file_inventory(view)
    (view / "calculator.py").write_text("def add(a,b):\n    return a+b\n")
    copy_changes(job, view, repo, before)
    assert (repo / "private.py").read_bytes() == protected
    assert "a+b" in (repo / "calculator.py").read_text()
