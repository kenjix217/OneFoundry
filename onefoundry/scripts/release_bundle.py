"""Build and verify an offline OneFoundry container release bundle."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from time import monotonic
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
TARGETS = ("runtime", "runner", "input-worker")
BASE_IMAGES = ("postgres:16.10-alpine", "axllent/mailpit:v1.27.9")
VERSION_PATTERN = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)")
LEGACY_FILES = frozenset(
    {
        "VERSION",
        ".env.example",
        "compose.yaml",
        "compose.release.yaml",
        "images.tar",
        "scripts/__init__.py",
        "scripts/release_bundle.py",
        "scripts/onefoundry_update.py",
    }
)
OPERATOR_FILES = frozenset({"onefoundry", "scripts/operator_config.py", "scripts/operator_doctor.py"})
LICENSE_FILE = frozenset({"LICENSE"})


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_version(root: Path = ROOT) -> str:
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError("VERSION must be a numeric major.minor.patch")
    from tomllib import loads

    project = loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    lock = loads((root / "uv.lock").read_text(encoding="utf-8"))
    frontend = json.loads((root / "frontend/package.json").read_text(encoding="utf-8"))
    frontend_lock = json.loads((root / "frontend/package-lock.json").read_text(encoding="utf-8"))
    locked_project = next(row for row in lock["package"] if row["name"] == "onefoundry")
    values = (
        project["project"]["version"],
        locked_project["version"],
        frontend["version"],
        frontend_lock["version"],
        frontend_lock["packages"][""]["version"],
    )
    if any(value != version for value in values):
        raise ValueError(f"VERSION {version} disagrees with Python or frontend package metadata")
    return version


def image_refs(version: str) -> dict[str, str]:
    return {target: f"localhost/onefoundry-{target}:v{version}" for target in TARGETS}


def runtime_image_id(manifest: dict[str, object]) -> str:
    """Return the exact application image from a validated release identity."""
    version, images = manifest.get("version"), manifest.get("image_ids")
    if not isinstance(version, str) or not isinstance(images, dict):
        raise ValueError("release runtime identity is incomplete")
    value = images.get(image_refs(version)["runtime"])
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise ValueError("release runtime image identity is invalid")
    return value


def validate_manifest(bundle: Path) -> dict[str, object]:
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("release manifest must be an object")
    version = manifest.get("version")
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise ValueError("invalid release version")
    if manifest.get("format") != "onefoundry.release.v1":
        raise ValueError("unsupported release format")
    if manifest.get("images") != image_refs(version):
        raise ValueError("release image references disagree with version")
    if manifest.get("base_images") != list(BASE_IMAGES):
        raise ValueError("release base images disagree with pinned Compose images")
    image_ids = manifest.get("image_ids")
    expected_refs = {*image_refs(version).values(), *BASE_IMAGES}
    if (
        not isinstance(image_ids, dict)
        or set(image_ids) != expected_refs
        or any(
            not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value)
            for value in image_ids.values()
        )
    ):
        raise ValueError("release image IDs are incomplete or invalid")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) not in (
        LEGACY_FILES,
        LEGACY_FILES | OPERATOR_FILES,
        LEGACY_FILES | OPERATOR_FILES | LICENSE_FILE,
    ):
        raise ValueError("release file inventory is incomplete")
    for name, expected in files.items():
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError(f"invalid digest for {name}")
        if sha256(bundle / name) != expected:
            raise ValueError(f"release file checksum mismatch: {name}")
    if (bundle / "VERSION").read_text(encoding="utf-8").strip() != version:
        raise ValueError("release VERSION mismatch")
    return cast(dict[str, object], manifest)


def export_committed_source(
    destination: Path, revision: str = "HEAD", *, repository: Path = ROOT.parent, timeout: float = 60
) -> None:
    if revision != "HEAD" and not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("release source revision must be a commit SHA")
    archive_bytes = subprocess.check_output(
        ["git", "archive", "--format=tar", f"{revision}:onefoundry"], cwd=repository, timeout=timeout
    )
    destination.mkdir()
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as archive:
        archive.extractall(destination, filter="data")
    # The data filter leaves directory modes to the worker's private umask.
    # Only exported source needs traversal by the image's non-root runtime user.
    for directory, _, _ in os.walk(destination, followlinks=False):
        Path(directory).chmod(0o755)


def build(
    output: Path, *, repository: Path = ROOT.parent, revision: str = "HEAD", timeout_seconds: int = 1800
) -> Path:
    if revision != "HEAD" and not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("release source revision must be an exact commit")
    if not 60 <= timeout_seconds <= 7200:
        raise ValueError("release build timeout must be from 60 to 7200 seconds")
    deadline = monotonic() + timeout_seconds

    def remaining() -> float:
        value = deadline - monotonic()
        if value <= 0:
            raise TimeoutError("release preparation timed out")
        return value

    commit = subprocess.check_output(
        ["git", "rev-parse", revision], cwd=repository, text=True, timeout=remaining()
    ).strip()
    with tempfile.TemporaryDirectory(prefix="onefoundry-release-") as temporary:
        source_root = Path(temporary) / "source"
        export_committed_source(source_root, revision=commit, repository=repository, timeout=remaining())
        version = source_version(source_root)
        output.mkdir(parents=True, exist_ok=True)
        destination = output / f"onefoundry-{version}.tar.gz"
        if destination.exists():
            raise ValueError(f"release archive already exists: {destination}")
        bundle = Path(temporary) / "bundle"
        bundle.mkdir()
        refs = image_refs(version)
        compose_env = dict(os.environ)
        compose_env.update(
            {
                "DB_PASSWORD": "release-build-placeholder",
                "APP_DB_PASSWORD": "release-build-placeholder-2",
                "ONEFOUNDRY_RUNTIME_IMAGE": refs["runtime"],
                "ONEFOUNDRY_RUNNER_IMAGE": refs["runner"],
                "ONEFOUNDRY_INPUT_WORKER_IMAGE": refs["input-worker"],
            }
        )
        configured = subprocess.check_output(
            ["docker", "compose", "-f", "compose.yaml", "-f", "compose.release.yaml", "config", "--images"],
            cwd=source_root,
            text=True,
            env=compose_env,
            timeout=remaining(),
        )
        if set(configured.splitlines()) != {*refs.values(), *BASE_IMAGES}:
            raise ValueError("default Compose service images are not closed by this release bundle")
        for target, ref in refs.items():
            subprocess.run(
                ["docker", "build", "--target", target, "--tag", ref, "."],
                cwd=source_root,
                check=True,
                timeout=remaining(),
            )
        for ref in BASE_IMAGES:
            subprocess.run(["docker", "pull", ref], check=True, timeout=remaining())
        subprocess.run(
            ["docker", "save", "-o", str(bundle / "images.tar"), *refs.values(), *BASE_IMAGES],
            check=True,
            timeout=remaining(),
        )
        image_ids = {
            ref: subprocess.check_output(
                ["docker", "image", "inspect", "--format", "{{.Id}}", ref], text=True, timeout=remaining()
            ).strip()
            for ref in (*refs.values(), *BASE_IMAGES)
        }
        for name in ("VERSION", ".env.example", "compose.yaml", "compose.release.yaml"):
            shutil.copyfile(source_root / name, bundle / name)
        with (bundle / "LICENSE").open("wb") as license_file:
            subprocess.run(
                ["git", "show", f"{commit}:LICENSE"],
                cwd=repository,
                check=True,
                stdout=license_file,
                timeout=remaining(),
            )
        (bundle / "scripts").mkdir()
        for name in (
            "__init__.py",
            "release_bundle.py",
            "onefoundry_update.py",
            "operator_config.py",
            "operator_doctor.py",
        ):
            shutil.copyfile(source_root / "scripts" / name, bundle / "scripts" / name)
        shutil.copyfile(source_root / "onefoundry", bundle / "onefoundry")
        (bundle / "onefoundry").chmod(0o755)
        (bundle / ".infra-status").mkdir()
        manifest = {
            "format": "onefoundry.release.v1",
            "version": version,
            "commit": commit,
            "images": refs,
            "image_ids": image_ids,
            "base_images": list(BASE_IMAGES),
            "files": {
                name: sha256(bundle / name)
                for name in (
                    "VERSION",
                    ".env.example",
                    "compose.yaml",
                    "compose.release.yaml",
                    "images.tar",
                    "scripts/__init__.py",
                    "scripts/release_bundle.py",
                    "scripts/onefoundry_update.py",
                    "scripts/operator_config.py",
                    "scripts/operator_doctor.py",
                    "onefoundry",
                    "LICENSE",
                )
            },
        }
        (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        validate_manifest(bundle)
        with tarfile.open(destination, "w:gz") as archive:
            for path in sorted(bundle.rglob("*")):
                archive.add(path, arcname=Path("onefoundry") / path.relative_to(bundle), recursive=False)
    (destination.with_name(destination.name + ".sha256")).write_text(
        f"{sha256(destination)}  {destination.name}\n", encoding="utf-8"
    )
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("verify-source", "build"))
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    if args.command == "verify-source":
        print(source_version())
    else:
        print(build(args.output))


if __name__ == "__main__":
    main()
