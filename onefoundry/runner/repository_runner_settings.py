"""Validated configuration for the separate maintenance supervisor."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from onefoundry.repository_code_contract import RepositoryCodePolicy, exact_provider
from onefoundry.workflow_schemas import StrictModel


class RepositoryRunnerSettings(StrictModel):
    spool: Path
    source_root: Path
    credential_file: Path = Field(exclude=True)
    auth_kind: Literal["api_key", "codex_subscription"] = "api_key"
    cli_image: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    broker_image: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    qa_image: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    cli_executable: str
    broker_executable: str
    model_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,119}$")
    provider_url: str
    broker_port: int = Field(ge=1024, le=65535)
    request_timeout_seconds: int = Field(ge=1, le=300)
    provider_overload_retries: int = Field(default=1, ge=0, le=2)
    provider_overload_retry_delay_seconds: float = Field(default=1, ge=0.1, le=30)
    poll_seconds: float = Field(ge=0.1, le=10)
    capability_refresh_seconds: int = Field(ge=1, le=10)
    child_memory_mb: int = Field(ge=128, le=2048)
    child_processes: int = Field(ge=8, le=128)
    child_cpus: int = Field(ge=1, le=4)
    private_tmp_mb: int = Field(ge=8, le=128)
    policy: RepositoryCodePolicy

    _provider = field_validator("provider_url")(exact_provider)

    @field_validator("cli_executable", "broker_executable")
    @classmethod
    def executable(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts or any(ord(char) < 33 for char in value):
            raise ValueError("runner executable must be an exact container path")
        return value

    @model_validator(mode="after")
    def distinct_roots(self) -> RepositoryRunnerSettings:
        if self.auth_kind == "codex_subscription" and self.provider_url != (
            "https://chatgpt.com/backend-api/codex/responses"
        ):
            raise ValueError("subscription credential requires its exact Codex account endpoint")
        paths = (self.spool, self.source_root, self.credential_file)
        for path in paths:
            if not path.is_absolute() or path.resolve() != path or path.is_symlink():
                raise ValueError("runner paths must be canonical and independent")
        if any(
            left == right or left in right.parents or right in left.parents
            for i, left in enumerate(paths)
            for right in paths[i + 1 :]
        ):
            raise ValueError("runner source, credential and receipt paths must be disjoint")
        return self

    def validate_host(self) -> None:
        if not self.spool.is_dir() or not self.source_root.is_dir():
            raise ValueError("runner source or receipt storage is unavailable")
        info = self.credential_file.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or not 1 <= info.st_size <= 8193
        ):
            raise ValueError("runner credential file must be owned, regular and mode 0600")
