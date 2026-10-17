"""A separate release owner verifies exact approvals, packages and rollback."""

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from onefoundry.extension_activation_contract import ExtensionActivationJob
from runner.extension_release_supervisor import ReleaseSupervisorConfig, process_activation
from scripts.release_bundle import image_refs


def fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, ReleaseSupervisorConfig, dict[str, Any]]:
    spool, home, archives = tmp_path / "spool", tmp_path / "home", tmp_path / "archives"
    for path in (spool, home, archives):
        path.mkdir()
    for name in ("jobs", "claims", "receipts", "artifacts"):
        (spool / name).mkdir()
    (home / "data/updates").mkdir(parents=True)
    old, new = home / "old", home / "new"
    old.mkdir()
    new.mkdir()
    state: dict[str, Any] = {"current": old, "updates": 0, "rollbacks": 0}
    release = {
        "coding_run_id": str(uuid4()),
        "qa_job_id": str(uuid4()),
        "version": "1.1.0",
        "archive_sha256": "a" * 64,
        "app_image": "sha256:" + "b" * 64,
        "expected_installed_version": "1.0.0",
        "expected_installed_image": "sha256:" + "c" * 64,
        "configuration_effects": [],
        "migration_effects": [],
        "rollback_plan": "Restore the exact previous release and preserved data checkpoint.",
    }
    approval_id = str(uuid4())
    job = ExtensionActivationJob.model_validate(
        {
            "job_id": approval_id,
            "project_id": str(uuid4()),
            "approval_id": approval_id,
            "target_approval_id": str(uuid4()),
            "proposal_digest": "d" * 64,
            "candidate_commit": "e" * 40,
            "diff_sha256": "f" * 64,
            "qa_receipt_sha256": "1" * 64,
            "release": release,
            "original_work_order_id": str(uuid4()),
            "operation": "repository_read",
            "gap_source_id": None,
        }
    )
    # Correct job and inbox identity are the same exact approval cause.
    job.job_id = job.approval_id
    path = spool / "jobs" / f"{job.job_id}.json"
    path.write_text(job.model_dump_json())
    (archives / f"{job.release.archive_sha256}.tar.gz").write_bytes(b"fixture archive")

    def manifest(current: Path) -> dict[str, Any]:
        version, image = (
            ("1.0.0", job.release.expected_installed_image)
            if current == old
            else ("1.1.0", job.release.app_image)
        )
        return {
            "version": version,
            "commit": job.candidate_commit,
            "image_ids": {image_refs(version)["runtime"]: image},
        }

    def update(_args: Any) -> None:
        state["updates"] += 1
        state["current"] = new
        (home / "data/updates/rollback.json").write_text(
            json.dumps({"state": "applied", "previous": "1.0.0"})
        )

    def rollback(_args: Any) -> None:
        state["rollbacks"] += 1
        state["current"] = old

    monkeypatch.setattr(
        "runner.extension_release_supervisor.installer.read_install", lambda _home: (state["current"], {})
    )
    monkeypatch.setattr("runner.extension_release_supervisor.validate_manifest", manifest)
    monkeypatch.setattr("runner.extension_release_supervisor.installer.unpack", lambda *_args: manifest(new))
    monkeypatch.setattr("runner.extension_release_supervisor.installer.update", update)
    monkeypatch.setattr("runner.extension_release_supervisor.installer.rollback", rollback)
    return path, spool, ReleaseSupervisorConfig(home, archives, 30), state


def test_exact_release_activation_is_one_shot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path, spool, config, state = fixture(tmp_path, monkeypatch)
    receipt = process_activation(
        path,
        spool,
        config,
        capability_readback=lambda _: True,
        quiescent=lambda: True,
        approval_current=lambda _: True,
    )
    assert receipt is not None and receipt["status"] == "activated" and receipt["checkpoint_saved"] is True
    assert (
        process_activation(
            path,
            spool,
            config,
            capability_readback=lambda _: True,
            quiescent=lambda: True,
            approval_current=lambda _: True,
        )
        is None
    )
    assert state["updates"] == 1


@pytest.mark.parametrize("approved,idle", [(False, True), (True, False)])
def test_rejection_and_active_work_are_not_claimed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, approved: bool, idle: bool
) -> None:
    path, spool, config, state = fixture(tmp_path, monkeypatch)
    assert (
        process_activation(
            path,
            spool,
            config,
            capability_readback=lambda _: True,
            quiescent=lambda: idle,
            approval_current=lambda _: approved,
        )
        is None
    )
    assert state["updates"] == 0 and not list((spool / "claims").iterdir())


def test_owner_failure_restores_and_reports_actual_previous_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, spool, config, state = fixture(tmp_path, monkeypatch)

    def unavailable(_job: object) -> bool:
        raise OSError("owner unavailable")

    receipt = process_activation(
        path,
        spool,
        config,
        capability_readback=unavailable,
        quiescent=lambda: True,
        approval_current=lambda _: True,
    )
    assert receipt is not None and receipt["status"] == "failed" and receipt["installed_version"] == "1.0.0"
    assert state["updates"] == 1 and state["rollbacks"] == 1


def test_stale_installed_base_denies_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path, spool, config, state = fixture(tmp_path, monkeypatch)
    value = json.loads(path.read_text())
    value["release"]["expected_installed_version"] = "0.9.0"
    path.write_text(json.dumps(value))
    receipt = process_activation(
        path,
        spool,
        config,
        capability_readback=lambda _: True,
        quiescent=lambda: True,
        approval_current=lambda _: True,
    )
    assert receipt is not None and receipt["status"] == "failed" and state["updates"] == 0


def test_maintenance_refusal_does_not_claim_or_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, spool, config, state = fixture(tmp_path, monkeypatch)
    assert (
        process_activation(
            path,
            spool,
            config,
            capability_readback=lambda _: True,
            quiescent=lambda: True,
            approval_current=lambda _: True,
            acquire_maintenance=lambda _: False,
        )
        is None
    )
    assert not list((spool / "claims").iterdir()) and state["updates"] == 0


def test_approved_grant_failure_restores_before_releasing_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, spool, config, state = fixture(tmp_path, monkeypatch)
    value = json.loads(path.read_text())
    value["tool_grants"] = [{"profile_id": str(uuid4()), "tool_id": "fixture_tool_v1", "tools_before": []}]
    path.write_text(json.dumps(value))
    released: list[object] = []

    def release(job: object) -> bool:
        assert state["current"] == config.install_home / "old"
        released.append(job)
        return True

    receipt = process_activation(
        path,
        spool,
        config,
        capability_readback=lambda _: True,
        quiescent=lambda: True,
        approval_current=lambda _: True,
        acquire_maintenance=lambda _: True,
        configure_role_grants=lambda _: False,
        release_maintenance=release,
    )
    assert receipt is not None and receipt["status"] == "failed" and state["rollbacks"] == 1
    assert len(released) == 1
    assert (
        process_activation(
            path,
            spool,
            config,
            capability_readback=lambda _: True,
            quiescent=lambda: True,
            approval_current=lambda _: True,
            release_maintenance=release,
        )
        is None
    )
    assert state["updates"] == 1


@pytest.mark.parametrize("ready", [True, False])
def test_managed_source_tracks_activation_and_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ready: bool
) -> None:
    path, spool, config, state = fixture(tmp_path, monkeypatch)
    moved: list[str] = []

    class Source:
        def advance(self) -> None:
            moved.append("advance")

        def restore(self) -> None:
            moved.append("restore")

    monkeypatch.setattr("runner.extension_release_supervisor.prepare_source", lambda *_: Source())
    config = ReleaseSupervisorConfig(config.install_home, config.archive_root, 30, tmp_path, "managed")
    receipt = process_activation(
        path,
        spool,
        config,
        capability_readback=lambda _: ready,
        quiescent=lambda: True,
        approval_current=lambda _: True,
    )
    assert receipt is not None
    assert receipt["status"] == ("activated" if ready else "failed")
    assert moved == (["advance"] if ready else ["advance", "restore"])
    assert state["rollbacks"] == (0 if ready else 1)


def test_online_activation_handles_large_approval_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from io import BytesIO

    from onefoundry.repository_code_contract import digest
    from runner import extension_release_supervisor as owner

    path, spool, config, state = fixture(tmp_path, monkeypatch)
    job = ExtensionActivationJob.model_validate_json(path.read_text())
    content = job.model_dump(
        mode="json",
        exclude={"schema_version", "kind", "job_id", "project_id", "approval_id", "proposal_digest"},
    )
    content["kind"] = "extension_activation_proposal_v1"
    body = json.dumps(content)
    job.proposal_digest = digest(
        {"action_type": "deploy", "target": "Install exact candidate", "content": body}
    )
    path.write_text(job.model_dump_json())
    approved = {
        "id": str(job.approval_id),
        "project_id": str(job.project_id),
        "status": "approved",
        "action_type": "deploy",
        "target": "Install exact candidate",
        "content": body,
        "proposal_digest": job.proposal_digest,
    }
    history = [{"id": str(uuid4()), "content": "earlier approved work " * 200}] * 40 + [approved]
    assert len(json.dumps(history)) > 65536

    class Opener:
        def open(self, request: Any, **_kwargs: Any) -> BytesIO:
            url = request if isinstance(request, str) else request.full_url
            if "approvals?" in url:
                return BytesIO(json.dumps(history).encode())
            if url.endswith("preflight"):
                return BytesIO(b'{"quiescent":true}')
            if url.endswith("quiesce"):
                return BytesIO(b'{"acquired":true}')
            if url.endswith("release"):
                return BytesIO(b'{"released":true}')
            return BytesIO(
                json.dumps(
                    {
                        "capability_readiness": {
                            "checks": [{"operation": job.operation, "source_id": None, "status": "ready"}]
                        }
                    }
                ).encode()
            )

    monkeypatch.setattr(owner, "build_opener", lambda *_args: Opener())
    receipt = owner.process_online_activation(path, spool, config, "http://127.0.0.1:8765")
    assert receipt is not None and receipt["status"] == "activated"
    assert state["updates"] == 1


@pytest.mark.parametrize(
    "payload",
    [b"{}", b"[null]", b"[{},]", b"[{} {}]", b"[{}]x", b'[{"bad":}]', b'[{"x":"\xff"}]', b'[{"x":[}]', b"[{"],
)
def test_approval_history_rejects_malformed_input(payload: bytes) -> None:
    from io import BytesIO

    from runner.extension_approval_readback import matches_approval

    with pytest.raises((ValueError, UnicodeDecodeError)):
        matches_approval(BytesIO(payload).read, lambda _: True, record_limit=100, remaining=lambda: 1.0)


def test_approval_history_bounds_each_record_and_preserves_false() -> None:
    from io import BytesIO

    from runner.extension_approval_readback import matches_approval

    def check(payload: bytes, limit: int) -> bool:
        return matches_approval(
            BytesIO(payload).read,
            lambda row: row.get("id") == "selected",
            record_limit=limit,
            remaining=lambda: 1.0,
        )

    record = b'{"id":"selected"}'
    assert check(b"[" + record + b"]", len(record))
    assert not check(b"[]", 1)
    assert not check(b'[{"id":"another"}]', 100)
    with pytest.raises(ValueError, match="exceeds_bound"):
        check(b"[" + record + b"]", len(record) - 1)
    with pytest.raises(ValueError, match="exceeds_bound"):
        check(b"[" + record + b',{"unrelated":"' + b"x" * 100 + b'"}]', 50)
    with pytest.raises(ValueError, match="limit_invalid"):
        check(b"[]", 0)


def test_approval_history_handles_fragmented_unicode_and_nested_strings() -> None:
    from io import BytesIO

    from runner.extension_approval_readback import matches_approval

    record = {"id": "selected", "details": [{"text": 'é closing } and escaped " quote'}]}
    stream = BytesIO(json.dumps([record], ensure_ascii=False).encode())
    assert matches_approval(
        lambda _: stream.read(1), lambda value: value == record, record_limit=500, remaining=lambda: 1.0
    )


def test_approval_history_preserves_dependency_failure_and_deadline() -> None:
    from io import BytesIO

    from runner.extension_approval_readback import matches_approval

    failure = OSError("synthetic dependency failure")

    def read(_limit: int) -> bytes:
        raise failure

    with pytest.raises(OSError) as observed:
        matches_approval(read, lambda _: True, record_limit=100, remaining=lambda: 1.0)
    assert observed.value is failure

    def expired() -> float:
        raise TimeoutError("wall_time_exceeded")

    stream = BytesIO(b"[]")
    with pytest.raises(TimeoutError):
        matches_approval(stream.read, lambda _: True, record_limit=100, remaining=expired)
    assert stream.tell() == 0
