"""Immutable task and profile snapshots for local run history."""

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, bindparam, text
from sqlalchemy.dialects.postgresql import JSONB

from onefoundry.skill_store import checked_procedures


def insert_run(
    connection: Connection,
    project_id: UUID,
    run_id: UUID,
    order_id: UUID,
    profile_id: UUID,
    actor_id: str,
    mode: str,
    *,
    procedures: list[dict[str, Any]] | None = None,
    extension_worker_image: str | None = None,
) -> None:
    # The work order is locked by each caller. Lock the profile against edits
    # until its initial snapshot and the run have committed together.
    profile = _profile(connection, project_id, profile_id)
    task = (
        connection.execute(
            text("SELECT title, instructions FROM work_orders WHERE project_id=:project_id AND id=:order_id"),
            {"project_id": project_id, "order_id": order_id},
        )
        .mappings()
        .one()
    )
    connection.execute(
        text(
            "INSERT INTO runs (id,project_id,actor_id,work_order_id,profile_id,status,mode,"
            "task_title_snapshot,task_instructions_snapshot,tokens_used,cost_usd) "
            "VALUES (:id,:project_id,:actor_id,:order_id,:profile_id,'running',"
            ":mode,:title,:instructions,:tokens_used,:cost_usd)"
        ),
        {
            "id": run_id,
            "project_id": project_id,
            "actor_id": actor_id,
            "order_id": order_id,
            "profile_id": profile_id,
            "mode": mode,
            "title": task["title"],
            "instructions": task["instructions"],
            "tokens_used": None if mode in {"live_text", "external_code"} else 0,
            "cost_usd": None if mode in {"live_text", "external_code"} else 0,
        },
    )
    _insert_profile(connection, project_id, run_id, actor_id, 0, profile, extension_worker_image)
    selected = (
        procedures if procedures is not None else checked_procedures(connection, project_id, profile_id)
    )
    _insert_skills(connection, project_id, run_id, actor_id, 0, profile_id, selected)


def record_handoff(
    connection: Connection,
    project_id: UUID,
    run_id: UUID,
    profile_id: UUID,
    actor_id: str,
    *,
    used_profile: dict[str, Any] | None = None,
    extension_worker_image: str | None = None,
) -> None:
    # Callers lock the run before assigning its next role. A prepared SDK
    # profile records the configuration actually passed to that fixture.
    # Even that path locks the profile to serialize exact skill binding pins.
    if used_profile is not None:
        _profile(connection, project_id, profile_id)
    profile = used_profile if used_profile is not None else _profile(connection, project_id, profile_id)
    index = connection.scalar(
        text(
            "SELECT coalesce(max(transition_index), -1) + 1 FROM run_profile_snapshots "
            "WHERE project_id=:project_id AND run_id=:run_id"
        ),
        {"project_id": project_id, "run_id": run_id},
    )
    _insert_profile(connection, project_id, run_id, actor_id, int(index), profile, extension_worker_image)
    selected = checked_procedures(connection, project_id, profile_id)
    _insert_skills(connection, project_id, run_id, actor_id, int(index), profile_id, selected)


def list_skills(connection: Connection, project_id: UUID, run_id: UUID) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            "SELECT skill_id,skill_name,version,sha256,transition_index,actor_id,created_at "
            "FROM run_skill_snapshots WHERE project_id=:project_id AND run_id=:run_id "
            "ORDER BY transition_index,skill_name,skill_id"
        ),
        {"project_id": project_id, "run_id": run_id},
    ).mappings()
    return [dict(row) for row in rows]


def _insert_skills(
    connection: Connection,
    project_id: UUID,
    run_id: UUID,
    actor_id: str,
    index: int,
    profile_id: UUID,
    procedures: list[dict[str, Any]],
) -> None:
    for skill in procedures:
        connection.execute(
            text(
                "INSERT INTO run_skill_snapshots "
                "(id,project_id,actor_id,run_id,transition_index,profile_id,"
                "skill_id,skill_name,version,sha256) "
                "VALUES (:id,:project_id,:actor_id,:run_id,:transition_index,:profile_id,"
                ":skill_id,:skill_name,:version,:sha256)"
            ),
            {
                "id": uuid4(),
                "project_id": project_id,
                "actor_id": actor_id,
                "run_id": run_id,
                "transition_index": index,
                "profile_id": profile_id,
                "skill_id": skill["skill_id"],
                "skill_name": skill["skill_name"],
                "version": skill["version"],
                "sha256": skill["sha256"],
            },
        )


def list_profiles(connection: Connection, project_id: UUID, run_id: UUID) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            "SELECT profile_id, profile_name, profile_model, profile_reasoning_effort, profile_instructions, "
            "memory_scope, allowed_tools, autonomy_level, "
            "transition_index, actor_id, recorded_at FROM run_profile_snapshots "
            "WHERE project_id=:project_id AND run_id=:run_id ORDER BY transition_index"
        ),
        {"project_id": project_id, "run_id": run_id},
    ).mappings()
    return [dict(row) for row in rows]


def _profile(connection: Connection, project_id: UUID, profile_id: UUID) -> dict[str, Any]:
    return dict(
        connection.execute(
            text(
                "SELECT id, name, model, reasoning_effort, instructions, memory_scope, allowed_tools, "
                "autonomy_level FROM profiles "
                "WHERE project_id=:project_id AND id=:profile_id FOR SHARE"
            ),
            {"project_id": project_id, "profile_id": profile_id},
        )
        .mappings()
        .one()
    )


def _insert_profile(
    connection: Connection,
    project_id: UUID,
    run_id: UUID,
    actor_id: str,
    index: int,
    profile: dict[str, Any],
    extension_worker_image: str | None = None,
) -> None:
    connection.execute(
        text(
            "INSERT INTO run_profile_snapshots "
            "(id,project_id,run_id,actor_id,transition_index,profile_id,profile_name,"
            "profile_model,profile_reasoning_effort,profile_instructions,"
            "memory_scope,allowed_tools,autonomy_level) "
            "VALUES (:id,:project_id,:run_id,:actor_id,:index,:profile_id,:name,:model,:reasoning_effort,"
            ":instructions,:memory_scope,:allowed_tools,:autonomy_level)"
        ).bindparams(bindparam("allowed_tools", type_=JSONB)),
        {
            "id": uuid4(),
            "project_id": project_id,
            "run_id": run_id,
            "actor_id": actor_id,
            "index": index,
            "profile_id": profile["id"],
            "name": profile["name"],
            "model": profile["model"],
            "reasoning_effort": profile.get("reasoning_effort"),
            "instructions": profile["instructions"],
            "memory_scope": profile["memory_scope"],
            "allowed_tools": profile["allowed_tools"],
            "autonomy_level": profile["autonomy_level"],
        },
    )
    if any(str(tool_id).startswith("ext_") for tool_id in profile["allowed_tools"]):
        from onefoundry.gateway_extension_registry import snapshot

        snapshot(
            connection,
            project_id,
            run_id,
            profile["id"],
            index,
            actor_id,
            profile["allowed_tools"],
            extension_worker_image,
        )
