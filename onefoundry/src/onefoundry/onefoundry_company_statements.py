"""Operator-owned, versioned company facts for outreach context."""

from typing import Any
from uuid import UUID, uuid4

from pydantic import Field, field_validator
from sqlalchemy import text

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.workflow_schemas import StrictModel, local_text


class StatementCreate(StrictModel):
    body: str = Field(min_length=1, max_length=2000)
    statement_key: UUID | None = None
    expected_version: int | None = Field(default=None, ge=1)

    @field_validator("body")
    @classmethod
    def safe_body(cls, value: str) -> str:
        return local_text(value)


class StatementDecision(StrictModel):
    expected_version: int = Field(ge=1)


def statement(connection: Any, project_id: UUID, statement_id: UUID) -> dict[str, Any]:
    row = (
        connection.execute(
            text(
                "SELECT id,project_id,statement_key,version,body,state,approved_by,approved_at,"
                "retired_by,retired_at,created_at FROM onefoundry_company_statements "
                "WHERE project_id=:p AND id=:id"
            ),
            {"p": project_id, "id": statement_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise NotFound("company statement not found")
    result = dict(row)
    result["is_current"] = (
        connection.scalar(
            text(
                "SELECT 1 FROM onefoundry_company_statements WHERE project_id=:p "
                "AND statement_key=:k AND version>:v LIMIT 1"
            ),
            {"p": project_id, "k": row["statement_key"], "v": row["version"]},
        )
        is None
    )
    return result


def list_statements(connection: Any, project_id: UUID) -> list[dict[str, Any]]:
    rows = [
        dict(row)
        for row in connection.execute(
            text(
                "SELECT id,project_id,statement_key,version,body,state,approved_by,approved_at,"
                "retired_by,retired_at,created_at FROM onefoundry_company_statements "
                "WHERE project_id=:p ORDER BY created_at DESC,id DESC"
            ),
            {"p": project_id},
        ).mappings()
    ]
    latest: dict[UUID, int] = {}
    for row in rows:
        key = row["statement_key"]
        latest[key] = max(latest.get(key, 0), row["version"])
    for row in rows:
        row["is_current"] = row["version"] == latest[row["statement_key"]]
    return rows


def create_statement(gateway: Any, project_id: UUID, data: StatementCreate) -> dict[str, Any]:
    statement_id = uuid4()
    with gateway.engine.begin() as connection:
        gateway._require_project(connection, project_id)
        if data.statement_key is None:
            if data.expected_version is not None:
                raise Conflict("new statement cannot specify an expected version")
            key, version = uuid4(), 1
        else:
            latest = (
                connection.execute(
                    text(
                        "SELECT version,state FROM onefoundry_company_statements "
                        "WHERE project_id=:p AND statement_key=:k ORDER BY version DESC LIMIT 1 FOR UPDATE"
                    ),
                    {"p": project_id, "k": data.statement_key},
                )
                .mappings()
                .one_or_none()
            )
            if latest is None:
                raise NotFound("company statement not found")
            if latest["version"] != data.expected_version or latest["state"] != "approved":
                raise Conflict("statement version changed or is not approved")
            key, version = data.statement_key, latest["version"] + 1
        connection.execute(
            text(
                "INSERT INTO onefoundry_company_statements "
                "(id,project_id,actor_id,statement_key,version,body) "
                "VALUES (:id,:p,:actor,:key,:version,:body)"
            ),
            {
                "id": statement_id,
                "p": project_id,
                "actor": gateway.actor_id,
                "key": key,
                "version": version,
                "body": data.body,
            },
        )
        gateway._event(
            connection,
            project_id,
            None,
            None,
            "onefoundry_statement_created",
            {"statement_id": str(statement_id), "version": version},
        )
        return statement(connection, project_id, statement_id)


def decide_statement(
    gateway: Any, project_id: UUID, statement_id: UUID, decision: str, data: StatementDecision
) -> dict[str, Any]:
    with gateway.engine.begin() as connection:
        connection.execute(
            text("SELECT id FROM onefoundry_company_statements WHERE project_id=:p AND id=:id FOR UPDATE"),
            {"p": project_id, "id": statement_id},
        ).one_or_none()
        current = statement(connection, project_id, statement_id)
        if current["version"] != data.expected_version:
            raise Conflict("statement version changed")
        if decision == "approve":
            if current["state"] != "draft":
                raise Conflict("only a draft statement can be approved")
            newer = connection.scalar(
                text(
                    "SELECT 1 FROM onefoundry_company_statements WHERE project_id=:p "
                    "AND statement_key=:k AND version>:version"
                ),
                {"p": project_id, "k": current["statement_key"], "version": current["version"]},
            )
            if newer is not None:
                raise Conflict("a newer statement version exists")
            changed = connection.execute(
                text(
                    "UPDATE onefoundry_company_statements SET state='approved',approved_by=:actor,"
                    "approved_at=clock_timestamp() WHERE project_id=:p AND id=:id AND state='draft'"
                ),
                {"actor": gateway.actor_id, "p": project_id, "id": statement_id},
            )
        elif decision == "retire":
            if current["state"] != "approved":
                raise Conflict("only an approved statement can be retired")
            changed = connection.execute(
                text(
                    "UPDATE onefoundry_company_statements SET state='retired',retired_by=:actor,"
                    "retired_at=clock_timestamp() WHERE project_id=:p AND id=:id AND state='approved'"
                ),
                {"actor": gateway.actor_id, "p": project_id, "id": statement_id},
            )
        else:
            raise ValueError("unsupported statement decision")
        if changed.rowcount != 1:
            raise Conflict("statement decision changed concurrently")
        gateway._event(
            connection,
            project_id,
            None,
            None,
            f"onefoundry_statement_{decision}d",
            {"statement_id": str(statement_id), "version": current["version"]},
        )
        return statement(connection, project_id, statement_id)
