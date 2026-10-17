"""Release/source parity, exact reconstruction, dirty-checkout refusal and restore."""

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from onefoundry.extension_activation_contract import ExtensionActivationJob, ExtensionRelease
from onefoundry.extension_qa_contract import ExtensionQaJob, ExtensionQaReceipt
from onefoundry.external_code_schemas import ExternalCodeTest
from onefoundry.repository_code_contract import digest
from runner.extension_source_checkout import SOURCE_EFFECT, prepare
from runner.fixture_git import Deadline, _git
from runner.repository_code_stage import candidate, stage
from tests.test_repository_code import job_at


def source_fixture(tmp_path: Path) -> tuple[ExtensionActivationJob, Path, Path]:
    engineer, root, spool = job_at(tmp_path)
    staged = tmp_path / "staged"
    _, baseline = stage(engineer, root, staged)
    (staged / "calculator.py").write_text("def add(a, b):\n    return a+b\n")
    commit, diff = candidate(engineer, staged, baseline, Deadline(30))
    value = dict(
        schema_version=1,
        kind="extension_qa_v1",
        job_id=str(uuid4()),
        engineer_job=engineer.model_dump(mode="json"),
        baseline_commit=baseline,
        candidate_commit=commit,
        diff_sha256=hashlib.sha256(diff).hexdigest(),
        diff_byte_size=len(diff),
    )
    qa = ExtensionQaJob.model_validate(value | {"request_sha256": digest(value)})
    (spool / "jobs" / f"{qa.job_id}.json").write_text(qa.model_dump_json())
    receipt = ExtensionQaReceipt(
        job_id=qa.job_id,
        project_id=engineer.project_id,
        request_sha256=qa.request_sha256,
        candidate_commit=commit,
        diff_sha256=qa.diff_sha256,
        test_image=engineer.target.test_image,
        status="passed",
        reason="completed",
        tests=[ExternalCodeTest(name="candidate", status="passed", exit_code=0)],
        observed_at=datetime.now(UTC),
    )
    raw = receipt.model_dump_json().encode()
    (spool / "receipts" / f"{qa.job_id}.json").write_bytes(raw)
    artifacts = spool / "artifacts" / str(engineer.attempt_id)
    artifacts.mkdir()
    (artifacts / "change.diff").write_bytes(diff)
    approval = uuid4()
    release = ExtensionRelease(
        coding_run_id=engineer.run_id,
        qa_job_id=qa.job_id,
        version="1.1.0",
        archive_sha256="a" * 64,
        app_image="sha256:" + "b" * 64,
        expected_installed_version="1.0.0",
        expected_installed_image="sha256:" + "c" * 64,
        configuration_effects=[SOURCE_EFFECT],
        migration_effects=[],
        rollback_plan="Restore the previous release, data and source checkpoint.",
    )
    job = ExtensionActivationJob(
        job_id=approval,
        approval_id=approval,
        project_id=engineer.project_id,
        target_approval_id=engineer.target_approval_id,
        proposal_digest="d" * 64,
        candidate_commit=commit,
        diff_sha256=qa.diff_sha256,
        qa_receipt_sha256=hashlib.sha256(raw).hexdigest(),
        release=release,
        original_work_order_id=engineer.work_order_id,
        operation="repository_tree_v1",
        gap_source_id=None,
    )
    return job, root, spool


def test_candidate_advances_only_clean_managed_clone_and_restores(tmp_path: Path) -> None:
    job, root, spool = source_fixture(tmp_path)
    source = prepare(job, spool, root, "repo")
    assert _git(source.path, Deadline(10), "rev-parse", "HEAD").decode() == source.previous
    source.advance()
    assert _git(source.path, Deadline(10), "rev-parse", "HEAD").decode() == job.candidate_commit
    assert (source.path / "calculator.py").read_text() == "def add(a, b):\n    return a+b\n"
    source.restore()
    source.restore()
    assert _git(source.path, Deadline(10), "rev-parse", "HEAD").decode() == source.previous


@pytest.mark.parametrize("fault", ["dirty", "untracked", "alias", "effect", "qa", "candidate"])
def test_source_preflight_refuses_changed_or_unapproved_inputs(tmp_path: Path, fault: str) -> None:
    job, root, spool = source_fixture(tmp_path)
    alias = "repo"
    if fault == "dirty":
        (root / "repo/calculator.py").write_text("operator edits")
    if fault == "untracked":
        (root / "repo/new.txt").write_text("operator file")
    if fault == "alias":
        alias = "other"
    if fault == "effect":
        job.release.configuration_effects = []
    if fault == "qa":
        job.qa_receipt_sha256 = "f" * 64
    if fault == "candidate":
        job.candidate_commit = "f" * 40
    before = _git(root / "repo", Deadline(10), "rev-parse", "HEAD")
    with pytest.raises(ValueError):
        prepare(job, spool, root, alias)
    assert _git(root / "repo", Deadline(10), "rev-parse", "HEAD") == before


def test_race_and_rollback_never_discard_new_operator_changes(tmp_path: Path) -> None:
    job, root, spool = source_fixture(tmp_path)
    source = prepare(job, spool, root, "repo")
    (source.path / "new.txt").write_text("operator file")
    with pytest.raises(ValueError, match="source_changed"):
        source.advance()
    (source.path / "new.txt").unlink()
    source.advance()
    (source.path / "calculator.py").write_text("operator edits")
    with pytest.raises(ValueError, match="source_changed"):
        source.restore()
    assert (source.path / "calculator.py").read_text() == "operator edits"


def test_managed_source_setting_is_explicit_validated_and_has_no_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pydantic import ValidationError

    from runner.extension_release_prepare import ReleasePreparationSettings

    values = dict(
        install_home=tmp_path / "home",
        archive_root=tmp_path / "archives",
        source_root=tmp_path / "sources",
        build_timeout_seconds=300,
    )
    monkeypatch.setenv("MANAGED_SOURCE_ALIAS", "untrusted-environment")
    assert ReleasePreparationSettings.model_validate(values).managed_source_alias is None
    assert (
        ReleasePreparationSettings.model_validate(
            values | {"managed_source_alias": "onefoundry"}
        ).managed_source_alias
        == "onefoundry"
    )
    for invalid in ("../outside", "", "/absolute", "bad space"):
        with pytest.raises(ValidationError):
            ReleasePreparationSettings.model_validate(values | {"managed_source_alias": invalid})
