"""Actual fake HTTPS CAS to isolated Codex CLI; no app or live account."""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from onefoundry.github_snapshot_codex_contract import expected_diff
from runner import codex_workorder_fixture as bridge
from runner import docker_egress_proof as egress
from runner import github_https_fixture_server as https_fixture
from runner.external_cli import CliRequest, CliResult
from runner.github_fixture_snapshot_materialize import SnapshotMaterializationError
from runner.github_fixture_snapshot_supervisor import HTTPSFixtureConfig, process_job
from runner.github_snapshot_codex_fixture_proof import (
    AFTER,
    BEFORE,
    SnapshotCodexProofError,
    prove_snapshot_codex_fixture,
)
from runner.local_source_stage import StageLimits
from tests.test_github_fixture_snapshot_supervisor import job, roots
from tests.test_github_https_fixture_fetch import TOKEN, fixture

FAKE_IMAGE = "sha256:" + "a" * 64
FAKE_FIXTURE_IMAGE = "sha256:" + "b" * 64
COMMIT = https_fixture.COMMIT
TREE = https_fixture.TREE


def runtime_routes() -> dict[str, object]:
    root = https_fixture.ROOT
    return {
        root + "/commits/" + COMMIT: {"sha": COMMIT, "tree": {"sha": TREE}},
        root + "/trees/" + TREE: {
            "sha": TREE,
            "truncated": False,
            "tree": [{"path": "file.txt", "mode": "100644", "type": "blob", "sha": https_fixture.BLOB}],
        },
        root + "/blobs/" + https_fixture.BLOB: {
            "sha": https_fixture.BLOB,
            "encoding": "base64",
            "content": base64.b64encode(https_fixture.DATA).decode(),
        },
    }


def test_proof_consumes_compose_https_fixture_bytes() -> None:
    assert BEFORE == https_fixture.DATA


def source(tmp_path: Path) -> tuple[Path, str, str, int, str, Path]:
    spool, snapshot_root = roots(tmp_path)
    manifest = job(spool, (datetime.now(UTC) + timedelta(minutes=5)).isoformat())
    with fixture(tmp_path, runtime_routes()) as (endpoint, ca_file, _):
        config = HTTPSFixtureConfig(
            spool, endpoint, ca_file, TOKEN, StageLimits(10, 4096, 4096, 4), snapshot_root
        )
        receipt = process_job(manifest, config)
    assert receipt is not None and receipt["status"] == "done"
    assert receipt["source_revision"] == COMMIT and receipt["source_tree"] == TREE
    work = tmp_path / "work"
    work.mkdir()
    return (
        snapshot_root,
        receipt["snapshot_path"],
        receipt["snapshot_sha256"],
        receipt["snapshot_byte_size"],
        receipt["source_tree"],
        work,
    )


def test_snapshot_file_edit_proof_uses_materialized_cas_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = source(tmp_path)
    calls: list[Path] = []

    def fake_invoke(request: CliRequest, _image: str, _fixture_image: str) -> CliResult:
        calls.append(request.repository)
        assert (request.repository / "file.txt").read_bytes() == https_fixture.DATA
        (request.repository / "file.txt").write_bytes(AFTER)
        return CliResult("done", "provider_completed", "fixture-session", 28, None, b"synthetic-only\n")

    monkeypatch.setattr("runner.github_snapshot_codex_fixture_proof.invoke_file_codex", fake_invoke)
    result = prove_snapshot_codex_fixture(*arguments, FAKE_IMAGE, FAKE_FIXTURE_IMAGE)
    assert result.source_tree == TREE and result.snapshot_sha256 == arguments[2]
    assert result.synthetic_baseline_commit != COMMIT
    assert (result.before_text_check, result.after_text_check) == ("failed", "passed")
    assert result.diff_sha256 == hashlib.sha256(expected_diff()).hexdigest()
    assert result.provider_session_id == "fixture-session"
    assert result.tokens_used == 28 and result.cost_usd is None
    assert calls and not calls[0].exists()
    assert not list(arguments[-1].iterdir())


def test_bad_cas_and_reference_never_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    arguments = source(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        "runner.github_snapshot_codex_fixture_proof.invoke_file_codex",
        lambda *_args: calls.append("launched"),
    )
    root, relative, digest, size, tree, work = arguments
    with pytest.raises(SnapshotMaterializationError, match="snapshot_reference_invalid"):
        prove_snapshot_codex_fixture(
            root, "../" + relative, digest, size, tree, work, FAKE_IMAGE, FAKE_FIXTURE_IMAGE
        )
    (root / relative).write_bytes(b"x" * size)
    with pytest.raises(SnapshotMaterializationError, match="snapshot_hash_mismatch"):
        prove_snapshot_codex_fixture(root, relative, digest, size, tree, work, FAKE_IMAGE, FAKE_FIXTURE_IMAGE)
    assert calls == [] and not list(work.iterdir())


def test_wrong_tree_and_uncertain_invocation_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    arguments = source(tmp_path)
    calls: list[str] = []

    def uncertain(*_args: object) -> CliResult:
        calls.append("launched")
        raise SnapshotCodexProofError("fixture_unavailable")

    monkeypatch.setattr("runner.github_snapshot_codex_fixture_proof.invoke_file_codex", uncertain)
    root, relative, digest, size, _tree, work = arguments
    with pytest.raises(SnapshotCodexProofError, match="fake_codex_snapshot_unexpected"):
        prove_snapshot_codex_fixture(
            root, relative, digest, size, "0" * 40, work, FAKE_IMAGE, FAKE_FIXTURE_IMAGE
        )
    assert calls == [] and not list(work.iterdir())
    with pytest.raises(SnapshotCodexProofError, match="fixture_unavailable"):
        prove_snapshot_codex_fixture(*arguments, FAKE_IMAGE, FAKE_FIXTURE_IMAGE)
    assert calls == ["launched"] and not list(work.iterdir())


@pytest.fixture
def admitted_images() -> Iterator[tuple[str, str]]:
    executable = shutil.which("docker")
    assert executable is not None
    from runner import codex_cli_admission
    from runner.codex_cli_admission import DockerEndpoint, build_and_prove

    endpoint = DockerEndpoint(Path(executable).resolve(), Path("/var/run/docker.sock"))
    fixture_image = egress._inspect("image", os.environ["ONEFOUNDRY_EGRESS_TEST_IMAGE"])["Id"]
    assert isinstance(fixture_image, str) and bridge.SHA_IMAGE.fullmatch(fixture_image)
    admitted = build_and_prove(endpoint)
    try:
        image_id = admitted["image_id"]
        assert codex_cli_admission._inspect(endpoint, "image", admitted["image_tag"])["Id"] == image_id
        yield image_id, fixture_image
    finally:
        codex_cli_admission._remove_tag_if_present(endpoint, admitted["image_tag"])


@pytest.mark.integration
def test_real_pinned_codex_cli_edits_only_materialized_fake_https_file(
    tmp_path: Path, admitted_images: tuple[str, str]
) -> None:
    arguments = source(tmp_path)
    before = hashlib.sha256((arguments[0] / arguments[1]).read_bytes()).hexdigest()
    result = prove_snapshot_codex_fixture(*arguments, *admitted_images)
    assert result.source_tree == TREE and result.snapshot_sha256 == arguments[2]
    assert result.synthetic_baseline_commit != COMMIT
    assert (result.before_text_check, result.after_text_check) == ("failed", "passed")
    assert result.provider_session_id and result.tokens_used == 28 and result.cost_usd is None
    assert result.sanitized_log.startswith(
        b"synthetic_cli=codex_cli model=gpt-5 command=snapshot_file_repair "
    )
    assert admitted_images[0].encode() in result.sanitized_log
    assert b"local-test-only" not in result.sanitized_log
    assert hashlib.sha256((arguments[0] / arguments[1]).read_bytes()).hexdigest() == before
    assert not list(arguments[-1].iterdir())
