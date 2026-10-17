"""Operator-owned repository allowlist for the installed setup worker."""

from __future__ import annotations

import hashlib
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from pydantic import Field, model_validator

from onefoundry.capability_setup_host_contract import HostSetupCatalog, RepositoryOffer
from onefoundry.workflow_schemas import StrictModel


class HostRepository(StrictModel):
    alias: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
    path: Path
    projects: list[UUID] = Field(min_length=1, max_length=32)

    def validate_host(self) -> None:
        if (
            not self.path.is_absolute()
            or self.path.resolve(strict=True) != self.path
            or self.path in {Path("/"), Path.home()}
            or not (self.path / ".git").is_dir()
            or (self.path / ".git").is_symlink()
        ):
            raise ValueError("setup_repository_path_invalid")


class HostSetupSettings(StrictModel):
    repositories: list[HostRepository] = Field(min_length=1, max_length=16)
    git_timeout_seconds: int = Field(ge=1, le=30)
    restart_timeout_seconds: int = Field(ge=1, le=600)

    @model_validator(mode="after")
    def distinct(self) -> HostSetupSettings:
        if (
            len({r.alias for r in self.repositories}) != len(self.repositories)
            or len({r.path for r in self.repositories}) != len(self.repositories)
            or any(len(set(r.projects)) != len(r.projects) for r in self.repositories)
        ):
            raise ValueError("setup repository aliases, paths and projects must be distinct")
        return self

    def catalog(self) -> HostSetupCatalog:
        offers: list[RepositoryOffer] = []
        for repo in self.repositories:
            repo.validate_host()
            revision = self._git(repo, "rev-parse", "--verify", "HEAD^{commit}")
            try:
                branch = self._git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
            except subprocess.CalledProcessError as exc:
                if exc.returncode != 1:
                    raise
                branch = "HEAD"
            for project in repo.projects:
                offers.append(
                    RepositoryOffer(
                        alias=repo.alias,
                        project_id=project,
                        branch=branch,
                        revision=revision,
                        path_sha256=hashlib.sha256(str(repo.path).encode()).hexdigest(),
                    )
                )
        return HostSetupCatalog(observed_at=datetime.now(UTC), repositories=offers)

    def _git(self, repo: HostRepository, *args: str) -> str:
        result = subprocess.run(  # noqa: S603 - fixed read-only Git operations, configured checkout
            ["git", "-C", str(repo.path), *args],  # noqa: S607 - installed Git prerequisite
            check=True,
            capture_output=True,
            text=True,
            timeout=self.git_timeout_seconds,
        )
        return result.stdout.strip()
