"""Manage a small, non-secret set of installed Compose settings."""

from __future__ import annotations

import json
import os
import re
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
