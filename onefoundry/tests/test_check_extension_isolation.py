"""Disposable Docker isolation gate orchestration, without launching Docker."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import check_extension_isolation as gate


def _report(command: list[str], *, skipped: bool = False) -> None:
    argument = next(part for part in command if part.startswith("--junitxml="))
    path = Path(argument.partition("=")[2])
    if "scripts/check_extension_postgres.py" in command:
        second = '<skipped message="unavailable" />' if skipped else ""
        path.write_text(
            '<testsuites><testsuite><testcase name="gateway" />'
            f'<testcase name="reference">{second}</testcase></testsuite></testsuites>'
        )
        return
    third = '<skipped message="unavailable" />' if skipped else ""
    path.write_text(
        '<testsuites><testsuite tests="3">'
        '<testcase name="isolation" />'
        '<testcase name="stdio" />'
        f'<testcase name="fixture">{third}</testcase>'
        "</testsuite></testsuites>",
        encoding="utf-8",
    )


def test_gate_pins_image_id_runs_all_fixtures_and_removes_only_own_tag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], int, dict[str, str] | None]] = []

    def fake_run(
        command: list[str], *, timeout: int, env: dict[str, str] | None = None, capture: bool = False
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, timeout, env))
        if command[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(command, 0, "sha256:" + "a" * 64 + "\n", "")
        if "pytest" in command or "scripts/check_extension_postgres.py" in command:
            _report(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(gate, "_run", fake_run)
    gate.run_gate("docker", "python")

    build, inspect, test, end_to_end, cleanup = (call[0] for call in calls)
    assert build[:4] == ["docker", "build", "-f", "runner/extension_worker_image/Dockerfile"]
    assert build[-1] == "."
    tag = build[-2]
    assert inspect == ["docker", "image", "inspect", "--format", "{{.Id}}", tag]
    assert test[:5] == ["python", "-m", "pytest", "-q", "tests/test_extension_worker_container.py"]
    assert calls[2][2] is not None
    assert calls[2][2]["ONEFOUNDRY_EXTENSION_TEST_IMAGE"] == "sha256:" + "a" * 64
    assert end_to_end[:4] == [
        "python",
        "scripts/check_extension_postgres.py",
        "tests/test_gateway_extension_end_to_end.py",
        "tests/test_gateway_extension_reference_postgres.py",
    ]
    assert calls[3][2] == calls[2][2]
    assert cleanup == ["docker", "image", "rm", tag]
    assert [call[1] for call in calls] == [600, 15, 240, 240, 30]


@pytest.mark.parametrize("skipped_phase", ["container", "gateway"])
def test_gate_rejects_skipped_fixture_and_still_removes_own_tag(
    monkeypatch: pytest.MonkeyPatch, skipped_phase: str
) -> None:
    calls: list[list[str]] = []

    def fake_run(
        command: list[str], *, timeout: int, env: dict[str, str] | None = None, capture: bool = False
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(command, 0, "sha256:" + "a" * 64, "")
        if "pytest" in command:
            _report(command, skipped=skipped_phase == "container")
        if "scripts/check_extension_postgres.py" in command:
            _report(command, skipped=skipped_phase == "gateway")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(gate, "_run", fake_run)
    with pytest.raises(gate.IsolationGateError, match="without skips"):
        gate.run_gate("docker", "python")
    assert calls[-1][1:3] == ["image", "rm"]


def test_gate_reports_bounded_command_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(["docker", "build"], 600)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(gate.IsolationGateError, match="timed out after 600s"):
        gate._run(["docker", "build"], timeout=600)
