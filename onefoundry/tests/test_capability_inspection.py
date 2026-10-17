"""Readiness distinguishes setup, denied access and actual implementation gaps."""

from pathlib import Path
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from onefoundry.capability_inspection import inspect_capabilities
from onefoundry.capability_schemas import CapabilityCheck, CapabilityReport
from onefoundry.settings import Settings
from onefoundry.task_execution_capabilities import execution_capabilities
from tests.test_role_repository_file_unit import _git


def source(**values: Any) -> dict[str, Any]:
    return {
        "id": uuid4(),
        "kind": "local",
        "root_id": "workspace",
        "relative_path": "repo",
        "branch": "main",
        "revision": "a" * 40,
        **values,
    }


def report(settings: Settings, sources: list[dict[str, Any]], tools: list[str]) -> CapabilityReport:
    return inspect_capabilities(uuid4(), settings, sources, tools, profile_id=uuid4())


def test_all_setup_blockers_are_retained_and_never_create_extension_work(tmp_path: Path) -> None:
    settings = Settings(
        db_password="fixture-only",
        local_source_root=str(tmp_path / "missing"),
        team_repository_read_enabled=False,
    )
    check = report(settings, [source()], []).checks[-1]
    assert check.operation == "repository_read"
    assert {item.code for item in check.blockers} == {
        "repository_disabled",
        "repository_permission",
        "source_unavailable",
    }
    assert check.next_action == "configure"
    assert check.status == "disabled"


def test_ready_source_uses_exact_bounded_repository_owner_and_detects_drift(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Fixture")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "README.md").write_text("Public fixture\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    revision = _git(repo, "rev-parse", "HEAD").strip()
    settings = Settings(db_password="fixture-only", local_source_root=str(tmp_path))
    saved = source(revision=revision)
    check = report(settings, [saved], ["repository_file_v1"]).checks[-1]
    assert check.status == "ready" and check.next_action == "use"
    assert check.revision == revision and check.source_id == saved["id"]
    _git(repo, "checkout", "-qb", "changed")
    changed = report(settings, [saved], ["repository_file_v1"]).checks[-1]
    assert changed.status == "configuration_missing"
    assert changed.blockers[0].code == "source_revision_drifted"
    assert changed.next_action == "configure"


@pytest.mark.parametrize("path", ["../escape", "/absolute", "link"])
def test_unsafe_or_symlink_sources_are_unavailable_without_exposing_paths(tmp_path: Path, path: str) -> None:
    (tmp_path / "link").symlink_to(tmp_path.parent, target_is_directory=True)
    settings = Settings(db_password="fixture-only", local_source_root=str(tmp_path))
    check = report(settings, [source(relative_path=path)], ["repository_file_v1"]).checks[-1]
    assert check.status == "configuration_missing"
    assert str(tmp_path) not in check.model_dump_json()
    assert path not in check.model_dump_json()


def test_github_registration_requires_a_current_owner_probe() -> None:
    check = report(
        Settings(db_password="fixture-only"), [source(kind="github")], ["repository_file_v1"]
    ).checks[-1]
    assert check.status == "unknown" and check.next_action == "reconcile"
    denied = report(Settings(db_password="fixture-only"), [source(kind="github")], []).checks[-1]
    assert denied.status == "permission_required" and denied.next_action == "grant_access"
    assert len(denied.blockers) == 2


def test_fixture_runner_does_not_imply_isolated_maintenance_readiness() -> None:
    gateway = Mock()
    gateway._execution_gateway.return_value._capabilities.return_value = {
        "codex_cli": None,
        "claude_code": None,
    }
    result = inspect_capabilities(uuid4(), Settings(db_password="fixture-only"), [], [], gateway=gateway)
    coding = next(c for c in result.checks if c.operation == "project_code_change")
    assert coding.status == "dependency_unavailable" and coding.next_action == "configure"
    assert not any(b.kind == "missing_implementation" for b in coding.blockers)
    gateway._execution_gateway.return_value._capabilities.return_value = {
        "codex_cli": "token=never-output",
        "claude_code": "stale",
    }
    coding = next(
        c
        for c in inspect_capabilities(
            uuid4(), Settings(db_password="fixture-only"), [], [], gateway=gateway
        ).checks
        if c.operation == "project_code_change"
    )
    assert coding.status == "dependency_unavailable" and coding.next_action == "configure"
    assert "never-output" not in coding.model_dump_json()


def test_empty_sources_and_missing_search_endpoint_are_precise_setup() -> None:
    result = report(Settings(db_password="fixture-only"), [], [])
    checks = {c.operation: c for c in result.checks}
    assert checks["repository_read"].status == "configuration_missing"
    assert checks["public_web_search"].status == "configuration_missing"
    assert checks["team_execution"].status == "ready"
    assert checks["public_web_fetch"].status == "ready"
    assert checks["local_recording"].status == "ready"


def test_invalid_observations_and_unknown_fields_fail_validation() -> None:
    with pytest.raises(ValidationError):
        CapabilityCheck.model_validate(
            {
                "operation": "repository_read",
                "title": "Read",
                "owner": "reader",
                "status": "ready",
                "next_action": "use",
                "blockers": [],
                "unexpected": True,
            }
        )
    with pytest.raises(ValidationError):
        CapabilityReport.model_validate(
            {"project_id": uuid4(), "observed_at": "2026-10-12T12:00:00", "checks": []}
        )


def test_source_count_uses_the_observed_rows_when_later_state_changes() -> None:
    project = uuid4()
    connection = Mock()
    connection.scalar.side_effect = [project, 0]
    selected = source(kind="github", display_name="Fixture", source_count=3)
    connection.execute.return_value.scalars.return_value.all.return_value = []
    connection.execute.return_value.mappings.return_value.all.side_effect = [
        [{"id": uuid4(), "allowed_tools": []}],
        [selected],
    ]
    observed = execution_capabilities(connection, project, Settings(db_password="fixture-only"))
    assert observed["readiness"]["omitted_sources"] == 2


def test_owner_timeout_is_a_dependency_failure_not_missing_source_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from onefoundry import role_repository_file

    (tmp_path / "repo/.git/objects").mkdir(parents=True)

    def timeout(*_args: Any) -> Any:
        raise role_repository_file.RepositoryReadError(
            "repository read timed out", code="source_probe_timed_out"
        )

    monkeypatch.setattr(role_repository_file, "_head", timeout)
    check = report(
        Settings(db_password="fixture-only", local_source_root=str(tmp_path)),
        [source()],
        ["repository_file_v1"],
    ).checks[-1]
    assert check.status == "dependency_unavailable"
    assert check.blockers[0].code == "source_probe_timed_out"
