"""Saved coding artifact text is available only behind a reconciled receipt."""

from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import Connection, create_engine, text

from onefoundry.role_tool_admission import RunContext, ToolOutputRejected
from onefoundry.runner_spool import RunnerSpool


def _artifact_module() -> Any:
    return importlib.import_module("onefoundry.role_coding_artifact")


def _context(project_id: UUID) -> RunContext:
    return RunContext(
        project_id=project_id,
        run_id=uuid4(),
        work_order_id=uuid4(),
        profile_id=uuid4(),
        snapshot_tools=frozenset(),
        current_tools=frozenset(),
        snapshot_memory_scope="project",
        current_memory_scope="project",
        max_items=5,
        current_role_name="QA",
        current_role_kind="builtin",
        snapshot_role_name="QA",
    )


def _setup(connection: Connection, root: Path, payload: bytes) -> dict[str, Any]:
    project_id, run_id, attempt_id, artifact_id = [uuid4() for _ in range(4)]
    digest = hashlib.sha256(payload).hexdigest()
    connection.execute(text("CREATE TABLE runs (id TEXT, project_id TEXT, mode TEXT, status TEXT)"))
    connection.execute(
        text(
            "CREATE TABLE external_code_attempts (id TEXT, project_id TEXT, run_id TEXT, "
            "adapter_id TEXT, status TEXT, reason_code TEXT, diff_sha256 TEXT, "
            "receipt_sha256 TEXT, tests TEXT)"
        )
    )
    connection.execute(
        text(
            "CREATE TABLE external_code_artifacts (id TEXT, project_id TEXT, run_id TEXT, "
            "attempt_id TEXT, kind TEXT, path TEXT, sha256 TEXT, byte_size INTEGER)"
        )
    )
    connection.execute(
        text("CREATE TABLE events (project_id TEXT, run_id TEXT, event_type TEXT, payload TEXT)")
    )
    connection.execute(
        text("INSERT INTO runs VALUES (:id,:project,'external_code','done')"),
        {"id": str(run_id), "project": str(project_id)},
    )
    connection.execute(
        text(
            "INSERT INTO external_code_attempts VALUES "
            "(:id,:project,:run,'codex_cli','done','completed',:diff,:receipt,:tests)"
        ),
        {
            "id": str(attempt_id),
            "project": str(project_id),
            "run": str(run_id),
            "diff": digest,
            "receipt": "f" * 64,
            "tests": json.dumps([{"name": "pytest", "status": "passed", "exit_code": 0}]),
        },
    )
    path = f"artifacts/{attempt_id}/change.diff"
    connection.execute(
        text(
            "INSERT INTO external_code_artifacts VALUES (:id,:project,:run,:attempt,'diff',:path,:sha,:size)"
        ),
        {
            "id": str(artifact_id),
            "project": str(project_id),
            "run": str(run_id),
            "attempt": str(attempt_id),
            "path": path,
            "sha": digest,
            "size": len(payload),
        },
    )
    receipt = {
        "attempt_id": str(attempt_id),
        "adapter_id": "codex_cli",
        "receipt_sha256": "f" * 64,
        "status": "done",
        "reason_code": "completed",
        "diff_sha256": digest,
        "tests": [{"name": "pytest", "status": "passed", "exit_code": 0}],
    }
    connection.execute(
        text("INSERT INTO events VALUES (:project,:run,'external_code_receipt',:payload)"),
        {"project": str(project_id), "run": str(run_id), "payload": json.dumps(receipt)},
    )
    artifact_dir = root / "artifacts" / str(attempt_id)
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "change.diff").write_bytes(payload)
    return {
        "project_id": project_id,
        "run_id": run_id,
        "attempt_id": attempt_id,
        "artifact_id": artifact_id,
        "digest": digest,
        "path": artifact_dir / "change.diff",
        "receipt": receipt,
    }


def test_exact_reconciled_artifact_returns_bounded_text(tmp_path: Path) -> None:
    module = _artifact_module()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    payload = b"diff --git a/math.py b/math.py\n+return 2\n"
    with engine.begin() as connection:
        item = _setup(connection, tmp_path, payload)
        result = module.coding_artifact(
            connection,
            _context(item["project_id"]),
            {"run_id": str(item["run_id"]), "artifact_id": str(item["artifact_id"])},
            RunnerSpool(str(tmp_path)),
        )
    module.validate_coding_artifact(result)
    assert result == {
        "run_id": str(item["run_id"]),
        "artifact_id": str(item["artifact_id"]),
        "found": True,
        "kind": "diff",
        "sha256": item["digest"],
        "byte_size": len(payload),
        "text": payload.decode(),
        "truncated": False,
    }
    assert "artifacts/" not in json.dumps(result)


def test_scope_or_missing_receipt_never_reads_artifact(tmp_path: Path) -> None:
    module = _artifact_module()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        item = _setup(connection, tmp_path, b"safe diff\n")
        arguments = {"run_id": str(item["run_id"]), "artifact_id": str(item["artifact_id"])}
        other_project = module.coding_artifact(
            connection, _context(uuid4()), arguments, RunnerSpool(str(tmp_path))
        )
        assert other_project["found"] is False and other_project["text"] is None
        item["path"].unlink()
        connection.execute(
            text("UPDATE events SET payload=:payload"),
            {"payload": json.dumps({**item["receipt"], "diff_sha256": "0" * 64})},
        )
        mismatched = module.coding_artifact(
            connection, _context(item["project_id"]), arguments, RunnerSpool(str(tmp_path))
        )
        assert mismatched["found"] is False and mismatched["text"] is None
        connection.execute(
            text("UPDATE events SET payload=:payload"), {"payload": json.dumps(item["receipt"])}
        )
        connection.execute(text("UPDATE external_code_attempts SET status='queued'"))
        queued = module.coding_artifact(
            connection, _context(item["project_id"]), arguments, RunnerSpool(str(tmp_path))
        )
        assert queued["found"] is False and queued["text"] is None


def test_corrupt_binary_or_secret_spool_bytes_are_rejected(tmp_path: Path) -> None:
    module = _artifact_module()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        item = _setup(connection, tmp_path, b"safe diff\n")
        arguments = {"run_id": str(item["run_id"]), "artifact_id": str(item["artifact_id"])}
        item["path"].write_bytes(b"changed")
        with pytest.raises(ToolOutputRejected):
            module.coding_artifact(
                connection, _context(item["project_id"]), arguments, RunnerSpool(str(tmp_path))
            )
        for unsafe in (b"binary\x00diff", b"password=letmein\n"):
            item["path"].write_bytes(unsafe)
            digest = hashlib.sha256(unsafe).hexdigest()
            connection.execute(
                text("UPDATE external_code_artifacts SET sha256=:sha,byte_size=:size"),
                {"sha": digest, "size": len(unsafe)},
            )
            connection.execute(text("UPDATE external_code_attempts SET diff_sha256=:sha"), {"sha": digest})
            connection.execute(
                text("UPDATE events SET payload=:payload"),
                {"payload": json.dumps({**item["receipt"], "diff_sha256": digest})},
            )
            with pytest.raises(ToolOutputRejected):
                module.coding_artifact(
                    connection, _context(item["project_id"]), arguments, RunnerSpool(str(tmp_path))
                )


def test_large_log_is_truncated_and_arguments_are_typed(tmp_path: Path) -> None:
    module = _artifact_module()
    run_id, artifact_id = uuid4(), uuid4()
    assert module.parse_coding_artifact({"run_id": str(run_id), "artifact_id": str(artifact_id)}) == {
        "run_id": str(run_id),
        "artifact_id": str(artifact_id),
    }
    for bad in (
        {},
        {"run_id": str(run_id), "artifact_id": "../private"},
        {"run_id": str(run_id), "artifact_id": str(artifact_id), "path": "secret"},
    ):
        with pytest.raises(ValidationError):
            module.parse_coding_artifact(bad)
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        item = _setup(connection, tmp_path, b"A" * 10000)
        result = module.coding_artifact(
            connection,
            _context(item["project_id"]),
            {"run_id": str(item["run_id"]), "artifact_id": str(item["artifact_id"])},
            RunnerSpool(str(tmp_path)),
        )
    assert result["found"] is True and result["truncated"] is True
    assert result["text"] == "A" * 8192
    assert len(result["text"].encode()) == 8192
