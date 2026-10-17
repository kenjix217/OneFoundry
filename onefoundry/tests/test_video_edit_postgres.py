"""Native scripted agent → Gateway → durable job → separate real renderer → exact team artifact.

No paid model is called. ScriptedModel proves the SDK/tool integration; the FFmpeg
child and PostgreSQL are real. Human hearing and caption readability stay unverified.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import struct
import subprocess
import sys
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from sqlalchemy import create_engine, text

from onefoundry.main import create_app
from onefoundry.role_tool_admission import ToolDenied, ToolInputError
from onefoundry.task_session_assignments import DelegateRequest, TaskSessionAssignments
from onefoundry.task_session_attempts import TaskSessionAttempts
from onefoundry.task_session_sdk_turn import TaskSessionSdkTurn, TeamModel
from onefoundry.tool_gateway import ToolGateway
from onefoundry.video_edit_jobs import VideoEditJobs
from runner.video_edit_worker import VideoEditWorker
from tests.test_role_tool_admission_postgres import _run
from tests.test_team_workflow_postgres import ORIGIN, _step, _tool_reply, activate_fixture, settings_for
from tests.test_team_workflow_postgres import quiet_worker as quiet_worker

TOOLS = ["edit_video_v1", "video_edit_status_v1", "cancel_video_edit_v1"]


def settings_for_edit(tmp_path: Path) -> Any:
    settings = settings_for(tmp_path)
    root = tmp_path / "media-assets"
    root.mkdir(mode=0o700)
    return settings.model_copy(
        update={
            "video_edit_enabled": True,
            "video_edit_asset_root": str(root),
            "video_edit_image": os.environ.get("ONEFOUNDRY_VIDEO_TEST_IMAGE", "sha256:" + "a" * 64),
            "team_turn_timeout_seconds": 120,
            "media_max_width": 640,
            "media_max_height": 480,
        }
    )


def png(color: str) -> bytes:
    image = Image.new("RGB", (640, 360), color)
    draw = ImageDraw.Draw(image)
    for xy in [(0, 0, 24, 24), (615, 0, 639, 24), (0, 335, 24, 359), (615, 335, 639, 359)]:
        draw.rectangle(xy, fill="white")
    data = io.BytesIO()
    image.save(data, format="PNG")
    return data.getvalue()


def tone(seconds: float, frequency: int) -> bytes:
    data = io.BytesIO()
    with wave.open(data, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(48000)
        output.writeframes(
            b"".join(
                struct.pack("<h", round(5000 * math.sin(2 * math.pi * frequency * i / 48000)))
                for i in range(round(48000 * seconds))
            )
        )
    return data.getvalue()


def bright_pixels(image: Image.Image) -> int:
    raw = image.tobytes()
    return sum(min(r, g, b) > 200 for r, g, b in zip(raw[0::3], raw[1::3], raw[2::3], strict=True))


def upload(client: TestClient, project: UUID, mime: str, data: bytes, order: UUID | None = None) -> str:
    params = {}
    if order:
        params["work_order_id"] = str(order)
    response = client.post(
        f"/api/projects/{project}/video-edits/assets",
        params=params,
        content=data,
        headers={
            **ORIGIN,
            "content-type": mime,
            "x-media-provenance": "Synthetic local fixture, generated for this test",
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["asset_id"])


def invoke(
    gateway: ToolGateway,
    project: UUID,
    run: UUID,
    profile: UUID,
    tool: str,
    arguments: dict[str, Any],
    call: str | None = None,
) -> dict[str, Any]:
    return gateway.invoke_role_tool(
        project, run, profile, call or uuid4().hex, tool, arguments, tool_version=1
    )


@pytest.mark.parametrize("fault,expected", [("missing_order", 404), ("quota", 409)])
def test_upload_preserves_owner_failure_status(tmp_path: Path, fault: str, expected: int) -> None:
    settings = settings_for_edit(tmp_path).model_copy(update={"input_file_max_project_count": 1})
    with TestClient(
        create_app(settings), base_url="http://localhost:8765", raise_server_exceptions=False
    ) as client:
        gateway = ToolGateway(create_engine(settings.database_url), "operator", live_text_settings=settings)
        project, _run_id, _profile = _run(client, gateway, tools=TOOLS)
        params = {}
        if fault == "quota":
            upload(client, project, "image/png", png("red"))
        else:
            params["work_order_id"] = str(uuid4())
        response = client.post(
            f"/api/projects/{project}/video-edits/assets",
            params=params,
            content=png("blue"),
            headers={
                **ORIGIN,
                "content-type": "image/png",
                "x-media-provenance": "Synthetic failure fixture",
            },
        )
        assert response.status_code == expected, response.text


def test_edit_admission_scope_idempotency_cancel_and_retry_bounds(tmp_path: Path) -> None:
    settings = settings_for_edit(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        gateway = ToolGateway(create_engine(settings.database_url), "operator", live_text_settings=settings)
        project, run, profile = _run(client, gateway, tools=TOOLS)
        asset = upload(client, project, "image/png", png("red"))
        recipe = {
            "request_key": str(uuid4()),
            "width": 640,
            "height": 360,
            "scenes": [{"asset_id": asset, "kind": "image", "seconds": 1}],
        }
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(invoke, gateway, project, run, profile, TOOLS[0], recipe) for _ in range(2)
            ]
            replies = [future.result(timeout=10)["result"] for future in futures]
        assert replies[0]["job_id"] == replies[1]["job_id"]
        job = UUID(replies[0]["job_id"])
        replay = invoke(gateway, project, run, profile, TOOLS[0], recipe, "exact-call")
        assert invoke(gateway, project, run, profile, TOOLS[0], recipe, "exact-call") == replay
        with pytest.raises(Exception, match="different recipe"):
            invoke(gateway, project, run, profile, TOOLS[0], recipe | {"height": 480})
        with pytest.raises(ToolInputError):
            invoke(gateway, project, run, profile, TOOLS[0], recipe | {"command": "invalid"})
        other_project, other_run, other_profile = _run(client, gateway, tools=TOOLS)
        with pytest.raises(ToolDenied):
            invoke(gateway, other_project, other_run, other_profile, TOOLS[0], recipe)
        assert client.get(f"/api/projects/{other_project}/video-edits/{job}").status_code == 404
        with gateway.engine.connect() as connection:
            foreign_order = connection.scalar(
                text("SELECT work_order_id FROM runs WHERE project_id=:p AND id=:r"), {"p": project, "r": run}
            )
        scoped = upload(client, project, "image/png", png("blue"), foreign_order)
        assert scoped
        different_order = client.post(
            f"/api/projects/{project}/work-orders",
            json={"title": "Separate cut", "instructions": "Different scope", "profile_id": str(profile)},
            headers=ORIGIN,
        ).json()["id"]
        assert (
            client.post(
                f"/api/projects/{project}/work-orders/{different_order}/ready", headers=ORIGIN
            ).status_code
            == 200
        )
        different_run = gateway.start_synthetic_text(project, UUID(different_order))["run_id"]
        with pytest.raises(ToolDenied):
            invoke(
                gateway,
                project,
                UUID(str(different_run)),
                profile,
                TOOLS[0],
                recipe
                | {
                    "request_key": str(uuid4()),
                    "scenes": [{"asset_id": scoped, "kind": "image", "seconds": 1}],
                },
            )
        cancel = invoke(gateway, project, run, profile, TOOLS[2], {"job_id": str(job)})
        assert cancel["result"]["status"] == "canceled"
        assert VideoEditWorker(gateway).process_one(project) is None
        assert (
            client.post(f"/api/projects/{project}/video-edits/{job}/retry", headers=ORIGIN).status_code == 409
        )
        with gateway.engine.connect() as c:
            assert (
                c.scalar(text("SELECT count(*) FROM video_edit_jobs WHERE project_id=:p"), {"p": project})
                == 1
            )
        bad = client.post(
            f"/api/projects/{project}/video-edits/assets",
            content=b"invalid",
            headers={**ORIGIN, "content-type": "image/png", "x-media-provenance": "fixture"},
        )
        assert bad.status_code == 422
        assert client.post(f"/api/projects/{project}/video-edits/{job}/cancel").status_code == 403


def test_scoped_once_worker_leaves_earlier_foreign_job_queued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runner.video_edit_supervisor import VideoWorkerFailure

    settings = settings_for_edit(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        gateway = ToolGateway(create_engine(settings.database_url), "operator", live_text_settings=settings)
        jobs: list[tuple[UUID, UUID]] = []
        for color in ("red", "blue"):
            project, run, profile = _run(client, gateway, tools=TOOLS)
            asset = upload(client, project, "image/png", png(color))
            reply = invoke(
                gateway,
                project,
                run,
                profile,
                TOOLS[0],
                {
                    "request_key": str(uuid4()),
                    "width": 640,
                    "height": 360,
                    "scenes": [{"asset_id": asset, "kind": "image", "seconds": 1}],
                },
            )["result"]
            jobs.append((project, UUID(reply["job_id"])))

        def fail(_manifest: Any, _assets: Any, _settings: Any, _canceled: Any) -> Any:
            raise VideoWorkerFailure("video_render_dependency_failure")

        monkeypatch.setattr("runner.video_edit_worker.run_render", fail)
        selected_project, selected_job = jobs[1]
        row = VideoEditWorker(gateway).process_one(selected_project)
        assert row is not None and row["id"] == selected_job and row["status"] == "failed"
        with gateway.engine.connect() as connection:
            foreign = connection.execute(
                text("SELECT status,attempt FROM video_edit_jobs WHERE project_id=:p AND id=:id"),
                {"p": jobs[0][0], "id": jobs[0][1]},
            ).one()
        assert foreign == ("queued", 0)
        assert VideoEditWorker(gateway).process_one(selected_project) is None


@pytest.mark.integration
def test_one_native_agent_job_renders_and_reports_actual_artifact(tmp_path: Path) -> None:
    assert os.environ.get("ONEFOUNDRY_VIDEO_TEST_IMAGE"), "explicit pinned disposable renderer image required"
    settings = settings_for_edit(tmp_path)
    evidence_root = Path(os.environ["ONEFOUNDRY_VIDEO_TEST_OUTPUT"])
    evidence_root.mkdir(parents=True, exist_ok=True)
    # Keep the review fixture on dedicated private volumes, independent of
    # pytest's retention sweep while the full suite and browser QA run.
    private = evidence_root / "native-private"
    private.mkdir(mode=0o700, exist_ok=True)
    for name in ("artifacts", "assets", "secrets"):
        (private / name).mkdir(mode=0o700, exist_ok=True)
    settings = settings.model_copy(
        update={
            "artifact_workspace_root": str(private / "artifacts"),
            "video_edit_asset_root": str(private / "assets"),
            "model_secret_root": str(private / "secrets"),
        }
    )
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        project, oid, profiles, base = activate_fixture(client)
        pid = UUID(project)
        changed = client.patch(
            f"/api/projects/{project}/profiles/{profiles['GTM']}",
            json={"allowed_tools": TOOLS},
            headers=ORIGIN,
        )
        assert changed.status_code == 200, changed.text
        gateway = ToolGateway(create_engine(settings.database_url), "operator", live_text_settings=settings)
        attempts = TaskSessionAttempts(gateway)
        assert (
            client.post(
                f"{base}/continue", json={"client_attempt_id": str(uuid4())}, headers=ORIGIN
            ).status_code
            == 202
        )
        chief = attempts.claim_next(pid)
        assert chief is not None
        main = client.get(base).json()["session"]["id"]
        assignment = TaskSessionAssignments(gateway).delegate(
            pid,
            UUID(main),
            chief["id"],
            DelegateRequest(
                client_action_id=uuid4(),
                profile_id=UUID(profiles["GTM"]),
                objective="Render the supplied synthetic edit",
                expected_result="A playable WebM returned from edit_video_v1",
                acceptance_kind="artifact",
                acceptance_check="Exact bytes, decoded audio ending, contained framing and captions",
            ),
        )
        qa = TaskSessionAssignments(gateway).delegate(
            pid,
            UUID(main),
            chief["id"],
            DelegateRequest(
                client_action_id=uuid4(),
                profile_id=UUID(profiles["QA"]),
                objective="Inspect the actual synthetic video",
                expected_result="Evidence and honest remaining human review",
                acceptance_kind="qa_review",
                acceptance_check="Exact artifact plus render QC; do not claim subjective listening",
                depends_on=(assignment["id"],),
            ),
        )
        attempts.settle(pid, chief["id"], "done", public_reply="Synthetic edit delegated.")
        first = upload(client, pid, "image/png", png("red"))
        last = upload(client, pid, "image/png", png("blue"))
        narration = upload(client, pid, "audio/wav", tone(3, 440))
        music = upload(client, pid, "audio/wav", tone(4, 220))
        source = tmp_path / "synthetic-source.mp4"
        subprocess.run(  # noqa: S603 - fixed test-only executable/argv
            [
                "/usr/bin/ffmpeg",
                "-nostdin",
                "-y",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=green:s=640x360:r=24:d=3",
                "-vf",
                "drawbox=x=0:y=0:w=iw:h=ih:color=red:t=fill:enable='lt(t,0.5)+gte(t,2.5)'",
                "-c:v",
                "libx264",
                "-threads",
                "1",
                str(source),
            ],
            check=True,
            timeout=10,
        )  # noqa: S603 - test source creation only; never the final edit
        footage = upload(client, pid, "video/mp4", source.read_bytes())
        recipe = {
            "request_key": str(uuid4()),
            "width": 640,
            "height": 360,
            "scenes": [
                {"asset_id": first, "kind": "image", "seconds": 1},
                {"asset_id": footage, "kind": "video", "start": 0.5, "seconds": 2},
                {"asset_id": last, "kind": "image", "seconds": 1},
            ],
            "captions": [{"start": 0, "end": 3.9, "text": "Native agent editing test"}],
            "audio": [
                {"asset_id": narration, "kind": "narration", "start": 0.5, "seconds": 3},
                {"asset_id": music, "kind": "music", "seconds": 4, "gain": 0.15},
            ],
        }
        identities: dict[str, Any] = {}

        def render_then_status(call: Any) -> list[Any]:
            queued = _tool_reply(call)["result"]
            assert queued["status"] == "queued", queued
            identities["job_id"] = queued["job_id"]
            # Start a separate trusted worker process. Only this process invokes
            # Docker; the native agent requested the edit through the registered tool.
            environment = {
                key: value
                for key, value in os.environ.items()
                if key in {"PATH", "PYTHONPATH", "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"}
            }
            environment.update(
                {
                    "DB_USER": settings.db_user,
                    "DB_PASSWORD": settings.db_password,
                    "VIDEO_EDIT_ENABLED": "true",
                    "VIDEO_EDIT_IMAGE": settings.video_edit_image,
                    "VIDEO_EDIT_ASSET_ROOT": settings.video_edit_asset_root,
                    "ARTIFACT_WORKSPACE_ROOT": settings.artifact_workspace_root,
                    "MEDIA_MAX_WIDTH": "640",
                    "MEDIA_MAX_HEIGHT": "480",
                }
            )
            completed = subprocess.run(  # noqa: S603 - fixed test-only executable/argv
                [sys.executable, "-m", "runner.video_edit_worker", "--once", "--project", project],
                env=environment,
                capture_output=True,
                timeout=150,
                check=False,
            )  # noqa: S603
            (evidence_root / "worker-diagnostic.txt").write_bytes(completed.stdout + completed.stderr)
            assert completed.returncode == 0, completed.stderr.decode()
            owner = VideoEditJobs(gateway).get(pid, UUID(queued["job_id"]))
            assert owner["status"] == "done", owner
            return [
                function_call(
                    "role_tool",
                    {"tool_id": TOOLS[1], "arguments": {"job_id": queued["job_id"]}},
                    call_id="status",
                )
            ]

        def report(call: Any) -> list[Any]:
            saved = _tool_reply(call)["result"]
            assert saved["status"] == "done" and saved["full_decode_verified"] is True
            identities.update(saved)
            return [
                function_call(
                    "report_result",
                    {
                        "body": "Actual synthetic WebM rendered by the registered tool. "
                        "Decoded audio ending and contained frames verified; hearing remains unverified.",
                        "artifact_id": saved["artifact_id"],
                        "artifact_run_id": saved["artifact_run_id"],
                    },
                    call_id="report",
                )
            ]

        model = ScriptedModel(
            [
                _step("role_tool", {"tool_id": TOOLS[0], "arguments": recipe}),
                ModelStep.respond(render_then_status),
                ModelStep.respond(report),
                ModelStep(output=[assistant_message("Actual artifact returned for QA and operator review.")]),
            ]
        )
        sdk = TaskSessionSdkTurn(
            gateway,
            settings,
            model_factory=lambda _r, _s: TeamModel(model, "ollama", "fixture-text", False, False),
        )
        result = sdk.run_next(pid)
        assert result is not None and result["outcome"] == "done", result
        artifact = gateway.workspace_artifact_bytes(
            pid, UUID(identities["artifact_run_id"]), UUID(identities["artifact_id"])
        )
        assert hashlib.sha256(artifact).hexdigest() == identities["sha256"]
        (evidence_root / "native-agent-edit.webm").write_bytes(artifact)
        # Inspect decoded sink pixels. The source video's first half-second is
        # red, so the green middle scene also proves its requested source trim.
        for moment, name, color in [(0.25, "first", 0), (1.1, "middle", 1), (3.5, "last", 2)]:
            decoded = subprocess.run(  # noqa: S603 - fixed test-only artifact inspection
                [
                    "/usr/bin/ffmpeg",
                    "-v",
                    "error",
                    "-ss",
                    str(moment),
                    "-i",
                    str(evidence_root / "native-agent-edit.webm"),
                    "-frames:v",
                    "1",
                    "-f",
                    "image2pipe",
                    "-vcodec",
                    "png",
                    "-",
                ],
                check=True,
                capture_output=True,
                timeout=10,
            ).stdout
            frame = Image.open(io.BytesIO(decoded)).convert("RGB")
            (evidence_root / f"scene-{name}.png").write_bytes(decoded)
            center = frame.getpixel((320, 140))
            assert isinstance(center, tuple)
            assert center[color] > 100 and all(center[i] < 30 for i in range(3) if i != color), center
            if name != "middle":
                # Contain + reserved caption band should preserve all corners.
                for box in [(55, 0, 85, 24), (554, 0, 586, 24), (55, 270, 85, 294), (554, 270, 586, 294)]:
                    assert bright_pixels(frame.crop(box)) > 40
            assert bright_pixels(frame.crop((0, 294, 640, 360))) > 40
        (evidence_root / "native-agent-job.json").write_text(
            json.dumps(
                {
                    "project_id": project,
                    "work_order_id": oid,
                    "child_session_id": str(assignment["child_session_id"]),
                    "qa_session_id": str(qa["child_session_id"]),
                    "recipe": recipe,
                    "result": identities,
                    "sdk_outcome": result,
                },
                indent=2,
                default=str,
            )
            + "\n"
        )
        # Same exact request never creates a second job or artifact.
        assert VideoEditWorker(gateway).process_one(pid) is None
        with gateway.engine.connect() as c:
            assert c.scalar(text("SELECT count(*) FROM video_edit_jobs WHERE project_id=:p"), {"p": pid}) == 1
            assert (
                c.scalar(text("SELECT count(*) FROM workspace_artifacts WHERE project_id=:p"), {"p": pid})
                == 1
            )
            calls = c.execute(
                text("SELECT tool_name,status FROM task_session_tool_calls WHERE project_id=:p"), {"p": pid}
            ).all()
            assert ("onefoundry.report_result", "done") in calls
            events = [
                dict(x)
                for x in c.execute(
                    text(
                        "SELECT event_type,payload,actor_id FROM events WHERE project_id=:p "
                        "ORDER BY created_at,id"
                    ),
                    {"p": pid},
                ).mappings()
            ]
        (evidence_root / "native-agent-events.json").write_text(
            json.dumps(events, indent=2, default=str) + "\n"
        )
        checked = client.get(
            f"/api/projects/{project}/runs/{identities['artifact_run_id']}/workspace-artifacts/{identities['artifact_id']}"
        ).json()
        assert checked["playback_verified"] is False
        assert "full_decode_verified" in checked["content"]
        assert identities["decoded_audio_seconds"] >= 4 - 1 / 24
        assert identities["ending_mix_similarity"] > 0.8
        assert identities["isolation"]["network"] == "none"

        def inspect_qa(call: Any) -> list[Any]:
            dependency = _tool_reply(call)
            assert identities["sha256"] in str(dependency), dependency
            assert "full_decode_verified" in str(dependency), dependency
            (evidence_root / "qa-native-dependency-read.json").write_text(
                json.dumps(dependency, indent=2, default=str) + "\n"
            )
            return [
                function_call(
                    "publish_update",
                    {
                        "body": (
                            "QA read the saved artifact and render QC. "
                            "Human listening/readability remain unverified."
                        )
                    },
                    call_id="qa-inspection",
                )
            ]

        qa_model = ScriptedModel(
            [
                _step("read_dependency_result", {"assignment_id": str(assignment["id"]), "offset": 0}),
                ModelStep.respond(inspect_qa),
                ModelStep(
                    output=[assistant_message("Exact native artifact inspected; operator review pending.")]
                ),
            ]
        )
        qa_turn = TaskSessionSdkTurn(
            gateway,
            settings,
            model_factory=lambda _r, _s: TeamModel(qa_model, "ollama", "fixture-text", False, False),
        ).run_next(pid)
        assert qa_turn and qa_turn["outcome"] == "done", qa_turn
        assert qa_turn["session_id"] == qa["child_session_id"]
        (evidence_root / "qa-native-inspection.json").write_text(
            json.dumps(qa_turn, indent=2, default=str) + "\n"
        )


@pytest.mark.parametrize("fault", ["dependency", "timeout", "running_cancel", "revoke"])
def test_worker_known_failures_retry_cancel_and_revocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    from runner.video_edit_supervisor import VideoWorkerCanceled, VideoWorkerFailure

    settings = settings_for_edit(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        gateway = ToolGateway(create_engine(settings.database_url), "operator", live_text_settings=settings)
        project, run, profile = _run(client, gateway, tools=TOOLS)
        asset = upload(client, project, "image/png", png("green"))
        recipe = {
            "request_key": str(uuid4()),
            "width": 640,
            "height": 360,
            "scenes": [{"asset_id": asset, "kind": "image", "seconds": 1}],
        }
        queued = invoke(gateway, project, run, profile, TOOLS[0], recipe)["result"]
        job = UUID(queued["job_id"])

        def fail(_manifest: Any, _assets: Any, _settings: Any, canceled: Any) -> Any:
            if fault == "running_cancel":
                response = client.post(f"/api/projects/{project}/video-edits/{job}/cancel", headers=ORIGIN)
                assert response.json()["status"] == "cancel_requested"
                assert canceled()
                raise VideoWorkerCanceled("video_edit_canceled")
            if fault == "revoke":
                response = client.patch(
                    f"/api/projects/{project}/profiles/{profile}", json={"allowed_tools": []}, headers=ORIGIN
                )
                assert response.status_code == 200 and canceled()
                raise VideoWorkerCanceled("video_edit_permission_revoked")
            raise VideoWorkerFailure(
                "video_edit_timed_out" if fault == "timeout" else "video_render_dependency_failure"
            )

        monkeypatch.setattr("runner.video_edit_worker.run_render", fail)
        row = VideoEditWorker(gateway).process_one(project)
        assert row is not None
        if fault in {"running_cancel", "revoke"}:
            assert row["status"] == "canceled"
        else:
            assert row["status"] == "failed"
            retry = client.post(f"/api/projects/{project}/video-edits/{job}/retry", headers=ORIGIN)
            assert retry.status_code == 200, retry.text
            VideoEditWorker(gateway).process_one(project)
            assert (
                client.post(f"/api/projects/{project}/video-edits/{job}/retry", headers=ORIGIN).status_code
                == 409
            )
        with gateway.engine.connect() as c:
            assert (
                c.scalar(text("SELECT count(*) FROM workspace_artifacts WHERE project_id=:p"), {"p": project})
                == 0
            )


@pytest.mark.parametrize("action", ["cancel", "revoke", "expire"])
def test_publication_rechecks_late_cancellation_scope_and_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    from onefoundry.artifact_workspace import ArtifactWorkspace

    settings = settings_for_edit(tmp_path)
    with TestClient(create_app(settings), base_url="http://localhost:8765") as client:
        gateway = ToolGateway(create_engine(settings.database_url), "operator", live_text_settings=settings)
        project, run, profile = _run(client, gateway, tools=TOOLS)
        asset = upload(client, project, "image/png", png("green"))
        queued = invoke(
            gateway,
            project,
            run,
            profile,
            TOOLS[0],
            {
                "request_key": str(uuid4()),
                "width": 640,
                "height": 360,
                "scenes": [{"asset_id": asset, "kind": "image", "seconds": 1}],
            },
        )["result"]
        job = UUID(queued["job_id"])
        worker = VideoEditWorker(gateway)
        # This test exercises publication fencing, not rendering. The real render
        # and exact-byte browser playback are covered by the one native job above.
        content = b"\x1aE\xdf\xa3webm" + b"synthetic-publication-boundary" * 8
        monkeypatch.setattr(
            "runner.video_edit_worker.run_render",
            lambda *_args: (
                content,
                {"sha256": hashlib.sha256(content).hexdigest(), "byte_size": len(content)},
            ),
        )
        original_write = ArtifactWorkspace.write

        def interrupted_write(self: Any, *args: Any, **kwargs: Any) -> Any:
            saved = original_write(self, *args, **kwargs)
            if action == "cancel":
                assert (
                    client.post(f"/api/projects/{project}/video-edits/{job}/cancel", headers=ORIGIN).json()[
                        "status"
                    ]
                    == "cancel_requested"
                )
            elif action == "revoke":
                assert (
                    client.patch(
                        f"/api/projects/{project}/profiles/{profile}",
                        json={"allowed_tools": []},
                        headers=ORIGIN,
                    ).status_code
                    == 200
                )
            else:
                with gateway.engine.begin() as connection:
                    connection.execute(
                        text(
                            "UPDATE video_edit_jobs SET "
                            "started_at=clock_timestamp()-make_interval(secs => :s) "
                            "WHERE project_id=:p AND id=:id"
                        ),
                        {
                            "s": settings.video_edit_wall_seconds + settings.runner_receipt_wait_seconds + 1,
                            "p": project,
                            "id": job,
                        },
                    )
                assert worker.reconcile_expired() == 1
            return saved

        monkeypatch.setattr(ArtifactWorkspace, "write", interrupted_write)
        result = worker.process_one(project)
        assert result and result["status"] == ("unknown" if action == "expire" else "canceled")
        assert result["artifact_id"] is None
        assert (
            client.post(f"/api/projects/{project}/video-edits/{job}/retry", headers=ORIGIN).status_code == 409
        )
        assert worker.process_one(project) is None
        with gateway.engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM workspace_artifacts WHERE project_id=:p"), {"p": project}
                )
                == 0
            )


def test_default_poll_can_read_an_empty_queue(tmp_path: Path) -> None:
    """The persistent worker polls without a project filter."""
    settings = settings_for_edit(tmp_path)
    engine = create_engine(settings.database_url)
    try:
        gateway = ToolGateway(engine, settings.operator_actor_id, live_text_settings=settings)
        # Existing test projects may retain queued work. Skip those locked rows without changing them.
        with engine.begin() as connection:
            connection.execute(text("SELECT id FROM video_edit_jobs WHERE status='queued' FOR UPDATE"))
            assert VideoEditWorker(gateway).process_one() is None
    finally:
        engine.dispose()
