"""OneFoundry extension authoring SDK. Importing this package has no runtime effects."""

from .models import ConnectionTemplate, ExtensionManifest, OperationSpec, SkillSpec, ToolSpec
from .package import PackageError, PackageInspection, build_package, canonical_manifest_json, inspect_package
from .runtime import ToolContext, tool

__all__ = [
    "ConnectionTemplate",
    "ExtensionManifest",
    "OperationSpec",
    "PackageError",
    "PackageInspection",
    "SkillSpec",
    "ToolContext",
    "ToolSpec",
    "build_package",
    "canonical_manifest_json",
    "inspect_package",
    "tool",
]
