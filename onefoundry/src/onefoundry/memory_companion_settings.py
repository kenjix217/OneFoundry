"""Validate the private Compose launch inputs without reading secret contents."""

import os
import re
import stat
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class MemoryCompanionSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MEMORY_", env_file=None, extra="forbid")

    runtime_image: str = Field(pattern=r"^(?:[a-zA-Z0-9][a-zA-Z0-9./:_-]*@)?sha256:[0-9a-f]{64}$")
    private_env_file: Path
    public_key_host_path: Path
    public_key_container_path: Path
    database_network: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")

    @field_validator("private_env_file", "public_key_host_path", "public_key_container_path")
    @classmethod
    def absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute() or ".." in value.parts or value == Path("/"):
            raise ValueError("use an absolute non-root path without parent traversal")
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in value.parts[1:]):
            raise ValueError("deployment paths must not contain Compose interpolation or control characters")
        return value

    def validate_host_files(self) -> None:
        """Fail closed before Compose loads the private environment or mounts a key."""
        info = self.private_env_file.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or not info.st_mode & stat.S_IRUSR
        ):
            raise ValueError("memory environment must be an owned readable regular file with mode 0600")
        key = self.public_key_host_path.lstat()
        if not stat.S_ISREG(key.st_mode) or key.st_mode & 0o022:
            raise ValueError("memory public key must be a regular file without other writers")


def main() -> None:
    try:
        MemoryCompanionSettings().validate_host_files()  # type: ignore[call-arg]
    except Exception:
        raise SystemExit(
            "memory_companion_preflight_failed: check pinned image, paths and private files"
        ) from None
    print("memory_companion_preflight_passed: deployment inputs only; live service not verified")


if __name__ == "__main__":
    main()
