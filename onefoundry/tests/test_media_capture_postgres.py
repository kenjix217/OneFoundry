"""SDK recording requests, operator byte ownership and distinct playback evidence."""

import hashlib
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event
from typing import Any
from uuid import UUID, uuid4

import pytest
from agents.testing import ModelStep, ScriptedModel, assistant_message
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from onefoundry.artifact_workspace import ArtifactWorkspace
from onefoundry.gateway_errors import Conflict
from onefoundry.main import create_app
from onefoundry.media_capture_store import MediaCaptureStore
from onefoundry.task_session_assignments import DelegateRequest, TaskSessionAssignments
from onefoundry.task_session_attempts import TaskSessionAttempts
from onefoundry.task_session_decisions import TaskSessionDecisions
from onefoundry.task_session_sdk_turn import TaskSessionSdkTurn, TeamModel
from onefoundry.task_session_tools import TaskSessionTools
from onefoundry.tool_gateway import ToolGateway
from tests.test_media_capture_contracts import WEBM_HEADER
from tests.test_team_workflow_postgres import ORIGIN, _step, activate_fixture, settings_for
from tests.test_team_workflow_postgres import quiet_worker as quiet_worker


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "disabled",
        "agent_actor",
        "revoked_before",
        "revoked_during",
        "storage_failure",
        "upload_timeout",
        "concurrent_duplicate",
    ],
)
def test_sdk_request_upload_hash_playback_and_no_implicit_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    settings = settings_for(tmp_path).model_copy(
        update={"media_capture_enabled": True, "media_max_bytes": 1024}
    )
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, oid, profiles, base = activate_fixture(client)
        pid = UUID(project)
        assert (
            client.patch(
                f"/api/projects/{project}/profiles/{profiles['GTM']}",
                json={"allowed_tools": ["request_video_capture_v1"]},
                headers=ORIGIN,
            ).status_code
            == 200
        )
        gateway = ToolGateway(
            create_engine(settings.database_url), settings.operator_actor_id, live_text_settings=settings
        )
        attempts = TaskSessionAttempts(gateway)
        client.post(f"{base}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN)
        chief = attempts.claim_next(pid)
        assert chief is not None
        main = client.get(base).json()["session"]["id"]
        gtm_assignment = TaskSessionAssignments(gateway).delegate(
            pid,
            UUID(main),
            chief["id"],
            DelegateRequest(
                client_action_id=uuid4(),
                profile_id=UUID(profiles["GTM"]),
                objective="Request one local recording",
                expected_result="An exact saved video",
                acceptance_check="Operator playback and independent QA",
                acceptance_kind="artifact",
            ),
        )
        if fault == "none":
            TaskSessionAssignments(gateway).delegate(
                pid,
                UUID(main),
                chief["id"],
                DelegateRequest(
                    client_action_id=uuid4(),
                    profile_id=UUID(profiles["QA"]),
                    objective="Review the exact local video",
                    expected_result="An independent QA verdict",
                    acceptance_check="Exact saved hash and operator playback evidence",
                    acceptance_kind="qa_review",
                    depends_on=(gtm_assignment["id"],),
                ),
            )
        attempts.settle(pid, chief["id"], "done", public_reply="The recording request is delegated.")
        model = ScriptedModel(
            [
                _step(
                    "role_tool",
                    {
                        "tool_id": "request_video_capture_v1",
                        "arguments": {
                            "title": "Disposable local recording",
                            "caption": "Synthetic app fixture; no live company data.",
                        },
                    },
                ),
                ModelStep(
                    output=[assistant_message("Wait for operator recording; no recording was launched.")]
                ),
            ]
        )
        worker = TaskSessionSdkTurn(
            gateway,
            settings,
            model_factory=lambda _runtime, _settings: TeamModel(
                model, "ollama", "fixture-text", False, False
            ),
        )
        result = worker.run_next(pid)
        assert result is not None and result["outcome"] == "done", result
        captures = client.get(f"/api/projects/{project}/work-orders/{oid}/media-captures").json()
        assert len(captures) == 1 and captures[0]["status"] == "waiting_for_operator"
        waiting = client.get(f"{base}/sessions/{gtm_assignment['child_session_id']}").json()
        assert waiting["session"]["status"] == "waiting_for_operator"
        assert waiting["session"]["waiting_reason"] == "local_recording"
        assert client.get(base).json()["decision_causes"] == []
        assert "waiting for your local recording" in client.get(base + "/readiness").json()["next_step"]
        capture = captures[0]
        rid = capture["run_id"]
        path = f"/api/projects/{project}/runs/{rid}/media-captures/{capture['capture_id']}"
        with gateway.engine.connect() as c:
            count = c.scalar(
                text("SELECT count(*) FROM task_session_attempts WHERE project_id=:p"), {"p": pid}
            )
        if fault != "none":
            if fault == "concurrent_duplicate":
                entered, release = Event(), Event()
                original = MediaCaptureStore._store_upload

                def pause_write(self: MediaCaptureStore, *args: Any, **kwargs: Any) -> Any:
                    entered.set()
                    assert release.wait(5), "concurrent upload fixture did not release storage"
                    return original(self, *args, **kwargs)

                monkeypatch.setattr(MediaCaptureStore, "_store_upload", pause_write)
                headers = {**ORIGIN, "content-type": "video/webm", "x-onefoundry-recording-reviewed": "true"}
                with ThreadPoolExecutor(max_workers=1) as pool:
                    first = pool.submit(client.post, path, content=WEBM_HEADER, headers=headers)
                    try:
                        assert entered.wait(5)
                        assert client.post(path, content=WEBM_HEADER, headers=headers).status_code == 409
                    finally:
                        release.set()
                    assert first.result(timeout=5).status_code == 200
                with gateway.engine.connect() as c:
                    assert (
                        c.scalar(
                            text(
                                "SELECT count(*) FROM events WHERE project_id=:p "
                                "AND event_type='media_save_started'"
                            ),
                            {"p": pid},
                        )
                        == 1
                    )
            elif fault == "upload_timeout":

                @asynccontextmanager
                async def timeout(_seconds: float) -> AsyncIterator[None]:
                    yield
                    raise TimeoutError("synthetic intake deadline")

                monkeypatch.setattr("onefoundry.media_capture_api.asyncio.timeout", timeout)
                headers = {**ORIGIN, "content-type": "video/webm", "x-onefoundry-recording-reviewed": "true"}
                assert client.post(path, content=WEBM_HEADER, headers=headers).status_code == 408
            elif fault in ("disabled", "agent_actor"):
                owner_gateway = ToolGateway(
                    gateway.engine,
                    settings.operator_actor_id if fault == "disabled" else f"profile:{profiles['GTM']}",
                    live_text_settings=settings.model_copy(update={"media_capture_enabled": False})
                    if fault == "disabled"
                    else settings,
                )
                with pytest.raises(Conflict):
                    MediaCaptureStore(owner_gateway).save(
                        pid, UUID(rid), UUID(capture["capture_id"]), WEBM_HEADER, reviewed_window=True
                    )
            else:

                def revoke() -> None:
                    with gateway.engine.begin() as c:
                        c.execute(
                            text(
                                "UPDATE profiles SET allowed_tools='[]'::jsonb WHERE project_id=:p AND id=:id"
                            ),
                            {"p": pid, "id": UUID(profiles["GTM"])},
                        )

                if fault == "revoked_before":
                    revoke()
                elif fault == "storage_failure":

                    def fail_storage(*_args: Any, **_kwargs: Any) -> None:
                        raise OSError("synthetic storage unavailable")

                    monkeypatch.setattr(ArtifactWorkspace, "write", fail_storage)
                else:
                    original = MediaCaptureStore._store_upload

                    def revoke_after_write(self: MediaCaptureStore, *args: Any, **kwargs: Any) -> Any:
                        result = original(self, *args, **kwargs)
                        revoke()
                        return result

                    monkeypatch.setattr(MediaCaptureStore, "_store_upload", revoke_after_write)
                headers = {**ORIGIN, "content-type": "video/webm", "x-onefoundry-recording-reviewed": "true"}
                assert client.post(path, content=WEBM_HEADER, headers=headers).status_code == 409
                assert client.post(path, content=WEBM_HEADER, headers=headers).status_code == 409
            with gateway.engine.connect() as c:
                states = c.scalars(
                    text("SELECT status FROM workspace_artifacts WHERE project_id=:p"), {"p": pid}
                ).all()
                assert states == (
                    ["unknown"]
                    if fault in ("revoked_during", "storage_failure")
                    else ["ready"]
                    if fault == "concurrent_duplicate"
                    else []
                )
                assert (
                    c.scalar(
                        text("SELECT count(*) FROM task_session_attempts WHERE project_id=:p"), {"p": pid}
                    )
                    == count
                )
            gateway.engine.dispose()
            return
        assert (
            client.post(
                path, content=WEBM_HEADER, headers={**ORIGIN, "content-type": "text/plain"}
            ).status_code
            == 415
        )
        assert (
            client.post(
                path, content=WEBM_HEADER, headers={**ORIGIN, "content-type": "video/webm"}
            ).status_code
            == 409
        )
        headers = {**ORIGIN, "content-type": "video/webm", "x-onefoundry-recording-reviewed": "true"}
        assert client.post(path, content=b"", headers=headers).status_code == 422
        assert client.post(path, content=WEBM_HEADER * 100, headers=headers).status_code == 413
        saved = client.post(path, content=WEBM_HEADER, headers=headers)
        assert saved.status_code == 200, saved.text
        artifact = saved.json()
        assert (
            client.get(f"{base}/sessions/{gtm_assignment['child_session_id']}").json()["session"][
                "waiting_reason"
            ]
            == "recording_ready"
        )
        aid = artifact["id"]
        assert artifact["kind"] == "video" and artifact["playback_verified"] is False
        assert artifact["sha256"] == hashlib.sha256(WEBM_HEADER).hexdigest()
        assert client.post(path, content=WEBM_HEADER, headers=headers).status_code == 200
        assert client.post(path, content=WEBM_HEADER + b"x", headers=headers).status_code == 409
        owner = f"/api/projects/{project}/runs/{rid}/workspace-artifacts/{aid}"
        assert client.get(owner + "/bytes").content == WEBM_HEADER
        part = client.get(owner + "/bytes", headers={"range": "bytes=0-3"})
        assert part.status_code == 206 and part.content == WEBM_HEADER[:4]
        assert client.get(owner + "/bytes", headers={"range": "bytes=999-1000"}).status_code == 416
        assert client.get(owner + "/bytes", headers={"range": "bytes=" + "9" * 5000 + "-"}).status_code == 416
        assert client.get(owner.replace(project, str(uuid4())) + "/bytes").status_code == 404
        verification = {
            "expected_sha256": artifact["sha256"],
            "watched_picture_and_captions": True,
            "narration_reviewed_if_present": True,
        }
        assert (
            client.post(
                owner + "/verify-playback", json={**verification, "expected_sha256": "a" * 64}, headers=ORIGIN
            ).status_code
            == 409
        )
        with gateway.engine.connect() as c:
            assert (
                c.scalar(text("SELECT count(*) FROM task_session_attempts WHERE project_id=:p"), {"p": pid})
                == count
            )
        child = str(gtm_assignment["child_session_id"])
        queued = client.post(
            f"{base}/sessions/{child}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN
        )
        assert queued.status_code == 202, queued.text
        reporter = ScriptedModel(
            [
                _step(
                    "report_result",
                    {
                        "body": "Exact synthetic video; independent playback still required.",
                        "artifact_id": aid,
                        "artifact_run_id": rid,
                    },
                ),
                ModelStep(output=[assistant_message("Video artifact reported for QA.")]),
            ]
        )
        report_worker = TaskSessionSdkTurn(
            gateway,
            settings,
            model_factory=lambda _runtime, _settings: TeamModel(
                reporter, "ollama", "fixture-text", False, False
            ),
        )
        reported = report_worker.run_next(pid)
        assert reported is not None and reported["outcome"] == "done", reported
        qa = attempts.claim_next(pid)
        assert qa is not None
        tools = TaskSessionTools(gateway, pid, qa["session_id"], qa["id"])
        denied = tools.invoke(
            "onefoundry.report_result",
            {"body": "Attempting QA before playback", "verdict": "passed"},
            "before-playback",
        )
        assert denied["success"] is False
        with gateway.engine.connect() as c:
            count = c.scalar(
                text("SELECT count(*) FROM task_session_attempts WHERE project_id=:p"), {"p": pid}
            )
        assert client.post(owner + "/verify-playback", json=verification, headers=ORIGIN).status_code == 200
        assert client.post(owner + "/verify-playback", json=verification, headers=ORIGIN).status_code == 200
        assert client.get(owner).json()["playback_verified"] is True
        with gateway.engine.connect() as c:
            assert (
                c.scalar(text("SELECT count(*) FROM task_session_attempts WHERE project_id=:p"), {"p": pid})
                == count
            )
            assert (
                c.scalar(
                    text(
                        "SELECT count(*) FROM events WHERE project_id=:p "
                        "AND event_type='media_playback_verified'"
                    ),
                    {"p": pid},
                )
                == 1
            )
        passed = tools.invoke(
            "onefoundry.report_result",
            {"body": "Exact hash and explicit operator playback reviewed.", "verdict": "passed"},
            "after-playback",
        )
        assert passed["success"] is True, passed
        attempts.settle(pid, qa["id"], "done", public_reply="Synthetic video passed the QA evidence gates.")
        assert TaskSessionDecisions(gateway).dispatch_next(pid) is not None
        completion = ScriptedModel(
            [
                _step(
                    "submit_completion",
                    {
                        "body": "Synthetic video and exact QA receipt submitted.",
                        "artifact_id": aid,
                        "artifact_run_id": rid,
                    },
                ),
                ModelStep(output=[assistant_message("Video ready for operator review.")]),
            ]
        )
        final_worker = TaskSessionSdkTurn(
            gateway,
            settings,
            model_factory=lambda _runtime, _settings: TeamModel(
                completion, "ollama", "fixture-text", False, False
            ),
        )
        final = final_worker.run_next(pid)
        assert final is not None and final["outcome"] == "done", final
        order = next(w for w in client.get(f"/api/projects/{project}/work-orders").json() if w["id"] == oid)
        assert order["status"] == "needs_review"
        accepted = client.post(
            f"/api/projects/{project}/work-orders/{oid}/accept",
            json={
                "expected_version": order["version"],
                "reason": "Synthetic fixture acceptance only; no actual video claim.",
            },
            headers=ORIGIN,
        )
        assert accepted.status_code == 200 and accepted.json()["status"] == "done", accepted.text
        gateway.engine.dispose()
