"""Installed setup lease, scoped source registration and verified continuation."""

from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, event, text

from onefoundry.capability_setup_resume import step
from onefoundry.task_session_attempts import TaskSessionAttempts
from onefoundry.task_session_runs import bind
from tests.test_capability_setup_postgres import setup_task as setup_task
from tests.test_self_extension_postgres import payload
from tests.test_team_workflow_postgres import ORIGIN


@pytest.mark.parametrize("already_registered", [True, False])
def test_host_setup_lease_registration_and_real_read(
    setup_task: tuple[Any, ...], already_registered: bool
) -> None:
    from onefoundry.capability_setup_host_contract import HostSetupReceipt
    from runner.capability_setup_settings import HostSetupSettings
    from runner.repository_transport import publish_new, publish_replace

    client, gateway, project, profiles, tools, claimed, request, repo = setup_task
    base = f"/api/projects/{project}"
    if not already_registered:
        assert client.delete(base + f"/sources/{request['source_id']}", headers=ORIGIN).status_code == 204
    spool = gateway._execution_gateway().spool.root
    for directory in ("jobs", "claims", "receipts", "artifacts"):
        (spool / directory).mkdir(parents=True, exist_ok=True)
    config = HostSetupSettings.model_validate(
        {
            "repositories": [{"alias": "repo", "path": str(repo), "projects": [project]}],
            "git_timeout_seconds": 3,
            "restart_timeout_seconds": 5,
        }
    )
    publish_replace(spool / "setup_capabilities.json", config.catalog().model_dump_json().encode())
    request.pop("source_id")
    request["repository_alias"] = "repo"
    proposed = payload(tools.invoke("onefoundry.propose_capability_setup", request, "host-setup"))
    route = base + f"/capability-setup/{proposed['approval_id']}"
    body = {"proposal_digest": proposed["proposal_digest"]}
    assert client.post(route + "/quiesce", json=body, headers=ORIGIN).status_code == 409
    assert (
        client.post(
            base + f"/approvals/{proposed['approval_id']}/decision",
            json={"decision": "approved"},
            headers=ORIGIN,
        ).status_code
        == 200
    )
    assert client.post(route + "/quiesce", json=body, headers=ORIGIN).json() == {"acquired": False}
    TaskSessionAttempts(gateway).settle(UUID(project), claimed["id"], "done", public_reply="Setup approved.")
    assert not step(gateway)  # Dispatch only: mounting has not been observed.
    assert (spool / "jobs" / f"{proposed['approval_id']}.json").is_file()
    assert client.post(route + "/register", json=body, headers=ORIGIN).status_code == 409
    assert client.post(route + "/quiesce", json=body, headers=ORIGIN).json() == {"acquired": True}
    assert (
        client.patch(
            base + f"/profiles/{profiles['Engineer']}",
            json={"instructions": "Paused during setup."},
            headers=ORIGIN,
        ).status_code
        == 409
    )

    def bounded_lock(dbapi: Any, _record: Any, _proxy: Any) -> None:
        # A second transaction waiting on its caller must fail instead of hanging
        # this first-registration regression indefinitely.
        with dbapi.cursor() as cursor:
            cursor.execute("SET lock_timeout = '1s'")

    event.listen(Engine, "checkout", bounded_lock)
    try:
        registered = client.post(route + "/register", json=body, headers=ORIGIN)
    finally:
        event.remove(Engine, "checkout", bounded_lock)
    assert registered.status_code == 200, registered.text
    assert client.post(route + "/register", json=body, headers=ORIGIN).json() == registered.json()
    assert client.post(route + "/release", json={}, headers=ORIGIN).status_code == 409
    receipt = HostSetupReceipt(
        job_id=proposed["approval_id"],
        project_id=project,
        proposal_digest=proposed["proposal_digest"],
        status="configured",
        reason="setup_source_registered",
        source_id=registered.json()["source_id"],
    )
    publish_new(spool / "receipts" / f"{proposed['approval_id']}.json", receipt.model_dump_json().encode())
    assert client.post(route + "/release", json={}, headers=ORIGIN).json() == {"released": True}
    assert step(gateway)
    assert not step(gateway)
    with gateway.engine.connect() as connection:
        observed = connection.scalar(
            text("SELECT payload FROM events WHERE project_id=:p AND event_type='capability_setup_resumed'"),
            {"p": UUID(project)},
        )
    assert observed["observation"]["status"] == "verified"
    assert len(observed["observation"]["checks"]) == 2
    resumed = TaskSessionAttempts(gateway).claim_next(UUID(project))
    assert resumed is not None
    with gateway.engine.connect() as connection:
        continuation = connection.scalar(
            text(
                "SELECT m.body FROM task_session_messages m JOIN task_session_attempts a "
                "ON a.project_id=m.project_id AND a.source_message_id=m.id "
                "WHERE a.project_id=:p AND a.id=:id"
            ),
            {"p": UUID(project), "id": resumed["id"]},
        )
    assert registered.json()["source_id"] in continuation
    assert request["revision"] in continuation
    from onefoundry.capability_setup import status

    saved = next(
        row
        for row in status(gateway, UUID(project), claimed["work_order_id"])
        if row["id"] == proposed["approval_id"]
    )
    assert saved["observation"] == observed["observation"]
    bind(gateway, UUID(project), resumed)
    TaskSessionAttempts(gateway).settle(UUID(project), resumed["id"], "done", public_reply="Verified.")
