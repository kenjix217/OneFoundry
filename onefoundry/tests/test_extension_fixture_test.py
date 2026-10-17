"""Behavioral fixture driver stays on the isolated worker contract."""

from __future__ import annotations

import json

import pytest
from onefoundry_sdk import ExtensionManifest, build_package

from runner import extension_fixture_test as fixture
from runner.extension_worker_host import canonical_job_sha256
from tests.test_extension_worker_host import _policy


def _archive(case: dict[str, object], *, effect: str = "pure") -> bytes:
    manifest = ExtensionManifest.model_validate(
        {
            "id": "fixture-package",
            "version": "1.0.0",
            "name": "Fixture package",
            "tools": [
                {
                    "id": "calculate",
                    "description": "Calculate",
                    "parameters": {"type": "object", "properties": {"value": {"type": "integer"}}},
                    "returns": {"type": "object", "properties": {"answer": {"type": "integer"}}},
                    "effect": effect,
                    "runtime": "python",
                    "entrypoint": "calculator:calculate",
                }
            ],
        }
    )
    return build_package(
        manifest,
        {
            "src/calculator.py": b"def calculate(context, value): return {'answer': value + 1}\n",
            "tests/calculate.json": json.dumps(case).encode(),
        },
    )


def test_driver_runs_exact_job_and_compares_output(monkeypatch: pytest.MonkeyPatch) -> None:
    archive = _archive({"tool_id": "calculate", "input": {"value": 4}, "expected_output": {"answer": 5}})
    calls: list[dict[str, object]] = []

    def fake_run(job: dict[str, object], policy: object) -> dict[str, object]:
        calls.append(job)
        return {"status": "done", "result": {"answer": 5}}

    monkeypatch.setattr(fixture, "run_job", fake_run)
    report = fixture.run_fixtures(archive, _policy())
    assert report["fixtures"] == 1 and report["passed"] == 1
    assert calls[0]["job_sha256"] == canonical_job_sha256(calls[0])
    assert calls[0]["worker_image"] == _policy().image_id
    assert calls[0]["execution_kind"] == "python"


def test_driver_rejects_invalid_fixture_and_nonpure_tool() -> None:
    for case, effect in (
        ({"tool_id": "unknown", "input": {}, "expected_output": {}}, "pure"),
        ({"tool_id": "calculate", "input": {}, "expected_output": {}}, "write"),
        ({"tool_id": "calculate", "input": {}, "expected_output": {}, "shell": "run"}, "pure"),
    ):
        with pytest.raises(ValueError):
            fixture._fixtures(_archive(dict(case), effect=effect))
