"""Local HTTPS GitHub-shaped fixture; no real GitHub account or endpoint is used."""

import base64
import hashlib
import json
import ssl
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from runner.github_https_fixture_fetch import (
    FetchRequest,
    FixtureCredentialBroker,
    FixtureFetchError,
)
from runner.local_source_stage import StageLimits

TOKEN = "dummy-fixture-token"
COMMIT = "a" * 40
DATA = b"fixture source\n"
BLOB = hashlib.sha1(f"blob {len(DATA)}\0".encode() + DATA).hexdigest()  # noqa: S324
TREE = hashlib.sha1(("tree 36\0" + "100644 file.txt\0").encode() + bytes.fromhex(BLOB)).hexdigest()  # noqa: S324
REQUEST = FetchRequest("sample", "project", "main", COMMIT)
LIMITS = StageLimits(10, 4096, 4096, 4)


class FixtureHandler(BaseHTTPRequestHandler):
    routes: dict[str, object] = {}
    observed: list[tuple[str, str]] = []
    drift_after_first = False

    def do_GET(self) -> None:
        self.observed.append((self.path, self.headers.get("Authorization", "")))
        if self.headers.get("Authorization") != "Bearer " + TOKEN:
            self.send_error(401)
            return
        value = self.routes.get(self.path)
        if self.drift_after_first and self.path.endswith("/ref/heads/main"):
            checks = sum(path == self.path for path, _ in self.observed)
            if checks > 1:
                value = {"ref": "refs/heads/main", "object": {"sha": "b" * 40}}
        if isinstance(value, tuple):
            self.send_response(302)
            self.send_header("Location", value[0])
            self.end_headers()
            return
        if value == "dribble":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "100")
            self.end_headers()
            try:
                for _ in range(100):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.03)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if value is None:
            self.send_error(404)
            return
        raw = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, _format: str, *_args: object) -> None:
        pass


def routes() -> dict[str, object]:
    root = "/repos/sample/project/git"
    return {
        root + "/ref/heads/main": {"ref": "refs/heads/main", "object": {"sha": COMMIT}},
        root + "/commits/" + COMMIT: {"sha": COMMIT, "tree": {"sha": TREE}},
        root + "/trees/" + TREE: {
            "sha": TREE,
            "truncated": False,
            "tree": [{"path": "file.txt", "mode": "100644", "type": "blob", "sha": BLOB}],
        },
        root + "/blobs/" + BLOB: {
            "sha": BLOB,
            "encoding": "base64",
            "content": base64.b64encode(DATA).decode(),
        },
    }


@contextmanager
def fixture(
    tmp_path: Path, override: dict[str, object] | None = None
) -> Iterator[tuple[str, Path, type[FixtureHandler]]]:
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(  # noqa: S603 - fixed local certificate generator for the disposable fixture
        [
            "/usr/bin/openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    handler: type[FixtureHandler] = type(
        "LocalFixtureHandler", (FixtureHandler,), {"routes": routes(), "observed": []}
    )
    if override:
        handler.routes = {**routes(), **override}
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://localhost:{server.server_port}", cert, handler
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_exact_tls_fetch_is_bounded_secret_free_and_verification_only(tmp_path: Path) -> None:
    with fixture(tmp_path) as (endpoint, cert, handler):
        receipt = FixtureCredentialBroker(endpoint, cert, TOKEN, LIMITS).fetch(REQUEST)
    assert receipt.status == "done" and receipt.reason_code == "verified_only"
    assert receipt.source_revision == COMMIT and receipt.source_tree == TREE
    assert receipt.file_count == 1 and receipt.total_bytes == len(DATA)
    assert TOKEN not in repr(receipt)
    assert len(handler.observed) == 5  # branch, commit, tree, blob, branch
    assert all(auth == "Bearer " + TOKEN for _, auth in handler.observed)
    assert all(path.startswith("/repos/sample/project/git/") for path, _ in handler.observed)
    assert not list(tmp_path.glob("*stage*"))


def test_wrong_token_and_wrong_revision_fail_closed(tmp_path: Path) -> None:
    with fixture(tmp_path) as (endpoint, cert, _):
        with pytest.raises(FixtureFetchError, match="fixture_unavailable"):
            FixtureCredentialBroker(endpoint, cert, "wrong-token", LIMITS).fetch(REQUEST)
        with pytest.raises(FixtureFetchError, match="source_revision_drift"):
            FixtureCredentialBroker(endpoint, cert, TOKEN, LIMITS).fetch(
                FetchRequest("sample", "project", "main", "b" * 40)
            )


def test_redirect_and_foreign_endpoint_are_denied(tmp_path: Path) -> None:
    head = "/repos/sample/project/git/ref/heads/main"
    with fixture(tmp_path, {head: ("https://example.com/steal",)}) as (endpoint, cert, handler):
        with pytest.raises(FixtureFetchError, match="fixture_unavailable"):
            FixtureCredentialBroker(endpoint, cert, TOKEN, LIMITS).fetch(REQUEST)
        assert len(handler.observed) == 1
    with pytest.raises(FixtureFetchError, match="fixture_endpoint_invalid"):
        FixtureCredentialBroker("https://api.github.com", cert, TOKEN, LIMITS)


def test_blob_mismatch_unsafe_tree_and_response_limit(tmp_path: Path) -> None:
    blob = "/repos/sample/project/git/blobs/" + BLOB
    bad_blob = {"sha": BLOB, "encoding": "base64", "content": "YmFk"}
    with fixture(tmp_path, {blob: bad_blob}) as (endpoint, cert, _):
        with pytest.raises(FixtureFetchError, match="blob_mismatch"):
            FixtureCredentialBroker(endpoint, cert, TOKEN, LIMITS).fetch(REQUEST)
    tree = "/repos/sample/project/git/trees/" + TREE
    unsafe = {
        "sha": TREE,
        "truncated": False,
        "tree": [{"path": "evil", "mode": "120000", "type": "blob", "sha": BLOB}],
    }
    with fixture(tmp_path, {tree: unsafe}) as (endpoint, cert, _):
        with pytest.raises(FixtureFetchError, match="tree_unsafe"):
            FixtureCredentialBroker(endpoint, cert, TOKEN, LIMITS).fetch(REQUEST)
    with fixture(tmp_path) as (endpoint, cert, _):
        with pytest.raises(FixtureFetchError, match="fetch_limit"):
            FixtureCredentialBroker(endpoint, cert, TOKEN, StageLimits(10, 1, 4096, 4)).fetch(REQUEST)


def test_late_branch_drift_and_wrong_repository_fail(tmp_path: Path) -> None:
    with fixture(tmp_path) as (endpoint, cert, handler):
        handler.drift_after_first = True
        with pytest.raises(FixtureFetchError, match="source_revision_drift"):
            FixtureCredentialBroker(endpoint, cert, TOKEN, LIMITS).fetch(REQUEST)
        with pytest.raises(FixtureFetchError, match="fixture_unavailable"):
            FixtureCredentialBroker(endpoint, cert, TOKEN, LIMITS).fetch(
                FetchRequest("sample", "other", "main", COMMIT)
            )


def test_tree_hash_and_input_validation_fail_closed(tmp_path: Path) -> None:
    tree = "/repos/sample/project/git/trees/" + TREE
    changed = {
        "sha": TREE,
        "truncated": False,
        "tree": [{"path": "renamed.txt", "mode": "100644", "type": "blob", "sha": BLOB}],
    }
    with fixture(tmp_path, {tree: changed}) as (endpoint, cert, _):
        broker = FixtureCredentialBroker(endpoint, cert, TOKEN, LIMITS)
        with pytest.raises(FixtureFetchError, match="tree_mismatch"):
            broker.fetch(REQUEST)
        with pytest.raises(FixtureFetchError, match="invalid_source"):
            broker.fetch(FetchRequest("example.com:443", "project", "main", COMMIT))


def test_whole_attempt_deadline_kills_slow_dribble(tmp_path: Path) -> None:
    head = "/repos/sample/project/git/ref/heads/main"
    with fixture(tmp_path, {head: "dribble"}) as (endpoint, cert, handler):
        broker = FixtureCredentialBroker(endpoint, cert, TOKEN, StageLimits(10, 4096, 4096, 0.6))
        started = time.monotonic()
        with pytest.raises(FixtureFetchError, match="fetch_timeout"):
            broker.fetch(REQUEST)
        assert time.monotonic() - started < 2.0
        assert len(handler.observed) == 1
