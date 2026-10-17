from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import urllib.request
from argparse import ArgumentTypeError, Namespace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.request import ProxyHandler

import pytest

from scripts import onefoundry_update, release_bundle


def memory_asset_overlay(home: Path) -> Path:
    path = home / "data/config/memory-assets.compose.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    volumes = []
    for name in ("RUNTIME", "MODEL"):
        value = "${MEMORY_EMBEDDING_" + name + "_PATH:?Set an approved absolute local asset path}"
        volumes.append({"type": "bind", "source": value, "target": value, "read_only": True})
    path.write_text(json.dumps({"services": {"app": {"volumes": volumes}}}), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_installed_memory_assets_are_optional_and_strict(tmp_path: Path) -> None:
    assert onefoundry_update.memory_assets_override(tmp_path) == []
    path = memory_asset_overlay(tmp_path)
    assert onefoundry_update.memory_assets_override(tmp_path) == ["-f", str(path)]
    original = path.read_text(encoding="utf-8")
    for payload in ("{", "[]", "{}", "x" * 4097, original.replace('"read_only": true', '"read_only": false')):
        path.write_text(payload, encoding="utf-8")
        with pytest.raises(ValueError, match="memory asset"):
            onefoundry_update.memory_assets_override(tmp_path)
    path.write_text(original, encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="memory asset"):
        onefoundry_update.memory_assets_override(tmp_path)
    path.chmod(0o600)
    moved = path.with_suffix(".saved")
    path.rename(moved)
    path.symlink_to(moved)
    with pytest.raises(ValueError, match="memory asset"):
        onefoundry_update.memory_assets_override(tmp_path)


def local_source_overlay(home: Path) -> Path:
    path = home / "data/config/local-sources.compose.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    (path.parent / ".env").write_text("LOCAL_SOURCE_ROOT=/repositories\n", encoding="utf-8")
    (path.parent / ".env").chmod(0o600)
    checkout = home / "repository"
    (checkout / ".git").mkdir(parents=True)
    mount = {"type": "bind", "source": str(checkout), "target": "/repositories/project", "read_only": True}
    path.write_text(json.dumps({"services": {"app": {"volumes": [mount]}}}), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_installed_local_sources_are_optional_and_exact_read_only_binds(tmp_path: Path) -> None:
    assert onefoundry_update.local_sources_override(tmp_path) == []
    path = local_source_overlay(tmp_path)
    assert onefoundry_update.local_sources_override(tmp_path) == ["-f", str(path)]
    original = json.loads(path.read_text(encoding="utf-8"))
    mount = original["services"]["app"]["volumes"][0]
    for bad in (
        {**mount, "read_only": False},
        {**mount, "target": "/runner-spool/project"},
        {**mount, "target": "/repositories/../project"},
        {**mount, "target": "/repositories/project/nested"},
        {**mount, "source": str(tmp_path / "missing")},
        {**mount, "source": str(Path.home())},
        {**mount, "source": "${PRIVATE_ROOT}/project"},
    ):
        path.write_text(json.dumps({"services": {"app": {"volumes": [bad]}}}), encoding="utf-8")
        with pytest.raises(ValueError, match="local source override"):
            onefoundry_update.local_sources_override(tmp_path)
    bad_configs: list[object] = [
        {"services": {"app": {"volumes": [mount], "privileged": True}}},
        {"services": {"runner": {"volumes": [mount]}}},
        {"services": {"app": {"volumes": [mount, mount]}}},
        {"services": {"app": {"volumes": []}}},
        [],
    ]
    for bad_config in bad_configs:
        path.write_text(json.dumps(bad_config), encoding="utf-8")
        with pytest.raises(ValueError, match="local source override"):
            onefoundry_update.local_sources_override(tmp_path)
    path.write_text(json.dumps(original), encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(ValueError, match="owned private"):
        onefoundry_update.local_sources_override(tmp_path)
    path.chmod(0o600)
    moved = path.with_suffix(".saved")
    path.rename(moved)
    path.symlink_to(moved)
    with pytest.raises(ValueError, match="owned private"):
        onefoundry_update.local_sources_override(tmp_path)


def test_installed_source_root_requires_config_and_ignores_ambient_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = local_source_overlay(tmp_path)
    env = path.parent / ".env"
    monkeypatch.setenv("LOCAL_SOURCE_ROOT", "/ambient")
    assert onefoundry_update.local_sources_override(tmp_path) == ["-f", str(path)]
    for invalid in ("", "/", "/repositories/../root", "${UNAPPROVED}"):
        env.write_text(f"LOCAL_SOURCE_ROOT={invalid}\n", encoding="utf-8")
        with pytest.raises(ValueError, match="LOCAL_SOURCE_ROOT"):
            onefoundry_update.local_sources_override(tmp_path)


def test_compose_retains_installed_read_only_source_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay = local_source_overlay(tmp_path)
    memory_overlay = memory_asset_overlay(tmp_path)
    monkeypatch.setattr(
        onefoundry_update,
        "validate_manifest",
        lambda _path: {
            "images": {"runtime": "r", "runner": "c", "input-worker": "w"},
        },
    )
    monkeypatch.setenv("LOCAL_SOURCE_ROOT", "/ambient")
    calls: list[Any] = []
    monkeypatch.setenv("TEAM_REPOSITORY_READ_ENABLED", "true")

    def collect(*args: object, **kwargs: object) -> str:
        calls.append((args, kwargs))
        return ""

    monkeypatch.setattr(onefoundry_update, "run", collect)
    onefoundry_update.compose(
        tmp_path, tmp_path / "release", "fixture", "up", "-d", "--no-build", "--no-deps", "app"
    )
    args, kwargs = calls[0]
    assert ("-f", str(memory_overlay), "-f", str(overlay)) == args[-9:-5]
    assert args[-5:] == ("up", "-d", "--no-build", "--no-deps", "app")
    assert "LOCAL_SOURCE_ROOT" not in kwargs["env"]
    assert "TEAM_REPOSITORY_READ_ENABLED" not in kwargs["env"]


def test_compose_retains_installed_memory_mounts_and_env_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay = memory_asset_overlay(tmp_path)
    (overlay.parent / ".env").write_text(
        "MEMORY_EMBEDDING_MODEL_PATH=/operator/model.gguf\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        onefoundry_update,
        "validate_manifest",
        lambda _path: {"images": {"runtime": "r", "runner": "c", "input-worker": "w"}},
    )
    monkeypatch.setenv("MEMORY_EMBEDDING_MODEL_PATH", "/ambient/wrong.gguf")
    calls = []

    def called(
        *args: str, env: dict[str, str] | None = None, capture: bool = False, timeout: float | None = None
    ) -> str:
        calls.append((args, env, capture))
        return "ready"

    monkeypatch.setattr(onefoundry_update, "run", called)
    assert onefoundry_update.compose(tmp_path, tmp_path / "release", "pilot", "up", capture=True) == "ready"
    args, env, captured = calls[0]
    assert args[-3:] == ("-f", str(overlay), "up")
    assert env is not None and "MEMORY_EMBEDDING_MODEL_PATH" not in env
    assert env["ONEFOUNDRY_RUNTIME_IMAGE"] == "r" and captured
    monkeypatch.setattr(os, "getuid", lambda: 123456)
    with pytest.raises(ValueError, match="memory asset"):
        onefoundry_update.compose(tmp_path, tmp_path / "release", "pilot", "up")
    assert len(calls) == 1


def release_archive(
    tmp_path: Path, *, extra: str | None = None, legacy: bool = False, licensed: bool = True
) -> tuple[Path, str]:
    bundle = tmp_path / "bundle"
    bundle.mkdir(parents=True)
    version = "0.1.0"
    for name, value in {
        "VERSION": version,
        ".env.example": "DB_PASSWORD=\nAPP_DB_PASSWORD=\n",
        "compose.yaml": "services: {}\n",
        "compose.release.yaml": "services: {}\n",
    }.items():
        (bundle / name).write_text(value, encoding="utf-8")
    (bundle / "images.tar").write_bytes(b"test image archive")
    (bundle / "scripts").mkdir()
    script_names = ["__init__.py", "release_bundle.py", "onefoundry_update.py"]
    if not legacy:
        script_names.extend(("operator_config.py", "operator_doctor.py"))
    for name in script_names:
        (bundle / "scripts" / name).write_text("# test\n", encoding="utf-8")
    if not legacy:
        (bundle / "onefoundry").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        if licensed:
            (bundle / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    manifest = {
        "format": "onefoundry.release.v1",
        "version": version,
        "commit": "a" * 40,
        "images": release_bundle.image_refs(version),
        "base_images": list(release_bundle.BASE_IMAGES),
        "image_ids": {
            ref: "sha256:" + "a" * 64
            for ref in (*release_bundle.image_refs(version).values(), *release_bundle.BASE_IMAGES)
        },
        "files": {
            name: release_bundle.sha256(bundle / name)
            for name in (
                release_bundle.LEGACY_FILES
                if legacy
                else release_bundle.LEGACY_FILES
                | release_bundle.OPERATOR_FILES
                | (release_bundle.LICENSE_FILE if licensed else frozenset())
            )
        },
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    archive_path = tmp_path / "release.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        for path in bundle.rglob("*"):
            archive.add(path, arcname=f"onefoundry/{path.relative_to(bundle)}", recursive=False)
        if extra:
            info = tarfile.TarInfo(extra)
            content = b"untrusted"
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return archive_path, release_bundle.sha256(archive_path)


def test_source_version_matches_all_package_metadata() -> None:
    assert (
        release_bundle.source_version()
        == (release_bundle.ROOT / "VERSION").read_text(encoding="utf-8").strip()
    )


def test_committed_source_export_excludes_untracked_files(tmp_path: Path) -> None:
    source = tmp_path / "committed"
    release_bundle.export_committed_source(source)
    assert (source / "Dockerfile").is_file()
    assert not (source / ".venv").exists()
    with pytest.raises(ValueError, match="commit SHA"):
        release_bundle.export_committed_source(tmp_path / "rejected", revision="main; exit 1")


@pytest.mark.parametrize("worker_umask", [0o007, 0o077])
def test_committed_source_export_is_readable_by_runtime_user(tmp_path: Path, worker_umask: int) -> None:
    private = tmp_path / "private-worker-storage"
    private.mkdir(mode=0o700)
    source = private / "committed"
    previous = os.umask(worker_umask)
    try:
        release_bundle.export_committed_source(source)
    finally:
        os.umask(previous)
    assert private.stat().st_mode & 0o777 == 0o700
    for path in (source, source / "src", source / "src/onefoundry"):
        assert path.stat().st_mode & 0o777 == 0o755
    assert (source / "src/onefoundry/main.py").stat().st_mode & 0o777 == 0o644
    assert (source / "scripts/check_postgres.sh").stat().st_mode & 0o777 == 0o755


def test_checked_bundle_rejects_wrong_digest_and_extra_path(tmp_path: Path) -> None:
    archive, digest = release_archive(tmp_path)
    manifest = onefoundry_update.unpack(archive, digest, tmp_path / "valid")
    assert manifest["version"] == "0.1.0"
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        onefoundry_update.unpack(archive, "0" * 64, tmp_path / "wrong")
    forged, forged_digest = release_archive(tmp_path / "forged", extra="onefoundry/../../escape")
    with pytest.raises(ValueError, match="unexpected release archive entry"):
        onefoundry_update.unpack(forged, forged_digest, tmp_path / "rejected")
    assert not (tmp_path / "escape").exists()


def test_new_operator_can_read_previous_release_bundle(tmp_path: Path) -> None:
    archive, digest = release_archive(tmp_path, legacy=True)
    manifest = onefoundry_update.unpack(archive, digest, tmp_path / "previous")
    assert manifest["version"] == "0.1.0"
    archive, digest = release_archive(tmp_path / "operator", licensed=False)
    manifest = onefoundry_update.unpack(archive, digest, tmp_path / "previous-operator")
    assert manifest["version"] == "0.1.0"


def test_license_is_in_new_release_and_checksum_protected(tmp_path: Path) -> None:
    archive, digest = release_archive(tmp_path)
    onefoundry_update.unpack(archive, digest, tmp_path / "release")
    assert (tmp_path / "release/LICENSE").read_text(encoding="utf-8") == "MIT License\n"
    (tmp_path / "release/LICENSE").write_text("changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum mismatch: LICENSE"):
        release_bundle.validate_manifest(tmp_path / "release")


def test_manifest_detects_tampered_compose(tmp_path: Path) -> None:
    archive, digest = release_archive(tmp_path)
    onefoundry_update.unpack(archive, digest, tmp_path / "release")
    (tmp_path / "release/compose.yaml").write_text("services: {app: {}}", encoding="utf-8")
    with pytest.raises(ValueError, match="checksum mismatch"):
        release_bundle.validate_manifest(tmp_path / "release")


def test_update_failed_start_restores_previous_release_and_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    previous = home / "releases/0.1.0"
    previous.mkdir(parents=True)
    (home / "data/updates").mkdir(parents=True)
    (home / "current").symlink_to(previous)
    archive = tmp_path / "candidate.tar.gz"
    archive.write_bytes(b"candidate")
    backup = home / "data/backups/checkpoint"
    events: list[str] = []

    def fake_unpack(_archive: Path, _sha: str, destination: Path) -> dict[str, str]:
        destination.mkdir(parents=True)
        return {"version": "0.1.1"}

    def fake_compose(
        _home: Path, release: Path, _project: str, *arguments: str, capture: bool = False
    ) -> str:
        del capture
        if arguments == ("config", "--services"):
            return "app\nonefoundry-coordinator"
        operation = f"{release.name}:{arguments[0]}"
        events.append(operation)
        if operation == "0.1.1:up":
            raise RuntimeError("new app failed")
        return ""

    monkeypatch.setattr(onefoundry_update, "read_install", lambda _home: (previous, {"project": "pilot"}))
    monkeypatch.setattr(onefoundry_update, "unpack", fake_unpack)
    monkeypatch.setattr(onefoundry_update, "validate_manifest", lambda release: {"version": release.name})
    monkeypatch.setattr(onefoundry_update, "run", lambda *args, **kwargs: "")
    monkeypatch.setattr(onefoundry_update, "verify_loaded_images", lambda _release: None)
    monkeypatch.setattr(onefoundry_update, "compose", fake_compose)
    monkeypatch.setattr(onefoundry_update, "snapshot", lambda *_args: backup)
    monkeypatch.setattr(
        onefoundry_update, "restore", lambda _backup, _project, _version, **_kwargs: events.append("restore")
    )
    monkeypatch.setattr(onefoundry_update, "health", lambda *_args, **_kwargs: events.append("health"))
    args = Namespace(
        home=home,
        archive=archive,
        sha256=hashlib.sha256(b"candidate").hexdigest(),
        health_timeout_seconds=90,
    )
    with pytest.raises(RuntimeError, match="previous release and data checkpoint were restored"):
        onefoundry_update.update(args)
    assert (home / "current").resolve() == previous
    assert events == ["0.1.0:down", "0.1.1:up", "0.1.1:down", "restore", "0.1.0:up", "health"]
    state = json.loads((home / "data/updates/rollback.json").read_text(encoding="utf-8"))
    assert state["state"] == "restored_after_failure"


def test_update_accepts_default_coordinator_and_rejects_extra_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    services = "app\nonefoundry-coordinator\n"
    active = "app\nonefoundry-coordinator\n"
    monkeypatch.setattr(onefoundry_update, "compose", lambda *_args, **_kwargs: services)
    monkeypatch.setattr(onefoundry_update, "run", lambda *_args, **_kwargs: active)
    onefoundry_update.verify_default_services(tmp_path, tmp_path, "pilot")
    active += "optional-profile-service\n"
    with pytest.raises(ValueError, match="unsupported profile or extra services"):
        onefoundry_update.verify_default_services(tmp_path, tmp_path, "pilot")


def test_rollback_requires_explicit_data_restore(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="--restore-data --yes"):
        onefoundry_update.rollback(Namespace(home=tmp_path, restore_data=False, yes=False))


def test_install_env_requires_distinct_passwords_and_no_profiles(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("DB_PASSWORD=owner\nAPP_DB_PASSWORD=owner\n", encoding="utf-8")
    env_file.chmod(0o600)
    with pytest.raises(ValueError, match="must differ"):
        onefoundry_update.validate_env_file(env_file)
    env_file.write_text("DB_PASSWORD=owner\nAPP_DB_PASSWORD=app\nCOMPOSE_PROFILES=local-model\n")
    with pytest.raises(ValueError, match="optional Compose profiles"):
        onefoundry_update.validate_env_file(env_file)
    env_file.write_text("DB_PASSWORD=owner\nAPP_DB_PASSWORD=app\n")
    onefoundry_update.validate_env_file(env_file)
    env_file.chmod(0o644)
    with pytest.raises(ValueError, match="mode 0600"):
        onefoundry_update.validate_env_file(env_file)


def test_missing_declared_volume_blocks_update_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(onefoundry_update, "compose", lambda *_args, **_kwargs: "postgres_data")

    def missing_volume(*_args: str, **_kwargs: object) -> str:
        raise RuntimeError("volume absent")

    monkeypatch.setattr(onefoundry_update, "run", missing_volume)
    with pytest.raises(RuntimeError, match="volume absent"):
        onefoundry_update.volume_names(tmp_path, tmp_path, "pilot")


def test_compose_uses_private_env_instead_of_ambient_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    config_dir = home / "data/config"
    config_dir.mkdir(parents=True)
    (config_dir / ".env").write_text("DB_PASSWORD=private\nAPP_PORT=18888\n", encoding="utf-8")
    monkeypatch.setenv("DB_PASSWORD", "ambient")
    monkeypatch.setenv("APP_PORT", "9999")
    monkeypatch.setenv("COMPOSE_PROFILES", "local-model")
    monkeypatch.setattr(
        onefoundry_update,
        "validate_manifest",
        lambda _release: {"images": release_bundle.image_refs("0.1.0")},
    )
    observed: dict[str, object] = {}

    def fake_run(*command: str, env: dict[str, str], capture: bool, timeout: float | None = None) -> str:
        observed.update({"command": command, "env": env, "capture": capture})
        return ""

    monkeypatch.setattr(onefoundry_update, "run", fake_run)
    onefoundry_update.compose(home, tmp_path / "release", "pilot", "config", "--images", capture=True)
    actual_env = observed["env"]
    assert isinstance(actual_env, dict)
    assert "DB_PASSWORD" not in actual_env
    assert "APP_PORT" not in actual_env
    assert "COMPOSE_PROFILES" not in actual_env
    assert actual_env["ONEFOUNDRY_RUNTIME_IMAGE"] == "localhost/onefoundry-runtime:v0.1.0"
    actual_command = observed["command"]
    assert isinstance(actual_command, tuple)
    assert "--env-file" in actual_command


def test_readiness_probe_uses_direct_loopback_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(onefoundry_update, "compose", lambda *_args, **_kwargs: "127.0.0.1:8765")
    observed: dict[str, object] = {}

    class Ready(io.BytesIO):
        status = 200

    def make_opener(handler: ProxyHandler) -> SimpleNamespace:
        observed["proxies"] = vars(handler).get("proxies")

        def open_url(url: str, *, timeout: int) -> Ready:
            observed["url"] = url
            observed["timeout"] = timeout
            return Ready(b'{"status":"ready"}')

        return SimpleNamespace(open=open_url)

    monkeypatch.setattr(urllib.request, "build_opener", make_opener)
    onefoundry_update.health(tmp_path, tmp_path, "pilot", timeout=1)
    assert observed["proxies"] == {}
    assert observed["url"] == "http://127.0.0.1:8765/health/ready"
    monkeypatch.setattr(onefoundry_update, "compose", lambda *_args, **_kwargs: "0.0.0.0:8765")
    with pytest.raises(ValueError, match="unexpected app port mapping"):
        onefoundry_update.health(tmp_path, tmp_path, "pilot", timeout=1)


def test_health_timeout_setting_accepts_bounds_and_rejects_invalid() -> None:
    assert onefoundry_update.health_timeout_seconds("1") == 1
    assert onefoundry_update.health_timeout_seconds("600") == 600
    for value in ("0", "601", "bad"):
        with pytest.raises(ArgumentTypeError):
            onefoundry_update.health_timeout_seconds(value)
