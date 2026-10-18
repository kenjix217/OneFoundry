"""Install, update, and roll back a prebuilt OneFoundry Compose release."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from typing import Any

from scripts.operator_config import (
    INTERNAL_SEARCH_ENDPOINT,
    configured_values,
    installed_search_endpoint,
    private_search_compose,
    public_search_enabled,
)
from scripts.release_bundle import BASE_IMAGES, LEGACY_FILES, sha256, validate_manifest

RELEASE_FILES = frozenset(
    {
        "onefoundry/VERSION",
        "onefoundry/.env.example",
        "onefoundry/compose.yaml",
        "onefoundry/compose.release.yaml",
        "onefoundry/images.tar",
        "onefoundry/manifest.json",
        "onefoundry/scripts/__init__.py",
        "onefoundry/scripts/release_bundle.py",
        "onefoundry/scripts/onefoundry_update.py",
        "onefoundry/scripts/operator_config.py",
        "onefoundry/scripts/operator_doctor.py",
        "onefoundry/onefoundry",
        "onefoundry/LICENSE",
    }
)
LEGACY_RELEASE_FILES = frozenset(
    {"onefoundry/manifest.json"} | {f"onefoundry/{name}" for name in LEGACY_FILES}
)
OPERATOR_RELEASE_FILES = frozenset(RELEASE_FILES - {"onefoundry/LICENSE"})
VOLUME_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*")
PROJECT_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]*")


def run(
    *command: str, env: dict[str, str] | None = None, capture: bool = False, timeout: float | None = None
) -> str:
    result = subprocess.run(
        command,
        env=env,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        timeout=timeout,
    )
    return result.stdout.strip() if capture else ""


def unpack(archive_path: Path, expected_sha256: str, destination: Path) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("expected SHA-256 must be 64 lowercase hexadecimal characters")
    if sha256(archive_path) != expected_sha256:
        raise ValueError("release archive SHA-256 mismatch")
    destination.mkdir(parents=True, exist_ok=False)
    seen: set[str] = set()
    try:
        with tarfile.open(archive_path, "r:gz") as tar:
            for member in tar:
                if member.isdir() and member.name.rstrip("/") in {
                    "onefoundry",
                    "onefoundry/.infra-status",
                    "onefoundry/scripts",
                }:
                    continue
                if member.name not in RELEASE_FILES or not member.isfile() or member.name in seen:
                    raise ValueError(f"unexpected release archive entry: {member.name}")
                seen.add(member.name)
                source = tar.extractfile(member)
                if source is None:
                    raise ValueError(f"unreadable release archive entry: {member.name}")
                relative = Path(member.name).relative_to("onefoundry")
                target_path = destination / relative
                target_path.parent.mkdir(parents=True, exist_ok=True)
                with target_path.open("xb") as target:
                    shutil.copyfileobj(source, target)
        if seen not in (RELEASE_FILES, OPERATOR_RELEASE_FILES, LEGACY_RELEASE_FILES):
            raise ValueError("release archive is missing files")
        (destination / ".infra-status").mkdir()
        if "onefoundry/onefoundry" in seen:
            (destination / "onefoundry").chmod(0o755)
        return validate_manifest(destination)
    except BaseException:
        shutil.rmtree(destination)
        raise


def read_install(home: Path) -> tuple[Path, dict[str, Any]]:
    current = home / "current"
    config_path = home / "data/updates/install.json"
    if not current.is_symlink() or not config_path.is_file():
        raise ValueError("OneFoundry is not installed at this home")
    release = current.resolve(strict=True)
    if release.parent != (home / "releases").resolve():
        raise ValueError("current release pointer leaves the install home")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if (
        not isinstance(config, dict)
        or not isinstance(config.get("project"), str)
        or not PROJECT_PATTERN.fullmatch(config["project"])
    ):
        raise ValueError("invalid install configuration")
    validate_manifest(release)
    validate_env_file(home / "data/config/.env")
    return release, config


def validate_env_file(path: Path) -> None:
    if not path.is_file():
        raise ValueError("a private .env file with database passwords is required")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ValueError("the .env file must be private (mode 0600)")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if re.match(r"^\s*COMPOSE_PROFILES\s*=", line):
            raise ValueError("optional Compose profiles are not supported by this release installer")
        if "=" in line and not line.lstrip().startswith("#"):
            name, value = line.split("=", 1)
            if name.strip() in {"DB_PASSWORD", "APP_DB_PASSWORD"}:
                values[name.strip()] = value.strip().strip("\"'")
    if not values.get("DB_PASSWORD") or not values.get("APP_DB_PASSWORD"):
        raise ValueError("DB_PASSWORD and APP_DB_PASSWORD must be set")
    if values["DB_PASSWORD"] == values["APP_DB_PASSWORD"]:
        raise ValueError("database owner and app passwords must differ")


def configured_env_names(path: Path) -> set[str]:
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match:
            names.add(match.group(1))
    return names


def start_private_search(home: Path, project: str) -> None:
    path = private_search_compose(home, project)
    if (
        path is not None
        and public_search_enabled(home)
        and installed_search_endpoint(home) == INTERNAL_SEARCH_ENDPOINT
    ):
        # Compose recreates the container to attach it to the app's current network.
        run(
            "docker",
            "compose",
            "-p",
            f"{project}-public-search",
            "-f",
            str(path),
            "up",
            "-d",
            "--no-build",
            "--pull",
            "never",
            "--force-recreate",
        )


def stop_private_search(home: Path, project: str) -> None:
    path = private_search_compose(home, project)
    if path is not None:
        run("docker", "compose", "-p", f"{project}-public-search", "-f", str(path), "down")


def needs_private_search_health(home: Path, project: str) -> bool:
    private_search_compose(home, project)
    return public_search_enabled(home) and installed_search_endpoint(home) == INTERNAL_SEARCH_ENDPOINT


def memory_assets_override(home: Path) -> list[str]:
    """Retain only the operator's two read-only embedding binds across restarts."""
    path = home / "data/config/memory-assets.compose.json"
    if not path.exists() and not path.is_symlink():
        return []
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_size > 4096
    ):
        raise ValueError("installed memory asset override must be an owned private regular file")
    volumes = []
    for name in ("RUNTIME", "MODEL"):
        value = "${MEMORY_EMBEDDING_" + name + "_PATH:?Set an approved absolute local asset path}"
        volumes.append({"type": "bind", "source": value, "target": value, "read_only": True})
    try:
        configured = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise ValueError("invalid installed memory asset override") from None
    if configured != {"services": {"app": {"volumes": volumes}}}:
        raise ValueError("installed memory asset override permits only two read-only app asset binds")
    return ["-f", str(path)]


def local_sources_override(home: Path) -> list[str]:
    """Retain explicitly configured repository binds, restricted to read-only app access."""
    path = home / "data/config/local-sources.compose.json"
    if not path.exists() and not path.is_symlink():
        return []
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_size > 4096
    ):
        raise ValueError("installed local source override must be an owned private regular file")
    root = configured_values(home / "data/config/.env", ("LOCAL_SOURCE_ROOT",))["LOCAL_SOURCE_ROOT"] or ""
    if (
        not root.startswith("/")
        or root == "/"
        or str(PurePosixPath(root)) != root
        or ".." in PurePosixPath(root).parts
        or any(character in root for character in ("$", "\n", "\x00"))
    ):
        raise ValueError("installed local source override needs a dedicated configured LOCAL_SOURCE_ROOT")
    try:
        configured = json.loads(path.read_text(encoding="utf-8"))
        mounts = configured["services"]["app"]["volumes"]
        if (
            not isinstance(mounts, list)
            or not mounts
            or configured != {"services": {"app": {"volumes": mounts}}}
        ):
            raise ValueError
        targets: set[str] = set()
        sources: set[str] = set()
        for mount in mounts:
            source, target = mount["source"], mount["target"]
            if not isinstance(source, str) or not isinstance(target, str):
                raise ValueError
            checkout = Path(source)
            alias = PurePosixPath(target).relative_to(root)
            if (
                mount != {"type": "bind", "source": source, "target": target, "read_only": True}
                or len(alias.parts) != 1
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", alias.name)
                or target != root + "/" + alias.name
                or any(character in source for character in ("$", "\n", "\x00"))
                or not checkout.is_absolute()
                or checkout.resolve(strict=True) != checkout
                or checkout in {Path("/"), Path.home()}
                or not (checkout / ".git").is_dir()
                or (checkout / ".git").is_symlink()
                or source in sources
                or target in targets
            ):
                raise ValueError
            sources.add(source)
            targets.add(target)
    except (OSError, ValueError, TypeError, KeyError):
        raise ValueError(
            "installed local source override permits only exact read-only repository binds"
        ) from None
    return ["-f", str(path)]


def maintenance_transport_override(home: Path) -> list[str]:
    """Only a private evidence channel is shared with the separately configured worker."""
    path = home / "data/config/maintenance-transport.compose.json"
    if not path.exists() and not path.is_symlink():
        return []
    import stat

    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_size > 4096
    ):
        raise ValueError("maintenance transport override must be an owned private file")
    try:
        value = json.loads(path.read_text())
        app = value["services"]["app"]
        mount = app["volumes"][0]
        source = Path(mount["source"])
        group = app["group_add"][0]
        expected = {
            "services": {
                "app": {
                    "group_add": [group],
                    "volumes": [
                        {
                            "type": "bind",
                            "source": str(source),
                            "target": "/runner-spool/external",
                            "read_only": False,
                        }
                    ],
                }
            }
        }
        if (
            value != expected
            or not isinstance(group, str)
            or not group.isdecimal()
            or not 1 <= int(group) <= 2147483647
            or not source.is_absolute()
            or source.resolve() != source
            or source.is_symlink()
            or not source.is_dir()
            or source in {Path("/"), Path.home()}
            or any(char in str(source) for char in ("$", "\n", "\x00"))
        ):
            raise ValueError
        for folder in (source, *(source / name for name in ("jobs", "claims", "receipts", "artifacts"))):
            state = folder.lstat()
            if (
                not stat.S_ISDIR(state.st_mode)
                or state.st_uid != os.getuid()
                or state.st_gid != int(group)
                or stat.S_IMODE(state.st_mode) != 0o2770
            ):
                raise ValueError
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        raise ValueError("maintenance transport permits only the private shared evidence channel") from None
    return ["-f", str(path)]


def compose(
    home: Path,
    release: Path,
    project: str,
    *arguments: str,
    capture: bool = False,
    timeout: float | None = None,
) -> str:
    manifest = validate_manifest(release)
    refs = manifest["images"]
    assert isinstance(refs, dict)
    env = dict(os.environ)
    for name in configured_env_names(home / "data/config/.env"):
        env.pop(name, None)
    env.pop("COMPOSE_PROFILES", None)
    # Use the installed value or packaged default, regardless of the caller's shell.
    env.pop("TEAM_REPOSITORY_READ_ENABLED", None)
    env.pop("PUBLIC_WEB_SEARCH_ENDPOINT", None)
    env.pop("PUBLIC_WEB_ENABLED", None)
    env.update(
        {
            "ONEFOUNDRY_RUNTIME_IMAGE": refs["runtime"],
            "ONEFOUNDRY_RUNNER_IMAGE": refs["runner"],
            "ONEFOUNDRY_INPUT_WORKER_IMAGE": refs["input-worker"],
        }
    )
    if arguments and arguments[0] == "up":
        private_search_compose(home, project)
    if arguments and arguments[0] == "down":
        stop_private_search(home, project)
    result = run(
        "docker",
        "compose",
        "--env-file",
        str(home / "data/config/.env"),
        "-p",
        project,
        "-f",
        str(release / "compose.yaml"),
        "-f",
        str(release / "compose.release.yaml"),
        *memory_assets_override(home),
        *local_sources_override(home),
        *maintenance_transport_override(home),
        *arguments,
        env=env,
        capture=capture,
        timeout=timeout,
    )
    if arguments and arguments[0] == "up":
        start_private_search(home, project)
    return result


def verify_loaded_images(release: Path) -> None:
    manifest = validate_manifest(release)
    refs = manifest["images"]
    image_ids = manifest["image_ids"]
    assert isinstance(refs, dict) and isinstance(image_ids, dict)
    for ref in (*refs.values(), *BASE_IMAGES):
        actual = run("docker", "image", "inspect", "--format", "{{.Id}}", ref, capture=True)
        if actual != image_ids[ref]:
            raise ValueError(f"loaded image ID mismatch: {ref}")


def set_current(home: Path, release: Path) -> None:
    temporary = home / ".current-next"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(release)
    temporary.replace(home / "current")


def preserve_infra_status(previous: Path, release: Path) -> None:
    """Retain an explicitly bound local status feed during a release switch."""
    source = previous / ".infra-status"
    target = release / ".infra-status"
    if not source.is_symlink():
        return
    resolved = source.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("prior Infra status feed is not a directory")
    if target.is_symlink():
        if target.resolve(strict=True) != resolved:
            raise ValueError("candidate Infra status feed points elsewhere")
    else:
        target.rmdir()
        target.symlink_to(resolved, target_is_directory=True)


def health(home: Path, release: Path, project: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    address = compose(home, release, project, "port", "app", "8000", capture=True, timeout=timeout)
    host, separator, port = address.rpartition(":")
    if not separator or host.strip("[]") not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError(f"unexpected app port mapping: {address}")
    loopback = "[::1]" if host.strip("[]") == "::1" else "127.0.0.1"
    url = f"http://{loopback}:{int(port)}/health/ready"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last_error = "not ready"
    search_required = needs_private_search_health(home, project)
    while time.monotonic() < deadline:
        try:
            with opener.open(url, timeout=2) as response:
                payload = json.load(response)
                if response.status == 200 and payload.get("status") == "ready":
                    if not search_required:
                        return
                    compose(
                        home,
                        release,
                        project,
                        "exec",
                        "-T",
                        "app",
                        "python",
                        "-c",
                        "from onefoundry.public_web_client import PublicWebClient\n"
                        "from onefoundry.settings import Settings\n"
                        "s = Settings()\n"
                        "result = PublicWebClient().search("
                        "'OneFoundry public search readiness', s.public_web_search_endpoint, "
                        "1, s.public_web_timeout_seconds, s.public_web_max_response_bytes)\n"
                        "if not result['results']: raise RuntimeError('public search returned no results')\n",
                        timeout=min(20, max(1, deadline - time.monotonic())),
                    )
                    return
                last_error = f"readiness returned {payload}"
        except (
            OSError,
            ValueError,
            urllib.error.URLError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ) as error:
            last_error = str(error)
        time.sleep(1)
    raise RuntimeError(f"OneFoundry readiness failed: {last_error}")


def volume_names(home: Path, release: Path, project: str) -> list[str]:
    declared = compose(home, release, project, "config", "--volumes", capture=True).splitlines()
    names: list[str] = []
    if not declared:
        raise ValueError("Compose release declares no persistent volumes")
    for name in declared:
        if not VOLUME_PATTERN.fullmatch(name):
            raise ValueError(f"invalid Compose volume name: {name}")
        full_name = f"{project}_{name}"
        run("docker", "volume", "inspect", full_name, capture=True)
        names.append(full_name)
    return names


def snapshot(home: Path, release: Path, project: str) -> Path:
    backup = home / "data/backups" / time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    backup.mkdir(mode=0o700, parents=True, exist_ok=False)
    volumes = volume_names(home, release, project)
    manifest: dict[str, Any] = {"version": validate_manifest(release)["version"], "volumes": {}}
    try:
        for name in volumes:
            target = backup / f"{name}.tar"
            with target.open("xb") as stream:
                os.chmod(target, 0o600)
                subprocess.run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--network",
                        "none",
                        "--mount",
                        f"type=volume,source={name},target=/data,readonly",
                        "--entrypoint",
                        "tar",
                        "postgres:16.10-alpine",
                        "-C",
                        "/data",
                        "-cf",
                        "-",
                        ".",
                    ],
                    stdout=stream,
                    check=True,
                )
            manifest["volumes"][name] = sha256(target)
        bindings = {"private-config": home / "data/config"}
        if maintenance_transport_override(home):
            overlay = json.loads((home / "data/config/maintenance-transport.compose.json").read_text())
            bindings["maintenance-transport"] = Path(overlay["services"]["app"]["volumes"][0]["source"])
        manifest["bindings"] = {}
        for name, source in bindings.items():
            target = backup / f"{name}.tar"
            with target.open("xb") as stream:
                target.chmod(0o600)
                subprocess.run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--network",
                        "none",
                        "--mount",
                        f"type=bind,source={source},target=/data,readonly",
                        "--entrypoint",
                        "tar",
                        BASE_IMAGES[0],
                        "-C",
                        "/data",
                        "-cf",
                        "-",
                        ".",
                    ],
                    stdout=stream,
                    check=True,
                )
            manifest["bindings"][name] = {"path": str(source), "sha256": sha256(target)}
        (backup / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        os.chmod(backup / "manifest.json", 0o600)
        return backup
    except BaseException:
        shutil.rmtree(backup)
        raise


def restore(backup: Path, project: str, expected_version: str, *, home: Path | None = None) -> None:
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    volumes = manifest["volumes"]
    if manifest.get("version") != expected_version or not isinstance(volumes, dict) or not volumes:
        raise ValueError("backup version or volume inventory mismatch")
    bindings = manifest.get("bindings", {})
    if not isinstance(bindings, dict) or set(bindings) - {"private-config", "maintenance-transport"}:
        raise ValueError("backup private storage inventory invalid")
    if bindings:
        if home is None or backup.resolve().parent != (home / "data/backups").resolve():
            raise ValueError("private storage restore needs the exact installation home")
        admitted = {"private-config": home / "data/config"}
        if maintenance_transport_override(home):
            overlay = json.loads((home / "data/config/maintenance-transport.compose.json").read_text())
            admitted["maintenance-transport"] = Path(overlay["services"]["app"]["volumes"][0]["source"])
        for name, value in bindings.items():
            if (
                not isinstance(value, dict)
                or set(value) != {"path", "sha256"}
                or name not in admitted
                or str(admitted[name]) != value["path"]
                or sha256(backup / f"{name}.tar") != value["sha256"]
            ):
                raise ValueError("backup private storage identity changed")
    for name, digest in volumes.items():
        if (
            not VOLUME_PATTERN.fullmatch(name)
            or not name.startswith(f"{project}_")
            or sha256(backup / f"{name}.tar") != digest
        ):
            raise ValueError(f"backup checksum mismatch: {name}")
        run("docker", "volume", "inspect", name, capture=True)
    for name in volumes:
        with (backup / f"{name}.tar").open("rb") as stream:
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "-i",
                    "--mount",
                    f"type=volume,source={name},target=/data",
                    "--entrypoint",
                    "sh",
                    "postgres:16.10-alpine",
                    "-c",
                    "find /data -mindepth 1 -maxdepth 1 -exec rm -rf -- {} + && tar -C /data -xf -",
                ],
                stdin=stream,
                check=True,
            )
    for name, value in bindings.items():
        with (backup / f"{name}.tar").open("rb") as stream:
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "-i",
                    "--mount",
                    f"type=bind,source={value['path']},target=/data",
                    "--entrypoint",
                    "sh",
                    BASE_IMAGES[0],
                    "-c",
                    "find /data -mindepth 1 -maxdepth 1 -exec rm -rf -- {} + && tar -C /data -xf -",
                ],
                stdin=stream,
                check=True,
            )


def install(args: argparse.Namespace) -> None:
    home: Path = args.home.resolve()
    if home.exists() and any(home.iterdir()):
        raise ValueError("install home must be empty")
    if not PROJECT_PATTERN.fullmatch(args.project):
        raise ValueError("invalid Compose project name")
    validate_env_file(args.env_file)
    if subprocess.run(
        ["docker", "ps", "-a", "-q", "--filter", f"label=com.docker.compose.project={args.project}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip():
        raise ValueError("Compose project already owns containers")
    if subprocess.run(
        ["docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={args.project}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip():
        raise ValueError("Compose project already owns volumes")
    existing_volumes = run("docker", "volume", "ls", "--format", "{{.Name}}", capture=True).splitlines()
    if any(name.startswith(f"{args.project}_") for name in existing_volumes):
        raise ValueError("Compose project volume names are already in use")
    with tempfile.TemporaryDirectory(prefix="onefoundry-check-") as temporary:
        candidate = Path(temporary) / "release"
        manifest = unpack(args.archive, args.sha256, candidate)
        version = manifest["version"]
    release = home / "releases" / str(version)
    release.parent.mkdir(parents=True, exist_ok=True)
    unpack(args.archive, args.sha256, release)
    config_dir = home / "data/config"
    config_dir.mkdir(mode=0o700, parents=True)
    shutil.copyfile(args.env_file, config_dir / ".env")
    os.chmod(config_dir / ".env", 0o600)
    updates = home / "data/updates"
    updates.mkdir(mode=0o700)
    (updates / "install.json").write_text(json.dumps({"project": args.project}) + "\n", encoding="utf-8")
    set_current(home, release)
    run("docker", "load", "-i", str(release / "images.tar"))
    verify_loaded_images(release)
    compose(home, release, args.project, "up", "-d", "--no-build", "--pull", "never")
    health(home, release, args.project, timeout=args.health_timeout_seconds)
    print(f"Installed OneFoundry {version} at {home}")


def verify_default_services(home: Path, release: Path, project: str) -> None:
    known_services = set(compose(home, release, project, "config", "--services", capture=True).splitlines())
    if not known_services:
        raise ValueError("installed release declares no Compose services")
    active_services = set(
        run(
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--format",
            '{{.Label "com.docker.compose.service"}}',
            capture=True,
        ).splitlines()
    )
    if active_services - known_services:
        raise ValueError("installed Compose project has unsupported profile or extra services")


def update(args: argparse.Namespace) -> None:
    home: Path = args.home.resolve()
    previous, config = read_install(home)
    project = config["project"]
    verify_default_services(home, previous, project)
    with tempfile.TemporaryDirectory(prefix="onefoundry-check-") as temporary:
        manifest = unpack(args.archive, args.sha256, Path(temporary) / "release")
    version = str(manifest["version"])
    old_version = str(validate_manifest(previous)["version"])
    if tuple(map(int, version.split("."))) <= tuple(map(int, old_version.split("."))):
        raise ValueError("update version must be newer than the installed version")
    release = home / "releases" / version
    if release.exists():
        if validate_manifest(release) != manifest:
            raise ValueError("existing release directory disagrees with checked archive")
    else:
        unpack(args.archive, args.sha256, release)
    preserve_infra_status(previous, release)
    run("docker", "load", "-i", str(release / "images.tar"))
    verify_loaded_images(release)
    try:
        compose(home, previous, project, "down")
        backup = snapshot(home, previous, project)
    except BaseException:
        compose(home, previous, project, "up", "-d", "--no-build", "--pull", "never")
        health(home, previous, project, timeout=args.health_timeout_seconds)
        raise
    state_path = home / "data/updates/rollback.json"
    state = {"previous": old_version, "candidate": version, "backup": str(backup), "state": "prepared"}
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    try:
        set_current(home, release)
        compose(home, release, project, "up", "-d", "--no-build", "--pull", "never")
        health(home, release, project, timeout=args.health_timeout_seconds)
    except BaseException:
        state["state"] = "update_failed"
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        compose(home, release, project, "down")
        restore(backup, project, old_version, home=home)
        set_current(home, previous)
        compose(home, previous, project, "up", "-d", "--no-build", "--pull", "never")
        health(home, previous, project, timeout=args.health_timeout_seconds)
        state["state"] = "restored_after_failure"
        state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        raise RuntimeError("update failed; previous release and data checkpoint were restored") from None
    state["state"] = "applied"
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    print(f"Updated OneFoundry {old_version} -> {version}; backup: {backup}")


def rollback(args: argparse.Namespace) -> None:
    if not args.restore_data or not args.yes:
        raise ValueError("rollback requires --restore-data --yes because later writes will be lost")
    home: Path = args.home.resolve()
    current, config = read_install(home)
    state_path = home / "data/updates/rollback.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if (
        state.get("state") not in {"applied", "update_failed"}
        or state.get("candidate") != validate_manifest(current)["version"]
    ):
        raise ValueError("no applied update has a matching rollback point")
    previous = home / "releases" / state["previous"]
    validate_manifest(previous)
    backup = Path(state["backup"])
    if backup.resolve().parent != (home / "data/backups").resolve():
        raise ValueError("rollback checkpoint leaves the install home")
    project = config["project"]
    compose(home, current, project, "down")
    restore(backup, project, str(state["previous"]), home=home)
    set_current(home, previous)
    compose(home, previous, project, "up", "-d", "--no-build", "--pull", "never")
    health(home, previous, project, timeout=args.health_timeout_seconds)
    state["state"] = "rolled_back"
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    print(f"Rolled back OneFoundry to {state['previous']}")


def backup(args: argparse.Namespace) -> None:
    if not args.yes:
        raise ValueError("backup briefly stops the installed project; pass --yes")
    home: Path = args.home.resolve()
    release, config = read_install(home)
    project = str(config["project"])
    compose(home, release, project, "down")
    try:
        checkpoint = snapshot(home, release, project)
    finally:
        compose(home, release, project, "up", "-d", "--no-build", "--pull", "never")
        health(home, release, project, timeout=args.health_timeout_seconds)
    print(json.dumps({"backup": str(checkpoint), "version": validate_manifest(release)["version"]}))


def health_timeout_seconds(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError("health timeout must be an integer") from error
    if not 1 <= value <= 600:
        raise argparse.ArgumentTypeError("health timeout must be between 1 and 600 seconds")
    return value


def log_tail(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError("log tail must be an integer") from error
    if not 1 <= value <= 1000:
        raise argparse.ArgumentTypeError("log tail must be between 1 and 1000")
    return value


def main(default_home: Path | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "check",
            "install",
            "start",
            "stop",
            "restart",
            "update",
            "rollback",
            "status",
            "version",
            "health",
            "doctor",
            "config",
            "logs",
            "backup",
            "cli",
        ),
    )
    parser.add_argument("action", nargs="?")
    parser.add_argument("setting", nargs="?")
    parser.add_argument("value", nargs="?")
    parser.add_argument("--home", type=Path, default=default_home, required=default_home is None)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--project", default="onefoundry")
    parser.add_argument("--health-timeout-seconds", type=health_timeout_seconds, default=90)
    parser.add_argument("--restore-data", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--tail", type=log_tail, default=100)
    args = parser.parse_args()
    try:
        if args.command not in {"config", "logs", "cli"} and any(
            item is not None for item in (args.action, args.setting, args.value)
        ):
            raise ValueError("unexpected argument for this command")
        if args.command in {"check", "install", "update"} and (not args.archive or not args.sha256):
            raise ValueError("--archive and trusted --sha256 are required")
        if args.command == "check":
            with tempfile.TemporaryDirectory(prefix="onefoundry-check-") as temporary:
                manifest = unpack(args.archive, args.sha256, Path(temporary) / "release")
                installed: str | None = None
                if (args.home / "current").is_symlink():
                    release, _ = read_install(args.home.resolve())
                    installed = str(validate_manifest(release)["version"])
                available = installed is None or tuple(map(int, str(manifest["version"]).split("."))) > tuple(
                    map(int, installed.split("."))
                )
                print(
                    json.dumps(
                        {
                            "version": manifest["version"],
                            "commit": manifest["commit"],
                            "installed": installed,
                            "update_available": available,
                        }
                    )
                )
        elif args.command == "install":
            if args.env_file is None:
                raise ValueError("--env-file is required for install")
            install(args)
        elif args.command == "start":
            release, config = read_install(args.home.resolve())
            verify_loaded_images(release)
            compose(
                args.home.resolve(), release, config["project"], "up", "-d", "--no-build", "--pull", "never"
            )
            health(
                args.home.resolve(),
                release,
                config["project"],
                timeout=args.health_timeout_seconds,
            )
            print(f"Started OneFoundry {validate_manifest(release)['version']}")
        elif args.command == "stop":
            release, config = read_install(args.home.resolve())
            compose(args.home.resolve(), release, config["project"], "down")
            print(f"Stopped OneFoundry {validate_manifest(release)['version']} without deleting volumes")
        elif args.command == "restart":
            home = args.home.resolve()
            release, config = read_install(home)
            verify_loaded_images(release)
            compose(home, release, config["project"], "down")
            compose(home, release, config["project"], "up", "-d", "--no-build", "--pull", "never")
            health(home, release, config["project"], timeout=args.health_timeout_seconds)
            print(f"Restarted OneFoundry {validate_manifest(release)['version']}")
        elif args.command == "update":
            update(args)
        elif args.command == "rollback":
            rollback(args)
        elif args.command == "backup":
            backup(args)
        elif args.command == "cli":
            home = args.home.resolve()
            release, _config = read_install(home)
            executable = home / "current/onefoundry"
            if not (release / "onefoundry").is_file():
                raise ValueError("installed release does not contain the onefoundry launcher")
            if args.action == "path":
                print(executable)
            elif args.action == "install":
                if args.setting is not None or args.value is not None:
                    raise ValueError("cli install takes no extra arguments")
                target_dir = Path.home() / ".local/bin"
                target_dir.mkdir(parents=True, exist_ok=True)
                link = target_dir / "onefoundry"
                if link.is_symlink():
                    if link.readlink() != executable:
                        raise ValueError(f"another launcher already owns {link}")
                elif link.exists():
                    raise ValueError(f"another file already owns {link}")
                else:
                    link.symlink_to(executable)
                print(f"Installed OneFoundry launcher at {link}")
            else:
                raise ValueError("cli action must be install or path")
        elif args.command == "config":
            from scripts.operator_config import describe, output, set_value

            home = args.home.resolve()
            read_install(home)
            path = home / "data/config/.env"
            if args.action == "set":
                if args.setting is None or args.value is None:
                    raise ValueError("config set requires SETTING VALUE")
                output(set_value(path, args.setting, args.value))
            elif args.action in {"list", "get", "explain"}:
                if args.value is not None or (args.action != "list" and args.setting is None):
                    raise ValueError("invalid config arguments")
                output(describe(path, args.action, args.setting))
            else:
                raise ValueError("config action must be list, get, set, or explain")
        elif args.command == "doctor":
            from scripts.operator_doctor import print_report

            if not print_report(args.home.resolve()):
                raise RuntimeError("doctor found a failed check")
        elif args.command == "logs":
            if args.setting is not None or args.value is not None:
                raise ValueError("logs accepts at most one service")
            home = args.home.resolve()
            release, config = read_install(home)
            services = set(
                compose(home, release, config["project"], "config", "--services", capture=True).splitlines()
            )
            if args.action is not None and args.action not in services:
                raise ValueError("unknown Compose service")
            command = ("logs", "--no-color", "--tail", str(args.tail))
            compose(home, release, config["project"], *command, *((args.action,) if args.action else ()))
        elif args.command in {"version", "health", "status"}:
            home = args.home.resolve()
            release, config = read_install(home)
            manifest = validate_manifest(release)
            if args.command == "version":
                print(json.dumps({"version": manifest["version"], "commit": manifest["commit"]}))
                return
            if args.command == "health":
                health(home, release, config["project"], timeout=args.health_timeout_seconds)
                print(json.dumps({"ready": True, "version": manifest["version"]}))
                return
            ready = False
            readiness_error: str | None = None
            try:
                health(home, release, config["project"], timeout=min(args.health_timeout_seconds, 3))
                ready = True
            except (ValueError, OSError, subprocess.CalledProcessError, RuntimeError) as error:
                readiness_error = str(error)
            print(
                json.dumps(
                    {
                        "version": manifest["version"],
                        "release": str(release),
                        "project": config["project"],
                        "ready": ready,
                        "readiness_error": readiness_error,
                    }
                )
            )
    except (ValueError, OSError, subprocess.CalledProcessError, RuntimeError, tarfile.TarError) as error:
        print(f"onefoundry update: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
