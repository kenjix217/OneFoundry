"""The portable package never invents a live connection or carries credentials."""

import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts.package_memory_plugin import PACKAGE_NAME, package


def test_private_catalog_resolves_the_existing_memory_plugin() -> None:
    root = Path(__file__).parents[1]
    catalog = json.loads((root / ".agents/plugins/marketplace.json").read_text())
    assert catalog["name"] == "onefoundry-local"
    assert len(catalog["plugins"]) == 1
    entry = catalog["plugins"][0]
    source = root / entry["source"]["path"]
    assert source.resolve().is_relative_to(root.resolve())
    manifest = json.loads((source / "plugin.json").read_text())
    assert entry["name"] == manifest["name"] == PACKAGE_NAME
    assert entry["policy"] == {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}
    assert (source / "skills/company-memory/SKILL.md").is_file()


def test_source_and_verified_connection_packages(tmp_path: Path) -> None:
    source = tmp_path / "source.zip"
    package(source)
    with ZipFile(source) as archive:
        assert len(archive.namelist()) == 4
        assert not any(name.endswith("mcp.json") for name in archive.namelist())
        manifest = json.loads(archive.read(f"{PACKAGE_NAME}/plugin.json"))
        assert len(manifest["extensions"]["com.openai"]["interface"]["shortDescription"]) <= 30
    connection = tmp_path / "connection.zip"
    package(connection, verified_mcp_url="https://memory.example.test/mcp")
    with ZipFile(connection) as archive:
        config = json.loads(archive.read(f"{PACKAGE_NAME}/mcp.json"))
        assert config["mcpServers"][PACKAGE_NAME] == {
            "type": "streamable-http",
            "url": "https://memory.example.test/mcp",
        }
        assert "authorization" not in json.dumps(config)
    with pytest.raises(ValueError):
        package(source)


def test_local_key_connection_packages_no_private_config(tmp_path: Path) -> None:
    connection = tmp_path / "local.zip"
    private = tmp_path / "private.json"
    private.write_text("never package this private configuration")
    package(connection, local_client_config=private)
    with ZipFile(connection) as archive:
        config = json.loads(archive.read(f"{PACKAGE_NAME}/mcp.json"))
        entry = config["mcpServers"][PACKAGE_NAME]
        assert entry["type"] == "stdio" and entry["args"][-1] == str(private)
        assert not any(b"never package this" in archive.read(name) for name in archive.namelist())
    with pytest.raises(ValueError):
        package(tmp_path / "invalid.zip", local_client_config=Path("relative.json"))
    with pytest.raises(ValueError):
        package(
            tmp_path / "both.zip",
            local_client_config=private,
            verified_mcp_url="https://memory.example.test/mcp",
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://memory.example.test/mcp",
        "https://user:secret@example.test/mcp",
        "https://memory.example.test/mcp?token=secret",
        "https://memory.example.test/mcp#fragment",
    ],
)
def test_package_rejects_unsafe_urls(tmp_path: Path, url: str) -> None:
    with pytest.raises(ValueError):
        package(tmp_path / "unsafe.zip", verified_mcp_url=url)
