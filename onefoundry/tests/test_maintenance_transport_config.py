"""Installed updates retain only the explicitly configured private worker transport."""

import json
import os
from pathlib import Path
from typing import Any

import pytest

from scripts.onefoundry_update import maintenance_transport_override


def configured(tmp_path: Path) -> tuple[Path, dict[str, Any], Path]:
    spool = tmp_path / "spool"
    spool.mkdir()
    spool.chmod(0o2770)
    for name in ("jobs", "claims", "receipts", "artifacts"):
        (spool / name).mkdir()
        (spool / name).chmod(0o2770)
    path = tmp_path / "data/config/maintenance-transport.compose.json"
    path.parent.mkdir(parents=True)
    value = {
        "services": {
            "app": {
                "group_add": [str(os.getgid())],
                "volumes": [
                    {
                        "type": "bind",
                        "source": str(spool),
                        "target": "/runner-spool/external",
                        "read_only": False,
                    }
                ],
            }
        }
    }
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path, value, spool


def test_absent_and_valid_transport(tmp_path: Path) -> None:
    assert maintenance_transport_override(tmp_path) == []
    path, _, _ = configured(tmp_path)
    assert maintenance_transport_override(tmp_path) == ["-f", str(path)]


@pytest.mark.parametrize("mutation", ["mode", "directory", "symlink", "service", "mount", "group"])
def test_rejects_broader_access_or_public_transport(tmp_path: Path, mutation: str) -> None:
    path, value, spool = configured(tmp_path)
    app = value["services"]["app"]
    if mutation == "mode":
        path.chmod(0o644)
    elif mutation == "directory":
        (spool / "jobs").chmod(0o777)
    elif mutation == "symlink":
        (spool / "jobs").rmdir()
        (spool / "jobs").symlink_to(spool / "claims")
    elif mutation == "service":
        app["privileged"] = True
    elif mutation == "mount":
        app["volumes"][0]["target"] = "/var/run/docker.sock"
    else:
        app["group_add"] = ["0"]
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        maintenance_transport_override(tmp_path)
