"""A terminal coding failure must reach Chief without another provider attempt."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from onefoundry.main import create_app
from onefoundry.repository_code_contract import RepositoryCodeTarget, RepositoryRunnerCapability
from onefoundry.task_session_attempts import TaskSessionAttempts
from onefoundry.task_session_runs import bind
from onefoundry.task_session_tools import TaskSessionTools
from onefoundry.tool_gateway import ToolGateway
from runner.repository_code_service import process_job
from tests.test_repository_code import fake_provider, job_at
from tests.test_self_extension_postgres import payload
from tests.test_team_workflow_postgres import ORIGIN, project_with_models, settings_for
from tests.test_team_workflow_postgres import quiet_worker as quiet_worker


@pytest.mark.parametrize("failure", ["candidate", "manifest", "unavailable"])
def test_failed_coding_blocks_assignment_and_notifies_chief_once(tmp_path: Path, failure: str) -> None:
    seed, root, _ = job_at(tmp_path)
    settings = settings_for(tmp_path)
    settings.local_source_root = str(root)
    settings.runner_spool_root = str(tmp_path)
    spool = tmp_path / "external"
    for name in ("jobs", "claims", "receipts", "artifacts"):
        (spool / name).mkdir(parents=True)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, profiles = project_with_models(client)
        pid, base = UUID(project), f"/api/projects/{project}"
        chief = next(p for p in client.get(base + "/profiles").json() if p["name"] == "Chief of Staff")
        assert (
            client.patch(
                base + f"/profiles/{profiles['Engineer']}",
                json={
                    "model": chief["model"],
                    "model_account_id": chief["model_account_id"],
                    "autonomy_level": "supervised",
                },
                headers=ORIGIN,
            ).status_code
            == 200
        )
        local = client.post(
            base + "/sources/local", json={"root_id": "workspace", "relative_path": "repo"}, headers=ORIGIN
        ).json()
        order = client.post(
            base + "/work-orders",
            json={
                "title": "Missing capability",
                "instructions": "Add the missing tool.",
                "profile_id": chief["id"],
                "deliverable_kind": "operator_review",
            },
            headers=ORIGIN,
        ).json()
        assert (
            client.post(
                base + f"/work-orders/{order['id']}/team-session/start",
                json={
                    "client_message_id": str(uuid4()),
                    "expected_version": order["version"],
                    "body": "Build the missing tool.",
                },
                headers=ORIGIN,
            ).status_code
            == 202
        )
        engine = create_engine(settings.database_url)
        gateway = ToolGateway(
            engine,
            settings.operator_actor_id,
            runner_spool_root=str(tmp_path),
            runner_limits=settings.runner_limits,
            runner_receipt_wait_seconds=settings.runner_receipt_wait_seconds,
            live_text_settings=settings,
        )
        attempts = TaskSessionAttempts(gateway)
        turn = attempts.claim_next(pid)
        assert turn is not None
        bind(gateway, pid, turn)
        tools = TaskSessionTools(gateway, pid, turn["session_id"], turn["id"])
        target = RepositoryCodeTarget(
            source_id=UUID(local["id"]),
            task_scope_id=UUID(order["id"]),
            revision=local["revision"],
            adapter_id="codex_cli",
            provider_url=seed.target.provider_url,
            allowed_paths=["calculator.py"],
            test_image=seed.target.test_image,
            test_commands=[
                command + (["x" * 500] * 3 if failure == "manifest" else [])
                for command in seed.target.test_commands * 5
            ],
        )
        approval = payload(
            tools.invoke("onefoundry.propose_maintenance_target", target.model_dump(mode="json"), "target")
        )["approval_id"]
        assert (
            client.post(
                base + f"/approvals/{approval}/decision", json={"decision": "approved"}, headers=ORIGIN
            ).status_code
            == 200
        )
        assigned = payload(
            tools.invoke(
                "onefoundry.request_extension",
                {
                    "operation": "tool:fixture_missing_v1",
                    "target_approval_id": approval,
                    "objective": "Add the missing tool.",
                    "acceptance_check": "Approved checks pass.",
                    "checkpoint": "Continue the original request.",
                },
                "request",
            )
        )
        attempts.settle(pid, turn["id"], "done", public_reply="Engineer is assigned.")
        child = attempts.claim_next(pid)
        assert child is not None
        bind(gateway, pid, child)
        report = RepositoryRunnerCapability(
            observed_at=datetime.now(UTC),
            isolated=True,
            provider_only_egress=True,
            credentials_separate=True,
            receipts_separate=True,
            source_root_ready=True,
            adapter_id="codex_cli",
            model_id=chief["model"],
            provider_url=seed.target.provider_url,
            image_id=seed.target.test_image,
            proof_sha256="f" * 64,
        )
        (spool / "repository_capabilities.json").write_text(report.model_dump_json())
        engineering = TaskSessionTools(gateway, pid, child["session_id"], child["id"])
        build_request = payload(
            engineering.invoke(
                "onefoundry.build_extension",
                {"target_approval_id": approval, "adapter_id": "codex_cli"},
                "build",
            )
        )
        from onefoundry.gateway_errors import NotFound
        from onefoundry.self_extension_dispatch_rejection import reject

        assert not reject(gateway, pid, build_request, "coding_dispatch_prerequisite_failed")
        with pytest.raises(NotFound):
            reject(gateway, uuid4(), build_request, "coding_dispatch_prerequisite_failed")
        attempts.settle(pid, child["id"], "done", public_reply="Waiting for the coding receipt.")
        if failure == "unavailable":
            (spool / "repository_capabilities.json").unlink()
        from onefoundry.self_extension_continuation import step as reconcile
        from onefoundry.self_extension_dispatch import dispatch_intent, waiting

        dispatch_row = {"project_id": pid, "payload": build_request}
        if failure != "candidate":
            # A known prelaunch rejection has no external attempt to reconcile.
            # It must release the assignment to Chief without endlessly retrying.
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: dispatch_intent(gateway, dispatch_row), range(2)))
            assert sorted(results) == [False, True]
            assert not list((spool / "jobs").glob("*.json"))
            # A worker that selected the old intent before rejection must not
            # launch it if the connection subsequently becomes available.
            if failure == "unavailable":
                (spool / "repository_capabilities.json").write_text(report.model_dump_json())
            assert not dispatch_intent(gateway, dispatch_row)
            with engine.connect() as connection:
                assert not waiting(connection, pid, child["session_id"])
                assert connection.scalar(
                    text(
                        "SELECT payload->>'reason' FROM events WHERE project_id=:p "
                        "AND event_type='self_extension_dispatch_rejected'"
                    ),
                    {"p": pid},
                ) == (
                    "coding_manifest_too_large"
                    if failure == "manifest"
                    else "coding_dispatch_prerequisite_failed"
                )
                assert (
                    connection.scalar(
                        text("SELECT status FROM task_session_assignments WHERE project_id=:p AND id=:id"),
                        {"p": pid, "id": UUID(assigned["assignment_id"])},
                    )
                    == "blocked"
                )
                assert (
                    connection.scalar(
                        text("SELECT count(*) FROM external_code_attempts WHERE project_id=:p"), {"p": pid}
                    )
                    == 0
                )
                assert (
                    connection.scalar(
                        text(
                            "SELECT count(*) FROM task_session_decision_causes "
                            "WHERE project_id=:p AND kind='blocker'"
                        ),
                        {"p": pid},
                    )
                    == 1
                )
                assert (
                    connection.scalar(
                        text(
                            "SELECT count(*) FROM events WHERE project_id=:p "
                            "AND event_type='self_extension_dispatch_rejected'"
                        ),
                        {"p": pid},
                    )
                    == 1
                )
            from onefoundry.task_session_decisions import TaskSessionDecisions

            TaskSessionDecisions(gateway).dispatch(pid, UUID(order["id"]))
            followup = attempts.claim_next(pid)
            assert followup is not None and followup["session_id"] == turn["session_id"]
            bind(gateway, pid, followup)
            recovery = TaskSessionTools(gateway, pid, followup["session_id"], followup["id"])
            assert (
                payload(
                    recovery.invoke(
                        "onefoundry.cancel_assignment",
                        {"assignment_id": assigned["assignment_id"]},
                        "cancel-rejected",
                    )
                )["status"]
                == "canceled"
            )
            attempts.settle(pid, followup["id"], "done", public_reply="Known prelaunch failure retired.")
            engine.dispose()
            return
        assert dispatch_intent(gateway, dispatch_row)
        assert not reject(gateway, pid, build_request, "coding_dispatch_prerequisite_failed")
        manifest = next((spool / "jobs").glob("*.json"))

        def failed_check(*args: Any, **kwargs: Any) -> tuple[dict[str, Any], bytes]:
            return {
                "name": "observed",
                "status": "failed",
                "exit_code": 1,
            }, b"actual dependency fixture failure"

        receipt = process_job(manifest, spool, root, fake_provider, test_runner=failed_check)
        assert receipt is not None and receipt["status"] == "failed"
        # The global worker may also advance other test projects. Check this exact cause below.
        for _ in range(8):
            reconcile(gateway)
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT status FROM task_session_assignments WHERE project_id=:p AND id=:id"),
                    {"p": pid, "id": UUID(assigned["assignment_id"])},
                )
                == "blocked"
            )
            causes = connection.scalar(
                text(
                    "SELECT count(*) FROM task_session_decision_causes WHERE project_id=:p AND kind='blocker'"
                ),
                {"p": pid},
            )
            assert causes == 1
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM external_code_attempts WHERE project_id=:p"), {"p": pid}
                )
                == 1
            )
        progress = client.get(base + f"/work-orders/{order['id']}/extensions").json()
        assert progress[0]["stage"] == "coding_failed"
        # Failure artifacts reconcile and remain readable for repair, while the
        # same failed receipt cannot become an accepted Engineer/QA candidate.
        detail = gateway._execution_gateway().detail(pid, UUID(receipt["run_id"]))
        failed_attempt = detail["attempts"][-1]
        assert failed_attempt["status"] == "failed"
        assert {item["kind"] for item in failed_attempt["artifacts"]} == {"diff", "log"}
        from onefoundry.role_coding_artifact import coding_artifact
        from onefoundry.role_coding_evidence import coding_evidence
        from onefoundry.runner_spool import RunnerSpool
        from tests.test_role_coding_artifact_unit import _context

        with engine.connect() as connection:
            evidence = coding_evidence(connection, _context(pid), {"run_id": receipt["run_id"]})
            observed = evidence["attempts"][0]
            assert observed["target_id"] == "repository_code_v1"
            assert observed["source_kind"] == "local" and observed["source_id"] == local["id"]
            assert observed["source_revision"] == local["revision"] and len(observed["tests"]) == 10
            log = next(item for item in observed["artifacts"] if item["kind"] == "log")
            content = coding_artifact(
                connection,
                _context(pid),
                {"run_id": receipt["run_id"], "artifact_id": log["artifact_id"]},
                RunnerSpool(str(spool)),
            )
            assert content["found"] and "actual dependency fixture failure" in content["text"]
        from onefoundry.gateway_errors import Conflict
        from onefoundry.self_extension_results import _coding

        with pytest.raises(Conflict, match="not a confirmed candidate"):
            _coding(engineering, {"root_work_order_id": order["id"]}, UUID(receipt["run_id"]))
        engine.dispose()


def test_cold_start_reconciles_under_maintenance_without_admitting_work(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from threading import Event

    from onefoundry.remote_channel_core import RemoteChannelCore

    settings = settings_for(tmp_path)
    settings.runner_spool_root = str(tmp_path / "spool")
    external = Path(settings.runner_spool_root) / "external"
    external.mkdir(parents=True)
    lease = external / "runtime-maintenance.json"
    lease.write_text('{"kind":"test_bootstrap_lease"}')
    reconciled = Event()
    original = RemoteChannelCore.reconcile_interrupted

    def observed(core: RemoteChannelCore) -> None:
        original(core)
        reconciled.set()

    monkeypatch.setattr(RemoteChannelCore, "reconcile_interrupted", observed)
    app = create_app(settings)
    with TestClient(app, base_url="http://localhost:8765") as client:
        assert reconciled.wait(timeout=3), "Maintenance blocks required startup reconciliation"
        assert client.get("/health/ready").status_code == 200
        response = client.post("/api/projects", json={"name": "Must remain paused"}, headers=ORIGIN)
        assert response.status_code == 409
        assert lease.exists()
        assert app.state.remote_channel_runtime._workers == {}
