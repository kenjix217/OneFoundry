"""The fixture test process is isolated even when provider edits are hostile."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from runner import docker_egress_proof as docker
from runner.fixture_git import FIXTURE, Deadline, ExecutionError
from runner.isolated_fixture_test import run_isolated_fixture_test


def test_rejects_unpinned_image_before_launch(tmp_path: Path) -> None:
    with pytest.raises(ExecutionError, match="fixture_test_input_invalid"):
        run_isolated_fixture_test(tmp_path, Deadline(5), 4096, "before", image_id="latest")


@pytest.mark.integration
def test_fixed_fixture_fails_then_passes_in_disposable_container(tmp_path: Path) -> None:
    image_id = docker._inspect("image", os.environ["ONEFOUNDRY_EGRESS_TEST_IMAGE"])["Id"]
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURE, repo)
    before, before_log = run_isolated_fixture_test(repo, Deadline(20), 16384, "before", image_id=image_id)
    assert (before["name"], before["status"], before["exit_code"]) == ("before", "failed", 1)
    assert b"FAILED" in before_log
    (repo / "calculator.py").write_text("def add_one(value: int) -> int:\n    return value + 1\n")
    after, after_log = run_isolated_fixture_test(repo, Deadline(20), 16384, "after", image_id=image_id)
    assert (after["name"], after["status"], after["exit_code"]) == ("after", "passed", 0)
    assert b"Ran 1 test" in after_log and b"OK" in after_log


@pytest.mark.integration
def test_hostile_fixture_test_cannot_execute_on_host(tmp_path: Path) -> None:
    image_id = docker._inspect("image", os.environ["ONEFOUNDRY_EGRESS_TEST_IMAGE"])["Id"]
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tests").mkdir()
    marker = Path("/tmp") / f"onefoundry-host-execution-{uuid4().hex}"  # noqa: S108
    (repo / "tests" / "test_host_escape.py").write_text(
        "import pathlib, unittest\n"
        "class HostEscape(unittest.TestCase):\n"
        "    def test_write_marker(self):\n"
        f"        pathlib.Path({str(marker)!r}).write_text('executed')\n"
        "        self.assertFalse(pathlib.Path('/runner-spool').exists())\n"
        "        self.assertFalse(pathlib.Path('/var/run/docker.sock').exists())\n"
    )
    try:
        result, log = run_isolated_fixture_test(repo, Deadline(20), 16384, "after", image_id=image_id)
        assert result["status"] == "passed", log
        assert b"test_write_marker" in log
        assert not marker.exists()
    finally:
        marker.unlink(missing_ok=True)


@pytest.mark.integration
def test_disposable_workspace_enforces_byte_and_file_limits() -> None:
    image_id = docker._inspect("image", os.environ["ONEFOUNDRY_EGRESS_TEST_IMAGE"])["Id"]
    code = (
        "import json, os\n"
        "fd = os.open('/workspace/large', os.O_WRONLY | os.O_CREAT, 0o600)\n"
        "written = 0\n"
        "try:\n"
        "    while written < 9 * 1024 * 1024:\n"
        "        written += os.write(fd, b'x' * 65536)\n"
        "except OSError:\n"
        "    pass\n"
        "os.close(fd)\n"
        "os.unlink('/workspace/large')\n"
        "files = 0\n"
        "try:\n"
        "    for index in range(256):\n"
        "        os.close(os.open(f'/workspace/{index}', os.O_WRONLY | os.O_CREAT, 0o600))\n"
        "        files += 1\n"
        "except OSError:\n"
        "    pass\n"
        "print(json.dumps({'bytes_denied': written < 9 * 1024 * 1024, 'files_denied': files < 256}))\n"
    )
    output = docker._docker(
        "run",
        "--rm",
        "--pull=never",
        "--network=none",
        "--read-only",
        "--user=1000:1000",
        "--tmpfs",
        "/workspace:rw,nosuid,nodev,size=8m,nr_inodes=128,uid=1000,gid=1000",
        "--entrypoint=/usr/local/bin/python3",
        image_id,
        "-c",
        code,
    )
    assert json.loads(output) == {"bytes_denied": True, "files_denied": True}
