"""Regression checks for Git publication boundaries."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.git_hygiene import (
    REPO_ROOT,
    _git,
    allowed_git_identity,
    effective_identity_violations,
    email_violations,
    historical_identity_violations,
    historical_path_violations,
    inventory_violations,
)


def _entry(path: str, mode: str = "100644") -> bytes:
    return f"{mode} {'0' * 40} 0\t{path}\0".encode()


def test_inventory_rejects_private_runtime_files_and_gitlinks() -> None:
    index = b"".join(
        (
            _entry("onefoundry/output/a.zip"),
            _entry("onefoundry/config/.env.local"),
            _entry("onefoundry/data/operator.sqlite3"),
            _entry("onefoundry/vendor/foreign", "160000"),
        )
    )
    assert inventory_violations(index) == (3, 1)


def test_inventory_keeps_samples_migrations_and_source() -> None:
    index = b"".join(
        (
            _entry("onefoundry/.env.example"),
            _entry("onefoundry/.env.execution.example"),
            _entry("onefoundry/migrations/versions/0001_init.sql"),
            _entry("onefoundry/src/onefoundry/model_secrets.py"),
            _entry("onefoundry/tests/test_project_scope.py"),
        )
    )
    assert inventory_violations(index) == (0, 0)


def test_email_check_only_rejects_added_non_example_addresses() -> None:
    private_domain = "private-" + "mail.com"
    patch = f"""--- a/test.txt
+++ b/test.txt
-operator@{private_domain}
+reader@example.com
+owner@{private_domain}
+person@local.test
"""
    assert email_violations(patch) == 1


def test_email_check_ignores_empty_and_context_lines() -> None:
    private_domain = "private-" + "mail.com"
    assert email_violations(f" context person@{private_domain}\n") == 0
    assert email_violations("") == 0


@pytest.mark.parametrize(
    "text",
    [
        '"scanner-456@' + 'im.wechat"',
        '"scanner-456@' + 'IM.WECHAT"',
        '"postgresql://alice:fixture@' + 'db.internal/company"',
        '"https://fixture@' + 'mail-fixture.zohomcp.com/mcp/message"',
    ],
)
def test_email_check_distinguishes_protocol_identifiers_and_url_userinfo(text: str) -> None:
    assert email_violations("+" + text) == 0


@pytest.mark.parametrize(
    "text",
    [
        "operator@" + "private-mail.com",
        "scanner@" + "im.wechat.evil.com",
        "https://example.com/contact/owner@" + "private-mail.com",
        "https://example.com/?email=owner@" + "private-mail.com",
        "https://example.com/#owner@" + "private-mail.com",
        "mailto:owner@" + "private-mail.com",
        "https://owner@" + "private-mail.com@host.example/path",
    ],
)
def test_email_check_retains_actual_addresses_and_malformed_authorities(text: str) -> None:
    assert email_violations("+" + text) >= 1


def test_protocol_identifier_is_still_rejected_as_a_git_identity() -> None:
    assert not allowed_git_identity("scanner-456@" + "im.wechat")


def test_protocol_field_does_not_hide_an_address_elsewhere_on_the_line() -> None:
    protocol = "postgresql://alice:fixture@" + "db.internal/company"
    email = "owner@" + "private-mail.com"
    assert email_violations(f'+"{protocol}" "{email}"') == 1


def test_history_blocks_previous_private_files_even_after_index_removal() -> None:
    paths = (
        b"onefoundry/output/local.zip\0"
        b"onefoundry/output/local.zip\0"
        b"onefoundry/migrations/versions/0001_init.sql\0"
    )
    assert historical_path_violations(paths) == 1


def test_malformed_index_fails_closed() -> None:
    with pytest.raises(ValueError):
        inventory_violations(b"100644 malformed entry\0")


def test_force_added_ignored_runtime_file_is_rejected(tmp_path: Path) -> None:
    git_executable = shutil.which("git")
    assert git_executable is not None
    # The test runs fixed Git commands against its disposable repository.
    subprocess.run([git_executable, "init", "-q", str(tmp_path)], check=True)  # noqa: S603
    (tmp_path / ".gitignore").write_text("/onefoundry/output/\n")
    runtime = tmp_path / "onefoundry" / "output" / "private.db"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"not a real database")
    subprocess.run(  # noqa: S603
        [git_executable, "-C", str(tmp_path), "add", "-f", "onefoundry/output/private.db"],
        check=True,
    )
    index = subprocess.check_output(  # noqa: S603
        [git_executable, "-C", str(tmp_path), "ls-files", "--stage", "-z"]
    )
    assert inventory_violations(index) == (1, 0)


def test_inventory_uses_repository_root_from_nested_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_index = _git("ls-files", "--stage", "-z")
    monkeypatch.chdir(Path(__file__).parent)
    assert _git("ls-files", "--stage", "-z") == root_index


@pytest.mark.parametrize("version", [None, "8.30.10"])
def test_precommit_fails_closed_without_exact_scanner(tmp_path: Path, version: str | None) -> None:
    git_executable = shutil.which("git")
    bash_executable = shutil.which("bash")
    assert git_executable is not None and bash_executable is not None
    subprocess.run([git_executable, "init", "-q", str(tmp_path)], check=True)  # noqa: S603
    if version is not None:
        scanner = tmp_path / ".git" / "tools" / "gitleaks"
        scanner.parent.mkdir(parents=True)
        scanner.write_text(f"#!/bin/sh\nprintf '%s\\n' '{version}'\n")
        scanner.chmod(0o700)
    hook = REPO_ROOT / ".githooks" / "pre-commit"
    result = subprocess.run(  # noqa: S603
        [bash_executable, str(hook)], cwd=tmp_path, capture_output=True, text=True
    )
    assert result.returncode == 2
    assert "Pinned Gitleaks" in result.stderr


def _ident(email: str) -> bytes:
    return f"Synthetic operator <{email}> 1700000000 +0000\n".encode()


@pytest.mark.parametrize(
    "email",
    [
        "42+fixture@users.noreply.github.com",
        "noreply@github.com",
        "operator@example.com",
        "agent@fixture.invalid",
    ],
)
def test_git_identity_allows_public_noreply_and_reserved_examples(email: str) -> None:
    assert allowed_git_identity(email)
    assert effective_identity_violations(_ident(email), _ident(email)) == 0
    assert email_violations("+" + email) == 0


def test_git_identity_rejects_private_or_malformed_fields() -> None:
    private = "synthetic@" + "private-" + "mail.com"
    assert not allowed_git_identity(private)
    assert effective_identity_violations(_ident(private), _ident("operator@example.com")) == 1
    assert effective_identity_violations(b"", b"malformed") == 2
    assert not allowed_git_identity("missing-at")
    assert not allowed_git_identity("agent@host.local")


def test_git_identity_resolves_environment_override(monkeypatch: pytest.MonkeyPatch) -> None:
    private = "synthetic@" + "private-" + "mail.com"
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Synthetic operator")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Synthetic operator")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", private)
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "operator@example.com")
    assert (
        effective_identity_violations(_git("var", "GIT_AUTHOR_IDENT"), _git("var", "GIT_COMMITTER_IDENT"))
        == 1
    )


def test_history_retains_identity_block_after_configuration_changes() -> None:
    private = ("synthetic@" + "private-" + "mail.com").encode()
    public = b"operator@example.com"
    assert historical_identity_violations(private + b"\t" + public + b"\0\n") == 1
    assert historical_identity_violations(public + b"\t" + public + b"\0\n") == 0
    assert historical_identity_violations(b"malformed\0") == 2
    assert historical_identity_violations(b"") == 0


def test_missing_git_identity_owner_fails_without_exposing_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from scripts import git_hygiene

    def unavailable(*_args: str) -> bytes:
        raise subprocess.CalledProcessError(1, "git", stderr=b"private output is withheld")

    monkeypatch.setattr(git_hygiene, "_git", unavailable)
    monkeypatch.setattr("sys.argv", ["git_hygiene.py"])
    assert git_hygiene.main() == 2
    assert "private output is withheld" not in capsys.readouterr().err


def test_github_checks_require_explicit_manual_dispatch() -> None:
    """Routine source publication must not consume hosted Actions minutes."""
    workflow = (REPO_ROOT / ".github/workflows/onefoundry-check.yml").read_text()
    trigger = workflow.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    assert trigger.strip() == "workflow_dispatch:"
