"""Read-only checks for an installed OneFoundry Compose project."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from scripts import onefoundry_update as ops
from scripts.operator_config import values
from scripts.release_bundle import validate_manifest


def report(home: Path) -> dict[str, Any]:
    checks: dict[str, dict[str, object]] = {}

    def check(name: str, operation: Callable[[], object]) -> None:
        try:
            detail = operation()
            checks[name] = {"ok": True, "detail": detail}
        except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as error:
            checks[name] = {"ok": False, "detail": str(error)}

    try:
        release, install = ops.read_install(home)
    except (ValueError, OSError, RuntimeError, subprocess.CalledProcessError) as error:
        return {"ok": False, "checks": {"install": {"ok": False, "detail": str(error)}}}
    project = str(install["project"])
    manifest = validate_manifest(release)
    checks["install"] = {
        "ok": True,
        "detail": {"version": manifest["version"], "commit": manifest["commit"], "project": project},
    }
    check("config", lambda: values(home / "data/config/.env"))

    def loaded_images() -> str:
        ops.verify_loaded_images(release)
        return "loaded image IDs match manifest"

    check("images", loaded_images)
    check("volumes", lambda: ops.volume_names(home, release, project))

    required = {"app", "db", "mailpit", "runner", "external-runner", "input-file-worker"}

    def running_services() -> list[str]:
        running = set(
            ops.compose(
                home, release, project, "ps", "--services", "--status", "running", capture=True
            ).splitlines()
        )
        missing = required - running
        if missing:
            raise RuntimeError("required services are not running: " + ", ".join(sorted(missing)))
        return sorted(running)

    check("services", running_services)

    def runtime_images() -> str:
        refs = manifest["images"]
        ids = manifest["image_ids"]
        assert isinstance(refs, dict) and isinstance(ids, dict)
        for service, target in (
            ("app", "runtime"),
            ("runner", "runner"),
            ("input-file-worker", "input-worker"),
        ):
            container = ops.compose(home, release, project, "ps", "-q", service, capture=True)
            if not container:
                raise RuntimeError(f"{service} container is absent")
            actual = ops.run("docker", "inspect", "--format", "{{.Image}}", container, capture=True)
            if actual != ids[refs[target]]:
                raise RuntimeError(f"{service} container image differs from release manifest")
        return "running app, runner, and input worker image IDs match manifest"

    check("runtime_images", runtime_images)

    def readiness() -> str:
        ops.health(home, release, project, timeout=3)
        return "ready"

    check("readiness", readiness)
    return {"ok": all(item["ok"] for item in checks.values()), "checks": checks}


def print_report(home: Path) -> bool:
    result = report(home)
    print(json.dumps(result, sort_keys=True))
    return bool(result["ok"])
