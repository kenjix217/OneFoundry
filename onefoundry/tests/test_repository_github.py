"""Exact GitHub commit traversal must not trust returned URLs or working branches."""

import base64
import hashlib
from time import monotonic
from typing import Any
from unittest.mock import patch

import pytest

from onefoundry.repository_github import read_blob
from onefoundry.role_repository_file import RepositoryReadError
from onefoundry.settings import Settings

REVISION = "a" * 40
TREE = "b" * 40
DATA = b"# Architecture\nA small local service.\n"
BLOB = hashlib.sha1(b"blob " + str(len(DATA)).encode() + b"\0" + DATA).hexdigest()  # noqa: S324


def responses() -> list[dict[str, Any]]:
    return [
        {"sha": REVISION, "tree": {"sha": TREE}},
        {
            "sha": TREE,
            "truncated": False,
            "tree": [
                {"path": "ARCHITECTURE.md", "mode": "100644", "type": "blob", "sha": BLOB, "size": len(DATA)}
            ],
        },
        {"sha": BLOB, "size": len(DATA), "encoding": "base64", "content": base64.b64encode(DATA).decode()},
    ]


def read() -> bytes:
    return read_blob(
        "example",
        "repo",
        REVISION,
        "ARCHITECTURE.md",
        "fixture-credential",
        Settings(db_password="fixture"),
        limit=1024,
        deadline=monotonic() + 5,
    )


def test_exact_commit_tree_and_verified_blob() -> None:
    with patch("onefoundry.repository_github._get", side_effect=responses()) as request:
        assert read() == DATA
    assert [call.args[0] for call in request.call_args_list] == [
        f"/repos/example/repo/git/commits/{REVISION}",
        f"/repos/example/repo/git/trees/{TREE}",
        f"/repos/example/repo/git/blobs/{BLOB}",
    ]


@pytest.mark.parametrize(
    "change",
    [
        "commit",
        "tree",
        "truncated",
        "symlink",
        "submodule",
        "oversize",
        "blob",
        "size",
        "encoding",
        "bytes",
        "missing",
    ],
)
def test_rejects_changed_or_unsafe_github_objects(change: str) -> None:
    values = responses()
    if change == "commit":
        values[0]["sha"] = "c" * 40
    elif change == "tree":
        values[1]["sha"] = "c" * 40
    elif change == "truncated":
        values[1]["truncated"] = True
    elif change == "symlink":
        values[1]["tree"][0]["mode"] = "120000"
    elif change == "submodule":
        values[1]["tree"][0].update(mode="160000", type="commit")
    elif change == "oversize":
        values[1]["tree"][0]["size"] = 1025
    elif change == "blob":
        values[2]["sha"] = "c" * 40
    elif change == "size":
        values[2]["size"] = len(DATA) + 1
    elif change == "encoding":
        values[2]["encoding"] = "utf-8"
    elif change == "bytes":
        values[2]["content"] = base64.b64encode(b"x" * len(DATA)).decode()
    elif change == "missing":
        values[1]["tree"] = []
    with patch("onefoundry.repository_github._get", side_effect=values):
        with pytest.raises(RepositoryReadError):
            read()


def test_deadline_prevents_a_request() -> None:
    with patch("onefoundry.repository_github._get") as request:
        with pytest.raises(RepositoryReadError, match="timed out"):
            read_blob(
                "example",
                "repo",
                REVISION,
                "README.md",
                "fixture-credential",
                Settings(db_password="fixture"),
                limit=1024,
                deadline=monotonic() - 1,
            )
        request.assert_not_called()


@pytest.mark.parametrize("path", ["../secret", ".env", ".git/config", "/file", "a//b", "keys/token.pem"])
def test_invalid_paths_never_reach_github(path: str) -> None:
    with patch("onefoundry.repository_github._get") as request:
        with pytest.raises(RepositoryReadError):
            read_blob(
                "example",
                "repo",
                REVISION,
                path,
                "fixture-credential",
                Settings(db_password="fixture"),
                limit=1024,
                deadline=monotonic() + 5,
            )
        request.assert_not_called()
