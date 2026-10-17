"""Private packaging never invents an endpoint or includes secret configuration."""

import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts.package_mail_plugin import PACKAGE_NAME, package


def test_source_archive_has_no_claimed_tool_connection(tmp_path: Path) -> None:
    output = tmp_path / "source.zip"
    package(output)
    with ZipFile(output) as archive:
        names = archive.namelist()
        assert len(names) == 3 and all(name.startswith(f"{PACKAGE_NAME}/") for name in names)
        assert not any("mcp.json" in name for name in names)
        manifest = json.loads(archive.read(f"{PACKAGE_NAME}/plugin.json"))
        assert manifest["name"] == PACKAGE_NAME
        assert len(manifest["extensions"]["com.openai"]["interface"]["shortDescription"]) <= 30


def test_connection_archive_contains_only_supplied_verified_endpoint(tmp_path: Path) -> None:
    output = tmp_path / "connection.zip"
    package(output, verified_mcp_url="https://mail-plugin.example.test/mcp")
    with ZipFile(output) as archive:
        config = json.loads(archive.read(f"{PACKAGE_NAME}/mcp.json"))
        assert config["mcpServers"] == {
            "zoho-mail-read": {"type": "streamable-http", "url": "https://mail-plugin.example.test/mcp"}
        }
        assert b"Authorization" not in archive.read(f"{PACKAGE_NAME}/mcp.json")
    with pytest.raises(ValueError):
        package(output)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost/mcp",
        "https://token@x.test/mcp",
        "https://x.test/mcp?secret=x",
        "https://x.test/not-mcp",
    ],
)
def test_invalid_endpoint_is_not_packaged(tmp_path: Path, endpoint: str) -> None:
    output = tmp_path / "invalid.zip"
    with pytest.raises(ValueError):
        package(output, verified_mcp_url=endpoint)
    assert not output.exists()
