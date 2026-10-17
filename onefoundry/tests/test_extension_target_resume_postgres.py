"""Independent live-DB oracle for the observed stranded target decision."""

from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text

from onefoundry.repository_code_contract import RepositoryCodeTarget
from onefoundry.self_extension_target_resume import step
from onefoundry.task_session_attempts import TaskSessionAttempts
from tests.test_capability_setup_postgres import setup_task as setup_task
from tests.test_self_extension_postgres import payload
from tests.test_team_workflow_postgres import ORIGIN


@pytest.mark.parametrize("decision", ["approved", "rejected"])
@pytest.mark.parametrize("superseded", [False, True])
def test_target_decision_wakes_original_task_once(
    setup_task: tuple[Any, ...], decision: str, superseded: bool
) -> None:
    client, gateway, project, profiles, tools, claimed, request, _repo = setup_task
    base = f"/api/projects/{project}"
    chief = next(p for p in client.get(base + "/profiles").json() if p["id"] == profiles["Chief of Staff"])
    r = client.patch(
        base + f"/profiles/{profiles['Engineer']}",
        json={
            "model": chief["model"],
            "model_account_id": chief["model_account_id"],
            "autonomy_level": "supervised",
        },
        headers=ORIGIN,
    )
    assert r.status_code == 200, r.text
    target = RepositoryCodeTarget(
        source_id=UUID(request["source_id"]),
        task_scope_id=claimed["work_order_id"],
        revision=request["revision"],
        adapter_id="codex_cli",
        provider_url="https://api.openai.com/v1/responses",
        allowed_paths=["README.md"],
        test_image="sha256:" + "a" * 64,
        test_commands=[["python", "-c", "assert True"]],
    )
    approval = payload(
        tools.invoke("onefoundry.propose_maintenance_target", target.model_dump(mode="json"), "target")
    )["approval_id"]
    step(gateway)
    r = client.post(base + f"/approvals/{approval}/decision", json={"decision": decision}, headers=ORIGIN)
    assert r.status_code == 200, r.text
    step(gateway)
    with gateway.engine.connect() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM task_session_attempts WHERE project_id=:p AND status='queued'"),
                {"p": UUID(project)},
            )
            == 0
        )
    if superseded:
        payload(
            tools.invoke(
                "onefoundry.propose_maintenance_target", target.model_dump(mode="json"), "new-target"
            )
        )
    TaskSessionAttempts(gateway).settle(
        UUID(project), claimed["id"], "done", public_reply="Waiting for target decision."
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: step(gateway), range(8)))
    step(gateway)
    with gateway.engine.connect() as c:
        assert c.scalar(
            text(
                "SELECT count(*) FROM task_session_attempts WHERE project_id=:p AND "
                "session_id=:s AND status='queued'"
            ),
            {"p": UUID(project), "s": claimed["session_id"]},
        ) == (0 if superseded else 1)
        assert (
            c.scalar(
                text("SELECT count(*) FROM external_code_attempts WHERE project_id=:p"), {"p": UUID(project)}
            )
            == 0
        )
