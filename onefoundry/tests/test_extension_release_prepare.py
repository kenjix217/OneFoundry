"""Packaging preserves candidate identity and never implies installation."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from onefoundry.extension_activation_contract import ExtensionReleaseRecord
from onefoundry.extension_release_preparation_contract import PrepareRelease, ReleasePreparationJob
from onefoundry.repository_code_contract import digest
from onefoundry.runner_spool import RunnerSpool
from runner.extension_qa_service import process_qa
from runner.extension_release_prepare import ReleasePreparationSettings, process_preparation
from runner.extension_release_record import _publish_record
from runner.repository_code_service import process_job
from scripts.release_bundle import image_refs
from tests.test_repository_code import fake_provider, fake_test, job_at, qa_job_for


def fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, ReleasePreparationSettings, dict[str, Any]]:
    engineer, root, spool = job_at(tmp_path)
    receipt = process_job(
        spool / "jobs" / f"{engineer.job_id}.json", spool, root, fake_provider, test_runner=fake_test
    )
    assert receipt is not None
    qa = qa_job_for(engineer, receipt, spool)
    assert process_qa(spool / "jobs" / f"{qa.job_id}.json", spool, root, test_runner=fake_test)
    observed = RunnerSpool(str(spool)).read_receipt(qa.job_id)
    assert observed is not None
    request = PrepareRelease(
        coding_run_id=engineer.run_id,
        qa_job_id=qa.job_id,
        configuration_effects=[],
        migration_effects=[],
        rollback_plan="Restore the previous installed release and its complete data checkpoint.",
    )
    value = {
        "kind": "extension_release_prepare_v1",
        "schema_version": 1,
        "job_id": str(uuid4()),
        "project_id": str(engineer.project_id),
        "session_id": str(uuid4()),
        "work_order_id": str(uuid4()),
        "qa_job": qa.model_dump(mode="json"),
        "qa_receipt_sha256": observed[1],
        "request": request.model_dump(mode="json"),
    }
    job = ReleasePreparationJob.model_validate({**value, "request_sha256": digest(value)})
    path = spool / "jobs" / f"{job.job_id}.json"
    path.write_text(job.model_dump_json())
    install, archive_root = tmp_path / "install", tmp_path / "archives"
    install.mkdir()
    archive_root.mkdir()
    config = ReleasePreparationSettings(
        install_home=install, archive_root=archive_root, source_root=root, build_timeout_seconds=60
    )
    old = {"version": "1.0.0", "image_ids": {image_refs("1.0.0")["runtime"]: "sha256:" + "a" * 64}}
    new = {
        "version": "1.1.0",
        "commit": receipt["commit"],
        "image_ids": {image_refs("1.1.0")["runtime"]: "sha256:" + "b" * 64},
    }
    monkeypatch.setattr("scripts.onefoundry_update.read_install", lambda _: (install, "fixture"))
    monkeypatch.setattr("scripts.release_bundle.validate_manifest", lambda _: old)
    monkeypatch.setattr("scripts.onefoundry_update.unpack", lambda *_: new)
    monkeypatch.setattr("runner.extension_release_record.unpack", lambda *_: new)
    monkeypatch.setattr("scripts.release_bundle.source_version", lambda _: "1.1.0")
    return path, spool, config, {"builds": 0, "candidate": receipt["commit"], "qa": qa}


def test_builds_exact_candidate_once_and_records_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, spool, config, state = fixture(tmp_path, monkeypatch)

    def build(output: Path, **kwargs: Any) -> Path:
        state["builds"] += 1
        assert kwargs["revision"] == state["candidate"]
        assert kwargs["repository"].is_dir() and kwargs["timeout_seconds"] == 60
        output.mkdir()
        archive = output / "release.tar.gz"
        archive.write_bytes(b"bounded fixture package")
        return archive

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(lambda _: process_preparation(path, spool, config, builder=build), range(2))
        )
    assert state["builds"] == 1
    saved = next(value for value in results if value is not None)
    assert saved["status"] == "prepared", saved
    assert saved["record"]["candidate_commit"] == state["candidate"]
    assert (config.archive_root / (saved["record"]["release"]["archive_sha256"] + ".tar.gz")).is_file()
    assert process_preparation(path, spool, config, builder=build) is None


def test_interrupted_preparation_records_unknown_without_rebuilding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, spool, config, _state = fixture(tmp_path, monkeypatch)
    (spool / "claims" / f"{path.stem}.claim").write_text("previous interrupted worker")

    def forbidden(*args: Any, **kwargs: Any) -> Path:
        pytest.fail("an interrupted preparation must not launch another build")

    result = process_preparation(path, spool, config, builder=forbidden)
    assert result and result["status"] == "unknown"
    assert process_preparation(path, spool, config, builder=forbidden) is None


def test_fresh_repackage_preserves_each_verified_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, spool, config, state = fixture(tmp_path, monkeypatch)

    def build(output: Path, **kwargs: Any) -> Path:
        state["builds"] += 1
        output.mkdir()
        archive = output / "release.tar.gz"
        archive.write_bytes(f"verified package {state['builds']}".encode())
        return archive

    first = process_preparation(path, spool, config, builder=build)
    assert first is not None and first["status"] == "prepared"
    records = spool / "artifacts" / str(state["qa"].engineer_job.attempt_id)
    # Reproduce a legacy preparation with only the canonical record present.
    for item in records.glob("release-*.json"):
        item.unlink()
    prior_receipt = (spool / "receipts" / path.name).read_bytes()
    value = json.loads(path.read_text())
    value["job_id"] = str(uuid4())
    value["request"]["configuration_effects"] = ["Normalize committed source directory permissions."]
    value.pop("request_sha256")
    value["request_sha256"] = digest(value)
    replacement = spool / "jobs" / f"{value['job_id']}.json"
    replacement.write_text(json.dumps(value))
    second = process_preparation(replacement, spool, config, builder=build)
    assert second is not None and second["status"] == "prepared"
    assert (spool / "receipts" / path.name).read_bytes() == prior_receipt
    immutable = [json.loads(item.read_text()) for item in records.glob("release-*.json")]
    assert first["record"] in immutable and second["record"] in immutable
    assert json.loads((records / "release.json").read_text()) == second["record"]
    assert process_preparation(replacement, spool, config, builder=build) is None
    assert state["builds"] == 2


def test_release_selection_is_atomic_and_rejects_other_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, spool, config, state = fixture(tmp_path, monkeypatch)

    def build(output: Path, **kwargs: Any) -> Path:
        output.mkdir()
        archive = output / "release.tar.gz"
        archive.write_bytes(b"concurrent record fixture")
        return archive

    observed = process_preparation(path, spool, config, builder=build)
    assert observed is not None and observed["status"] == "prepared"
    record = ExtensionReleaseRecord.model_validate(observed["record"])
    selection = spool / "artifacts" / str(state["qa"].engineer_job.attempt_id) / "release.json"
    before = selection.read_bytes()
    changed = record.model_copy(update={"candidate_commit": "f" * 40})
    with pytest.raises(ValueError, match="candidate_changed"):
        _publish_record(selection, changed)
    assert selection.read_bytes() == before
    versions = [
        record.model_copy(
            update={"release": record.release.model_copy(update={"configuration_effects": [label]})}
        )
        for label in ("First verified package", "Second verified package")
    ]
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda item: _publish_record(selection, item), versions))
    assert ExtensionReleaseRecord.model_validate_json(selection.read_bytes()) in versions
    immutable = [
        ExtensionReleaseRecord.model_validate_json(item.read_bytes())
        for item in selection.parent.glob("release-*.json")
    ]
    assert all(item in immutable for item in versions)


@pytest.mark.parametrize("failure", ["qa", "diff", "timeout", "dependency"])
def test_failed_or_unknown_packaging_is_not_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    path, spool, config, state = fixture(tmp_path, monkeypatch)
    if failure == "qa":
        (spool / "receipts" / f"{state['qa'].job_id}.json").write_text("{}")
    if failure == "diff":
        (spool / "artifacts" / str(state["qa"].engineer_job.job_id) / "change.diff").write_text("changed")

    def build(*args: Any, **kwargs: Any) -> Path:
        state["builds"] += 1
        if failure == "timeout":
            raise TimeoutError
        raise OSError("build dependency unavailable")

    receipt = process_preparation(path, spool, config, builder=build)
    assert receipt is not None and receipt["status"] == ("unknown" if failure == "timeout" else "failed")
    assert receipt["record"] is None
    assert process_preparation(path, spool, config, builder=build) is None


def test_job_identity_and_storage_fail_before_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path, spool, config, _ = fixture(tmp_path, monkeypatch)
    value = json.loads(path.read_text())
    value["project_id"] = str(uuid4())
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        process_preparation(path, spool, config)
    assert not (spool / "claims" / (path.stem + ".claim")).exists()
