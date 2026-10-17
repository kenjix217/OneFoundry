"""Build a disposable extension image and run every real container isolation fixture."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
IMAGE_ID_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
BUILD_TIMEOUT_SECONDS = 600
INSPECT_TIMEOUT_SECONDS = 15
TEST_TIMEOUT_SECONDS = 240
CLEANUP_TIMEOUT_SECONDS = 30


class IsolationGateError(RuntimeError):
    """A bounded disposable isolation gate could not finish."""


def _run(
    command: list[str], *, timeout: int, env: dict[str, str] | None = None, capture: bool = False
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603 - fixed local commands and a generated image tag
            command,
            cwd=ROOT,
            env=env,
            capture_output=capture,
            text=True,
            timeout=timeout,
            check=True,
        )
    except subprocess.TimeoutExpired as error:
        raise IsolationGateError(f"isolation gate command timed out after {timeout}s") from error
    except subprocess.CalledProcessError as error:
        raise IsolationGateError(
            f"isolation gate command failed with exit code {error.returncode}"
        ) from error


def _verify_report(report: Path, expected: int) -> None:
    try:
        cases = ET.parse(report).findall(".//testcase")  # noqa: S314 - local pytest report
    except (OSError, ET.ParseError) as error:
        raise IsolationGateError("extension fixture report is unavailable") from error
    if len(cases) != expected or any(
        case.find(kind) is not None for case in cases for kind in ("skipped", "failure", "error")
    ):
        raise IsolationGateError("all required extension fixtures must pass without skips")


def run_gate(docker: str, python: str) -> None:
    tag = f"onefoundry-extension-isolation-{uuid4().hex}:test"
    failure: IsolationGateError | None = None
    try:
        _run(
            [docker, "build", "-f", "runner/extension_worker_image/Dockerfile", "-t", tag, "."],
            timeout=BUILD_TIMEOUT_SECONDS,
        )
        image_id = _run(
            [docker, "image", "inspect", "--format", "{{.Id}}", tag],
            timeout=INSPECT_TIMEOUT_SECONDS,
            capture=True,
        ).stdout.strip()
        if not IMAGE_ID_PATTERN.fullmatch(image_id):
            raise IsolationGateError("Docker returned an invalid extension worker image ID")
        test_env = dict(os.environ)
        test_env["ONEFOUNDRY_EXTENSION_TEST_IMAGE"] = image_id
        with tempfile.TemporaryDirectory(prefix="onefoundry-extension-isolation-") as temporary:
            report = Path(temporary) / "pytest.xml"
            _run(
                [
                    python,
                    "-m",
                    "pytest",
                    "-q",
                    "tests/test_extension_worker_container.py",
                    f"--junitxml={report}",
                ],
                timeout=TEST_TIMEOUT_SECONDS,
                env=test_env,
            )
            _verify_report(report, 3)
            end_to_end = Path(temporary) / "gateway.xml"
            _run(
                [
                    python,
                    "scripts/check_extension_postgres.py",
                    "tests/test_gateway_extension_end_to_end.py",
                    "tests/test_gateway_extension_reference_postgres.py",
                    f"--junitxml={end_to_end}",
                ],
                timeout=TEST_TIMEOUT_SECONDS,
                env=test_env,
            )
            _verify_report(end_to_end, 2)
    except IsolationGateError as error:
        failure = error
    finally:
        try:
            _run([docker, "image", "rm", tag], timeout=CLEANUP_TIMEOUT_SECONDS, capture=True)
        except IsolationGateError as error:
            if failure is None:
                failure = error
            else:
                print(f"Disposable image tag cleanup also failed: {error}", file=sys.stderr)
    if failure is not None:
        raise failure


def main() -> int:
    docker = shutil.which("docker")
    if docker is None:
        print("Docker CLI is required for the extension isolation gate", file=sys.stderr)
        return 1
    try:
        run_gate(docker, sys.executable)
    except IsolationGateError as error:
        print(str(error), file=sys.stderr)
        return 1
    print("Extension isolation gate passed (3 container fixtures and 2 Gateway end-to-end cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
