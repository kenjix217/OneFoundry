"""Contract tests for inert, reproducible extension packaging."""

from __future__ import annotations

import io
import json
import stat
import zipfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from onefoundry_sdk import ExtensionManifest, PackageError, build_package, inspect_package, tool
from onefoundry_sdk.cli import main
from onefoundry_sdk.formats import export_plugin, inspect_foreign_folder


def sample_manifest() -> ExtensionManifest:
    return ExtensionManifest.model_validate(
        {
            "id": "demo-tools",
            "version": "1.2.3",
            "name": "Demo tools",
            "connections": [
                {
                    "id": "weather",
                    "name": "Weather",
                    "kind": "http",
                    "operations": [{"id": "lookup", "method": "GET", "path": "/v1/lookup"}],
                }
            ],
            "tools": [
                {
                    "id": "sum_values",
                    "description": "Sum values",
                    "parameters": {"type": "object", "properties": {}},
                    "returns": {"type": "object", "properties": {}},
                    "effect": "pure",
                    "runtime": "python",
                    "entrypoint": "calculator:sum_values",
                },
                {
                    "id": "weather_read",
                    "description": "Read weather",
                    "parameters": {"type": "object", "properties": {}},
                    "returns": {"type": "object", "properties": {}},
                    "effect": "read",
                    "runtime": "http",
                    "connection": "weather",
                    "operation": "lookup",
                },
            ],
            "skills": [
                {
                    "id": "weather_help",
                    "name": "Weather help",
                    "content": "Use weather_read for forecasts.",
                    "required_tools": ["weather_read"],
                }
            ],
        }
    )


def test_deterministic_round_trip_and_exact_hashes() -> None:
    manifest = sample_manifest()
    files = {"src/calculator.py": b"def sum_values(context, values):\n    return {'value': sum(values)}\n"}
    first = build_package(manifest, files)
    assert first == build_package(manifest, dict(reversed(list(files.items()))))
    inspected = inspect_package(first)
    assert inspected.manifest == manifest
    assert len(inspected.package_sha256) == 64
    assert inspected.file_sha256["extension.json"]
    assert inspected.manifest_json.endswith(b"\n")


@pytest.mark.parametrize("name", ["../escape.py", "/abs.py", "src/../evil.py", "src//evil.py", "src/bad.sh"])
def test_build_rejects_unsafe_or_unsupported_members(name: str) -> None:
    with pytest.raises(PackageError):
        build_package(ExtensionManifest(id="demo", version="1.0.0", name="Demo"), {name: b"x"})


def test_inspector_rejects_duplicate_and_symlink_members() -> None:
    for duplicate in (False, True):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as archive:
            archive.writestr("extension.json", b'{"id":"demo","version":"1.0.0","name":"Demo"}')
            if duplicate:
                archive.writestr("extension.json", b"{}")
            else:
                member = zipfile.ZipInfo("src/payload.py")
                member.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(member, "elsewhere")
        with pytest.raises(PackageError):
            inspect_package(out.getvalue())


def test_manifest_rejects_unknown_fields_and_tool_reference() -> None:
    with pytest.raises(ValidationError):
        ExtensionManifest.model_validate(
            {"id": "demo", "version": "1.0.0", "name": "Demo", "hooks": ["shell"]}
        )


def test_json_schema_checks_reject_invalid_and_external_refs() -> None:
    for properties in ({"x": {"type": "impossible"}}, {"x": {"$ref": "https://example.com/schema"}}):
        with pytest.raises(ValidationError):
            ExtensionManifest.model_validate(
                {
                    "id": "demo",
                    "version": "1.0.0",
                    "name": "Demo",
                    "tools": [
                        {
                            "id": "inspect",
                            "description": "Inspect",
                            "parameters": {"type": "object", "properties": properties},
                            "returns": {"type": "object", "properties": {}},
                            "effect": "pure",
                            "runtime": "python",
                            "entrypoint": "inspector:inspect",
                        }
                    ],
                }
            )
    with pytest.raises(ValidationError):
        ExtensionManifest.model_validate(
            {
                "id": "demo",
                "version": "1.0.0",
                "name": "Demo",
                "skills": [{"id": "x", "name": "X", "content": "run", "required_tools": ["unknown"]}],
            }
        )


def test_secret_like_package_value_rejected() -> None:
    with pytest.raises(PackageError, match="secret_like_value"):
        build_package(
            ExtensionManifest(id="demo", version="1.0.0", name="Demo"),
            {"README.md": b"api_key = abcdefghijklmnop"},
        )


def test_missing_python_source_rejected() -> None:
    with pytest.raises(PackageError, match="source_missing"):
        inspect_package(build_package(sample_manifest()))


def test_decorator_only_labels_function() -> None:
    @tool("sum_values")
    def add(value: int) -> int:
        return value + 1

    assert add.__onefoundry_tool_id__ == "sum_values"  # type: ignore[attr-defined]
    assert add(3) == 4


def test_format_export_reports_inactive_execution(tmp_path: Path) -> None:
    manifest = sample_manifest()
    for format_name in ("codex-portable", "codex-legacy", "claude"):
        files, report = export_plugin(manifest, format_name)
        assert any(path.endswith("plugin.json") for path in files)
        assert files["skills/weather_help/SKILL.md"].startswith(b"---\nname:")
        assert manifest.skills[0].content.encode() in files["skills/weather_help/SKILL.md"]
        assert {item.status for item in report.components} == {"supported", "inactive"}
    root = tmp_path / "foreign"
    (root / "skills" / "weather_help").mkdir(parents=True)
    (root / "plugin.json").write_text(json.dumps({"name": "foreign", "version": "1.0.0"}))
    (root / "skills" / "weather_help" / "SKILL.md").write_text("Read weather")
    imported, report = inspect_foreign_folder(root, "codex-portable")
    assert imported.skills[0].id == "weather_help"
    assert report.components[0].status == "supported"


def test_foreign_remote_mcp_template_retained_without_execution(tmp_path: Path) -> None:
    (tmp_path / "plugin.json").write_text(json.dumps({"name": "foreign", "version": "1.0.0"}))
    (tmp_path / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "records": {
                        "type": "http",
                        "url": "https://example.org/mcp",
                        "tools": ["lookup"],
                        "resources": ["mcp://example.org/catalog"],
                    },
                    "local": {"command": "python", "args": ["server.py"]},
                }
            }
        )
    )
    imported, report = inspect_foreign_folder(tmp_path, "codex-portable")
    assert len(imported.connections) == 1
    connection = imported.connections[0]
    assert connection.endpoint_hint == "https://example.org/mcp"
    assert [item.id for item in connection.operations] == ["lookup"]
    assert connection.resource_refs == ["mcp://example.org/catalog"]
    assert imported.tools == []
    assert [(item.component, item.status) for item in report.components] == [
        ("mcp.json:local", "rejected"),
        ("mcp.json:records", "inactive"),
    ]
    assert inspect_package(build_package(imported)).manifest.connections == imported.connections
    files, export_report = export_plugin(imported, "codex-portable")
    assert json.loads(files["mcp.json"])["mcpServers"]["records"]["tools"] == ["lookup"]
    assert export_report.components[0].status == "inactive"


def test_foreign_static_resources_round_trip_and_executables_rejected(tmp_path: Path) -> None:
    (tmp_path / "plugin.json").write_text(json.dumps({"name": "foreign", "version": "1.0.0"}))
    (tmp_path / "resources").mkdir()
    (tmp_path / "resources" / "guide.md").write_text("A plain guide.\n")
    (tmp_path / "resources" / "run.sh").write_text("exit 1\n")
    (tmp_path / "LICENSE").write_text("MIT License\nOriginal author retained.\n")
    (tmp_path / "NOTICE").write_text("Third-party notices remain intact.\n")
    imported, report = inspect_foreign_folder(tmp_path, "codex-portable")
    assert ("resources/guide.md", "supported") in [
        (item.component, item.status) for item in report.components
    ]
    assert ("resources/run.sh", "rejected") in [(item.component, item.status) for item in report.components]
    assert ("LICENSE", "supported") in [(item.component, item.status) for item in report.components]
    assert ("NOTICE", "supported") in [(item.component, item.status) for item in report.components]
    from onefoundry_sdk.formats import read_foreign_provenance, read_foreign_resources

    resources, _ = read_foreign_resources(tmp_path)
    provenance, _ = read_foreign_provenance(tmp_path)
    assert resources == {"resources/guide.md": b"A plain guide.\n"}
    assert provenance == {
        "LICENSE": b"MIT License\nOriginal author retained.\n",
        "NOTICE": b"Third-party notices remain intact.\n",
    }
    archive = build_package(imported, {**resources, **provenance})
    assert "resources/guide.md" in inspect_package(archive).file_sha256
    assert "LICENSE" in inspect_package(archive).file_sha256
    files, _ = export_plugin(imported, "claude", resources=resources, provenance=provenance)
    assert files["resources/guide.md"] == b"A plain guide.\n"
    assert files["LICENSE"] == provenance["LICENSE"]
    assert files["NOTICE"] == provenance["NOTICE"]
    assert "author" not in json.loads(files[".claude-plugin/plugin.json"])
    portable, _ = export_plugin(imported, "codex-portable", provenance=provenance)
    assert "developerName" not in json.loads(portable["plugin.json"])["extensions"]["com.openai"]["interface"]
    drafted = tmp_path / "drafted"
    assert main(["import", str(tmp_path), "--format", "codex-portable", "--output", str(drafted)]) == 0
    assert (drafted / "resources" / "guide.md").read_bytes() == resources["resources/guide.md"]
    assert (drafted / "LICENSE").read_bytes() == provenance["LICENSE"]
    assert (drafted / "NOTICE").read_bytes() == provenance["NOTICE"]
    exported = tmp_path / "exported"
    assert main(["export", str(drafted), "--format", "claude", "--output", str(exported)]) == 0
    assert (exported / "resources" / "guide.md").read_bytes() == resources["resources/guide.md"]
    assert (exported / "LICENSE").read_bytes() == provenance["LICENSE"]
    assert (exported / "NOTICE").read_bytes() == provenance["NOTICE"]


def test_foreign_provenance_non_utf8_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "plugin.json").write_text(json.dumps({"name": "foreign", "version": "1.0.0"}))
    (tmp_path / "LICENSE").write_bytes(b"\xff")
    imported, report = inspect_foreign_folder(tmp_path, "codex-portable")
    assert imported.id == "foreign"
    assert ("LICENSE", "rejected") in [(item.component, item.status) for item in report.components]


def test_foreign_provenance_symlink_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "plugin.json").write_text(json.dumps({"name": "foreign", "version": "1.0.0"}))
    (tmp_path / "LICENSE").write_text("Original notice\n")
    (tmp_path / "NOTICE").symlink_to(tmp_path / "LICENSE")
    from onefoundry_sdk.formats import read_foreign_provenance

    files, report = read_foreign_provenance(tmp_path)
    assert files == {"LICENSE": b"Original notice\n"}
    assert ("NOTICE", "rejected") in [(item.component, item.status) for item in report]


def test_foreign_mcp_secret_or_url_query_is_not_imported(tmp_path: Path) -> None:
    (tmp_path / "plugin.json").write_text(json.dumps({"name": "foreign", "version": "1.0.0"}))
    (tmp_path / "mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"records": {"type": "http", "url": "https://example.org/mcp?token=hidden"}}}
        )
    )
    imported, report = inspect_foreign_folder(tmp_path, "codex-portable")
    assert imported.connections == []
    assert report.components[0].status == "rejected"


def test_cli_validate_and_build(tmp_path: Path) -> None:
    root = tmp_path / "extension"
    root.mkdir()
    (root / "src").mkdir()
    (root / "extension.json").write_bytes(json.dumps(sample_manifest().model_dump(mode="json")).encode())
    (root / "src" / "calculator.py").write_text("def sum_values(context, values):\n    return {}\n")
    assert main(["validate", str(root)]) == 0
    output = tmp_path / "output.zip"
    assert main(["build", str(root), "--output", str(output)]) == 0
    assert inspect_package(output.read_bytes()).manifest.id == "demo-tools"


def test_cli_test_checks_fixture_input_and_output_schemas(tmp_path: Path) -> None:
    root = tmp_path / "extension"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    manifest = sample_manifest()
    manifest.tools[0].parameters = {
        "type": "object",
        "properties": {"value": {"type": "integer"}},
        "required": ["value"],
        "additionalProperties": False,
    }
    manifest.tools[0].returns = {
        "type": "object",
        "properties": {"answer": {"type": "integer"}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    (root / "extension.json").write_bytes(json.dumps(manifest.model_dump(mode="json")).encode())
    (root / "src" / "calculator.py").write_text("def sum_values(context, value): return {'answer': value}\n")
    fixture = root / "tests" / "sum.json"
    fixture.write_text(
        json.dumps({"tool_id": "sum_values", "input": {"value": 4}, "expected_output": {"answer": 4}})
    )
    assert main(["test", str(root)]) == 0
    fixture.write_text(
        json.dumps({"tool_id": "sum_values", "input": {"value": "bad"}, "expected_output": {"answer": 4}})
    )
    assert main(["test", str(root)]) == 2
    fixture.write_text(
        json.dumps({"tool_id": "sum_values", "input": {"value": 4}, "expected_output": {"answer": "bad"}})
    )
    assert main(["test", str(root)]) == 2
