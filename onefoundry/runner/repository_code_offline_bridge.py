"""Real Docker isolation for an offline coding-adapter test, without credentials.

No provider egress or live readiness is advertised. A configured live bridge must
separately prove its provider broker before using the maintenance service.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from onefoundry.repository_code_contract import RepositoryCodeManifest
from runner import docker_egress_proof as docker
from runner.external_cli import CliResult, _parse_events
from runner.fixture_git import Deadline
from runner.isolated_fixture_test import _bounded_attach
from runner.repository_source_view import copy_changes, file_inventory, make_view, validate_edit_scope


@dataclass(frozen=True)
class OfflineRepositoryBridge:
    image_id: str
    command: tuple[str, ...]

    def validate_context(self, job: RepositoryCodeManifest, repo: Path) -> None:
        validate_edit_scope(job, repo)

    def __call__(self, job: RepositoryCodeManifest, repo: Path, deadline: Deadline) -> CliResult:
        if not self.image_id.startswith("sha256:") or len(self.image_id) != 71 or not self.command:
            raise ValueError("offline_repository_bridge_config_invalid")
        container = f"onefoundry-repository-code-{uuid4().hex[:12]}"
        with tempfile.TemporaryDirectory(prefix="onefoundry-provider-tree-") as temporary:
            plain = Path(temporary) / "work"
            make_view(repo, plain, job.target.allowed_paths, job.stage_limits.max_context_file_bytes)
            before = file_inventory(plain)
            created = False
            try:
                docker._docker(
                    "create",
                    "--pull=never",
                    "--name",
                    container,
                    "--network=none",
                    "--read-only",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges:true",
                    "--pids-limit=64",
                    "--memory=512m",
                    "--memory-swap=512m",
                    "--cpus=1",
                    "--user",
                    f"{os.getuid()}:{os.getgid()}",
                    "--log-driver=none",
                    "--tmpfs",
                    "/tmp:rw,nosuid,nodev,size=32m",  # noqa: S108 - private container tmpfs
                    "--tmpfs",
                    "/app:rw,noexec,nosuid,nodev,size=1m",
                    "--mount",
                    f"type=bind,src={plain},dst=/workspace",
                    "--workdir=/workspace",
                    "--env",
                    "HOME=/tmp",
                    "--env",
                    "PYTHONDONTWRITEBYTECODE=1",
                    f"--entrypoint={self.command[0]}",
                    self.image_id,
                    *self.command[1:],
                )
                created = True
                state = docker._inspect("container", container)
                host, config, mounts = state["HostConfig"], state["Config"], state["Mounts"]
                if (
                    state["Image"] != self.image_id
                    or host["NetworkMode"] != "none"
                    or host["ReadonlyRootfs"] is not True
                    or host["Privileged"] is not False
                    or host["CapDrop"] != ["ALL"]
                    or host["SecurityOpt"] != ["no-new-privileges:true"]
                    or host["PidsLimit"] != 64
                    or host["Memory"] != 512 * 1024 * 1024
                    or host["MemorySwap"] != 512 * 1024 * 1024
                    or host["NanoCpus"] != 1000000000
                    or config["User"] != f"{os.getuid()}:{os.getgid()}"
                    or len(mounts) != 1
                    or mounts[0]["Source"] != str(plain)
                    or mounts[0]["Destination"] != "/workspace"
                    or mounts[0]["RW"] is not True
                ):
                    raise ValueError("offline_repository_boundary_invalid")
                raw = _bounded_attach(container, deadline, job.limits["max_log_bytes"])
                final = docker._inspect("container", container)["State"]
                if final["Status"] != "exited" or type(final["ExitCode"]) is not int:
                    raise ValueError("offline_repository_outcome_unknown")
                terminal, session, tokens, cost = _parse_events(job.adapter_id, raw)
                done = terminal and session is not None and final["ExitCode"] == 0
                result = CliResult(
                    "done" if done else "unknown",
                    "completed" if done else "offline_repository_outcome_unknown",
                    session,
                    tokens,
                    cost,
                    b"Offline isolated coding adapter observation.\n",
                )
                if result.status == "done":
                    copy_changes(job, plain, repo, before)
                return result
            finally:
                if created:
                    docker._remove("container", container)
