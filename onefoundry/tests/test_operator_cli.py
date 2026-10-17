from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from scripts import onefoundry_update, operator_config, operator_doctor


def private_env(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text("DB_PASSWORD=owner\nAPP_DB_PASSWORD=app\nAPP_PORT=8765\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def test_config_only_exposes_validated_nonsecret_settings(tmp_path: Path) -> None:
    path = private_env(tmp_path)
    listed = operator_config.describe(path, "list", None)
    assert listed["settings"] == {"APP_PORT": "8765", "MAILPIT_UI_PORT": None, "RUNNER_WALL_SECONDS": None}
    assert "owner" not in json.dumps(listed)
    changed = operator_config.set_value(path, "APP_PORT", "18765")
    assert changed["restart_required"] is True
    assert path.stat().st_mode & 0o777 == 0o600
    assert "DB_PASSWORD=owner" in path.read_text(encoding="utf-8")
    assert operator_config.describe(path, "get", "APP_PORT")["value"] == "18765"
    with pytest.raises(ValueError, match="secret"):
        operator_config.set_value(path, "DB_PASSWORD", "leak")
    assert "leak" not in path.read_text(encoding="utf-8")


def test_config_rejects_invalid_duplicates_and_symlink(tmp_path: Path) -> None:
    path = private_env(tmp_path)
    operator_config.set_value(path, "RUNNER_WALL_SECONDS", "60")
    before = path.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="RUNNER_WALL_SECONDS"):
        operator_config.set_value(path, "RUNNER_WALL_SECONDS", "75")
    assert path.read_text(encoding="utf-8") == before
    for value in ("0", "65536", "bad", "8765 # comment"):
        with pytest.raises(ValueError, match="APP_PORT"):
            operator_config.set_value(path, "APP_PORT", value)
    path.write_text(path.read_text(encoding="utf-8") + "APP_PORT=9000\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        operator_config.values(path)
    link = tmp_path / "link"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="regular file"):
        operator_config.set_value(link, "APP_PORT", "9000")


def test_doctor_reports_install_failure_without_losing_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(_home: Path) -> None:
        raise ValueError("install metadata absent")

    monkeypatch.setattr(onefoundry_update, "read_install", missing)
    result = operator_doctor.report(tmp_path)
    assert result["ok"] is False
    assert result["checks"]["install"]["detail"] == "install metadata absent"


def test_doctor_detects_wrong_running_image(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    path = home / "data/config/.env"
    path.parent.mkdir(parents=True)
    path.write_text("APP_PORT=8765\n", encoding="utf-8")
    monkeypatch.setattr(
        onefoundry_update, "read_install", lambda _home: (home / "release", {"project": "pilot"})
    )
    monkeypatch.setattr(
        operator_doctor,
        "validate_manifest",
        lambda _release: {
            "version": "0.1.2",
            "commit": "a" * 40,
            "images": {"runtime": "runtime", "runner": "runner", "input-worker": "worker"},
            "image_ids": {"runtime": "expected", "runner": "expected", "worker": "expected"},
        },
    )
    monkeypatch.setattr(onefoundry_update, "verify_loaded_images", lambda _release: None)
    monkeypatch.setattr(onefoundry_update, "volume_names", lambda *_args: ["pilot_postgres_data"])
    monkeypatch.setattr(onefoundry_update, "health", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        onefoundry_update,
        "compose",
        lambda *_args, **_kwargs: (
            "app\ndb\nmailpit\nrunner\nexternal-runner\ninput-file-worker"
            if _args[3] == "ps" and "--services" in _args
            else "container"
        ),
    )
    monkeypatch.setattr(onefoundry_update, "run", lambda *_args, **_kwargs: "wrong")
    result = operator_doctor.report(home)
    assert result["ok"] is False
    assert result["checks"]["runtime_images"]["ok"] is False
    assert "differs" in str(result["checks"]["runtime_images"]["detail"])


def test_backup_stops_snapshots_and_restarts_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    release = home / "releases/0.1.2"
    events: list[str] = []
    monkeypatch.setattr(onefoundry_update, "read_install", lambda _home: (release, {"project": "pilot"}))
    monkeypatch.setattr(
        onefoundry_update,
        "compose",
        lambda _home, _release, _project, *args: events.append(args[0]),
    )

    def fake_snapshot(*_args: object) -> Path:
        events.append("snapshot")
        return home / "backup"

    monkeypatch.setattr(onefoundry_update, "snapshot", fake_snapshot)
    monkeypatch.setattr(onefoundry_update, "health", lambda *_args, **_kwargs: events.append("health"))
    monkeypatch.setattr(onefoundry_update, "validate_manifest", lambda _release: {"version": "0.1.2"})
    args = Namespace(home=home, yes=True, health_timeout_seconds=90)
    onefoundry_update.backup(args)
    assert events == ["down", "snapshot", "up", "health"]
    events.clear()
    with pytest.raises(ValueError, match="--yes"):
        onefoundry_update.backup(Namespace(home=home, yes=False, health_timeout_seconds=90))
    assert events == []


def test_update_preserves_explicit_infra_status_bind(tmp_path: Path) -> None:
    source = tmp_path / "host-status"
    source.mkdir()
    previous = tmp_path / "previous"
    previous.mkdir()
    (previous / ".infra-status").symlink_to(source, target_is_directory=True)
    candidate = tmp_path / "candidate"
    (candidate / ".infra-status").mkdir(parents=True)
    onefoundry_update.preserve_infra_status(previous, candidate)
    assert (candidate / ".infra-status").resolve() == source
