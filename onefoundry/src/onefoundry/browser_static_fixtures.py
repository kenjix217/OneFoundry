"""Durable operator owner for exact, private static browser fixtures.

This store neither launches Docker nor exposes source bytes through HTTP. A
trusted Tool Gateway caller may retrieve approved bytes for one bound run.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import text

from onefoundry.browser_target import BrowserTarget, BrowserTargetError, BrowserTargetLimits
from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.role_sensitive_text import contains_sensitive_text

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


class StaticFixtureInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: UUID
    approved_url: str = Field(min_length=1, max_length=2048)
    html: str = Field(min_length=1, max_length=262_144)
    css: str = Field(default="", max_length=262_144)
    viewport_width: StrictInt
    viewport_height: StrictInt
    max_page_bytes: StrictInt
    timeout_seconds: StrictInt
    lifetime_seconds: StrictInt


@dataclass(frozen=True, slots=True)
class StaticFixturePolicy:
    target_limits: BrowserTargetLimits
    max_fixture_bytes: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.target_limits, BrowserTargetLimits)
            or type(self.max_fixture_bytes) is not int
            or not 1 <= self.max_fixture_bytes <= 262_144
        ):
            raise ValueError("static browser fixture policy is invalid")


@dataclass(frozen=True, slots=True)
class ApprovedStaticFixture:
    fixture_id: UUID
    target: BrowserTarget
    html: bytes
    css: bytes
    source_sha256: str
    proposal_digest: str


def _source_hash(html: bytes, css: bytes) -> str:
    return hashlib.sha256(html + b"\x00" + css).hexdigest()


def _proposal_digest(project_id: UUID, run_id: UUID, data: StaticFixtureInput, source_hash: str) -> str:
    encoded = json.dumps(
        {
            "project_id": str(project_id),
            "run_id": str(run_id),
            "idempotency_key": str(data.idempotency_key),
            "approved_url": data.approved_url,
            "source_sha256": source_hash,
            "viewport_width": data.viewport_width,
            "viewport_height": data.viewport_height,
            "max_page_bytes": data.max_page_bytes,
            "timeout_seconds": data.timeout_seconds,
            "lifetime_seconds": data.lifetime_seconds,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _approval_content(
    fixture_id: UUID, project_id: UUID, run_id: UUID, data: StaticFixtureInput, source_hash: str
) -> str:
    return json.dumps(
        {
            "kind": "browser_static_fixture_v1",
            "fixture_id": str(fixture_id),
            "project_id": str(project_id),
            "run_id": str(run_id),
            "approved_url": data.approved_url,
            "source_sha256": source_hash,
            "viewport_width": data.viewport_width,
            "viewport_height": data.viewport_height,
            "max_page_bytes": data.max_page_bytes,
            "timeout_seconds": data.timeout_seconds,
            "lifetime_seconds": data.lifetime_seconds,
            "idempotency_key": str(data.idempotency_key),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "run_id": row["run_id"],
        "approval_id": row["approval_id"],
        "actor_id": row["actor_id"],
        "approved_url": row["approved_url"],
        "source_sha256": row["source_sha256"],
        "proposal_digest": row["proposal_digest"],
        "viewport_width": row["viewport_width"],
        "viewport_height": row["viewport_height"],
        "max_page_bytes": row["max_page_bytes"],
        "timeout_seconds": row["timeout_seconds"],
        "lifetime_seconds": row["lifetime_seconds"],
        "status": row["status"],
        "created_at": row["created_at"],
        "decided_at": row["decided_at"],
    }


class StaticFixtureStore:
    """Persist immutable source bound to the existing one-way approval inbox."""

    def __init__(self, gateway: ToolGateway, policy: StaticFixturePolicy) -> None:
        if not isinstance(gateway.actor_id, str) or not gateway.actor_id.strip():
            raise ValueError("operator actor is required")
        self.gateway = gateway
        self.engine = gateway.engine
        self.actor_id = gateway.actor_id
        self.policy = policy

    def _lock(self) -> str:
        return " FOR UPDATE" if self.engine.dialect.name == "postgresql" else ""

    def _validate(
        self, project_id: UUID, run_id: UUID, data: StaticFixtureInput, now: datetime
    ) -> tuple[bytes, bytes, str, str]:
        html, css = data.html.encode("utf-8"), data.css.encode("utf-8")
        if (
            len(html) + len(css) + len(b"<style></style>")
            > min(self.policy.max_fixture_bytes, data.max_page_bytes)
            or contains_sensitive_text(data.html)
            or contains_sensitive_text(data.css)
            or "</style" in data.css.lower()
        ):
            raise Conflict("static browser fixture source is invalid or exceeds policy")
        if not data.approved_url.startswith("http://127.0.0.1:"):
            raise Conflict("static browser target must use canonical IPv4 loopback")
        try:
            BrowserTarget.bind(
                project_id=project_id,
                run_id=run_id,
                actor_id=self.actor_id,
                approved_url=data.approved_url,
                expires_at=now + timedelta(seconds=data.lifetime_seconds),
                idempotency_key=data.idempotency_key,
                viewport_width=data.viewport_width,
                viewport_height=data.viewport_height,
                max_page_bytes=data.max_page_bytes,
                timeout_seconds=data.timeout_seconds,
                limits=self.policy.target_limits,
                now=now,
            )
        except (BrowserTargetError, OverflowError) as error:
            raise Conflict("static browser target violates configured policy") from error
        source_hash = _source_hash(html, css)
        return html, css, source_hash, _proposal_digest(project_id, run_id, data, source_hash)

    def create(
        self, project_id: UUID, run_id: UUID, data: StaticFixtureInput, *, now: datetime | None = None
    ) -> dict[str, Any]:
        checked_at = now or datetime.now(UTC)
        html, css, source_hash, digest = self._validate(project_id, run_id, data, checked_at)
        with self.engine.begin() as connection:
            project = (
                connection.execute(
                    text("SELECT status FROM projects WHERE id=:id" + self._lock()),  # noqa: S608 - fixed suffix
                    {"id": project_id},
                )
                .mappings()
                .one_or_none()
            )
            if project is None:
                raise NotFound("project not found")
            if project["status"] != "active":
                raise Conflict("project is not active")
            run_query = (
                text(
                    "SELECT work_order_id,status FROM runs "
                    "WHERE project_id=:project_id AND id=:run_id FOR UPDATE"
                )
                if self.engine.dialect.name == "postgresql"
                else text("SELECT work_order_id,status FROM runs WHERE project_id=:project_id AND id=:run_id")
            )
            run = (
                connection.execute(
                    run_query,
                    {"project_id": project_id, "run_id": run_id},
                )
                .mappings()
                .one_or_none()
            )
            if run is None:
                raise NotFound("run not found")
            if run["status"] != "running":
                raise Conflict("run is not active")
            work_order_id = run["work_order_id"]
            previous_query = (
                "SELECT f.*,a.status,a.decided_at FROM browser_static_fixtures f "
                "JOIN approval_requests a ON a.project_id=f.project_id AND a.run_id=f.run_id "
                "AND a.id=f.approval_id WHERE f.project_id=:project_id "
                "AND f.run_id=:run_id AND f.idempotency_key=:key"
            )
            if self.engine.dialect.name == "postgresql":
                previous_query += " FOR UPDATE"
            previous = (
                connection.execute(
                    text(previous_query),  # noqa: S608 - query has only fixed internal fragments
                    {"project_id": project_id, "run_id": run_id, "key": data.idempotency_key},
                )
                .mappings()
                .one_or_none()
            )
            if previous is not None:
                if previous["proposal_digest"] != digest:
                    raise Conflict("static browser fixture idempotency key is already bound")
                return _summary(dict(previous))
            fixture_id = uuid4()
            approval_id = uuid4()
            approval_content = _approval_content(fixture_id, project_id, run_id, data, source_hash)
            connection.execute(
                text(
                    "INSERT INTO approval_requests "
                    "(id,project_id,actor_id,work_order_id,run_id,action_type,target,content,"
                    "proposal_digest,status) VALUES "
                    "(:id,:project_id,:actor_id,:work_order_id,:run_id,'other',:target,:content,"
                    ":proposal_digest,'pending')"
                ),
                {
                    "id": approval_id,
                    "project_id": project_id,
                    "actor_id": self.actor_id,
                    "work_order_id": work_order_id,
                    "run_id": run_id,
                    "target": data.approved_url,
                    "content": approval_content,
                    "proposal_digest": digest,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO browser_static_fixtures "
                    "(id,project_id,run_id,approval_id,actor_id,idempotency_key,approved_url,html,css,"
                    "source_sha256,proposal_digest,viewport_width,viewport_height,max_page_bytes,"
                    "timeout_seconds,lifetime_seconds,created_at) VALUES "
                    "(:id,:project_id,:run_id,:approval_id,:actor_id,:idempotency_key,:approved_url,:html,:css,"
                    ":source_sha256,:proposal_digest,:viewport_width,:viewport_height,:max_page_bytes,"
                    ":timeout_seconds,:lifetime_seconds,:created_at)"
                ),
                {
                    "id": fixture_id,
                    "project_id": project_id,
                    "run_id": run_id,
                    "approval_id": approval_id,
                    "actor_id": self.actor_id,
                    "idempotency_key": data.idempotency_key,
                    "approved_url": data.approved_url,
                    "html": html,
                    "css": css,
                    "source_sha256": source_hash,
                    "proposal_digest": digest,
                    "viewport_width": data.viewport_width,
                    "viewport_height": data.viewport_height,
                    "max_page_bytes": data.max_page_bytes,
                    "timeout_seconds": data.timeout_seconds,
                    "lifetime_seconds": data.lifetime_seconds,
                    "created_at": checked_at,
                },
            )
            self.gateway._event(
                connection,
                project_id,
                work_order_id,
                run_id,
                "browser_static_fixture_proposed",
                {
                    "fixture_id": str(fixture_id),
                    "approval_id": str(approval_id),
                    "proposal_digest": digest,
                    "source_sha256": source_hash,
                },
            )
            return self.get(project_id, run_id, fixture_id, connection=connection)

    def get(
        self, project_id: UUID, run_id: UUID, fixture_id: UUID, *, connection: Any = None
    ) -> dict[str, Any]:
        if connection is None:
            with self.engine.connect() as owned:
                return self.get(project_id, run_id, fixture_id, connection=owned)
        row = (
            connection.execute(
                text(
                    "SELECT f.*,a.status,a.decided_at FROM browser_static_fixtures f "
                    "JOIN approval_requests a ON a.project_id=f.project_id AND a.run_id=f.run_id "
                    "AND a.id=f.approval_id WHERE f.project_id=:project_id "
                    "AND f.run_id=:run_id AND f.id=:id"
                ),
                {"project_id": project_id, "run_id": run_id, "id": fixture_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise NotFound("static browser fixture not found")
        return _summary(dict(row))

    def approved(
        self, project_id: UUID, run_id: UUID, fixture_id: UUID, *, now: datetime | None = None
    ) -> ApprovedStaticFixture:
        checked_at = now or datetime.now(UTC)
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT f.*,a.status,a.decided_at,a.target,a.content,"
                        "a.actor_id AS approval_actor,a.action_type,"
                        "a.proposal_digest AS approval_digest "
                        "FROM browser_static_fixtures f JOIN projects p ON p.id=f.project_id "
                        "JOIN runs r ON r.project_id=f.project_id AND r.id=f.run_id "
                        "JOIN approval_requests a ON a.project_id=f.project_id AND a.run_id=f.run_id "
                        "AND a.id=f.approval_id "
                        "WHERE f.project_id=:project_id AND f.run_id=:run_id AND f.id=:id "
                        "AND a.status='approved' AND p.status='active' AND r.status='running'"
                    ),
                    {"project_id": project_id, "run_id": run_id, "id": fixture_id},
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise NotFound("approved static browser fixture not found")
        html, css = bytes(row["html"]), bytes(row["css"])
        if _source_hash(html, css) != row["source_sha256"]:
            raise Conflict("static browser fixture owner bytes changed")
        try:
            data = StaticFixtureInput(
                idempotency_key=row["idempotency_key"],
                approved_url=row["approved_url"],
                html=html.decode("utf-8"),
                css=css.decode("utf-8"),
                viewport_width=row["viewport_width"],
                viewport_height=row["viewport_height"],
                max_page_bytes=row["max_page_bytes"],
                timeout_seconds=row["timeout_seconds"],
                lifetime_seconds=row["lifetime_seconds"],
            )
            _, _, _, digest = self._validate(project_id, run_id, data, row["decided_at"])
            if (
                digest != row["proposal_digest"]
                or row["approval_digest"] != digest
                or row["approval_actor"] != row["actor_id"]
                or row["action_type"] != "other"
                or row["target"] != row["approved_url"]
                or row["content"]
                != _approval_content(fixture_id, project_id, run_id, data, row["source_sha256"])
            ):
                raise Conflict("static browser fixture owner binding changed")
            target = BrowserTarget.bind(
                project_id=project_id,
                run_id=run_id,
                actor_id=row["actor_id"],
                approved_url=row["approved_url"],
                expires_at=row["decided_at"] + timedelta(seconds=row["lifetime_seconds"]),
                idempotency_key=row["idempotency_key"],
                viewport_width=row["viewport_width"],
                viewport_height=row["viewport_height"],
                max_page_bytes=row["max_page_bytes"],
                timeout_seconds=row["timeout_seconds"],
                limits=self.policy.target_limits,
                now=row["decided_at"],
            )
            target.check_navigation(target.approved_url, now=checked_at)
        except (BrowserTargetError, UnicodeError, ValueError, TypeError, OverflowError) as error:
            raise Conflict("static browser fixture owner binding invalid or expired") from error
        return ApprovedStaticFixture(fixture_id, target, html, css, row["source_sha256"], digest)
