"""Real PostgreSQL query contracts for superseded extension checkpoints.

Temporary tables isolate historical state combinations from the live fixture schema.
Native owner integration and immutable-event tests run separately in the full suite.
"""

import json
import os
from collections.abc import Iterator, Mapping
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Connection, create_engine, text

from onefoundry.gateway_errors import Conflict
from onefoundry.self_extension_resume import require_resumed
from onefoundry.settings import Settings

History = tuple[Connection, UUID, UUID, dict[str, str]]


@pytest.fixture
def history() -> Iterator[History]:
    settings = Settings(db_user=os.environ["APP_DB_USER"], db_password=os.environ["APP_DB_PASSWORD"])
    engine = create_engine(settings.database_url)
    with engine.begin() as connection:
        definitions = {
            "events": "id uuid, project_id uuid, event_type text, payload jsonb, created_at timestamptz",
            "task_sessions": "id uuid, project_id uuid, work_order_id uuid",
            "runs": "id uuid, project_id uuid, work_order_id uuid",
            "external_code_attempts": "id uuid, project_id uuid, run_id uuid, fixture_id text, status text, "
            "request_sha256 text, receipt_sha256 text",
            "task_session_assignments": "project_id uuid, root_work_order_id uuid, child_session_id uuid, "
            "status text",
            "approval_requests": "id uuid, project_id uuid, work_order_id uuid, content text, status text",
        }
        for table, columns in definitions.items():
            connection.exec_driver_sql(f"CREATE TEMP TABLE {table} ({columns}) ON COMMIT DROP")
        project, order, session, child, target = [uuid4() for _ in range(5)]
        request = {
            "original_work_order_id": str(order),
            "original_session_id": str(session),
            "child_session_id": str(child),
            "target_approval_id": str(target),
            "extension_key": str(uuid4()),
            "operation": "tool:fixture_reader_v1",
        }
        event(connection, project, "self_extension_requested", request, 1)
        connection.execute(
            text("INSERT INTO task_session_assignments VALUES (:p,:o,:s,'canceled')"),
            {"p": project, "o": order, "s": child},
        )
        yield connection, project, order, request
    engine.dispose()


def event(connection: Connection, project: UUID, kind: str, payload: Mapping[str, object], tick: int) -> None:
    connection.execute(
        text(
            "INSERT INTO events VALUES (:id,:p,:kind,CAST(:body AS jsonb),"
            "TIMESTAMPTZ '2026-01-01 00:00:00+00' + :tick * INTERVAL '1 second')"
        ),
        {"id": uuid4(), "p": project, "kind": kind, "body": json.dumps(payload), "tick": tick},
    )


def replacement(connection: Connection, project: UUID, request: dict[str, str], **changes: str) -> None:
    newer = {**request, "extension_key": str(uuid4()), **changes}
    event(connection, project, "self_extension_requested", newer, 2)
    event(connection, project, "self_extension_resumed", newer, 3)


def coding(connection: Connection, project: UUID, request: dict[str, str], status: str) -> UUID:
    run, attempt, work = [uuid4() for _ in range(3)]
    connection.execute(
        text("INSERT INTO task_sessions VALUES (:s,:p,:w)"),
        {"s": UUID(request["child_session_id"]), "p": project, "w": work},
    )
    connection.execute(text("INSERT INTO runs VALUES (:r,:p,:w)"), {"r": run, "p": project, "w": work})
    connection.execute(
        text(
            "INSERT INTO external_code_attempts VALUES "
            "(:a,:p,:r,'repository_code_v1',:status,'request','receipt')"
        ),
        {"a": attempt, "p": project, "r": run, "status": status},
    )
    return attempt


def activation(connection: Connection, project: UUID, request: dict[str, str], status: str) -> UUID:
    approval = uuid4()
    connection.execute(
        text("INSERT INTO approval_requests VALUES (:id,:p,:o,:content,:status)"),
        {
            "id": approval,
            "p": project,
            "o": UUID(request["original_work_order_id"]),
            "content": json.dumps({"target_approval_id": request["target_approval_id"]}),
            "status": status,
        },
    )
    event(connection, project, "self_extension_activation_proposed", {"approval_id": str(approval)}, 2)
    return approval


@pytest.mark.parametrize("prior", ["unlaunched", "rejected_candidate", "accepted_candidate"])
def test_verified_replacement_retires_terminal_unactivated_checkpoint(history: History, prior: str) -> None:
    connection, project, order, request = history
    if prior != "unlaunched":
        coding(connection, project, request, "done")
        activation(connection, project, request, "rejected")
    if prior == "accepted_candidate":
        connection.execute(text("UPDATE task_session_assignments SET status='satisfied'"))
    with pytest.raises(Conflict, match="activation"):
        require_resumed(connection, project, order)
    replacement(connection, project, request)
    require_resumed(connection, project, order)
    assert (
        connection.scalar(text("SELECT count(*) FROM events WHERE event_type='self_extension_resumed'")) == 1
    )


@pytest.mark.parametrize("status", ["queued", "unknown"])
def test_replacement_does_not_hide_active_or_unknown_coding(history: History, status: str) -> None:
    connection, project, order, request = history
    coding(connection, project, request, status)
    replacement(connection, project, request)
    with pytest.raises(Conflict, match="activation"):
        require_resumed(connection, project, order)


@pytest.mark.parametrize("status", ["pending", "approved"])
def test_nonrejected_activation_keeps_old_candidate_blocking(history: History, status: str) -> None:
    connection, project, order, request = history
    coding(connection, project, request, "done")
    activation(connection, project, request, status)
    replacement(connection, project, request)
    with pytest.raises(Conflict, match="activation"):
        require_resumed(connection, project, order)


def test_queued_activation_cannot_be_hidden_by_rejection_or_replacement(history: History) -> None:
    connection, project, order, request = history
    coding(connection, project, request, "done")
    activation(connection, project, request, "rejected")
    event(
        connection,
        project,
        "self_extension_activation_queued",
        {"manifest": {"target_approval_id": request["target_approval_id"]}},
        2,
    )
    replacement(connection, project, request)
    with pytest.raises(Conflict, match="activation"):
        require_resumed(connection, project, order)


@pytest.mark.parametrize("status", ["active", "blocked", "ready"])
def test_unlaunched_request_requires_terminal_assignment(history: History, status: str) -> None:
    connection, project, order, request = history
    connection.execute(text("UPDATE task_session_assignments SET status=:status"), {"status": status})
    replacement(connection, project, request)
    with pytest.raises(Conflict, match="activation"):
        require_resumed(connection, project, order)


@pytest.mark.parametrize(
    "changed", ["original_session_id", "original_work_order_id", "operation", "gap_source_id", "gap_revision"]
)
def test_unrelated_resumption_cannot_retire_checkpoint(history: History, changed: str) -> None:
    connection, project, order, request = history
    replacement(connection, project, request, **{changed: str(uuid4())})
    with pytest.raises(Conflict, match="activation"):
        require_resumed(connection, project, order)


def test_foreign_project_resumption_cannot_retire_checkpoint(history: History) -> None:
    connection, project, order, request = history
    replacement(connection, uuid4(), request)
    with pytest.raises(Conflict, match="activation"):
        require_resumed(connection, project, order)


@pytest.mark.parametrize("status", ["failed", "unknown"])
def test_confirmed_failed_or_explicitly_abandoned_coding_still_retires(history: History, status: str) -> None:
    connection, project, order, request = history
    attempt = coding(connection, project, request, status)
    if status == "unknown":
        event(
            connection,
            project,
            "repository_code_abandoned",
            {"attempt_id": str(attempt), "request_sha256": "request", "receipt_sha256": "receipt"},
            2,
        )
    replacement(connection, project, request)
    require_resumed(connection, project, order)


def test_no_extension_checkpoint_needs_no_resumption(history: History) -> None:
    connection, _project, order, _request = history
    require_resumed(connection, uuid4(), order)
