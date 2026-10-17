"""Host-side read-only local checkout setup, without Docker writes in tests."""

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from scripts import local_repositories as local_sources


def configuration(root: str = "/source-roots/workspace") -> dict[str, Any]:
    return {
        "x-onefoundry-local-source": {"git_timeout_seconds": 3, "compose_timeout_seconds": 120},
        "services": {"app": {"environment": {"LOCAL_SOURCE_ROOT": root}, "volumes": []}},
    }


def test_configuration_defaults_and_rejects_invalid_mounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "compose.local-source.json"
    template = tmp_path / "compose.local-source.example.json"
    template.write_text(json.dumps(configuration()))
    monkeypatch.setattr(local_sources, "TEMPLATE", template)
    _, root, git_timeout, compose_timeout = local_sources.load_configuration(path)
    assert (root, git_timeout, compose_timeout) == ("/source-roots/workspace", 3, 120)

    path.write_text(json.dumps(configuration()))
    _, root, git_timeout, compose_timeout = local_sources.load_configuration(path)
    assert (root, git_timeout, compose_timeout) == ("/source-roots/workspace", 3, 120)

    invalid = configuration("/")
    path.write_text(json.dumps(invalid))
    with pytest.raises(local_sources.LocalSourceError, match="dedicated absolute"):
        local_sources.load_configuration(path)

    invalid = configuration()
    invalid["services"]["app"]["volumes"] = [
        {
            "type": "bind",
            "source": str(tmp_path),
            "target": "/source-roots/workspace/repo",
            "read_only": False,
        }
    ]
    path.write_text(json.dumps(invalid))
    with pytest.raises(local_sources.LocalSourceError, match="read-only"):
        local_sources.load_configuration(path)

    invalid = configuration()
    invalid["x-onefoundry-local-source"]["git_timeout_seconds"] = 0
    path.write_text(json.dumps(invalid))
    with pytest.raises(local_sources.LocalSourceError, match="git_timeout_seconds"):
        local_sources.load_configuration(path)

    path.write_text(json.dumps({"services": {"app": {}}}))
    with pytest.raises(local_sources.LocalSourceError, match="Invalid local source configuration"):
        local_sources.load_configuration(path)


def test_checkout_validation_and_mount_aliases(tmp_path: Path) -> None:
    checkout = tmp_path / "districtAI"
    checkout.mkdir()
    subprocess.run([local_sources.executable("git"), "init", "-q", str(checkout)], check=True)  # noqa: S603
    (checkout / "inside").mkdir()
    assert local_sources.validate_checkout(str(checkout), 3) == checkout
    with pytest.raises(local_sources.LocalSourceError, match="checkout root"):
        local_sources.validate_checkout(str(checkout / "inside"), 3)
    symlink = tmp_path / "link"
    symlink.symlink_to(checkout)
    with pytest.raises(local_sources.LocalSourceError, match="symlinked"):
        local_sources.validate_checkout(str(symlink), 3)

    config = configuration()
    assert local_sources.add_mount(config, "/source-roots/workspace", checkout, "districtAI")
    assert not local_sources.add_mount(config, "/source-roots/workspace", checkout, "districtAI")
    assert config["services"]["app"]["volumes"] == [
        {
            "type": "bind",
            "source": str(checkout),
            "target": "/source-roots/workspace/districtAI",
            "read_only": True,
        }
    ]
    with pytest.raises(local_sources.LocalSourceError, match="already in use"):
        local_sources.add_mount(config, "/source-roots/workspace", tmp_path / "other", "districtAI")
    with pytest.raises(local_sources.LocalSourceError, match="Repository name"):
        local_sources.add_mount(config, "/source-roots/workspace", tmp_path / "other", "../other")


def test_apply_validates_candidate_and_confirms_exact_read_only_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "districtAI"
    checkout.mkdir()
    private = tmp_path / "compose.local-source.json"
    monkeypatch.setattr(local_sources, "REPO_DIR", tmp_path)
    monkeypatch.setattr(local_sources, "BASE_COMPOSE", tmp_path / "compose.yaml")
    monkeypatch.setattr(local_sources, "PRIVATE_COMPOSE", private)
    monkeypatch.setattr(local_sources, "executable", lambda name: f"/usr/bin/{name}")
    config = configuration()
    local_sources.add_mount(config, "/source-roots/workspace", checkout, "districtAI")
    target = "/source-roots/workspace/districtAI"
    calls: list[list[str]] = []

    def run(args: list[str], timeout_seconds: int, cwd: Path | None = None) -> str:
        assert timeout_seconds == 120
        calls.append(args)
        if args[-2:] == ["config", "--quiet"]:
            assert json.loads(Path(args[args.index("-f", 3) + 1]).read_text()) == config
        if args[-3:] == ["ps", "-q", "app"]:
            return "container-1"
        if args[:2] == ["/usr/bin/docker", "inspect"]:
            return json.dumps(
                [
                    {
                        "State": {"Running": True},
                        "Config": {"Env": ["LOCAL_SOURCE_ROOT=/source-roots/workspace"]},
                        "Mounts": [
                            {"Type": "bind", "Source": str(checkout), "Destination": target, "RW": False}
                        ],
                    }
                ]
            )
        return ""

    monkeypatch.setattr(local_sources, "run_command", run)
    local_sources.save_and_apply(config, 120)
    assert json.loads(private.read_text()) == config
    assert private.stat().st_mode & 0o777 == 0o600
    assert [args[-2:] for args in calls[:2]] == [["config", "--quiet"], ["--no-deps", "app"]]


def test_rejected_compose_candidate_preserves_previous_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = tmp_path / "compose.local-source.json"
    previous = configuration()
    private.write_text(json.dumps(previous))
    monkeypatch.setattr(local_sources, "REPO_DIR", tmp_path)
    monkeypatch.setattr(local_sources, "BASE_COMPOSE", tmp_path / "compose.yaml")
    monkeypatch.setattr(local_sources, "PRIVATE_COMPOSE", private)
    monkeypatch.setattr(local_sources, "executable", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        local_sources,
        "run_command",
        lambda *args: (_ for _ in ()).throw(subprocess.CalledProcessError(1, "docker compose config")),
    )
    checkout = tmp_path / "new"
    checkout.mkdir()
    candidate = configuration()
    local_sources.add_mount(candidate, "/source-roots/workspace", checkout, "new")
    with pytest.raises(local_sources.LocalSourceError, match="running app was not changed"):
        local_sources.save_and_apply(candidate, 120)
    assert json.loads(private.read_text()) == previous
