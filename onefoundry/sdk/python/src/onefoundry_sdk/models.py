"""Portable, inert extension authoring contracts."""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import SchemaError  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SLUG = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
TOOL_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?$")
ENTRYPOINT = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*:[a-zA-Z_][a-zA-Z0-9_]*$")
HTTP_PATH = re.compile(r"^/[A-Za-z0-9/_{}.-]*$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class OperationSpec(StrictModel):
    id: str = Field(min_length=1, max_length=64)
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] | None = None
    path: str | None = Field(default=None, max_length=512)

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not TOOL_ID.fullmatch(value):
            raise ValueError("operation id must be lower_underscore and at most 64 characters")
        return value

    @field_validator("path")
    @classmethod
    def relative_path(cls, value: str | None) -> str | None:
        if value is not None and (
            not HTTP_PATH.fullmatch(value) or value.startswith("//") or ".." in value.split("/")
        ):
            raise ValueError("operation path must be an absolute HTTP path without traversal")
        return value


class ConnectionTemplate(StrictModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["http", "mcp", "webhook"]
    operations: list[OperationSpec] = Field(default_factory=list, max_length=64)
    endpoint_hint: str | None = Field(default=None, max_length=512)
    resource_refs: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not TOOL_ID.fullmatch(value):
            raise ValueError("connection id must be lower_underscore")
        return value

    @field_validator("endpoint_hint")
    @classmethod
    def valid_endpoint_hint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(ord(char) < 33 for char in value)
        ):
            raise ValueError("endpoint hint requires an HTTPS URL without credentials or query")
        return value

    @field_validator("resource_refs")
    @classmethod
    def valid_resource_refs(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("duplicate resource reference")
        for item in value:
            parsed = urlsplit(item)
            if (
                len(item) > 512
                or parsed.scheme not in {"https", "mcp"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or any(ord(char) < 33 for char in item)
            ):
                raise ValueError("resource reference must be a bounded static URL")
        return value

    @model_validator(mode="after")
    def valid_operations(self) -> ConnectionTemplate:
        if len({operation.id for operation in self.operations}) != len(self.operations):
            raise ValueError("duplicate connection operation id")
        if self.kind in {"http", "webhook"} and any(
            operation.method is None or operation.path is None for operation in self.operations
        ):
            raise ValueError("HTTP and webhook operations require method and path")
        if self.kind == "mcp" and any(
            operation.method is not None or operation.path is not None for operation in self.operations
        ):
            raise ValueError("MCP operations use tool ids without HTTP method or path")
        return self


def _json_schema(value: dict[str, Any], label: str) -> dict[str, Any]:
    if value.get("type") != "object" or not isinstance(value.get("properties", {}), dict):
        raise ValueError(f"{label} must be an object JSON Schema")
    if any(not isinstance(key, str) or not key for key in value):
        raise ValueError(f"{label} has invalid keys")
    _reject_external_schema_refs(value)
    try:
        Draft202012Validator.check_schema(value)
    except SchemaError as exc:
        raise ValueError(f"{label} is not valid JSON Schema") from exc
    return value


def _reject_external_schema_refs(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"$dynamicRef", "$recursiveRef", "$id"}:
                raise ValueError("dynamic or externally scoped JSON Schema references are unsupported")
            if key == "$ref" and (not isinstance(child, str) or not child.startswith("#/")):
                raise ValueError("external JSON Schema references are forbidden")
            _reject_external_schema_refs(child)
    elif isinstance(value, list):
        for child in value:
            _reject_external_schema_refs(child)


class ToolSpec(StrictModel):
    id: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=500)
    parameters: dict[str, Any]
    returns: dict[str, Any]
    effect: Literal["pure", "read", "write"]
    runtime: Literal["http", "mcp", "python"]
    connection: str | None = None
    operation: str | None = None
    entrypoint: str | None = None
    required_connections: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not TOOL_ID.fullmatch(value):
            raise ValueError("tool id must be lower_underscore and at most 64 characters")
        return value

    @field_validator("parameters", "returns")
    @classmethod
    def valid_schema(cls, value: dict[str, Any], info: Any) -> dict[str, Any]:
        return _json_schema(value, info.field_name)

    @model_validator(mode="after")
    def runtime_contract(self) -> ToolSpec:
        if self.runtime == "python":
            if (
                self.connection
                or self.operation
                or not self.entrypoint
                or not ENTRYPOINT.fullmatch(self.entrypoint)
            ):
                raise ValueError("Python tools require module:function entrypoint and no connection")
        elif self.entrypoint or self.required_connections or not self.connection or not self.operation:
            raise ValueError("connected tools require connection and operation, without entrypoint")
        if len(set(self.required_connections)) != len(self.required_connections):
            raise ValueError("duplicate required connection")
        return self


class SkillSpec(StrictModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    content: str = Field(min_length=1, max_length=16000)
    required_tools: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not TOOL_ID.fullmatch(value):
            raise ValueError("skill id must be lower_underscore")
        return value

    @field_validator("content")
    @classmethod
    def bounded_content(cls, value: str) -> str:
        if not value.strip() or len(value.encode("utf-8")) > 16000:
            raise ValueError("skill content must be nonblank and at most 16000 UTF-8 bytes")
        return value


class ExtensionManifest(StrictModel):
    schema_version: Literal[1] = 1
    id: str = Field(min_length=1, max_length=80)
    version: str = Field(min_length=5, max_length=80)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=1000)
    tools: list[ToolSpec] = Field(default_factory=list, max_length=100)
    connections: list[ConnectionTemplate] = Field(default_factory=list, max_length=50)
    skills: list[SkillSpec] = Field(default_factory=list, max_length=100)

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not SLUG.fullmatch(value):
            raise ValueError("extension id must be a lowercase slug")
        return value

    @field_validator("version")
    @classmethod
    def valid_version(cls, value: str) -> str:
        if not SEMVER.fullmatch(value):
            raise ValueError("version must be SemVer major.minor.patch")
        return value

    @model_validator(mode="after")
    def references(self) -> ExtensionManifest:
        for label, ids in (
            ("tool", [tool.id for tool in self.tools]),
            ("connection", [connection.id for connection in self.connections]),
            ("skill", [skill.id for skill in self.skills]),
        ):
            if len(set(ids)) != len(ids):
                raise ValueError(f"duplicate {label} id")
        tools = {tool.id for tool in self.tools}
        connections = {connection.id: connection for connection in self.connections}
        for skill in self.skills:
            if (
                len(set(skill.required_tools)) != len(skill.required_tools)
                or set(skill.required_tools) - tools
            ):
                raise ValueError(f"skill {skill.id} references unknown or duplicate tools")
        for tool in self.tools:
            if set(tool.required_connections) - connections.keys():
                raise ValueError(f"tool {tool.id} references unknown connection slot")
            if tool.connection:
                connection = connections.get(tool.connection)
                if connection is None or tool.operation not in {item.id for item in connection.operations}:
                    raise ValueError(f"tool {tool.id} references unknown connection operation")
                if tool.runtime == "http" and connection.kind not in {"http", "webhook"}:
                    raise ValueError("HTTP tool requires HTTP or webhook connection")
                if tool.runtime == "mcp" and connection.kind != "mcp":
                    raise ValueError("MCP tool requires MCP connection")
        return self
