"""Private shared-group transport for the app and trusted host supervisors.

Coding containers never mount this channel. Credentials and supervisor
configuration retain their separate owner-only storage contract.
"""

from pathlib import Path
from typing import Any
from uuid import UUID

from onefoundry.runner_spool import RunnerSpool
from runner.service import _atomic_bytes, _atomic_new


def dispatch_job(spool: RunnerSpool, job_id: UUID, payload: dict[str, Any]) -> None:
    spool.write_job(job_id, payload, group_readable=True)


def publish_new(path: Path, data: bytes) -> None:
    _atomic_new(path, data, mode=0o640)


def publish_replace(path: Path, data: bytes) -> None:
    _atomic_bytes(path, data, mode=0o640)
