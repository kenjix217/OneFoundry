"""Host-only offline behavioral fixtures through the isolated extension worker."""

from __future__ import annotations

import argparse
import base64
import io
import json
import sys
import zipfile
from pathlib import Path
from typing import Any
from uuid import uuid4

from onefoundry_sdk import PackageError, inspect_package

from runner.extension_worker_host import WorkerError, WorkerPolicy, canonical_job_sha256, run_job
from runner.extension_worker_supervisor import _policy_from_config

MAX_FIXTURES = 16


def _fixtures(archive: bytes) -> list[tuple[str, dict[str, Any]]]:
    inspected = inspect_package(archive)
    tools = {tool.id: tool for tool in inspected.manifest.tools}
    cases: list[tuple[str, dict[str, Any]]] = []
    with zipfile.ZipFile(io.BytesIO(archive)) as package:
        for name in sorted(inspected.file_sha256):
            if not name.startswith("tests/") or not name.endswith(".json"):
                continue
            value = json.loads(package.read(name))
            if not isinstance(value, dict) or set(value) != {"tool_id", "input", "expected_output"}:
                raise ValueError("fixture_contract_invalid")
            if not isinstance(value["tool_id"], str):
                raise ValueError("fixture_tool_not_offline_pure")
            tool = tools.get(value["tool_id"])
            if (
                tool is None
                or tool.runtime != "python"
                or tool.effect != "pure"
                or tool.required_connections
                or not isinstance(value["input"], dict)
                or not isinstance(value["expected_output"], dict)
            ):
                raise ValueError("fixture_tool_not_offline_pure")
            cases.append((name, value))
    if not cases or len(cases) > MAX_FIXTURES:
        raise ValueError("fixture_count_invalid")
    return cases


def run_fixtures(archive: bytes, policy: WorkerPolicy) -> dict[str, Any]:
    inspected = inspect_package(archive)
    cases = _fixtures(archive)
    encoded_archive = base64.b64encode(archive).decode()
    failures: list[dict[str, str]] = []
    for name, case in cases:
        identity = str(uuid4())
        job: dict[str, Any] = {
            "schema_version": 1,
            "invocation_id": identity,
            "project_id": identity,
            "actor_id": "runner:extension-fixture",
            "run_id": identity,
            "profile_id": identity,
            "package_sha256": inspected.package_sha256,
            "archive_base64": encoded_archive,
            "tool_id": case["tool_id"],
            "arguments": case["input"],
            "timeout_seconds": policy.max_timeout_seconds,
            "max_output_bytes": policy.max_output_bytes,
            "worker_image": policy.image_id,
            "memory_mb": policy.memory_mb,
            "processes": policy.processes,
            "cpus": policy.cpus,
            "execution_kind": "python",
        }
        job["job_sha256"] = canonical_job_sha256(job)
        receipt = run_job(job, policy)
        if receipt["status"] != "done" or receipt["result"] != case["expected_output"]:
            reason = receipt.get("reason", "fixture_output_mismatch")
            failures.append({"fixture": name, "reason": str(reason)[:80]})
    return {
        "package_sha256": inspected.package_sha256,
        "fixtures": len(cases),
        "passed": len(cases) - len(failures),
        "failures": failures,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline isolated extension fixture execution")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        _, policy, _ = _policy_from_config(args.config)
        if (
            args.archive.is_symlink()
            or not args.archive.is_file()
            or args.archive.stat().st_size > 4 * 1024 * 1024
        ):
            raise ValueError("fixture_archive_invalid")
        report = run_fixtures(args.archive.read_bytes(), policy)
        print(json.dumps(report, sort_keys=True))
        return 0 if not report["failures"] else 1
    except (OSError, ValueError, PackageError, WorkerError):
        print(json.dumps({"status": "error", "reason": "fixture_preflight_failed"}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
