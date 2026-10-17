"""Storage owner for versioned project procedures and exact profile bindings."""

# All interpolated SQL fragments below are module constants.
# ruff: noqa: S608

import hashlib
import re
from collections import Counter
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from onefoundry.gateway_errors import Conflict, NotFound
from onefoundry.skill_schemas import SkillBind, SkillCreate, SkillRename, SkillVersionCreate
from onefoundry.workflow_schemas import local_text

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


SKILL_COLUMNS = (
    "s.id,s.project_id,s.name,s.description,s.created_at,"
    "(SELECT max(v.version) FROM skill_versions v "
    "WHERE v.project_id=s.project_id AND v.skill_id=s.id) AS latest_version"
)
VERSION_COLUMNS = "id,project_id,actor_id,skill_id,version,content,sha256,created_at"
BINDING_COLUMNS = (
    "b.id,b.project_id,b.actor_id,b.profile_id,b.skill_id,s.name AS skill_name,"
    "b.version,v.content,v.sha256,b.created_at"
)
AUTO_SKILL_LIMIT = 3
AUTO_CONTENT_BYTES = 16000
_SEARCH_WORDS = re.compile(r"[a-z0-9]+")
# Normalize common artifact names to the form used by project skill metadata.
# Keep aliases narrow: a broad semantic expansion would route unrelated skills.
_SEARCH_ALIASES = {
    "deck": "presentation",
    "decks": "presentation",
    "excel": "spreadsheet",
    "powerpoint": "presentation",
    "pptx": "presentation",
    "slide": "presentation",
    "slides": "presentation",
    "slideshow": "presentation",
    "tam": "market",
    "word": "docx",
    "workbook": "spreadsheet",
    "workbooks": "spreadsheet",
    "xlsx": "spreadsheet",
}
_SEARCH_STOP_WORDS = frozenset(
    {
        "a",
        "about",
        "onefoundry",
        "all",
        "an",
        "and",
        "as",
        "at",
        "be",
        "build",
        "by",
        "can",
        "codex",
        "create",
        "draft",
        "design",
        "edit",
        "for",
        "from",
        "get",
        "in",
        "into",
        "is",
        "it",
        "make",
        "of",
        "on",
        "or",
        "our",
        "paperclip",
        "plan",
        "prepare",
        "procedure",
        "project",
        "review",
        "skill",
        "source",
        "task",
        "the",
        "this",
        "to",
        "use",
        "using",
        "we",
        "with",
        "work",
        "write",
    }
)


def _search_terms(value: str) -> set[str]:
    terms = set()
    for word in _SEARCH_WORDS.findall(value.casefold()):
        if len(word) < 3 or word in _SEARCH_STOP_WORDS:
            continue
        term = _SEARCH_ALIASES.get(word, word)
        terms.add(term[:-1] if len(term) > 4 and term.endswith("s") and not term.endswith("ss") else term)
    return terms


def _check_procedure(procedure: dict[str, Any]) -> None:
    content = str(procedure["content"])
    try:
        local_text(content)
    except ValueError:
        raise Conflict("skill procedure is unsafe") from None
    if len(content.encode("utf-8")) > 16000:
        raise Conflict("skill procedure exceeds limit")
    if hashlib.sha256(content.encode("utf-8")).hexdigest() != procedure["sha256"]:
        raise Conflict("skill procedure digest mismatch")


def bound_procedures(connection: Connection, project_id: UUID, profile_id: UUID) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            f"SELECT {BINDING_COLUMNS} FROM profile_skill_bindings b "
            "JOIN skill_versions v ON v.project_id=b.project_id AND v.skill_id=b.skill_id "
            "AND v.version=b.version "
            "JOIN skills s ON s.project_id=b.project_id AND s.id=b.skill_id "
            "WHERE b.project_id=:project_id AND b.profile_id=:profile_id ORDER BY s.name,s.id"
        ),
        {"project_id": project_id, "profile_id": profile_id},
    ).mappings()
    return [dict(row) for row in rows]


def checked_procedures(connection: Connection, project_id: UUID, profile_id: UUID) -> list[dict[str, Any]]:
    procedures = [
        item
        for item in bound_procedures(connection, project_id, profile_id)
        if _prerequisites_available(connection, project_id, profile_id, item["skill_id"], item["version"])
    ]
    for procedure in procedures:
        _check_procedure(procedure)
    return procedures


def _prerequisites_available(
    connection: Connection, project_id: UUID, profile_id: UUID, skill_id: UUID, version: int
) -> bool:
    package_origin = connection.scalar(
        text(
            "SELECT p.state FROM extension_package_skills ps "
            "JOIN extension_packages p ON p.project_id=ps.project_id AND p.id=ps.package_id "
            "WHERE ps.project_id=:p AND ps.skill_id=:skill AND ps.skill_version=:version"
        ),
        {"p": project_id, "skill": skill_id, "version": version},
    )
    if package_origin is not None and package_origin != "active":
        return False
    prerequisites = list(
        connection.execute(
            text(
                "SELECT package_id,tool_id,package_sha256,descriptor_sha256 "
                "FROM extension_skill_prerequisites "
                "WHERE project_id=:p AND skill_id=:skill AND skill_version=:version"
            ),
            {"p": project_id, "skill": skill_id, "version": version},
        ).mappings()
    )
    if not prerequisites:
        return True
    from onefoundry.gateway_extension_registry import definitions
    from onefoundry.role_tool_admission import registered_tool_ids

    active = {(item.package_id, item.id): item for item in definitions(connection, project_id)}
    allowed = connection.scalar(
        text("SELECT allowed_tools FROM profiles WHERE project_id=:p AND id=:profile"),
        {"p": project_id, "profile": profile_id},
    )
    grants = set(allowed or [])
    for item in prerequisites:
        if item["tool_id"] not in grants:
            return False
        if item["package_id"] is None:
            if item["tool_id"] not in registered_tool_ids():
                return False
            continue
        current = active.get((item["package_id"], item["tool_id"]))
        if current is None:
            return False
        if item["package_sha256"] is not None and (
            current.package_sha256 != item["package_sha256"]
            or current.descriptor_sha256 != item["descriptor_sha256"]
        ):
            return False
    return True


def select_procedures(
    connection: Connection,
    project_id: UUID,
    profile_id: UUID,
    title: str,
    instructions: str,
    *,
    max_auto: int = AUTO_SKILL_LIMIT,
    max_content_bytes: int = AUTO_CONTENT_BYTES,
) -> list[dict[str, Any]]:
    """Select exact pins plus relevant latest project procedures for one task.

    Only metadata is searched. Full contents are loaded and checked for selected
    versions, so the size of the library does not expand the prompt. A pin takes
    precedence over the library's latest version and never counts as automatic.
    """
    if max_auto < 0 or max_content_bytes < 0:
        raise ValueError("skill selection bounds must be nonnegative")
    pinned = checked_procedures(connection, project_id, profile_id)
    selected = [{**skill, "selection_source": "pinned", "selection_score": 0} for skill in pinned]
    remaining = max(0, max_content_bytes - sum(len(str(s["content"]).encode("utf-8")) for s in pinned))
    query_title = _search_terms(title)
    query_instructions = _search_terms(instructions)
    query_terms = query_title | query_instructions
    if not query_terms or max_auto == 0 or remaining == 0:
        return selected

    pinned_ids = {skill["skill_id"] for skill in pinned}
    candidates = [
        dict(row)
        for row in connection.execute(
            text(f"SELECT {SKILL_COLUMNS} FROM skills s WHERE s.project_id=:project_id ORDER BY s.name,s.id"),
            {"project_id": project_id},
        ).mappings()
        if row["latest_version"] is not None and row["id"] not in pinned_ids
    ]
    if not candidates:
        return selected

    document_terms = [
        _search_terms(str(skill["name"])) | _search_terms(str(skill["description"])) for skill in candidates
    ]
    frequency: Counter[str] = Counter(term for terms in document_terms for term in terms)
    ranked: list[tuple[int, dict[str, Any]]] = []
    for skill in candidates:
        name_terms = _search_terms(str(skill["name"]))
        description_terms = _search_terms(str(skill["description"]))
        name_hits = query_terms & name_terms
        description_hits = query_terms & description_terms
        # A description-only match needs two specific concepts. This avoids
        # loading a procedure merely because generic prose mentions one word.
        if not name_hits and len(description_hits) < 2:
            continue
        matched = name_hits | description_hits
        # Words found in most of the library are weak routing evidence.
        if (
            not name_hits
            and len(candidates) >= 8
            and all(frequency[term] * 4 >= len(candidates) * 3 for term in matched)
        ):
            continue
        score = sum(
            (4 if term in name_hits else 1)
            * (2 if term in query_title else 1)
            * (1 + min(4, len(candidates) // (frequency[term] + 1)))
            for term in matched
        )
        # When two procedures share an artifact word, prefer the one whose
        # qualifiers (for example, "live session") the request also mentions.
        score -= len(name_terms - query_terms)
        ranked.append((score, skill))
    ranked.sort(key=lambda item: (-item[0], str(item[1]["name"]).casefold(), str(item[1]["id"])))

    for score, skill in ranked:
        if len(selected) - len(pinned) >= max_auto:
            break
        row = (
            connection.execute(
                text(
                    "SELECT v.id,v.project_id,v.actor_id,v.skill_id,s.name AS skill_name,"
                    "v.version,v.content,v.sha256,v.created_at "
                    "FROM skill_versions v JOIN skills s "
                    "ON s.project_id=v.project_id AND s.id=v.skill_id "
                    "WHERE v.project_id=:project_id AND v.skill_id=:skill_id AND v.version=:version"
                ),
                {"project_id": project_id, "skill_id": skill["id"], "version": skill["latest_version"]},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            continue
        procedure = dict(row)
        if not _prerequisites_available(
            connection, project_id, profile_id, procedure["skill_id"], procedure["version"]
        ):
            continue
        size = len(str(procedure["content"]).encode("utf-8"))
        if size > remaining:
            continue
        _check_procedure(procedure)
        selected.append({**procedure, "selection_source": "automatic", "selection_score": score})
        remaining -= size
    return selected


class SkillStore:
    def __init__(self, gateway: "ToolGateway") -> None:
        self.gateway = gateway

    def list_skills(self, project_id: UUID) -> list[dict[str, Any]]:
        with self.gateway.engine.connect() as connection:
            self.gateway._require_project(connection, project_id)
            rows = connection.execute(
                text(
                    f"SELECT {SKILL_COLUMNS} FROM skills s "
                    "WHERE s.project_id=:project_id ORDER BY s.name,s.id"
                ),
                {"project_id": project_id},
            ).mappings()
            return [dict(row) for row in rows]

    def create_skill(self, project_id: UUID, data: SkillCreate) -> dict[str, Any]:
        with self.gateway.engine.begin() as connection:
            return self.create_skill_in_transaction(connection, project_id, data)

    def create_skill_in_transaction(
        self, connection: Connection, project_id: UUID, data: SkillCreate
    ) -> dict[str, Any]:
        skill_id = uuid4()
        self.gateway._require_project(connection, project_id)
        try:
            row = (
                connection.execute(
                    text(
                        "INSERT INTO skills (id,project_id,actor_id,name,description) "
                        "VALUES (:id,:project_id,:actor_id,:name,:description) "
                        "RETURNING id,project_id,name,description,created_at,"
                        "NULL::integer AS latest_version"
                    ),
                    {
                        "id": skill_id,
                        "project_id": project_id,
                        "actor_id": self.gateway.actor_id,
                        "name": data.name,
                        "description": data.description,
                    },
                )
                .mappings()
                .one()
            )
        except IntegrityError as exc:
            if getattr(exc.orig, "sqlstate", None) == "23505":
                raise Conflict("skill name already exists") from exc
            raise
        self.gateway._event(connection, project_id, None, None, "skill_created", {"skill_id": str(skill_id)})
        return dict(row)

    def rename_skill(self, project_id: UUID, skill_id: UUID, data: SkillRename) -> dict[str, Any]:
        with self.gateway.engine.begin() as connection:
            current = (
                connection.execute(
                    text(
                        "SELECT name,description FROM skills "
                        "WHERE project_id=:project_id AND id=:skill_id FOR UPDATE"
                    ),
                    {"project_id": project_id, "skill_id": skill_id},
                )
                .mappings()
                .one_or_none()
            )
            if current is None:
                raise NotFound("skill not found")
            if (current["name"], current["description"]) != (data.name, data.description):
                if (current["name"], current["description"]) != (
                    data.expected_name,
                    data.expected_description,
                ):
                    raise Conflict("skill metadata changed")
                try:
                    connection.execute(
                        text(
                            "UPDATE skills SET name=:name,description=:description "
                            "WHERE project_id=:project_id AND id=:skill_id"
                        ),
                        {
                            "name": data.name,
                            "description": data.description,
                            "project_id": project_id,
                            "skill_id": skill_id,
                        },
                    )
                except IntegrityError as exc:
                    if getattr(exc.orig, "sqlstate", None) == "23505":
                        raise Conflict("skill name already exists") from exc
                    raise
                self.gateway._event(
                    connection,
                    project_id,
                    None,
                    None,
                    "skill_renamed",
                    {"skill_id": str(skill_id)},
                )
            row = (
                connection.execute(
                    text(
                        f"SELECT {SKILL_COLUMNS} FROM skills s "
                        "WHERE s.project_id=:project_id AND s.id=:skill_id"
                    ),
                    {"project_id": project_id, "skill_id": skill_id},
                )
                .mappings()
                .one()
            )
            return dict(row)

    def list_versions(self, project_id: UUID, skill_id: UUID) -> list[dict[str, Any]]:
        with self.gateway.engine.connect() as connection:
            self._require_skill(connection, project_id, skill_id)
            rows = connection.execute(
                text(
                    f"SELECT {VERSION_COLUMNS} FROM skill_versions "
                    "WHERE project_id=:project_id AND skill_id=:skill_id ORDER BY version DESC"
                ),
                {"project_id": project_id, "skill_id": skill_id},
            ).mappings()
            return [dict(row) for row in rows]

    def get_version(self, project_id: UUID, skill_id: UUID, version: int) -> dict[str, Any]:
        with self.gateway.engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        f"SELECT {VERSION_COLUMNS} FROM skill_versions "
                        "WHERE project_id=:project_id AND skill_id=:skill_id AND version=:version"
                    ),
                    {"project_id": project_id, "skill_id": skill_id, "version": version},
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise NotFound("skill version not found")
            return dict(row)

    def add_version(self, project_id: UUID, skill_id: UUID, data: SkillVersionCreate) -> dict[str, Any]:
        with self.gateway.engine.begin() as connection:
            return self.add_version_in_transaction(connection, project_id, skill_id, data)

    def add_version_in_transaction(
        self, connection: Connection, project_id: UUID, skill_id: UUID, data: SkillVersionCreate
    ) -> dict[str, Any]:
        self._require_skill(connection, project_id, skill_id)
        # The app role has no UPDATE privilege on immutable skill metadata.
        # A transaction advisory lock serializes next-version allocation.
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(CAST(:skill_id AS text)))"),
            {"skill_id": skill_id},
        )
        version = connection.scalar(
            text(
                "SELECT coalesce(max(version),0)+1 FROM skill_versions "
                "WHERE project_id=:project_id AND skill_id=:skill_id"
            ),
            {"project_id": project_id, "skill_id": skill_id},
        )
        digest = hashlib.sha256(data.content.encode("utf-8")).hexdigest()
        row = (
            connection.execute(
                text(
                    "INSERT INTO skill_versions "
                    "(id,project_id,actor_id,skill_id,version,content,sha256) "
                    "VALUES (:id,:project_id,:actor_id,:skill_id,:version,:content,:sha256) "
                    f"RETURNING {VERSION_COLUMNS}"
                ),
                {
                    "id": uuid4(),
                    "project_id": project_id,
                    "actor_id": self.gateway.actor_id,
                    "skill_id": skill_id,
                    "version": version,
                    "content": data.content,
                    "sha256": digest,
                },
            )
            .mappings()
            .one()
        )
        self.gateway._event(
            connection,
            project_id,
            None,
            None,
            "skill_version_added",
            {"skill_id": str(skill_id), "version": version, "sha256": digest},
        )
        return dict(row)

    def list_bindings(self, project_id: UUID, profile_id: UUID) -> list[dict[str, Any]]:
        with self.gateway.engine.connect() as connection:
            self._require_profile(connection, project_id, profile_id)
            return bound_procedures(connection, project_id, profile_id)

    def bind(self, project_id: UUID, profile_id: UUID, skill_id: UUID, data: SkillBind) -> dict[str, Any]:
        with self.gateway.engine.begin() as connection:
            self._require_profile(connection, project_id, profile_id, lock=True)
            current = connection.scalar(
                text(
                    "SELECT id FROM profile_skill_bindings "
                    "WHERE project_id=:project_id AND profile_id=:profile_id AND skill_id=:skill_id"
                ),
                {"project_id": project_id, "profile_id": profile_id, "skill_id": skill_id},
            )
            if current != data.expected_binding_id:
                raise Conflict("profile skill binding changed")
            version = connection.scalar(
                text(
                    "SELECT 1 FROM skill_versions "
                    "WHERE project_id=:project_id AND skill_id=:skill_id AND version=:version"
                ),
                {"project_id": project_id, "skill_id": skill_id, "version": data.version},
            )
            if version is None:
                raise NotFound("skill version not found")
            connection.execute(
                text(
                    "INSERT INTO profile_skill_bindings "
                    "(id,project_id,actor_id,profile_id,skill_id,version) "
                    "VALUES (:id,:project_id,:actor_id,:profile_id,:skill_id,:version) "
                    "ON CONFLICT (project_id,profile_id,skill_id) DO UPDATE "
                    "SET id=EXCLUDED.id,version=EXCLUDED.version,actor_id=EXCLUDED.actor_id,"
                    "created_at=clock_timestamp()"
                ),
                {
                    "id": uuid4(),
                    "project_id": project_id,
                    "actor_id": self.gateway.actor_id,
                    "profile_id": profile_id,
                    "skill_id": skill_id,
                    "version": data.version,
                },
            )
            binding = next(
                item
                for item in bound_procedures(connection, project_id, profile_id)
                if item["skill_id"] == skill_id
            )
            self.gateway._event(
                connection,
                project_id,
                None,
                None,
                "profile_skill_bound",
                {
                    "profile_id": str(profile_id),
                    "skill_id": str(skill_id),
                    "version": data.version,
                    "sha256": binding["sha256"],
                },
            )
            return binding

    def unbind(self, project_id: UUID, profile_id: UUID, skill_id: UUID, expected_binding_id: UUID) -> None:
        with self.gateway.engine.begin() as connection:
            self._require_profile(connection, project_id, profile_id, lock=True)
            current = connection.scalar(
                text(
                    "SELECT id FROM profile_skill_bindings "
                    "WHERE project_id=:project_id AND profile_id=:profile_id AND skill_id=:skill_id"
                ),
                {"project_id": project_id, "profile_id": profile_id, "skill_id": skill_id},
            )
            if current is None:
                raise NotFound("profile skill binding not found")
            if current != expected_binding_id:
                raise Conflict("profile skill binding changed")
            row = connection.execute(
                text(
                    "DELETE FROM profile_skill_bindings "
                    "WHERE project_id=:project_id AND profile_id=:profile_id AND skill_id=:skill_id "
                    "RETURNING version"
                ),
                {"project_id": project_id, "profile_id": profile_id, "skill_id": skill_id},
            ).one_or_none()
            assert row is not None
            self.gateway._event(
                connection,
                project_id,
                None,
                None,
                "profile_skill_unbound",
                {"profile_id": str(profile_id), "skill_id": str(skill_id), "version": row[0]},
            )

    @staticmethod
    def _require_skill(connection: Connection, project_id: UUID, skill_id: UUID) -> None:
        sql = "SELECT 1 FROM skills WHERE project_id=:project_id AND id=:skill_id"
        if connection.scalar(text(sql), {"project_id": project_id, "skill_id": skill_id}) is None:
            raise NotFound("skill not found")

    @staticmethod
    def _require_profile(
        connection: Connection, project_id: UUID, profile_id: UUID, *, lock: bool = False
    ) -> None:
        sql = "SELECT 1 FROM profiles WHERE project_id=:project_id AND id=:profile_id"
        if lock:
            sql += " FOR UPDATE"
        if connection.scalar(text(sql), {"project_id": project_id, "profile_id": profile_id}) is None:
            raise NotFound("profile not found")
