"""Manage a small, non-secret set of installed Compose settings."""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Setting:
    description: str
    minimum: int
    maximum: int


SETTINGS = {
    "APP_PORT": Setting("Loopback port for the OneFoundry app", 1, 65535),
    "MAILPIT_UI_PORT": Setting("Loopback port for the local mail preview", 1, 65535),
    "RUNNER_WALL_SECONDS": Setting("Maximum external runner job duration in seconds", 1, 60),
    "TEAM_MAX_ASSIGNMENTS_PER_ORDER": Setting(
        "Maximum unfinished team assignments per root work order", 1, 50
    ),
    "INPUT_FILE_MAX_PROJECT_COUNT": Setting("Maximum project input files, including staged files", 1, 100),
}
LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


def values(path: Path) -> dict[str, str | None]:
    found = configured_values(path, SETTINGS)
    for name, value in found.items():
        if value is not None:
            validate(name, value)
    return found


def configured_values(path: Path, names: Collection[str]) -> dict[str, str | None]:
    """Read only named installed values; never use ambient process overrides."""
    found: dict[str, str | None] = dict.fromkeys(names)
    seen: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = LINE.match(line)
        if match is None or match.group(1) not in names:
            continue
        name = match.group(1)
        if name in seen:
            raise ValueError(f"duplicate installed setting: {name}")
        seen.add(name)
        value = line.split("=", 1)[1].strip()
        found[name] = value
    return found


def validate(name: str, value: str) -> None:
    setting = SETTINGS.get(name)
    if setting is None:
        raise ValueError("setting is unavailable or secret; edit the private .env under operator control")
    if not re.fullmatch(r"[0-9]+", value) or not setting.minimum <= int(value) <= setting.maximum:
        raise ValueError(f"{name} must be an integer from {setting.minimum} to {setting.maximum}")


def describe(path: Path, action: str, name: str | None) -> dict[str, object]:
    current = values(path)
    if action == "list":
        return {"settings": current, "restart_required_after_set": True}
    if name not in SETTINGS:
        raise ValueError("unknown or secret setting")
    if action == "get":
        return {"setting": name, "value": current[name]}
    if action == "explain":
        setting = SETTINGS[name]
        return {
            "setting": name,
            "description": setting.description,
            "valid_range": [setting.minimum, setting.maximum],
            "configured_value": current[name],
            "restart_required_after_set": True,
        }
    raise ValueError("config action must be list, get, set, or explain")


def set_value(path: Path, name: str, value: str) -> dict[str, object]:
    validate(name, value)
    if path.is_symlink():
        raise ValueError("installed .env must be a regular file")
    values(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    replaced = False
    for index, line in enumerate(lines):
        match = LINE.match(line)
        if match is not None and match.group(1) == name:
            lines[index] = f"{name}={value}"
            replaced = True
    if not replaced:
        lines.append(f"{name}={value}")
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".env-next-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            stream.write("\n".join(lines) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        values(temporary)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {"setting": name, "value": value, "restart_required": True}


def output(data: dict[str, object]) -> None:
    print(json.dumps(data, sort_keys=True))


def installed_search_endpoint(home: Path) -> str | None:
    """Read Compose-style quoted or bare endpoint from the private installed environment."""
    return _installed_value(home, "PUBLIC_WEB_SEARCH_ENDPOINT")


def _installed_value(home: Path, name: str) -> str | None:
    raw = configured_values(home / "data/config/.env", (name,))[name]
    if raw is None:
        return None
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
        return raw[1:-1]
    return raw


INTERNAL_SEARCH_ENDPOINT = "http://searxng:8080/search"


def public_search_enabled(home: Path) -> bool:
    value = _installed_value(home, "PUBLIC_WEB_ENABLED")
    if value is None:
        return True
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise ValueError("installed PUBLIC_WEB_ENABLED must be a boolean")


def private_search_compose(home: Path, project: str) -> Path | None:
    """Accept only the operator's pinned, isolated SearXNG service."""
    path = home / "data/config/public-search/compose.json"
    if not path.exists() and not path.is_symlink():
        if public_search_enabled(home) and installed_search_endpoint(home) == INTERNAL_SEARCH_ENDPOINT:
            raise ValueError("internal public search requires private public-search/compose.json")
        return None
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o600
        or info.st_size > 8192
    ):
        raise ValueError("private public search Compose must be an owned mode-0600 regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        service = value["services"]["search"]
        image = service["image"]
        config_dir = home / "data/config/public-search"
        directory_info = config_dir.lstat()
        if (
            not stat.S_ISDIR(directory_info.st_mode)
            or directory_info.st_uid != os.getuid()
            or stat.S_IMODE(directory_info.st_mode) & 0o077
            or config_dir.resolve() != config_dir
        ):
            raise ValueError
        expected = {
            "services": {
                "search": {
                    "image": image,
                    "user": f"{os.getuid()}:{os.getgid()}",
                    "read_only": True,
                    "cap_drop": ["ALL"],
                    "security_opt": ["no-new-privileges:true"],
                    "restart": "unless-stopped",
                    "environment": {"FORCE_OWNERSHIP": "false"},
                    "tmpfs": ["/tmp:size=64m,mode=1777"],  # noqa: S108 - container-only scratch mount
                    "volumes": [
                        {
                            "type": "bind",
                            "source": str(config_dir / "config"),
                            "target": "/etc/searxng",
                            "read_only": True,
                        },
                        {"type": "bind", "source": str(config_dir / "cache"), "target": "/var/cache/searxng"},
                    ],
                    "networks": {"app": {"aliases": ["searxng"]}},
                }
            },
            "networks": {"app": {"external": True, "name": f"{project}_default"}},
        }
        if (
            value != expected
            or not isinstance(image, str)
            or not re.fullmatch(r"searxng/searxng@sha256:[0-9a-f]{64}", image)
        ):
            raise ValueError
        for name in ("config", "cache"):
            folder_info = (config_dir / name).lstat()
            if (
                not stat.S_ISDIR(folder_info.st_mode)
                or folder_info.st_uid != os.getuid()
                or stat.S_IMODE(folder_info.st_mode) & 0o077
            ):
                raise ValueError
    except (OSError, UnicodeError, ValueError, TypeError, KeyError):
        raise ValueError(
            "private public search Compose permits only the pinned isolated search service"
        ) from None
    return path
