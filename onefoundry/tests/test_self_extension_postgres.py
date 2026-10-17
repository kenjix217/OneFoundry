"""Native tools, exact attempts, independent QA and inbox boundaries on PostgreSQL."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from onefoundry.gateway_errors import Conflict
from onefoundry.main import create_app
from onefoundry.repository_code_contract import RepositoryCodeTarget, RepositoryRunnerCapability
from onefoundry.task_session_attempts import TaskSessionAttempts
from onefoundry.task_session_runs import bind
from onefoundry.task_session_tools import TaskSessionTools
from onefoundry.tool_gateway import ToolGateway
from runner.extension_qa_service import process_qa
from runner.repository_code_service import process_job
from tests.test_repository_code import fake_provider, fake_test, job_at
from tests.test_team_workflow_postgres import ORIGIN, project_with_models, settings_for


def payload(reply: dict[str, Any]) -> dict[str, Any]:
    assert reply["success"], reply
    return dict(json.loads(reply["contentItems"][0]["text"]))


@pytest.fixture(autouse=True)
def quiet_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    async def quiet(_self: object) -> None:
        return None

    monkeypatch.setattr("onefoundry.coordination_launcher.CoordinationLauncher.start", quiet)


def test_native_extension_build_qa_and_scope_denials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # This state-machine fixture models a missing repository owner; the real
    # GitHub reader has its own byte and project/account acceptance tests.
    from onefoundry import capability_inspection
    from onefoundry.capability_schemas import CapabilityBlocker, capability_check

    actual_repository = capability_inspection._repository

    def missing_fixture_owner(source: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        check = actual_repository(source, *args, **kwargs) if source["kind"] != "github" else None
        if check is not None:
            return check
        return capability_check(
            "repository_read",
            "Fixture reader",
            "fixture",
            [
                CapabilityBlocker(
                    kind="missing_implementation",
                    code="fixture_reader_missing",
                    owner="fixture",
                    message="The fixture has no reader.",
                    next_step="Implement the approved acceptance fixture.",
                )
            ],
            source_id=UUID(str(source["id"])),
            revision=source["revision"],
        )

    monkeypatch.setattr(capability_inspection, "_repository", missing_fixture_owner)
    seed, root, spool = job_at(tmp_path)
    settings = settings_for(tmp_path)
    settings.local_source_root = str(root)
    settings.runner_spool_root = str(tmp_path)
    input_root = tmp_path / "input-files"
    input_root.mkdir(mode=0o700)
    settings.input_file_root = str(input_root)
    (tmp_path / "external").mkdir()
    for name in ("jobs", "claims", "receipts", "artifacts"):
        (tmp_path / "external" / name).mkdir()
    spool = tmp_path / "external"
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, profiles = project_with_models(client)
        base = f"/api/projects/{project}"
        chief = next(p for p in client.get(base + "/profiles").json() if p["name"] == "Chief of Staff")
        for name in ("Chief of Staff", "Engineer"):
            changed = client.patch(
                base + f"/profiles/{profiles[name]}",
                json={
                    "model": chief["model"],
                    "model_account_id": chief["model_account_id"],
                    "allowed_tools": ["repository_file_v1"],
                    "autonomy_level": "supervised",
                },
                headers=ORIGIN,
            )
            assert changed.status_code == 200, changed.text
        local = client.post(
            base + "/sources/local", json={"root_id": "workspace", "relative_path": "repo"}, headers=ORIGIN
        ).json()
        order = client.post(
            base + "/work-orders",
            json={
                "title": "Read remote docs",
                "instructions": "Read the committed docs and retain reviewed company knowledge.",
                "profile_id": profiles["Chief of Staff"],
                "deliverable_kind": "operator_review",
            },
            headers=ORIGIN,
        ).json()
        assert client.get(f"/api/projects/{uuid4()}/work-orders/{order['id']}/extensions").status_code == 404
        assert client.get(base + f"/work-orders/{uuid4()}/extensions").status_code == 404
        assert client.get(f"/api/projects/{uuid4()}/extension-activation/preflight").status_code == 404
        target = RepositoryCodeTarget(
            source_id=UUID(local["id"]),
            provider_url=seed.target.provider_url,
            task_scope_id=UUID(order["id"]),
            adapter_id="codex_cli",
            revision=local["revision"],
            allowed_paths=["calculator.py"],
            test_image=seed.target.test_image,
            test_commands=seed.target.test_commands,
        )
        with patch("onefoundry.source_store.resolve_revision", return_value=("main", "a" * 40)):
            account = client.post(
                base + "/github/accounts",
                json={"label": "Offline fixture", "token": "test-only-github-credential"},
                headers=ORIGIN,
            ).json()
            remote = client.post(
                base + "/sources/github",
                json={"account_id": account["id"], "owner": "example", "repo": "docs", "branch": "main"},
                headers=ORIGIN,
            ).json()
        started = client.post(
            base + f"/work-orders/{order['id']}/team-session/start",
            json={
                "client_message_id": str(uuid4()),
                "expected_version": order["version"],
                "body": "Read repository documents.",
            },
            headers=ORIGIN,
        )
        assert started.status_code == 202, started.text
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
        claimed = attempts.claim_next(UUID(project))
        assert claimed is not None
        bind(gateway, UUID(project), claimed)
        tools = TaskSessionTools(gateway, UUID(project), claimed["session_id"], claimed["id"])
        proposal = payload(
            tools.invoke(
                "onefoundry.propose_maintenance_target", target.model_dump(mode="json"), "maintenance-target"
            )
        )
        assert (
            payload(
                tools.invoke(
                    "onefoundry.propose_maintenance_target",
                    target.model_dump(mode="json"),
                    "maintenance-target",
                )
            )
            == proposal
        )
        approval_id = proposal["approval_id"]
        decision = client.post(
            base + f"/approvals/{approval_id}/decision", json={"decision": "approved"}, headers=ORIGIN
        )
        assert decision.status_code == 200, decision.text
        memory_marker = "extension" + uuid4().hex
        knowledge = {
            "summary": f"The selected repository contains a local addition function. {memory_marker}",
            "scope": "company",
            "citations": [
                {
                    "source_id": local["id"],
                    "revision": local["revision"],
                    "path": "calculator.py",
                    "sha256": hashlib.sha256(b"def add(a, b):\n    return a-b\n").hexdigest(),
                    "excerpt": "def add(a, b):\n    return a-b\n",
                }
            ],
        }
        memory_proposal = payload(
            tools.invoke("onefoundry.propose_company_knowledge", knowledge, "knowledge")
        )
        assert client.get(base + f"/memory?scope=company&query={memory_marker}").json() == []
        approved = client.post(
            base + f"/approvals/{memory_proposal['approval_id']}/decision",
            json={"decision": "approved"},
            headers=ORIGIN,
        )
        assert approved.status_code == 200, approved.text
        candidates = client.get(base + "/candidates?status=pending").json()
        fact = next(item for item in candidates if item["summary"] == knowledge["summary"])
        assert client.get(base + f"/memory?scope=company&query={memory_marker}").json() == []
        assert (
            client.post(
                base + f"/candidates/{fact['id']}/review", json={"decision": "approved"}, headers=ORIGIN
            ).status_code
            == 200
        )
        retrieved = client.get(base + f"/memory?scope=company&query={memory_marker}").json()
        assert len(retrieved) == 1 and retrieved[0]["text"] == knowledge["summary"]
        rejected_knowledge = payload(
            tools.invoke("onefoundry.propose_company_knowledge", knowledge, "knowledge-rejected")
        )
        assert (
            client.post(
                base + f"/approvals/{rejected_knowledge['approval_id']}/decision",
                json={"decision": "rejected"},
                headers=ORIGIN,
            ).status_code
            == 200
        )
        assert len(client.get(base + "/candidates?status=pending").json()) == 0
        revoked = payload(
            tools.invoke("onefoundry.propose_company_knowledge", knowledge, "knowledge-revoked")
        )
        changed = client.patch(
            base + f"/profiles/{profiles['Chief of Staff']}", json={"allowed_tools": []}, headers=ORIGIN
        )
        assert changed.status_code == 200
        revoked_decision = client.post(
            base + f"/approvals/{revoked['approval_id']}/decision",
            json={"decision": "approved"},
            headers=ORIGIN,
        )
        assert revoked_decision.status_code == 409 and "revoked" in revoked_decision.text
        assert any(
            item["id"] == revoked["approval_id"]
            for item in client.get(base + "/approvals?status=pending").json()
        )
        assert (
            client.patch(
                base + f"/profiles/{profiles['Chief of Staff']}",
                json={"allowed_tools": ["repository_file_v1"]},
                headers=ORIGIN,
            ).status_code
            == 200
        )
        assert (
            client.post(
                base + f"/approvals/{revoked['approval_id']}/decision",
                json={"decision": "rejected"},
                headers=ORIGIN,
            ).status_code
            == 200
        )
        request = {
            "operation": "repository_read",
            "gap_source_id": remote["id"],
            "gap_revision": remote["revision"],
            "target_approval_id": approval_id,
            "objective": "Repair the disposable addition acceptance fixture.",
            "acceptance_check": "Addition returns the expected sum.",
            "checkpoint": "Then continue reading the original docs.",
        }
        missing = payload(
            tools.invoke("onefoundry.inspect_capability", {"tool_id": "fixture_addition_v1"}, "inspect-new")
        )
        assert missing["status"] == "missing_implementation"
        denied_tool = payload(
            tools.invoke(
                "onefoundry.inspect_capability", {"tool_id": "write_text_artifact_v1"}, "inspect-denied"
            )
        )
        assert denied_tool["status"] == "permission_required"
        assigned = payload(tools.invoke("onefoundry.request_extension", request, "request-extension"))
        assert payload(tools.invoke("onefoundry.request_extension", request, "request-extension")) == assigned
        repeated = payload(tools.invoke("onefoundry.request_extension", request, "same-gap-new-call"))
        assert repeated == assigned
        progress = client.get(base + f"/work-orders/{order['id']}/extensions").json()
        assert len(progress) == 1 and progress[0]["extension_key"] == assigned["extension_key"]
        denied = {**request, "operation": "public_web_search", "gap_source_id": None, "gap_revision": None}
        assert (
            tools.invoke("onefoundry.request_extension", denied, "setup-is-not-feature")["success"] is False
        )
        attempts.settle(UUID(project), claimed["id"], "done", public_reply="Engineer is assigned.")
        engineer = attempts.claim_next(UUID(project))
        assert engineer is not None and engineer["session_id"] == UUID(assigned["child_session_id"])
        bind(gateway, UUID(project), engineer)
        engineering = TaskSessionTools(gateway, UUID(project), engineer["session_id"], engineer["id"])
        with pytest.raises(Conflict, match="not admitted"):
            engineering.invoke("onefoundry.request_extension", request, "engineer-cannot-chief")
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
        built = payload(
            engineering.invoke(
                "onefoundry.build_extension",
                {"target_approval_id": approval_id, "adapter_id": "codex_cli"},
                "build",
            )
        )
        assert built["status"] == "waiting_for_turn"
        attempts.settle(
            UUID(project), engineer["id"], "done", public_reply="Coding is queued after this turn."
        )
        from onefoundry.self_extension_dispatch import step as dispatch_extension

        assert dispatch_extension(gateway)
        with engine.connect() as connection:
            code = (
                connection.execute(
                    text(
                        "SELECT a.id AS attempt_id,a.run_id FROM external_code_attempts a JOIN runs r "
                        "ON r.project_id=a.project_id AND r.id=a.run_id "
                        "WHERE a.project_id=:p AND r.work_order_id=:o"
                    ),
                    {"p": UUID(project), "o": engineer["work_order_id"]},
                )
                .mappings()
                .one()
            )
        built = {"run_id": str(code["run_id"]), "attempt_id": str(code["attempt_id"])}
        # The approved immutable wait replaces the legacy short fixture timeout.
        with patch("onefoundry.execution_gateway.datetime", wraps=datetime) as clock:
            clock.now.return_value = datetime.now(UTC) + timedelta(seconds=300)
            waiting = gateway._execution_gateway().reconcile(UUID(project), code["run_id"])
        assert waiting["attempts"][-1]["status"] == "queued"
        assert (spool / "jobs" / f"{built['attempt_id']}.json").exists()
        conversation = next(
            row for row in client.get(base + "/chief-workspace").json() if row["id"] == order["id"]
        )
        assert conversation["state"] == "Working"
        assert conversation["detail"] is None
        receipt = process_job(
            spool / "jobs" / f"{built['attempt_id']}.json", spool, root, fake_provider, test_runner=fake_test
        )
        assert receipt is not None and receipt["status"] == "done", receipt
        from onefoundry.self_extension_continuation import step as continue_extension

        assert continue_extension(gateway)
        returned = attempts.claim_next(UUID(project))
        assert returned is not None and returned["session_id"] == engineer["session_id"]
        bind(gateway, UUID(project), returned)
        engineering = TaskSessionTools(gateway, UUID(project), returned["session_id"], returned["id"])
        engineer = returned
        finished = payload(
            engineering.invoke(
                "onefoundry.finish_extension_build", {"run_id": built["run_id"]}, "finish-build"
            )
        )
        assert finished["assignment_id"] == assigned["assignment_id"]
        attempts.settle(
            UUID(project), engineer["id"], "done", public_reply="Candidate is saved for independent QA."
        )
        # Use the existing Chief continuation to assign a QA dependency.
        from onefoundry.task_session_decisions import TaskSessionDecisions

        TaskSessionDecisions(gateway).dispatch_next(UUID(project))
        chief_turn = attempts.claim_next(UUID(project))
        assert chief_turn is not None
        bind(gateway, UUID(project), chief_turn)
        chief_tools = TaskSessionTools(gateway, UUID(project), chief_turn["session_id"], chief_turn["id"])
        qa_assignment = payload(
            chief_tools.invoke(
                "onefoundry.delegate_agent",
                {
                    "profile_id": profiles["QA"],
                    "objective": "Independently verify this exact coding candidate.",
                    "expected_result": "Observed isolated QA receipt",
                    "acceptance_check": "Original addition acceptance works.",
                    "acceptance_kind": "qa_review",
                    "depends_on": [assigned["assignment_id"]],
                },
                "qa",
            )
        )
        attempts.settle(UUID(project), chief_turn["id"], "done", public_reply="Independent QA is assigned.")
        qa_turn = attempts.claim_next(UUID(project))
        assert qa_turn is not None and qa_turn["session_id"] == UUID(qa_assignment["child_session_id"])
        bind(gateway, UUID(project), qa_turn)
        qa_tools = TaskSessionTools(gateway, UUID(project), qa_turn["session_id"], qa_turn["id"])
        qa = payload(
            qa_tools.invoke("onefoundry.verify_extension_candidate", {"run_id": built["run_id"]}, "verify")
        )
        qa_receipt = process_qa(
            spool / "jobs" / f"{qa['qa_job_id']}.json", spool, root, test_runner=fake_test
        )
        assert qa_receipt is not None and qa_receipt["status"] == "passed", qa_receipt
        payload(
            qa_tools.invoke("onefoundry.finish_extension_review", {"qa_job_id": qa["qa_job_id"]}, "finish-qa")
        )
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM external_code_attempts WHERE project_id=:p AND fi"
                        "xture_id='repository_code_v1'"
                    ),
                    {"p": UUID(project)},
                )
                == 1
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM events WHERE project_id=:p AND event_type='self_e"
                        "xtension_qa_verified'"
                    ),
                    {"p": UUID(project)},
                )
                == 1
            )
        # Activation is a persisted state-machine fixture here. Installed image
        # readback and rollback are exercised by the separate supervisor tests.
        attempts.settle(UUID(project), qa_turn["id"], "done", public_reply="Independent QA passed.")
        TaskSessionDecisions(gateway).dispatch_next(UUID(project))
        review_turn = attempts.claim_next(UUID(project))
        assert review_turn is not None
        bind(gateway, UUID(project), review_turn)
        reviewer = TaskSessionTools(gateway, UUID(project), review_turn["session_id"], review_turn["id"])
        from onefoundry.extension_activation_contract import ExtensionRelease, ExtensionReleaseRecord
        from onefoundry.repository_code_observation import saved_manifest
        from onefoundry.runner_spool import RunnerSpool
        from onefoundry.self_extension_resume import require_resumed
        from onefoundry.self_extension_resume import step as resume_step

        release = ExtensionRelease(
            coding_run_id=UUID(built["run_id"]),
            qa_job_id=UUID(qa["qa_job_id"]),
            version="0.14.0",
            archive_sha256="a" * 64,
            app_image="sha256:" + "b" * 64,
            expected_installed_version="0.13.0",
            expected_installed_image="sha256:" + "c" * 64,
            configuration_effects=[],
            migration_effects=[],
            rollback_plan="Restore the verified previous release and complete data checkpoint.",
        )
        denied_release = reviewer.invoke(
            "onefoundry.propose_extension_activation", release.model_dump(mode="json"), "unverified-release"
        )
        assert denied_release["success"] is False
        manifest = saved_manifest(gateway._execution_gateway(), UUID(project), UUID(built["run_id"]))
        code_receipt = RunnerSpool(str(spool)).read_receipt(manifest.attempt_id)
        qa_observed = RunnerSpool(str(spool)).read_receipt(UUID(qa["qa_job_id"]))
        assert code_receipt is not None and qa_observed is not None
        release_record = ExtensionReleaseRecord(
            project_id=UUID(project),
            candidate_commit=code_receipt[0]["commit"],
            diff_sha256=code_receipt[0]["diff_sha256"],
            qa_receipt_sha256=qa_observed[1],
            release=release,
        )
        from onefoundry.extension_release_capability import ReleaseWorkerCapability
        from onefoundry.extension_release_preparation_contract import ReleasePreparationReceipt
        from onefoundry.self_extension_release import step as package_step
        from runner.repository_transport import publish_new

        prepare = {
            key: release.model_dump(mode="json")[key]
            for key in (
                "coding_run_id",
                "qa_job_id",
                "configuration_effects",
                "migration_effects",
                "rollback_plan",
            )
        }
        assert not reviewer.invoke("onefoundry.prepare_extension_release", prepare, "missing-worker")[
            "success"
        ]
        publish_new(
            spool / "release_capabilities.json",
            ReleaseWorkerCapability(
                observed_at=datetime.now(UTC),
                installed_version="0.13.0",
                installed_commit="a" * 40,
                installed_image=release.expected_installed_image,
            )
            .model_dump_json()
            .encode(),
        )
        prepared = payload(reviewer.invoke("onefoundry.prepare_extension_release", prepare, "prepare"))
        assert (
            payload(reviewer.invoke("onefoundry.prepare_extension_release", prepare, "prepare-again"))
            == prepared
        )
        assert not package_step(gateway)
        assert (spool / "jobs" / f"{prepared['job_id']}.json").is_file()
        attempts.settle(
            UUID(project), review_turn["id"], "done", public_reply="Package preparation requested."
        )
        publish_new(
            spool / "receipts" / f"{prepared['job_id']}.json",
            ReleasePreparationReceipt(
                job_id=prepared["job_id"],
                project_id=UUID(project),
                request_sha256=prepared["job"]["request_sha256"],
                status="prepared",
                reason="completed",
                record=release_record,
                observed_at=datetime.now(UTC),
            )
            .model_dump_json()
            .encode(),
        )
        assert package_step(gateway)
        assert not package_step(gateway)
        review_turn = attempts.claim_next(UUID(project))
        assert review_turn is not None
        bind(gateway, UUID(project), review_turn)
        reviewer = TaskSessionTools(gateway, UUID(project), review_turn["session_id"], review_turn["id"])
        (spool / "artifacts" / str(manifest.attempt_id) / "release.json").write_text(
            release_record.model_dump_json()
        )
        rejected_activation = payload(
            reviewer.invoke(
                "onefoundry.propose_extension_activation", release.model_dump(mode="json"), "release-rejected"
            )
        )
        assert (
            client.post(
                base + f"/approvals/{rejected_activation['approval_id']}/decision",
                json={"decision": "rejected"},
                headers=ORIGIN,
            ).status_code
            == 200
        )
        assert not (spool / "jobs" / f"{rejected_activation['approval_id']}.json").exists()
        activation = payload(
            reviewer.invoke(
                "onefoundry.propose_extension_activation", release.model_dump(mode="json"), "release-approved"
            )
        )
        assert (
            client.post(
                base + f"/approvals/{activation['approval_id']}/decision",
                json={"decision": "approved"},
                headers=ORIGIN,
            ).status_code
            == 200
        )
        lease_path = base + f"/extension-activation/{activation['approval_id']}"
        acquired = client.post(
            lease_path + "/quiesce", json={"proposal_digest": activation["proposal_digest"]}, headers=ORIGIN
        )
        assert acquired.status_code == 200 and acquired.json() == {"acquired": False}
        attempts.settle(
            UUID(project),
            review_turn["id"],
            "done",
            public_reply="The exact activation is approved and queued.",
        )
        uploaded = client.post(
            base + "/input-files",
            content=b"Disposable extraction must finish before activation.",
            headers={**ORIGIN, "X-File-Name": "pending.txt", "Content-Type": "application/octet-stream"},
        )
        assert uploaded.status_code == 201 and uploaded.json()["status"] == "pending"
        waiting_for_input = client.post(
            lease_path + "/quiesce", json={"proposal_digest": activation["proposal_digest"]}, headers=ORIGIN
        )
        assert waiting_for_input.json() == {"acquired": False}
        from onefoundry.input_file_bytes import InputFileBytes
        from onefoundry.input_file_worker import process_one

        assert process_one(engine, InputFileBytes(settings.input_file_root), settings)
        assert client.get(base + f"/input-files/{uploaded.json()['id']}").json()["status"] == "ready_text"
        acquired = client.post(
            lease_path + "/quiesce", json={"proposal_digest": activation["proposal_digest"]}, headers=ORIGIN
        )
        assert acquired.status_code == 200 and acquired.json() == {"acquired": True}, acquired.text
        with ThreadPoolExecutor(max_workers=3) as executor:
            acquired_again = list(
                executor.map(
                    lambda _: client.post(
                        lease_path + "/quiesce",
                        json={"proposal_digest": activation["proposal_digest"]},
                        headers=ORIGIN,
                    ).json(),
                    range(3),
                )
            )
        assert acquired_again == [{"acquired": True}] * 3
        from onefoundry.coordination_launcher import CoordinationLauncher
        from onefoundry.runtime_maintenance import for_gateway

        managed_worker = CoordinationLauncher(gateway, settings, maintenance=for_gateway(gateway))
        assert not managed_worker.team_step()
        assert not managed_worker.step()
        assert client.get(base + "/work-orders").status_code == 200
        blocked_write = client.post(
            base + "/work-orders",
            json={
                "title": "Must wait",
                "instructions": "Blocked during update.",
                "profile_id": profiles["Engineer"],
            },
            headers=ORIGIN,
        )
        assert blocked_write.status_code == 409 and "update in progress" in blocked_write.text
        with engine.connect() as connection:
            with pytest.raises(Conflict, match="activation"):
                require_resumed(connection, UUID(project), UUID(order["id"]))
        from onefoundry.extension_activation_contract import ExtensionActivationReceipt

        observed_activation = ExtensionActivationReceipt(
            job_id=UUID(activation["approval_id"]),
            project_id=UUID(project),
            proposal_digest=activation["proposal_digest"],
            status="activated",
            reason="completed",
            installed_version=release.version,
            installed_image=release.app_image,
            capability_ready=True,
            checkpoint_saved=True,
            observed_at=datetime.now(UTC),
        )
        (spool / "receipts" / f"{activation['approval_id']}.json").write_text(
            observed_activation.model_dump_json()
        )
        assert client.post(lease_path + "/release", headers=ORIGIN).status_code == 200
        assert resume_step(gateway)  # Saves the exact observed activation once.
        assert not resume_step(gateway)  # Current GitHub owner still cannot read files.
        from onefoundry.task_execution_capabilities import execution_capabilities as actual_capabilities

        def available_owner(*args: Any, **kwargs: Any) -> dict[str, Any]:
            value = actual_capabilities(*args, **kwargs)
            for check in value["readiness"]["checks"]:
                if check["operation"] == "repository_read" and check["source_id"] == remote["id"]:
                    check.update(status="ready", next_action="use", blockers=[])
            return value

        with patch("onefoundry.self_extension_resume.execution_capabilities", side_effect=available_owner):
            with ThreadPoolExecutor(max_workers=3) as executor:
                resumptions = list(executor.map(lambda _: resume_step(gateway), range(3)))
            assert sum(resumptions) == 1
            assert not resume_step(gateway)
        with engine.connect() as connection:
            require_resumed(connection, UUID(project), UUID(order["id"]))
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM events WHERE project_id=:p "
                        "AND event_type='self_extension_resumed'"
                    ),
                    {"p": UUID(project)},
                )
                == 1
            )
        progress = client.get(base + f"/work-orders/{order['id']}/extensions").json()
        assert progress[0]["stage"] == "resumed"
        foreign = client.post(
            f"/api/projects/{uuid4()}/work-orders/{order['id']}/repository-code/runs",
            json={
                "target_approval_id": approval_id,
                "idempotency_key": str(uuid4()),
                "adapter_id": "codex_cli",
            },
            headers=ORIGIN,
        )
        assert foreign.status_code == 404
        engine.dispose()
