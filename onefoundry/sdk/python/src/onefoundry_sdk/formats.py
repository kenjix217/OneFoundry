"""Inert projections for Codex and Claude plugin folders."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .models import ConnectionTemplate, ExtensionManifest, OperationSpec, SkillSpec
from .package import PackageError, _check_member

PluginFormat = Literal["codex-portable", "codex-legacy", "claude"]
ComponentStatus = Literal["supported", "inactive", "rejected"]


@dataclass(frozen=True)
class ComponentReport:
    component: str
    status: ComponentStatus
    reason: str


@dataclass(frozen=True)
class PortabilityReport:
    format: PluginFormat
    components: tuple[ComponentReport, ...]


def export_plugin(
    manifest: ExtensionManifest,
    format: PluginFormat,
    *,
    resources: dict[str, bytes] | None = None,
    provenance: dict[str, bytes] | None = None,
) -> tuple[dict[str, bytes], PortabilityReport]:
    """Export portable instructions and metadata; execution bindings need host setup."""
    if format not in {"codex-portable", "codex-legacy", "claude"}:
        raise ValueError("unsupported_plugin_format")
    reports: list[ComponentReport] = []
    files: dict[str, bytes] = {}
    descriptor: dict[str, Any] = {
        "name": manifest.id,
        "version": manifest.version,
        "description": manifest.description,
    }
    if format == "codex-portable":
        descriptor["$schema"] = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
        descriptor["extensions"] = {
            "com.openai": {
                "interface": {
                    "displayName": manifest.name,
                    "shortDescription": manifest.description[:30] or manifest.name[:30],
                    "longDescription": manifest.description or manifest.name,
                    "category": "Productivity",
                    "defaultPrompt": "Use this plugin's procedures when relevant.",
                }
            }
        }
        descriptor_path = "plugin.json"
    elif format == "codex-legacy":
        descriptor_path = ".codex-plugin/plugin.json"
    else:
        descriptor_path = ".claude-plugin/plugin.json"
    files[descriptor_path] = (json.dumps(descriptor, sort_keys=True, indent=2) + "\n").encode()
    for skill in manifest.skills:
        prefix = "skills/" + skill.id + "/SKILL.md"
        files[prefix] = _portable_skill_text(skill).encode("utf-8")
        reports.append(ComponentReport(f"skill:{skill.id}", "supported", "Markdown procedure exported"))
    for name, data in sorted((resources or {}).items()):
        if not name.startswith("resources/"):
            raise PackageError("foreign_resource_path_invalid")
        _check_member(name, data)
        files[name] = data
        reports.append(ComponentReport(name, "supported", "Static resource exported"))
    for name, data in sorted((provenance or {}).items()):
        if name not in {"LICENSE", "NOTICE"} or len(data) > 256_000:
            raise PackageError("foreign_provenance_invalid")
        _check_member(name, data)
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PackageError("foreign_provenance_invalid") from exc
        files[name] = data
        reports.append(ComponentReport(name, "supported", "Original provenance text exported"))
    remote_servers = {
        connection.id: {
            "type": "http",
            "url": connection.endpoint_hint,
            "tools": [operation.id for operation in connection.operations],
            "resources": connection.resource_refs,
        }
        for connection in manifest.connections
        if connection.kind == "mcp" and connection.endpoint_hint is not None
    }
    if remote_servers:
        files["mcp.json" if format == "codex-portable" else ".mcp.json"] = (
            json.dumps({"mcpServers": remote_servers}, sort_keys=True, indent=2) + "\n"
        ).encode()
    for connection in manifest.connections:
        reports.append(
            ComponentReport(
                f"connection:{connection.id}",
                "inactive",
                "Remote template exported for host review"
                if connection.id in remote_servers
                else "Configure endpoint and credentials in destination host",
            )
        )
    for tool in manifest.tools:
        reports.append(
            ComponentReport(
                f"tool:{tool.id}", "inactive", "OneFoundry tool admission and execution do not transfer"
            )
        )
    return files, PortabilityReport(format, tuple(reports))


def _portable_skill_text(skill: SkillSpec) -> str:
    if skill.content.startswith("---\n"):
        return skill.content
    description = skill.description or skill.name
    return (
        f"---\nname: {json.dumps(skill.id)}\ndescription: {json.dumps(description)}\n---\n\n" + skill.content
    )


def _remote_mcp_templates(root: Path, reports: list[ComponentReport]) -> list[ConnectionTemplate]:
    """Copy only static remote declarations; all imported bindings stay inactive."""
    templates: list[ConnectionTemplate] = []
    for filename in ("mcp.json", ".mcp.json"):
        path = root / filename
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 128_000:
            raise PackageError("foreign_mcp_descriptor_invalid")
        raw = path.read_bytes()
        _check_member("README.md", raw)
        try:
            document = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise PackageError("foreign_mcp_descriptor_invalid") from exc
        if not isinstance(document, dict):
            raise PackageError("foreign_mcp_descriptor_invalid")
        servers = document.get("mcpServers", document.get("servers"))
        if not isinstance(servers, dict) or len(servers) > 50:
            reports.append(ComponentReport(filename, "rejected", "Unsupported MCP descriptor shape"))
            continue
        for source_id, spec in sorted(servers.items()):
            component = f"{filename}:{source_id}"
            if not isinstance(source_id, str) or not isinstance(spec, dict):
                reports.append(ComponentReport(component, "rejected", "Invalid MCP server entry"))
                continue
            if set(spec) - {"type", "url", "tools", "resources"} or spec.get("type") not in {
                "http",
                "streamable-http",
            }:
                reports.append(
                    ComponentReport(
                        component, "rejected", "Local or executable MCP configuration is unsupported"
                    )
                )
                continue
            tool_refs = spec.get("tools", [])
            resource_refs = spec.get("resources", [])
            if (
                not isinstance(tool_refs, list)
                or not all(isinstance(item, str) for item in tool_refs)
                or not isinstance(resource_refs, list)
                or not all(isinstance(item, str) for item in resource_refs)
            ):
                reports.append(
                    ComponentReport(component, "rejected", "Invalid operation or resource references")
                )
                continue
            try:
                connection = ConnectionTemplate(
                    id=source_id.replace("-", "_"),
                    name=source_id,
                    kind="mcp",
                    endpoint_hint=spec["url"],
                    operations=[OperationSpec(id=item) for item in tool_refs],
                    resource_refs=resource_refs,
                )
                _check_member("README.md", connection.model_dump_json().encode())
            except (KeyError, ValueError):
                reports.append(
                    ComponentReport(component, "rejected", "Invalid or sensitive remote MCP template")
                )
                continue
            if any(existing.id == connection.id for existing in templates):
                reports.append(ComponentReport(component, "rejected", "Duplicate connection identifier"))
                continue
            templates.append(connection)
            reports.append(
                ComponentReport(
                    component, "inactive", "Remote template retained; configure and approve in OneFoundry"
                )
            )
    return templates


def read_foreign_resources(root: Path) -> tuple[dict[str, bytes], list[ComponentReport]]:
    """Retain bounded UTF-8 documents; reject executable and sensitive resources."""
    directory = root / "resources"
    if not directory.exists():
        return {}, []
    if directory.is_symlink() or not directory.is_dir():
        raise PackageError("foreign_resource_directory_invalid")
    resources: dict[str, bytes] = {}
    reports: list[ComponentReport] = []
    total = 0
    paths = sorted(directory.rglob("*"))
    if len(paths) > 64:
        raise PackageError("foreign_resource_count_exceeded")
    for path in paths:
        if path.is_symlink():
            raise PackageError("foreign_resource_symlink_rejected")
        if path.is_dir():
            continue
        name = path.relative_to(root).as_posix()
        if not path.is_file() or path.stat().st_size > 2_000_000:
            reports.append(ComponentReport(name, "rejected", "Unsupported or oversized resource"))
            continue
        content = path.read_bytes()
        try:
            _check_member(name, content)
        except PackageError:
            reports.append(ComponentReport(name, "rejected", "Unsupported or sensitive resource"))
            continue
        total += len(content)
        if total > 4_000_000:
            raise PackageError("foreign_resource_bytes_exceeded")
        resources[name] = content
        reports.append(ComponentReport(name, "supported", "Imported as inert static resource"))
    return resources, reports


def read_foreign_provenance(root: Path) -> tuple[dict[str, bytes], list[ComponentReport]]:
    """Retain original license notices without inventing attribution."""
    files: dict[str, bytes] = {}
    reports: list[ComponentReport] = []
    for name in ("LICENSE", "NOTICE"):
        path = root / name
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 256_000:
            reports.append(ComponentReport(name, "rejected", "Unsafe or oversized provenance file"))
            continue
        data = path.read_bytes()
        try:
            _check_member(name, data)
            data.decode("utf-8")
        except (PackageError, UnicodeDecodeError):
            reports.append(ComponentReport(name, "rejected", "Sensitive or non-text provenance file"))
            continue
        files[name] = data
        reports.append(ComponentReport(name, "supported", "Original provenance text retained"))
    return files, reports


def inspect_foreign_folder(root: Path, format: PluginFormat) -> tuple[ExtensionManifest, PortabilityReport]:
    """Import safe skill text as an inert draft; report all unsupported components."""
    if format not in {"codex-portable", "codex-legacy", "claude"}:
        raise ValueError("unsupported_plugin_format")
    descriptor = {
        "codex-portable": root / "plugin.json",
        "codex-legacy": root / ".codex-plugin" / "plugin.json",
        "claude": root / ".claude-plugin" / "plugin.json",
    }[format]
    if not descriptor.is_file() or descriptor.is_symlink() or descriptor.stat().st_size > 128_000:
        raise PackageError("foreign_plugin_descriptor_missing_or_invalid")
    raw = descriptor.read_bytes()
    _check_member("README.md", raw)
    try:
        metadata = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise PackageError("foreign_plugin_descriptor_invalid") from exc
    if not isinstance(metadata, dict):
        raise PackageError("foreign_plugin_descriptor_invalid")
    name = metadata.get("name")
    if not isinstance(name, str):
        raise PackageError("foreign_plugin_name_invalid")
    version = metadata.get("version", "0.1.0")
    description = metadata.get("description", "")
    if not isinstance(version, str) or not isinstance(description, str):
        raise PackageError("foreign_plugin_metadata_invalid")
    reports: list[ComponentReport] = []
    skills: list[SkillSpec] = []
    skills_root = root / "skills"
    if skills_root.exists():
        if skills_root.is_symlink() or not skills_root.is_dir():
            raise PackageError("foreign_skills_directory_invalid")
        for item in sorted(skills_root.iterdir()):
            if item.is_symlink() or not item.is_dir():
                raise PackageError("foreign_skill_path_invalid")
            path = item / "SKILL.md"
            if not path.is_file() or path.is_symlink() or path.stat().st_size > 16000:
                reports.append(
                    ComponentReport(f"skill:{item.name}", "rejected", "Missing or oversized SKILL.md")
                )
                continue
            content = path.read_bytes()
            _check_member("README.md", content)
            try:
                skills.append(
                    SkillSpec(
                        id=item.name.replace("-", "_"),
                        name=item.name,
                        content=content.decode(),
                        required_tools=[],
                    )
                )
            except ValueError:
                reports.append(ComponentReport(f"skill:{item.name}", "rejected", "Invalid skill metadata"))
                continue
            reports.append(
                ComponentReport(f"skill:{item.name}", "supported", "Imported as inert Markdown draft")
            )
    connections = _remote_mcp_templates(root, reports)
    _, resource_reports = read_foreign_resources(root)
    reports.extend(resource_reports)
    _, provenance_reports = read_foreign_provenance(root)
    reports.extend(provenance_reports)
    for component in ("hooks", "agents", "commands", "scripts"):
        if (root / component).exists():
            reports.append(
                ComponentReport(
                    component, "inactive", "Requires separate host review; never executed during import"
                )
            )
    manifest = ExtensionManifest(
        id=name,
        version=version,
        name=name,
        description=description,
        skills=skills,
        connections=connections,
    )
    return manifest, PortabilityReport(format, tuple(reports))
