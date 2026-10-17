"""Private protocol files preserve exact identity and reject unsafe filesystem state."""

import json
from pathlib import Path

import pytest

from onefoundry.gateway_extension_spool import job_digest, publish_json, read_json
from onefoundry.role_tool_admission import ToolDenied
from runner.extension_worker_host import canonical_job_sha256


def test_job_digest_agrees_with_host_for_unicode_and_ignores_digest_field() -> None:
    job = {"arguments": {"term": "café 笔记"}, "attempt_id": "first"}
    assert job_digest(job) == canonical_job_sha256(job)
    job["job_sha256"] = job_digest(job)
    assert job_digest(job) == canonical_job_sha256(job)


def test_private_file_is_exact_bounded_and_cannot_be_replaced(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"
    value = {"status": "done", "result": {"value": "é"}}
    publish_json(path, value)
    assert path.stat().st_mode & 0o777 == 0o600
    assert read_json(path, 1024) == value
    with pytest.raises(FileExistsError):
        publish_json(path, {"status": "changed"})
    with pytest.raises(ToolDenied):
        read_json(path, 2)
    path.chmod(0o644)
    with pytest.raises(ToolDenied):
        read_json(path, 1024)


def test_protocol_rejects_symlink_and_non_object(tmp_path: Path) -> None:
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps([]))
    path.chmod(0o600)
    with pytest.raises(ToolDenied):
        read_json(path, 1024)
    alias = tmp_path / "alias.json"
    alias.symlink_to(path)
    with pytest.raises(OSError):
        read_json(alias, 1024)
