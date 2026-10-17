"""Unwired transport proof against a local, TLS GitHub-shaped fixture.

This is not a production GitHub client or OneFoundry dispatcher. The fixture API
asserts the commit-to-tree link; raw commit bytes are not independently hashed.
Blob and tree bytes are hashed locally. Limits cover each response and decoded
content, not aggregate wire bytes. No checkout is retained.
"""

from __future__ import annotations

import base64
import hashlib
import json
import multiprocessing
import re
import ssl
import time
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from onefoundry.github_snapshot_bundle import BundleFile, BundleInvalid, build_bundle, write_snapshot
from onefoundry.source_github import BRANCH, REPO_PART, SHA
from runner.local_source_stage import StageLimits

SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class FixtureFetchError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class FetchRequest:
    owner: str
    repo: str
    branch: str
    saved_revision: str

    def validate(self) -> None:
        if (
            REPO_PART.fullmatch(self.owner) is None
            or REPO_PART.fullmatch(self.repo) is None
            or BRANCH.fullmatch(self.branch) is None
            or ".." in self.branch.split("/")
            or SHA.fullmatch(self.saved_revision) is None
        ):
            raise FixtureFetchError("invalid_source")


@dataclass(frozen=True)
class FetchReceipt:
    status: str
    reason_code: str
    request_sha256: str
    source_revision: str | None
    source_tree: str | None
    file_count: int | None
    total_bytes: int | None


@dataclass(frozen=True)
class SnapshotFetchReceipt:
    source_revision: str
    source_tree: str
    file_count: int
    total_bytes: int
    snapshot_path: str
    snapshot_sha256: str
    snapshot_byte_size: int


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, request: Request, fp: object, code: int, msg: str, headers: object, newurl: str
    ) -> None:
        return None


def _git_hash(kind: str, data: bytes) -> str:
    return hashlib.sha1(f"{kind} {len(data)}\0".encode() + data).hexdigest()  # noqa: S324


def _tree_hash(entries: list[tuple[str, str, str]]) -> str:
    raw = bytearray()

    def order(item: tuple[str, str, str]) -> bytes:
        return item[0].encode() + (b"/" if item[1] == "40000" else b"")

    for name, mode, sha in sorted(entries, key=order):
        raw.extend(f"{mode} {name}\0".encode())
        raw.extend(bytes.fromhex(sha))
    return _git_hash("tree", bytes(raw))


class FixtureCredentialBroker:
    """Test-only token owner; its endpoint can only be a local HTTPS fixture."""

    def __init__(self, endpoint: str, ca_file: Path, dummy_token: str, limits: StageLimits) -> None:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"localhost", "127.0.0.1"}
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.port is None
            or not dummy_token
            or any(ord(c) < 33 or ord(c) > 126 for c in dummy_token)
        ):
            raise FixtureFetchError("fixture_endpoint_invalid")
        limits.validate()
        self._endpoint = endpoint
        self._ca_file = ca_file
        self._token = dummy_token
        self._limits = limits
        context = ssl.create_default_context(cafile=str(ca_file))
        self._opener = build_opener(ProxyHandler({}), _NoRedirect(), HTTPSHandler(context=context))

    def _get(self, path: str, deadline: float, maximum: int) -> dict[str, object]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FixtureFetchError("fetch_timeout")
        request = Request(  # noqa: S310 - endpoint is constructor-validated local HTTPS
            self._endpoint + path,
            headers={"Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github+json"},
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=remaining) as response:  # noqa: S310 - fixed local TLS origin
                if response.status != 200 or response.url != request.full_url:
                    raise FixtureFetchError("fixture_response_invalid")
                raw = response.read(maximum + 1)
        except (HTTPError, URLError, TimeoutError, OSError):
            raise FixtureFetchError("fixture_unavailable") from None
        if len(raw) > maximum:
            raise FixtureFetchError("fetch_limit")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise FixtureFetchError("fixture_response_invalid") from None
        if not isinstance(value, dict):
            raise FixtureFetchError("fixture_response_invalid")
        return value

    def _head(self, root: str, branch: str, deadline: float) -> str:
        path = root + "/git/ref/heads/" + quote(branch, safe="")
        value = self._get(path, deadline, self._limits.max_tree_bytes)
        obj = value.get("object")
        sha = obj.get("sha") if isinstance(obj, dict) else None
        if value.get("ref") != "refs/heads/" + branch or not isinstance(sha, str) or not SHA.fullmatch(sha):
            raise FixtureFetchError("branch_invalid")
        return sha

    def fetch(self, request: FetchRequest) -> FetchReceipt:
        """Kill the complete network attempt when its wall deadline expires."""
        request.validate()
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_fetch_child,
            args=(sender, self._endpoint, self._ca_file, self._token, self._limits, request),
            daemon=True,
        )
        started = time.monotonic()
        try:
            process.start()
            sender.close()
            process.join(max(0.0, self._limits.wall_seconds - (time.monotonic() - started)))
            if process.is_alive():
                process.kill()
                process.join()
                raise FixtureFetchError("fetch_timeout")
            if process.exitcode != 0 or not receiver.poll():
                raise FixtureFetchError("fixture_unavailable")
            status, value = receiver.recv()
            if status == "error":
                raise FixtureFetchError(value)
            if status != "done" or not isinstance(value, FetchReceipt):
                raise FixtureFetchError("fixture_response_invalid")
            return value
        finally:
            sender.close()
            receiver.close()
            process.close()

    def fetch_snapshot(self, request: FetchRequest, snapshot_root: Path) -> SnapshotFetchReceipt:
        """Fetch into a bounded durable bundle without piping source bytes to the parent."""
        request.validate()
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_snapshot_child,
            args=(sender, self._endpoint, self._ca_file, self._token, self._limits, request, snapshot_root),
            daemon=True,
        )
        started = time.monotonic()
        try:
            process.start()
            sender.close()
            process.join(max(0.0, self._limits.wall_seconds - (time.monotonic() - started)))
            if process.is_alive():
                process.kill()
                process.join()
                raise FixtureFetchError("fetch_timeout")
            if process.exitcode != 0 or not receiver.poll():
                raise FixtureFetchError("fixture_unavailable")
            status, value = receiver.recv()
            if status == "error":
                raise FixtureFetchError(value)
            if status != "done" or not isinstance(value, SnapshotFetchReceipt):
                raise FixtureFetchError("fixture_response_invalid")
            return value
        finally:
            sender.close()
            receiver.close()
            process.close()

    def _fetch_direct(self, request: FetchRequest, collected: list[BundleFile] | None = None) -> FetchReceipt:
        request.validate()
        canonical = json.dumps(request.__dict__, sort_keys=True, separators=(",", ":")).encode()
        request_hash = hashlib.sha256(canonical).hexdigest()
        assert SHA256.fullmatch(request_hash)
        deadline = time.monotonic() + self._limits.wall_seconds
        root = "/repos/" + quote(request.owner, safe="") + "/" + quote(request.repo, safe="")
        first = self._head(root, request.branch, deadline)
        if first != request.saved_revision:
            raise FixtureFetchError("source_revision_drift")
        commit = self._get(root + "/git/commits/" + first, deadline, self._limits.max_tree_bytes)
        tree_ref = commit.get("tree")
        tree_sha = tree_ref.get("sha") if isinstance(tree_ref, dict) else None
        if commit.get("sha") != first or not isinstance(tree_sha, str) or not SHA.fullmatch(tree_sha):
            raise FixtureFetchError("commit_invalid")
        files = 0
        total = 0
        tree_visits = 0

        def read_tree(sha: str, depth: int, prefix: str = "") -> str:
            nonlocal files, total, tree_visits
            tree_visits += 1
            if depth > 32 or tree_visits > self._limits.max_files * 2 + 32:
                raise FixtureFetchError("tree_limit")
            result = self._get(root + "/git/trees/" + sha, deadline, self._limits.max_tree_bytes)
            rows = result.get("tree")
            if result.get("sha") != sha or result.get("truncated") is not False or not isinstance(rows, list):
                raise FixtureFetchError("tree_invalid")
            entries: list[tuple[str, str, str]] = []
            names: set[str] = set()
            for row in rows:
                if not isinstance(row, dict):
                    raise FixtureFetchError("tree_invalid")
                name, mode, kind, object_sha = (row.get(key) for key in ("path", "mode", "type", "sha"))
                if (
                    not isinstance(name, str)
                    or not name
                    or name in {".", ".."}
                    or "/" in name
                    or "\\" in name
                    or name.casefold() == ".git"
                    or any(ord(char) < 32 for char in name)
                    or name in names
                    or not isinstance(object_sha, str)
                    or SHA.fullmatch(object_sha) is None
                ):
                    raise FixtureFetchError("tree_unsafe")
                names.add(name)
                if mode == "040000" and kind == "tree":
                    actual = read_tree(object_sha, depth + 1, prefix + name + "/")
                    entries.append((name, "40000", actual))
                elif mode in {"100644", "100755"} and kind == "blob":
                    blob = self._get(
                        root + "/git/blobs/" + object_sha, deadline, self._limits.max_bytes * 2 + 4096
                    )
                    encoded = blob.get("content")
                    if (
                        blob.get("sha") != object_sha
                        or blob.get("encoding") != "base64"
                        or not isinstance(encoded, str)
                    ):
                        raise FixtureFetchError("blob_invalid")
                    try:
                        data = base64.b64decode(encoded, validate=True)
                    except ValueError:
                        raise FixtureFetchError("blob_invalid") from None
                    if _git_hash("blob", data) != object_sha:
                        raise FixtureFetchError("blob_mismatch")
                    files += 1
                    total += len(data)
                    if files > self._limits.max_files or total > self._limits.max_bytes:
                        raise FixtureFetchError("fetch_limit")
                    if collected is not None:
                        collected.append(BundleFile(prefix + name, mode, object_sha, data))
                    entries.append((name, mode, object_sha))
                else:
                    raise FixtureFetchError("tree_unsafe")
            calculated = _tree_hash(entries)
            if calculated != sha:
                raise FixtureFetchError("tree_mismatch")
            return calculated

        read_tree(tree_sha, 0)
        if self._head(root, request.branch, deadline) != first:
            raise FixtureFetchError("source_revision_drift")
        return FetchReceipt("done", "verified_only", request_hash, first, tree_sha, files, total)


def _fetch_child(
    pipe: Connection, endpoint: str, ca_file: Path, token: str, limits: StageLimits, request: FetchRequest
) -> None:
    try:
        broker = FixtureCredentialBroker(endpoint, ca_file, token, limits)
        pipe.send(("done", broker._fetch_direct(request)))
    except FixtureFetchError as error:
        pipe.send(("error", error.code))
    except (OSError, ValueError):
        pipe.send(("error", "fixture_unavailable"))
    finally:
        pipe.close()


def _snapshot_child(
    pipe: Connection,
    endpoint: str,
    ca_file: Path,
    token: str,
    limits: StageLimits,
    request: FetchRequest,
    snapshot_root: Path,
) -> None:
    try:
        broker = FixtureCredentialBroker(endpoint, ca_file, token, limits)
        collected: list[BundleFile] = []
        result = broker._fetch_direct(request, collected)
        assert result.source_revision is not None and result.source_tree is not None
        assert result.file_count is not None and result.total_bytes is not None
        bundle = build_bundle(result.source_tree, collected)
        stored = write_snapshot(snapshot_root, bundle)
        if len(stored.files) != result.file_count or stored.total_bytes != result.total_bytes:
            raise FixtureFetchError("snapshot_readback_mismatch")
        pipe.send(
            (
                "done",
                SnapshotFetchReceipt(
                    result.source_revision,
                    result.source_tree,
                    result.file_count,
                    result.total_bytes,
                    f"snapshots/{stored.sha256}.json",
                    stored.sha256,
                    stored.byte_size,
                ),
            )
        )
    except (FixtureFetchError, BundleInvalid) as error:
        pipe.send(("error", error.code if isinstance(error, FixtureFetchError) else "snapshot_invalid"))
    except (OSError, ValueError):
        pipe.send(("error", "fixture_unavailable"))
    finally:
        pipe.close()
