"""Add exact host Git checkouts to OneFoundry's private read-only Compose mounts."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

REPO_DIR = Path(__file__).resolve().parents[1]
BASE_COMPOSE = REPO_DIR / "compose.yaml"
PRIVATE_COMPOSE = REPO_DIR / "compose.local-source.json"
TEMPLATE = REPO_DIR / "compose.local-source.example.json"
ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}\Z")


class LocalSourceError(Exception):
    """An actionable local repository setup failure."""


def executable(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise LocalSourceError(f"{name} is required on this computer")
    return str(Path(found).resolve())


def run_command(args: list[str], timeout_seconds: int, cwd: Path | None = None) -> str:
    result = subprocess.run(  # noqa: S603 - no shell; command and path arguments are validated by callers.
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout_seconds,
    )
    return result.stdout.strip()


def load_configuration(path: Path = PRIVATE_COMPOSE) -> tuple[dict[str, Any], str, int, int]:
    source = path if path.exists() else TEMPLATE
    try:
        config = json.loads(source.read_text())
        app = config["services"]["app"]
        root = app["environment"]["LOCAL_SOURCE_ROOT"]
        mounts = app["volumes"]
        limits = config["x-onefoundry-local-source"]
        git_timeout = limits["git_timeout_seconds"]
        compose_timeout = limits["compose_timeout_seconds"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise LocalSourceError(f"Invalid local source configuration: {source.name}") from exc
    if (
        not isinstance(root, str)
        or not root.startswith("/")
        or root == "/"
        or str(PurePosixPath(root)) != root
        or ".." in PurePosixPath(root).parts
    ):
        raise LocalSourceError("LOCAL_SOURCE_ROOT must be a dedicated absolute container directory")
    if not isinstance(mounts, list):
        raise LocalSourceError("Local repository mounts must be a list")
    if type(git_timeout) is not int or not 1 <= git_timeout <= 30:
        raise LocalSourceError("git_timeout_seconds must be an integer from 1 to 30")
    if type(compose_timeout) is not int or not 1 <= compose_timeout <= 600:
        raise LocalSourceError("compose_timeout_seconds must be an integer from 1 to 600")
    for mount in mounts:
        if not isinstance(mount, dict) or mount.get("type") != "bind" or mount.get("read_only") is not True:
            raise LocalSourceError("Every local source mount must be a read-only bind")
        source_path, target = mount.get("source"), mount.get("target")
        if (
            not isinstance(source_path, str)
            or not Path(source_path).is_absolute()
            or not isinstance(target, str)
        ):
            raise LocalSourceError("Local source mounts need absolute source and target paths")
        relative = (
            PurePosixPath(target).relative_to(PurePosixPath(root)) if target.startswith(root + "/") else None
        )
        if relative is None or len(relative.parts) != 1 or not ALIAS.fullmatch(relative.name):
            raise LocalSourceError("Local source mounts must target one named repository inside the root")
    return config, root, git_timeout, compose_timeout


def validate_checkout(raw_path: str, timeout_seconds: int) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute() or ".." in path.parts or not path.is_dir():
        raise LocalSourceError("Enter an existing absolute Git checkout directory")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise LocalSourceError("The repository path cannot be resolved") from exc
    if resolved != path or path.is_symlink():
        raise LocalSourceError("Use a real repository directory, not a symlinked path")
    if resolved in {Path("/"), Path.home().resolve()}:
        raise LocalSourceError("Choose a Git checkout, not the host root or home directory")
    try:
        found_root = run_command(
            [executable("git"), "-C", str(path), "rev-parse", "--show-toplevel"],
            timeout_seconds,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise LocalSourceError("The directory is not a readable Git checkout") from exc
    if Path(found_root).resolve() != resolved:
        raise LocalSourceError("Choose the Git checkout root, not a folder inside it")
    return resolved


def add_mount(config: dict[str, Any], root: str, checkout: Path, name: str) -> bool:
    if not ALIAS.fullmatch(name):
        raise LocalSourceError("Repository name must use letters, numbers, underscores, or dashes")
    mounts = config["services"]["app"]["volumes"]
    target = f"{root.rstrip('/')}/{name}"
    for mount in mounts:
        if Path(mount["source"]) == checkout:
            if mount["target"] == target:
                return False
            raise LocalSourceError("This checkout is already mounted under another name")
        if mount["target"] == target:
            raise LocalSourceError("That repository name is already in use; choose --name")
    mounts.append({"type": "bind", "source": str(checkout), "target": target, "read_only": True})
    return True


def compose(args: list[str], timeout_seconds: int) -> str:
    try:
        return run_command(
            [executable("docker"), "compose", "-f", str(BASE_COMPOSE), "-f", str(PRIVATE_COMPOSE), *args],
            timeout_seconds,
            REPO_DIR,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise LocalSourceError(
            f"Docker Compose could not complete {' '.join(args)}; inspect the local app before retrying"
        ) from exc


def save_and_apply(config: dict[str, Any], timeout_seconds: int) -> None:
    descriptor, temp_name = tempfile.mkstemp(prefix=".compose.local-source-", suffix=".json", dir=REPO_DIR)
    candidate = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(config, stream, indent=2)
            stream.write("\n")
        # Validate the exact candidate before changing the active private override.
        try:
            run_command(
                [
                    executable("docker"),
                    "compose",
                    "-f",
                    str(BASE_COMPOSE),
                    "-f",
                    str(candidate),
                    "config",
                    "--quiet",
                ],
                timeout_seconds,
                REPO_DIR,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise LocalSourceError(
                "Docker Compose rejected the repository mount; the running app was not changed"
            ) from exc
        os.replace(candidate, PRIVATE_COMPOSE)
    finally:
        candidate.unlink(missing_ok=True)
    compose(["up", "-d", "--no-build", "--no-deps", "app"], timeout_seconds)
    container_id = compose(["ps", "-q", "app"], timeout_seconds)
    if not container_id:
        raise LocalSourceError(
            "Compose returned without a running app container; inspect saved state before retrying"
        )
    try:
        container = json.loads(run_command([executable("docker"), "inspect", container_id], timeout_seconds))[
            0
        ]
    except (OSError, ValueError, IndexError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise LocalSourceError(
            "Could not confirm the app container mount; inspect saved state before retrying"
        ) from exc
    if container.get("State", {}).get("Running") is not True:
        raise LocalSourceError("The app container is not running after the mount change")
    root = config["services"]["app"]["environment"]["LOCAL_SOURCE_ROOT"]
    if f"LOCAL_SOURCE_ROOT={root}" not in container["Config"]["Env"]:
        raise LocalSourceError("The app container did not confirm its repository root")
    mounted = container["Mounts"]
    for expected in config["services"]["app"]["volumes"]:
        if not any(
            mount["Type"] == "bind"
            and mount["Source"] == expected["source"]
            and mount["Destination"] == expected["target"]
            and mount["RW"] is False
            for mount in mounted
        ):
            raise LocalSourceError("The app container did not confirm every read-only repository mount")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Add local Git repositories to OneFoundry without an AI assistant"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("add", help="Mount one checkout read-only and restart only the OneFoundry app")
    add.add_argument("path", help="Absolute path to the Git checkout root on this computer")
    add.add_argument("--name", help="Name shown in OneFoundry, if the folder name is not suitable")
    commands.add_parser("list", help="Show configured repository mounts")
    args = parser.parse_args()
    try:
        config, root, git_timeout, compose_timeout = load_configuration()
        if args.command == "list":
            mounts = config["services"]["app"]["volumes"]
            for mount in mounts:
                print(f"{PurePosixPath(mount['target']).name}: {mount['source']}")
            if not mounts:
                print("No local repositories configured")
            return 0
        checkout = validate_checkout(args.path, git_timeout)
        name = args.name or checkout.name
        candidate = copy.deepcopy(config)
        added = add_mount(candidate, root, checkout, name)
        save_and_apply(candidate, compose_timeout)
        if not added:
            print(f"{name} was already configured; confirmed its read-only mount is active")
            return 0
        print(f"Confirmed read-only mount for {name}. Refresh OneFoundry, then select it for the project.")
        return 0
    except LocalSourceError as exc:
        parser.exit(1, f"Local repository setup: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
