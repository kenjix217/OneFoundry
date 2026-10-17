"""Offline OneFoundry extension authoring commands."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from pydantic import ValidationError

from .formats import export_plugin, inspect_foreign_folder, read_foreign_provenance, read_foreign_resources
from .models import ExtensionManifest
from .package import PackageError, build_package, canonical_manifest_json, inspect_package


def _load_manifest(path: Path) -> ExtensionManifest:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
        raise PackageError("manifest_missing_or_too_large")
    return ExtensionManifest.model_validate_json(path.read_bytes())


def _source_files(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for dirname in ("src", "skills", "tests", "resources"):
        directory = root / dirname
        if not directory.exists():
            continue
        if directory.is_symlink() or not directory.is_dir():
            raise PackageError("source_directory_invalid")
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise PackageError("source_symlink_rejected")
            if path.is_file():
                if path.stat().st_size > 2_000_000:
                    raise PackageError("source_member_too_large")
                files[path.relative_to(root).as_posix()] = path.read_bytes()
    for name in ("README.md", "LICENSE", "NOTICE"):
        path = root / name
        if path.exists():
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
                raise PackageError("source_file_invalid")
            files[name] = path.read_bytes()
    return files


def _report(value: dict[str, Any]) -> None:
    print(json.dumps(value, sort_keys=True, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="onefoundry-sdk")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="create an inert starter extension manifest")
    init.add_argument("directory", type=Path)
    init.add_argument("--id", required=True)
    init.add_argument("--name", required=True)
    validate = commands.add_parser("validate", help="validate extension manifest and source package")
    validate.add_argument("directory", type=Path)
    test = commands.add_parser("test", help="validate fixture JSON files without executing extension code")
    test.add_argument("directory", type=Path)
    build = commands.add_parser("build", help="build deterministic extension ZIP")
    build.add_argument("directory", type=Path)
    build.add_argument("--output", required=True, type=Path)
    inspect = commands.add_parser("inspect", help="inspect extension ZIP without executing it")
    inspect.add_argument("archive", type=Path)
    export = commands.add_parser("export", help="export inert Codex or Claude plugin folder")
    export.add_argument("directory", type=Path)
    export.add_argument("--format", required=True, choices=("codex-portable", "codex-legacy", "claude"))
    export.add_argument("--output", required=True, type=Path)
    foreign = commands.add_parser("import", help="inspect a foreign plugin folder as an inert draft")
    foreign.add_argument("directory", type=Path)
    foreign.add_argument("--format", required=True, choices=("codex-portable", "codex-legacy", "claude"))
    foreign.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            manifest = ExtensionManifest(id=args.id, version="0.1.0", name=args.name)
            args.directory.mkdir(parents=True, exist_ok=False)
            (args.directory / "extension.json").write_bytes(canonical_manifest_json(manifest))
            _report({"status": "created", "path": str(args.directory / "extension.json")})
        elif args.command in {"validate", "test", "build"}:
            manifest = _load_manifest(args.directory / "extension.json")
            contents = _source_files(args.directory)
            archive = build_package(manifest, contents)
            result = inspect_package(archive)
            fixture_count = 0
            if args.command == "test":
                for name, data in contents.items():
                    if name.startswith("tests/") and name.endswith(".json"):
                        value = json.loads(data)
                        if (
                            not isinstance(value, dict)
                            or not {"tool_id", "input", "expected_output"} <= value.keys()
                        ):
                            raise PackageError("invalid_test_fixture")
                        tool = next((tool for tool in manifest.tools if tool.id == value["tool_id"]), None)
                        if tool is None:
                            raise PackageError("test_fixture_unknown_tool")
                        if (
                            not isinstance(value["input"], dict)
                            or not isinstance(value["expected_output"], dict)
                            or not Draft202012Validator(tool.parameters).is_valid(value["input"])
                            or not Draft202012Validator(tool.returns).is_valid(value["expected_output"])
                        ):
                            raise PackageError("test_fixture_schema_invalid")
                        fixture_count += 1
            if args.command == "build":
                if args.output.exists():
                    raise PackageError("output_already_exists")
                args.output.write_bytes(archive)
            _report(
                {
                    "status": "valid",
                    "package_sha256": result.package_sha256,
                    "fixtures_validated": fixture_count,
                    "execution": "not_run",
                }
            )
        elif args.command == "inspect":
            if args.archive.stat().st_size > 4 * 1024 * 1024:
                raise PackageError("package_archive_too_large")
            result = inspect_package(args.archive.read_bytes())
            _report(
                {
                    "status": "valid",
                    "id": result.manifest.id,
                    "version": result.manifest.version,
                    "package_sha256": result.package_sha256,
                    "files": result.file_sha256,
                }
            )
        elif args.command == "export":
            manifest = _load_manifest(args.directory / "extension.json")
            contents = _source_files(args.directory)
            files, report = export_plugin(
                manifest,
                args.format,
                resources={name: data for name, data in contents.items() if name.startswith("resources/")},
                provenance={name: data for name, data in contents.items() if name in {"LICENSE", "NOTICE"}},
            )
            if args.output.exists():
                raise PackageError("output_already_exists")
            args.output.mkdir(parents=True)
            for name, data in files.items():
                path = args.output / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            _report({"status": "exported", "components": [item.__dict__ for item in report.components]})
        elif args.command == "import":
            manifest, report = inspect_foreign_folder(args.directory, args.format)
            resources, _ = read_foreign_resources(args.directory)
            provenance, _ = read_foreign_provenance(args.directory)
            if args.output.exists():
                raise PackageError("output_already_exists")
            args.output.mkdir(parents=True)
            (args.output / "extension.json").write_bytes(canonical_manifest_json(manifest))
            for name, data in {**resources, **provenance}.items():
                path = args.output / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            _report({"status": "draft", "components": [item.__dict__ for item in report.components]})
        return 0
    except (OSError, PackageError, ValidationError, ValueError) as exc:
        _report({"status": "error", "code": str(exc).split("\n", 1)[0][:240]})
        return 2


if __name__ == "__main__":
    sys.exit(main())
