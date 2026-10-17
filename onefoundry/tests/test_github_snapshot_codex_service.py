"""One-shot fake snapshot coding worker against a real CAS and disposable Git tree."""

import base64
import hashlib
import json
import os
import stat
from pathlib import Path
from uuid import uuid4

import pytest

from onefoundry.github_snapshot_codex_contract import expected_diff, manifest_payload
from runner import codex_broker_proof as broker_proof
from runner.external_cli import CliResult
from runner.github_https_fixture_server import DATA as HTTPS_DATA
from runner.github_snapshot_codex_fixture_proof import AFTER, BEFORE
from runner.github_snapshot_codex_service import process_snapshot_codex_job
from runner.github_snapshot_codex_supervisor import SnapshotCodexConfig, fence_orphans

IMAGE = "sha256:" + "a" * 64


def test_worker_fixture_matches_https_source_and_diff() -> None:
    assert BEFORE == HTTPS_DATA
    assert b"-synthetic HTTPS fixture only\n" in expected_diff()
    assert b"+synthetic HTTPS fixture repaired\n" in expected_diff()


def _blob(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()  # noqa: S324


def _tree(data: bytes) -> str:
    entry = b"100644 file.txt\0" + bytes.fromhex(_blob(data))
    return hashlib.sha1(f"tree {len(entry)}\0".encode() + entry).hexdigest()  # noqa: S324


def _job(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, object]]:
    root, spool = tmp_path / "snapshots-root", tmp_path / "spool"
    (root / "snapshots").mkdir(parents=True)
    for name in ("jobs", "claims", "receipts", "artifacts"):
        (spool / name).mkdir(parents=True)
    bundle = {
        "schema_version": 3,
        "kind": "github_fixture_snapshot_bundle_v3",
        "source_tree": _tree(BEFORE),
        "files": [
            {
                "path": "file.txt",
                "mode": "100644",
                "git_blob_sha": _blob(BEFORE),
                "content_base64": base64.b64encode(BEFORE).decode(),
            }
        ],
    }
    data = json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(data).hexdigest()
    (root / "snapshots" / f"{digest}.json").write_bytes(data)
    attempt = str(uuid4())
    unsigned: dict[str, object] = {
        "schema_version": 3,
        "target_id": "github_snapshot_codex_fixture_v1",
        "job_id": attempt,
        "project_id": str(uuid4()),
        "run_id": str(uuid4()),
        "attempt_id": attempt,
        "work_order_id": str(uuid4()),
        "idempotency_key": str(uuid4()),
        "intent_sha256": "b" * 64,
        "adapter_id": "codex_cli",
        "stage": "engineer",
        "actor_id": "operator",
        "task_title": "Repair fixture file",
        "task_instructions": "Repair only file.txt.",
        "source": {
            "snapshot_attempt_id": str(uuid4()),
            "source_id": str(uuid4()),
            "account_id": str(uuid4()),
            "owner": "fixture",
            "repo": "sample",
            "branch": "main",
            "saved_revision": "c" * 40,
            "source_tree": _tree(BEFORE),
            "snapshot_path": f"snapshots/{digest}.json",
            "snapshot_sha256": digest,
            "snapshot_byte_size": len(data),
            "snapshot_request_sha256": "d" * 64,
            "snapshot_receipt_sha256": "e" * 64,
        },
        "limits": {"wall_seconds": 60, "max_log_bytes": 65536, "max_diff_bytes": 65536},
    }
    job = manifest_payload(unsigned)
    path = spool / "jobs" / f"{attempt}.json"
    path.write_text(json.dumps(job, sort_keys=True, separators=(",", ":")))
    return root, spool, path, job


def test_worker_receipt_and_one_shot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, spool, path, job = _job(tmp_path)
    calls = 0

    def fake_invoke(request: object, image_id: str, fixture_image_id: str, _attempt_id: str) -> CliResult:
        nonlocal calls
        calls += 1
        from runner.external_cli import CliRequest

        assert isinstance(request, CliRequest)
        assert image_id == fixture_image_id == IMAGE
        assert (request.repository / "file.txt").read_bytes() == BEFORE
        (request.repository / "file.txt").write_bytes(AFTER)
        log = (
            f"synthetic_cli=codex_cli model=gpt-5 command=snapshot_file_repair "
            f"image_sha256={IMAGE} prompt_sha256={'f' * 64} "
            "allowed_provider_requests=2 blocked_other_connects=3\n"
        ).encode()
        return CliResult("done", "provider_completed", "fake-session", 28, None, log)

    monkeypatch.setattr("runner.github_snapshot_codex_service.invoke_file_codex", fake_invoke)
    receipt = process_snapshot_codex_job(path, spool, root, IMAGE, IMAGE)
    assert receipt is not None
    assert receipt["status"] == "done"
    assert receipt["snapshot"] == job["source"]
    assert receipt["baseline_commit"] != job["source"]["saved_revision"]  # type: ignore[index]
    assert receipt["cost_usd"] is None
    assert process_snapshot_codex_job(path, spool, root, IMAGE, IMAGE) is None
    assert calls == 1
    assert len(list((spool / "receipts").iterdir())) == 1


def test_tampered_cas_never_launches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, spool, path, job = _job(tmp_path)
    digest = job["source"]["snapshot_sha256"]  # type: ignore[index]
    (root / "snapshots" / f"{digest}.json").write_bytes(b"tampered")
    monkeypatch.setattr(
        "runner.github_snapshot_codex_service.invoke_file_codex",
        lambda *_args: pytest.fail("CLI launched for tampered CAS"),
    )
    receipt = process_snapshot_codex_job(path, spool, root, IMAGE, IMAGE)
    assert receipt is not None and receipt["status"] == "unknown"
    assert receipt["reason_code"] == "runner_failure_after_claim"


def test_failure_after_claim_is_terminal_and_never_relaunches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, spool, path, _job_data = _job(tmp_path)
    calls = 0

    def uncertain(*_args: object) -> CliResult:
        nonlocal calls
        calls += 1
        raise TimeoutError("synthetic fixture timeout")

    monkeypatch.setattr("runner.github_snapshot_codex_service.invoke_file_codex", uncertain)
    receipt = process_snapshot_codex_job(path, spool, root, IMAGE, IMAGE)
    assert receipt is not None and receipt["status"] == "unknown"
    assert receipt["reason_code"] == "runner_failure_after_claim"
    assert process_snapshot_codex_job(path, spool, root, IMAGE, IMAGE) is None
    assert calls == 1


def test_altered_manifest_never_claims_or_launches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, spool, path, job = _job(tmp_path)
    job["source"]["source_tree"] = "0" * 40  # type: ignore[index]
    path.write_text(json.dumps(job, sort_keys=True, separators=(",", ":")))
    monkeypatch.setattr(
        "runner.github_snapshot_codex_service.invoke_file_codex",
        lambda *_args: pytest.fail("CLI launched for altered manifest"),
    )
    with pytest.raises(ValueError, match="snapshot_coding_manifest_invalid"):
        process_snapshot_codex_job(path, spool, root, IMAGE, IMAGE)
    assert list((spool / "claims").iterdir()) == []


def test_orphan_fence_removes_exact_child_before_terminal_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, spool, _path, job = _job(tmp_path)
    attempt = str(job["attempt_id"])
    suffix = attempt.replace("-", "")
    cli = f"ap-snapshot-cli-{suffix}"
    claim = spool / "claims" / f"{attempt}.claim"
    claim.write_text(attempt)
    os.utime(claim, (1, 1))
    calls: list[tuple[str, ...]] = []

    def fake_docker(*args: str) -> str:
        calls.append(args)
        if args[0] == "ps":
            return cli if len([call for call in calls if call[0] == "ps"]) == 1 else ""
        assert args == ("rm", "-f", cli)
        return ""

    monkeypatch.setattr("runner.github_snapshot_codex_supervisor.egress._docker", fake_docker)
    assert fence_orphans(SnapshotCodexConfig(spool, root, IMAGE, IMAGE, 1)) == 1
    assert ("rm", "-f", cli) in calls
    fence = json.loads((spool / "claims" / f"{attempt}.fenced.json").read_text())
    assert fence["request_sha256"] == job["request_sha256"]
    assert fence["reason_code"] == "claimed_receipt_missing"


def test_dummy_broker_uid_and_client_group_share_only_disposable_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(mode=0o700)
    editable = repo / "file.txt"
    editable.write_bytes(BEFORE)
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("edit file.txt")
    secret = tmp_path / "broker-secret"
    secret.write_text("local-test-only")
    secret.chmod(0o600)
    broker_user, client_user = broker_proof._proof_users()
    assert broker_user == f"{os.geteuid()}:{os.getegid()}"
    assert client_user == f"1000:{os.getegid()}"
    broker_proof._share_disposable_client_mounts(repo, editable, prompt)
    assert stat.S_IMODE(repo.stat().st_mode) == 0o770
    assert stat.S_IMODE(editable.stat().st_mode) == 0o660
    assert stat.S_IMODE(prompt.stat().st_mode) == 0o640
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    commands: list[tuple[str, ...]] = []

    def capture(*args: str) -> str:
        commands.append(args)
        return ""

    monkeypatch.setattr(
        "runner.codex_broker_proof.egress._docker",
        capture,
    )
    broker_proof._start_broker("broker", "net", "172.16.0.2", tmp_path, secret, IMAGE, broker_user)
    broker_proof._start_client(
        "client", "net", IMAGE, "172.16.0.3", "172.16.0.2", repo, prompt, prompt, client_user
    )
    broker_command, client_command = commands
    assert f"--user={broker_user}" in broker_command
    assert f"--user={client_user}" in client_command
    assert any("dst=/broker-secret,readonly" in item for item in broker_command)
    client_mounts = [item for item in client_command if item.startswith("type=bind,")]
    assert len(client_mounts) == 3
    assert not any(
        "dst=/broker-secret" in item or "runner-spool" in item or "source-snapshots" in item
        for item in client_mounts
    )
