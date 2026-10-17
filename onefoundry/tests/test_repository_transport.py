"""Different trusted process users can exchange bounded private evidence."""

import json
import os
import stat
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from onefoundry.runner_spool import RunnerSpool
from runner.repository_transport import dispatch_job, publish_new, publish_replace


def test_transport_is_group_readable_without_group_or_public_write(tmp_path: Path) -> None:
    for name in ("jobs", "claims", "receipts", "artifacts"):
        (tmp_path / name).mkdir()
    job = uuid4()
    dispatch_job(RunnerSpool(str(tmp_path)), job, {"kind": "transport_fixture"})
    publish_new(tmp_path / "receipts" / f"{job}.json", b'{"status":"done"}')
    publish_replace(tmp_path / "repository_capabilities.json", b'{"ready":true}')
    for path in (
        tmp_path / "jobs" / f"{job}.json",
        tmp_path / "receipts" / f"{job}.json",
        tmp_path / "repository_capabilities.json",
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o640


@pytest.mark.integration
def test_real_app_and_host_uids_exchange_jobs_and_receipts(tmp_path: Path) -> None:
    image = os.environ.get("ONEFOUNDRY_EGRESS_TEST_IMAGE")
    if not image:
        pytest.fail("ONEFOUNDRY_EGRESS_TEST_IMAGE required for real UID acceptance")
    spool = tmp_path / "spool"
    spool.mkdir(mode=0o2770)
    spool.chmod(0o2770)
    for name in ("jobs", "claims", "receipts", "artifacts"):
        (spool / name).mkdir(mode=0o2770)
        (spool / name).chmod(0o2770)
    job = str(uuid4())
    module = Path(__file__).resolve().parents[1] / "runner/repository_transport.py"
    common = [
        "/usr/bin/docker",
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges:true",
        "--group-add",
        str(os.getgid()),
        "--mount",
        f"type=bind,src={spool},dst=/spool",
        "--mount",
        f"type=bind,src={module},dst=/app/runner/repository_transport.py,readonly",
    ]

    def actor(uid: int, program: str) -> None:
        result = subprocess.run(  # noqa: S603 - bounded trusted transport fixture, no coding/model process
            [*common, "--user", f"{uid}:{uid}", image, "python", "-c", program],
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr.decode()

    actor(
        10001,
        "from uuid import UUID; from onefoundry.runner_spool import RunnerSpool; "
        "from runner.repository_transport import dispatch_job; "
        f"dispatch_job(RunnerSpool('/spool'),UUID('{job}'),{{'kind':'transport_fixture'}})",
    )
    actor(
        10002,
        "from uuid import UUID; from pathlib import Path; from onefoundry.runner_spool import RunnerSpool; "
        "from runner.repository_transport import publish_new; "
        f"assert RunnerSpool._read_regular(Path('/spool/jobs/{job}.json'),8192); "
        f"publish_new(Path('/spool/receipts/{job}.json'),b'{{\"status\":\"done\"}}')",
    )
    actor(
        10001,
        "from uuid import UUID; from onefoundry.runner_spool import RunnerSpool; "
        f"assert RunnerSpool('/spool').read_receipt(UUID('{job}'))[0]=={{'status':'done'}}",
    )
    assert json.loads((spool / "receipts" / f"{job}.json").read_text()) == {"status": "done"}
