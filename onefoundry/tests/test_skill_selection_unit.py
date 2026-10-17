"""Metadata routing chooses only relevant checked procedure versions."""

import hashlib
import json
from pathlib import Path
from typing import cast
from uuid import UUID

from sqlalchemy import Connection, Engine, create_engine, text

from onefoundry.skill_store import select_procedures

PROJECT = UUID(int=1)
OTHER_PROJECT = UUID(int=2)
PROFILE = UUID(int=3)


def _id(value: UUID) -> UUID:
    # SQLite stores these test identifiers as text; production PostgreSQL uses UUID.
    return cast(UUID, str(value))


def _skill(
    connection: Connection,
    skill_id: int,
    name: str,
    description: str,
    content: str,
    *,
    project_id: UUID = PROJECT,
    version: int = 1,
) -> None:
    identity = str(UUID(int=skill_id))
    connection.execute(
        text(
            "INSERT OR IGNORE INTO skills (id,project_id,name,description) "
            "VALUES (:id,:project,:name,:description)"
        ),
        {"id": identity, "project": str(project_id), "name": name, "description": description},
    )
    connection.execute(
        text(
            "INSERT INTO skill_versions "
            "(id,project_id,actor_id,skill_id,version,content,sha256,created_at) "
            "VALUES (:id,:project,'operator',:skill,:version,:content,:sha256,'2026-01-01')"
        ),
        {
            "id": str(UUID(int=1000 + skill_id * 10 + version)),
            "project": str(project_id),
            "skill": identity,
            "version": version,
            "content": content,
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
        },
    )


def _connection() -> tuple[Connection, Engine]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    connection = engine.connect()
    connection.execute(
        text(
            "CREATE TABLE skills (id TEXT, project_id TEXT, name TEXT, description TEXT, "
            "created_at TEXT, PRIMARY KEY (project_id,id))"
        )
    )
    connection.execute(
        text(
            "CREATE TABLE skill_versions (id TEXT, project_id TEXT, actor_id TEXT, skill_id TEXT, "
            "version INTEGER, content TEXT, sha256 TEXT, created_at TEXT)"
        )
    )
    connection.execute(
        text(
            "CREATE TABLE profile_skill_bindings (id TEXT, project_id TEXT, actor_id TEXT, "
            "profile_id TEXT, skill_id TEXT, version INTEGER, created_at TEXT)"
        )
    )
    return connection, engine


def test_auto_selection_is_relevant_project_scoped_and_uses_latest_version() -> None:
    connection, engine = _connection()
    try:
        _skill(connection, 10, "PDF production intake", "Prepare PDF output", "# PDF v1")
        _skill(connection, 10, "PDF production intake", "Prepare PDF output", "# PDF v2", version=2)
        _skill(connection, 11, "Security threat model", "Trace attack paths", "# Security")
        _skill(
            connection, 12, "PDF production intake", "Prepare PDF output", "# Other", project_id=OTHER_PROJECT
        )
        selected = select_procedures(
            connection, _id(PROJECT), _id(PROFILE), "Produce a PDF", "Make a printable PDF document"
        )
        assert [(item["skill_name"], item["version"], item["selection_source"]) for item in selected] == [
            ("PDF production intake", 2, "automatic")
        ]
        assert selected[0]["content"] == "# PDF v2"
        assert selected[0]["selection_score"] > 0
        assert select_procedures(connection, _id(PROJECT), _id(PROFILE), "Hello", "Unrelated request") == []
    finally:
        connection.close()
        engine.dispose()


def test_exact_pin_precedes_auto_and_budget_caps_only_auto() -> None:
    connection, engine = _connection()
    try:
        _skill(connection, 20, "PDF production intake", "Make PDF output", "# Pinned PDF")
        _skill(connection, 20, "PDF production intake", "Make PDF output", "# Latest PDF", version=2)
        _skill(connection, 21, "Spreadsheet analysis", "Analyze spreadsheet data", "# Spreadsheet")
        connection.execute(
            text(
                "INSERT INTO profile_skill_bindings "
                "(id,project_id,actor_id,profile_id,skill_id,version,created_at) "
                "VALUES (:id,:project,'operator',:profile,:skill,1,'2026-01-01')"
            ),
            {
                "id": str(UUID(int=500)),
                "project": str(PROJECT),
                "profile": str(PROFILE),
                "skill": str(UUID(int=20)),
            },
        )
        selected = select_procedures(
            connection,
            _id(PROJECT),
            _id(PROFILE),
            "Analyze PDF spreadsheet",
            "Analyze PDF and spreadsheet data",
            max_content_bytes=len("# Pinned PDF"),
        )
        assert [(item["skill_name"], item["version"], item["selection_source"]) for item in selected] == [
            ("PDF production intake", 1, "pinned")
        ]
        expanded = select_procedures(
            connection,
            _id(PROJECT),
            _id(PROFILE),
            "Analyze PDF spreadsheet",
            "Analyze PDF and spreadsheet data",
        )
        assert [(item["skill_name"], item["selection_source"]) for item in expanded] == [
            ("PDF production intake", "pinned"),
            ("Spreadsheet analysis", "automatic"),
        ]
        assert expanded[0]["content"] == "# Pinned PDF"
    finally:
        connection.close()
        engine.dispose()


def test_rank_order_and_automatic_count_are_bounded() -> None:
    connection, engine = _connection()
    try:
        _skill(connection, 30, "Chart design", "Design a chart", "# Chart")
        _skill(connection, 31, "PDF chart production", "Create a PDF chart", "# PDF chart")
        _skill(connection, 32, "Copy editing", "Edit article copy", "# Copy")
        selected = select_procedures(
            connection,
            _id(PROJECT),
            _id(PROFILE),
            "PDF chart",
            "Create a PDF chart for a report",
            max_auto=1,
        )
        assert [item["skill_name"] for item in selected] == ["PDF chart production"]
        assert (
            select_procedures(
                connection, _id(PROJECT), _id(PROFILE), "PDF chart", "Create a PDF chart", max_auto=0
            )
            == []
        )
    finally:
        connection.close()
        engine.dispose()


def test_name_match_remains_eligible_in_single_topic_library() -> None:
    connection, engine = _connection()
    try:
        for index in range(10):
            _skill(connection, 40 + index, f"Market scenario {index}", "Estimate market", "# Market")
        selected = select_procedures(connection, _id(PROJECT), _id(PROFILE), "Market", "")
        assert 0 < len(selected) <= 3
        assert all(item["skill_name"].startswith("Market scenario") for item in selected)
    finally:
        connection.close()
        engine.dispose()


def test_presentation_aliases_route_real_pack_metadata() -> None:
    manifest_path = Path(__file__).resolve().parents[1] / "skill_packs/adapted/manifest.json"
    catalog = json.loads(manifest_path.read_text())["skills"]
    connection, engine = _connection()
    try:
        for index, skill in enumerate(catalog, start=100):
            _skill(connection, index, skill["name"], skill["description"], f"# {skill['name']}")
        powerpoint = select_procedures(
            connection,
            _id(PROJECT),
            _id(PROFILE),
            "Create a PowerPoint for investors",
            "",
        )
        assert powerpoint
        assert powerpoint[0]["skill_name"] == "OneFoundry presentation production intake"
        pitch_deck = select_procedures(
            connection,
            _id(PROJECT),
            _id(PROFILE),
            "Design a pitch deck for investors",
            "",
        )
        assert pitch_deck
        assert pitch_deck[0]["skill_name"] == "OneFoundry presentation production intake"
        assert "OneFoundry system design template intake" not in [skill["skill_name"] for skill in pitch_deck]
        market = select_procedures(
            connection, _id(PROJECT), _id(PROFILE), "Estimate TAM for a new product", ""
        )
        assert market
        assert market[0]["skill_name"] == "OneFoundry market sizing"
    finally:
        connection.close()
        engine.dispose()


def test_document_and_spreadsheet_aliases_route_real_pack_metadata() -> None:
    manifest_path = Path(__file__).resolve().parents[1] / "skill_packs/adapted/manifest.json"
    catalog = json.loads(manifest_path.read_text())["skills"]
    connection, engine = _connection()
    try:
        for index, skill in enumerate(catalog, start=100):
            _skill(connection, index, skill["name"], skill["description"], f"# {skill['name']}")
        cases = (
            ("Create a Word document", "OneFoundry DOCX production intake"),
            ("Edit a DOCX file", "OneFoundry DOCX production intake"),
            ("Create an Excel workbook", "OneFoundry spreadsheet production intake"),
            ("Build an XLSX workbook", "OneFoundry spreadsheet production intake"),
            ("Edit an open Excel session", "OneFoundry live Excel session intake"),
        )
        for title, expected in cases:
            selected = select_procedures(connection, _id(PROJECT), _id(PROFILE), title, "")
            assert selected, title
            assert selected[0]["skill_name"] == expected, title
    finally:
        connection.close()
        engine.dispose()
