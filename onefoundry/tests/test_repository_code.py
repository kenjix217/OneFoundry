"""Exact-source maintenance, offline Docker acceptance and failed owner contracts."""

import json
import os
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from onefoundry.extension_qa_contract import ExtensionQaJob
from onefoundry.repository_code_contract import (
    RepositoryCodeManifest,
    RepositoryCodePolicy,
    RepositoryCodeTarget,
    digest,
)
from onefoundry.settings import Settings
from runner.extension_qa_service import process_qa
from runner.external_cli import CliResult
from runner.fixture_git import Deadline
from runner.local_source_stage import StageError
from runner.repository_code_offline_bridge import OfflineRepositoryBridge
from runner.repository_code_service import process_job
from runner.repository_code_stage import candidate, stage


def test_reasoning_is_bound_without_changing_legacy_job_identity(tmp_path: Path) -> None:
    job, _root, _spool = job_at(tmp_path)
    legacy = job.model_dump(mode="json")
    assert "reasoning_effort" not in legacy["target"]
    assert RepositoryCodeManifest.model_validate(legacy) == job
    legacy["target"]["reasoning_effort"] = "xhigh"
    with pytest.raises(ValidationError, match="identity"):
        RepositoryCodeManifest.model_validate(legacy)
    legacy["request_sha256"] = digest({k: v for k, v in legacy.items() if k != "request_sha256"})
    assert RepositoryCodeManifest.model_validate(legacy).target.reasoning_effort == "xhigh"
    legacy["target"]["reasoning_effort"] = "invalid setting"
    with pytest.raises(ValidationError):
        RepositoryCodeManifest.model_validate(legacy)


def job_at(tmp_path: Path, *, image: str = "sha256:" + "a" * 64) -> tuple[RepositoryCodeManifest, Path, Path]:
    root = tmp_path / "sources"
    repo = root / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "calculator.py").write_text("def add(a, b):\n    return a-b\n")
    (repo / "tests" / "test_calculator.py").write_text(
        "import unittest\nfrom calculator import add\nclass TestAdd(unittest.TestCase):"
        "\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
    )
    for args in (
        ("init", "-q", "-b", "main"),
        ("add", "."),
        ("-c", "user.name=Fixture", "-c", "user.email=fixture@invalid.example", "commit", "-qm", "before"),
    ):
        subprocess.run(  # noqa: S603 - fixed disposable fixture commands
            ("/usr/bin/git", *args),  # noqa: S603 - fixed disposable fixture commands
            cwd=repo,
            check=True,
            capture_output=True,
        )
    revision = subprocess.check_output(("/usr/bin/git", "rev-parse", "HEAD"), cwd=repo).decode().strip()
    source = uuid4()
    target = RepositoryCodeTarget(
        source_id=source,
        provider_url="https://provider.test/v1/responses",
        task_scope_id=uuid4(),
        adapter_id="codex_cli",
        revision=revision,
        allowed_paths=["calculator.py"],
        test_image=image,
        test_commands=[["python", "-m", "unittest", "discover", "-s", "tests"]],
        model_id="fixture-model",
        execution_policy=Settings(db_password="unit-only").repository_code_policy,
        output_limits={"max_log_bytes": 65536, "max_diff_bytes": 32768},
    )
    attempt = uuid4()
    value = {
        "schema_version": 4,
        "target_id": "repository_code_v1",
        "job_id": str(attempt),
        "project_id": str(uuid4()),
        "run_id": str(uuid4()),
        "attempt_id": str(attempt),
        "work_order_id": str(uuid4()),
        "actor_id": "operator",
        "adapter_id": "codex_cli",
        "model_id": "fixture-model",
        "task_title": "Fix addition",
        "task_instructions": "Make addition work.",
        "target_approval_id": str(uuid4()),
        "target": target.model_dump(mode="json"),
        "source": {
            "source_id": str(source),
            "root_id": "workspace",
            "relative_path": "repo",
            "branch": "main",
            "revision": revision,
        },
        "idempotency_key": str(uuid4()),
        "intent_sha256": "c" * 64,
        "limits": {"wall_seconds": 60, "max_log_bytes": 65536, "max_diff_bytes": 32768},
        "stage_limits": Settings(db_password="unit-only").repository_code_policy.model_dump(),
    }
    job = RepositoryCodeManifest.model_validate({**value, "request_sha256": digest(value)})
    spool = tmp_path / "spool"
    spool.mkdir()
    for name in ("jobs", "claims", "receipts", "artifacts"):
        (spool / name).mkdir()
    (spool / "jobs" / f"{attempt}.json").write_text(job.model_dump_json())
    return job, root, spool


def fake_test(
    repo: Path, deadline: Deadline, limit: int, name: str, **kwargs: object
) -> tuple[dict[str, object], bytes]:
    code = 0 if "return a+b" in (repo / "calculator.py").read_text() else 1
    return {
        "name": name,
        "status": "passed" if code == 0 else "failed",
        "exit_code": code,
    }, b"bounded observed fixture test"


def fake_provider(job: RepositoryCodeManifest, repo: Path, deadline: Deadline) -> CliResult:
    (repo / "calculator.py").write_text("def add(a, b):\n    return a+b\n")
    return CliResult("done", "completed", "offline-session", 12, None, b"offline fixture\n")


def test_disposable_pinned_tree_and_candidate(tmp_path: Path) -> None:
    job, root, spool = job_at(tmp_path)
    (root / "repo" / "calculator.py").write_text("uncommitted operator changes")
    receipt = process_job(
        spool / "jobs" / f"{job.attempt_id}.json", spool, root, fake_provider, test_runner=fake_test
    )
    assert receipt is not None and receipt["status"] == "done"
    assert receipt["source"]["revision"] == job.target.revision
    assert receipt["tests"] == [
        {"name": "baseline_0", "status": "failed", "exit_code": 1},
        {"name": "candidate_0", "status": "passed", "exit_code": 0},
    ]
    assert (root / "repo" / "calculator.py").read_text() == "uncommitted operator changes"
    assert (
        process_job(
            spool / "jobs" / f"{job.attempt_id}.json", spool, root, fake_provider, test_runner=fake_test
        )
        is None
    )


def test_source_drift_and_path_denials(tmp_path: Path) -> None:
    job, root, _ = job_at(tmp_path)
    changed = job.model_dump(mode="json", exclude={"request_sha256"})
    changed["source"]["relative_path"] = "../other"
    with pytest.raises(ValueError):
        RepositoryCodeManifest.model_validate({**changed, "request_sha256": digest(changed)})
    subprocess.run(("/usr/bin/git", "checkout", "-qb", "different"), cwd=root / "repo", check=True)
    with pytest.raises(StageError, match="source_branch_drift"):
        stage(job, root, tmp_path / "disposable")


def test_candidate_scope_denied(tmp_path: Path) -> None:
    job, root, _ = job_at(tmp_path)
    repo = tmp_path / "disposable"
    _, baseline = stage(job, root, repo)
    (repo / "unexpected.py").write_text("new code")
    with pytest.raises(StageError, match="outside_scope"):
        candidate(job, repo, baseline, Deadline(10))


@pytest.mark.parametrize("deadline_expired", [False, True])
def test_provider_unknown_preserved_and_never_replayed(tmp_path: Path, deadline_expired: bool) -> None:
    job, root, spool = job_at(tmp_path)

    def uncertain(*args: object) -> CliResult:
        if deadline_expired:
            from runner.fixture_git import ExecutionError

            raise ExecutionError("maintenance_cli_timeout")
        raise TimeoutError("unknown")

    receipt = process_job(
        spool / "jobs" / f"{job.attempt_id}.json", spool, root, uncertain, test_runner=fake_test
    )
    assert receipt is not None and receipt["status"] == "unknown" and receipt["commit"] is None
    if deadline_expired:
        assert receipt["reason_code"] == "maintenance_cli_timeout"
    assert (
        process_job(
            spool / "jobs" / f"{job.attempt_id}.json", spool, root, fake_provider, test_runner=fake_test
        )
        is None
    )


def test_manifest_tampering_unclaimed(tmp_path: Path) -> None:
    job, root, spool = job_at(tmp_path)
    path = spool / "jobs" / f"{job.attempt_id}.json"
    value = json.loads(path.read_text())
    value["task_instructions"] = "different"
    path.write_text(json.dumps(value))
    with pytest.raises(ValidationError):
        process_job(path, spool, root, fake_provider, test_runner=fake_test)
    assert not list((spool / "claims").iterdir())


def test_failed_provider_metadata_persists_without_candidate_or_raw_output(tmp_path: Path) -> None:
    from tests.test_repository_codex_observation import events

    job, root, spool = job_at(tmp_path)

    def failed(*args: object) -> CliResult:
        return CliResult(
            "unknown",
            "maintenance_provider_turn_failed",
            "failed-session",
            None,
            None,
            events(exit_code=1) + b'\n{"private":"do not retain"}',
        )

    receipt = process_job(
        spool / "jobs" / f"{job.attempt_id}.json", spool, root, failed, test_runner=fake_test
    )
    assert receipt is not None and receipt["status"] == "unknown" and not receipt["artifacts"]
    saved = (spool / "claims" / f"{job.attempt_id}.repository-session.json").read_text()
    assert "do not retain" not in saved
    assert json.loads(saved)["diagnostics"][0]["exit_code"] == 1


def test_passing_baseline_never_launches_provider(tmp_path: Path) -> None:
    job, root, spool = job_at(tmp_path)

    def passing(*args: object, **kwargs: object) -> tuple[dict[str, object], bytes]:
        return {"name": "before", "status": "passed", "exit_code": 0}, b"passed"

    def forbidden(*args: object) -> CliResult:
        pytest.fail("a passing baseline cannot authorize coding")

    receipt = process_job(
        spool / "jobs" / f"{job.attempt_id}.json", spool, root, forbidden, test_runner=passing
    )
    assert receipt is not None and receipt["status"] == "failed"
    assert receipt["reason_code"] == "maintenance_gap_not_reproduced"
    assert not (spool / "claims" / f"{job.attempt_id}.repository-launch.json").exists()


def test_protected_context_failure_never_launches_provider(tmp_path: Path) -> None:
    job, root, spool = job_at(tmp_path)

    class RejectedBridge:
        def validate_context(self, *args: object) -> None:
            raise StageError("maintenance_edit_scope_protected_source")

        def __call__(self, *args: object) -> CliResult:
            pytest.fail("protected context must fail before provider effects")

    receipt = process_job(
        spool / "jobs" / f"{job.attempt_id}.json", spool, root, RejectedBridge(), test_runner=fake_test
    )
    assert receipt is not None and receipt["status"] == "failed"
    assert receipt["reason_code"] == "maintenance_edit_scope_protected_source"
    assert receipt["provider_session_id"] is None
    assert not (spool / "claims" / f"{job.attempt_id}.repository-launch.json").exists()


def qa_job_for(job: RepositoryCodeManifest, receipt: dict[str, Any], spool: Path) -> ExtensionQaJob:
    diff = next(item for item in receipt["artifacts"] if item["kind"] == "diff")
    unsigned = {
        "schema_version": 1,
        "kind": "extension_qa_v1",
        "job_id": str(uuid4()),
        "engineer_job": job.model_dump(mode="json"),
        "baseline_commit": receipt["baseline_commit"],
        "candidate_commit": receipt["commit"],
        "diff_sha256": diff["sha256"],
        "diff_byte_size": diff["byte_size"],
    }
    qa = ExtensionQaJob.model_validate({**unsigned, "request_sha256": digest(unsigned)})
    (spool / "jobs" / f"{qa.job_id}.json").write_text(qa.model_dump_json())
    return qa


def test_independent_qa_reconstructs_exact_candidate_and_rejects_tampered_bytes(tmp_path: Path) -> None:
    job, root, spool = job_at(tmp_path)
    receipt = process_job(
        spool / "jobs" / f"{job.job_id}.json", spool, root, fake_provider, test_runner=fake_test
    )
    assert receipt is not None
    qa = qa_job_for(job, receipt, spool)
    observed = process_qa(spool / "jobs" / f"{qa.job_id}.json", spool, root, test_runner=fake_test)
    assert observed is not None and observed["status"] == "passed", observed
    assert observed["candidate_commit"] == receipt["commit"]
    assert process_qa(spool / "jobs" / f"{qa.job_id}.json", spool, root, test_runner=fake_test) is None
    another = qa_job_for(job, receipt, spool)
    (spool / "artifacts" / str(job.attempt_id) / "change.diff").write_text("tampered")
    denied = process_qa(spool / "jobs" / f"{another.job_id}.json", spool, root, test_runner=fake_test)
    assert denied is not None and denied["status"] == "failed" and denied["tests"] == []


@pytest.mark.parametrize("value", ["{}", '{"max_files":0}', '{"max_bytes":null}', '{"extra":true}'])
def test_policy_valid_default_and_invalid_env(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("REPOSITORY_CODE_POLICY", value)
    if value == "{}":
        assert Settings(db_password="unit-only").repository_code_policy.max_files > 0
    else:
        with pytest.raises(ValidationError):
            Settings(db_password="unit-only")


def test_policy_environment_and_constructor_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOSITORY_CODE_POLICY", '{"max_files":50}')
    assert Settings(db_password="unit-only").repository_code_policy.max_files == 50
    assert (
        Settings(
            db_password="unit-only", repository_code_policy=RepositoryCodePolicy(max_files=25)
        ).repository_code_policy.max_files
        == 25
    )


@pytest.mark.integration
def test_real_networkless_disposable_coding_and_tests(tmp_path: Path) -> None:
    image = os.environ.get("ONEFOUNDRY_EGRESS_TEST_IMAGE")
    if not image:
        pytest.fail("ONEFOUNDRY_EGRESS_TEST_IMAGE required for real Docker acceptance")
    image_id = (
        subprocess.check_output(("/usr/bin/docker", "image", "inspect", "--format", "{{.Id}}", image))  # noqa: S603 - image inspection only
        .decode()
        .strip()
    )
    job, root, spool = job_at(tmp_path, image=image_id)
    command = (
        "python",
        "-c",
        "import pathlib,json,socket; assert not any(pathlib.Path(p).exists() for p in"
        " ['/workspace/.git','/runner-spool','/var/run/docker.sock','/run/secrets','/"
        "app/src']); s=socket.socket(); s.settimeout(.1); assert s.connect_ex(('1.1.1.1',"
        "443)) != 0; pathlib.Path('calculator.py').write_text('def add(a, b):\\n    re"
        "turn a+b\\n'.replace('\\\\n','\\n')); print(json.dumps({'type':'thread.started',"
        "'thread_id':'offline-proof'})); print(json.dumps({'type':'turn.completed'}))",
    )
    receipt = process_job(
        spool / "jobs" / f"{job.attempt_id}.json", spool, root, OfflineRepositoryBridge(image_id, command)
    )
    assert receipt is not None and receipt["status"] == "done", receipt
    assert (root / "repo" / "calculator.py").read_text().endswith("return a-b\n")


@pytest.mark.integration
def test_repository_qa_image_imports_candidate_and_reconstructs_exact_patch(tmp_path: Path) -> None:
    owned_tag = "onefoundry-repository-qa-proof:" + uuid4().hex
    image = os.environ.get("ONEFOUNDRY_REPOSITORY_QA_TEST_IMAGE")
    try:
        if not image:
            subprocess.run(  # noqa: S603 - owned disposable QA image build
                ("/usr/bin/docker", "build", "--target", "repository-qa", "-t", owned_tag, "."),
                cwd=Path(__file__).resolve().parents[1],
                check=True,
                capture_output=True,
                timeout=180,
            )
            image = owned_tag
        image_id = (
            subprocess.check_output(  # noqa: S603 - image inspection only
                ("/usr/bin/docker", "image", "inspect", "--format", "{{.Id}}", image)
            )
            .decode()
            .strip()
        )
        job, root, spool = job_at(tmp_path, image=image_id)
        repo = root / "repo"
        (repo / "tests" / "test_calculator.py").write_text(
            "import calculator, fastapi, pydantic\n"
            "def test_add():\n    assert calculator.add(2, 3) == 5\n"
            "def test_candidate_import_owner():\n"
            "    assert calculator.__file__.startswith('/workspace/')\n"
            "    assert pydantic.__file__.startswith('/opt/qa-venv/')\n"
            "    assert fastapi.__file__.startswith('/opt/qa-venv/')\n"
        )
        subprocess.run(("/usr/bin/git", "add", "."), cwd=repo, check=True)  # noqa: S603
        subprocess.run(  # noqa: S603 - exact fixture revision only
            (
                "/usr/bin/git",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@invalid.example",
                "commit",
                "-qm",
                "QA dependency acceptance",
            ),
            cwd=repo,
            check=True,
        )
        revision = subprocess.check_output(("/usr/bin/git", "rev-parse", "HEAD"), cwd=repo).decode().strip()
        value = job.model_dump(mode="json", exclude={"request_sha256"})
        value["source"]["revision"] = value["target"]["revision"] = revision
        value["target"]["test_commands"] = [["python", "-m", "pytest", "-q", "tests"]]
        job = RepositoryCodeManifest.model_validate({**value, "request_sha256": digest(value)})
        (spool / "jobs" / f"{job.job_id}.json").write_text(job.model_dump_json())
        receipt = process_job(spool / "jobs" / f"{job.job_id}.json", spool, root, fake_provider)
        assert receipt is not None and receipt["status"] == "done", receipt
        assert [check["status"] for check in receipt["tests"]] == ["failed", "passed"]
        qa = qa_job_for(job, receipt, spool)
        observed = process_qa(spool / "jobs" / f"{qa.job_id}.json", spool, root)
        assert observed is not None and observed["status"] == "passed", observed
        assert observed["candidate_commit"] == receipt["commit"]
        assert (repo / "calculator.py").read_text().endswith("return a-b\n")
    finally:
        if image == owned_tag:
            subprocess.run(("/usr/bin/docker", "image", "rm", owned_tag), capture_output=True, timeout=30)  # noqa: S603 - only the tag created by this test
