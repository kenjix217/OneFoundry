"""Exercise the PostgreSQL gate's image handoff without starting Docker."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_postgres.sh"


@pytest.mark.parametrize("override", [None, "operator-qa-image:tested", "", "sha256:" + "b" * 64])
@pytest.mark.parametrize("inspection", ["valid", "invalid", "missing"])
def test_repository_failure_gate_receives_qa_image(
    tmp_path: Path, override: str | None, inspection: str
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "if args and args[0] == 'build':\n"
        "    record = {'action': 'build', 'target': args[args.index('--target') + 1], "
        "'tag': args[args.index('-t') + 1]}\n"
        "elif args[:2] == ['image', 'inspect']:\n"
        "    mode = os.environ['SAFE_IMAGE_INSPECTION']\n"
        "    if mode == 'missing': sys.exit(1)\n"
        "    print('sha256:' + 'a' * 64 if mode == 'valid' else 'unverified-image')\n"
        "    record = {'action': 'inspect', 'tag': args[-1]}\n"
        "elif args[:2] == ['image', 'rm']:\n"
        "    record = {'action': 'remove', 'tag': args[2]}\n"
        "else:\n"
        "    record = None\n"
        "if record is not None:\n"
        "    with open(os.environ['SAFE_DOCKER_RECORD'], 'a', encoding='utf-8') as stream:\n"
        "        stream.write(json.dumps(record) + '\\n')\n",
        encoding="utf-8",
    )
    docker.chmod(0o700)
    uv = bin_dir / "uv"
    uv.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "record = {\n"
        "    'tests': [arg for arg in sys.argv[1:] if arg.startswith('tests/') and arg.endswith('.py')],\n"
        "    'egress_image': os.environ.get('ONEFOUNDRY_EGRESS_TEST_IMAGE'),\n"
        "    'qa_image': os.environ.get('ONEFOUNDRY_REPOSITORY_QA_TEST_IMAGE'),\n"
        "}\n"
        "with open(os.environ['SAFE_IMAGE_RECORD'], 'a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps(record) + '\\n')\n",
        encoding="utf-8",
    )
    uv.chmod(0o700)
    record_path = tmp_path / "images.jsonl"
    docker_record_path = tmp_path / "docker-images.jsonl"
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["SAFE_IMAGE_RECORD"] = str(record_path)
    env["SAFE_IMAGE_INSPECTION"] = inspection
    env["SAFE_DOCKER_RECORD"] = str(docker_record_path)
    if override is None:
        env.pop("ONEFOUNDRY_REPOSITORY_QA_TEST_IMAGE", None)
    else:
        env["ONEFOUNDRY_REPOSITORY_QA_TEST_IMAGE"] = override

    completed = subprocess.run(  # noqa: S603
        ["/bin/sh", str(SCRIPT), str(uv)], env=env, text=True, capture_output=True, check=False, timeout=30
    )
    assert (completed.returncode == 0) == (inspection == "valid")
    records = [json.loads(line) for line in record_path.read_text(encoding="utf-8").splitlines()]
    if inspection != "valid":
        assert not any("tests/test_repository_failure_evidence.py" in row["tests"] for row in records)
        return
    owner = next(
        record for record in records if "tests/test_repository_failure_evidence.py" in record["tests"]
    )
    assert re.fullmatch(r"onefoundry_check_[0-9]+-test:latest", owner["egress_image"])
    image_actions = (
        [json.loads(line) for line in docker_record_path.read_text(encoding="utf-8").splitlines()]
        if docker_record_path.exists()
        else []
    )
    if override:
        assert owner["qa_image"] == "sha256:" + "a" * 64
        assert image_actions == [{"action": "inspect", "tag": override}]
    else:
        expected = owner["egress_image"].replace("-test:latest", "-repository-qa:latest")
        assert owner["qa_image"] == "sha256:" + "a" * 64
        assert image_actions == [
            {"action": "build", "target": "repository-qa", "tag": expected},
            {"action": "inspect", "tag": expected},
            {"action": "remove", "tag": expected},
        ]
