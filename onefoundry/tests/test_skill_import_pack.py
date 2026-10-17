"""Offline tests for reviewed skill-pack transfer and source inventory."""

import hashlib
import json
import re
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from scripts.import_skill_pack import LocalSkillAPI, import_pack, load_pack
from scripts.skill_inventory import build_inventory

PROJECT = UUID("e45311b9-91f8-47e3-bb1f-084dc698eeef")


class FakeSkillAPI:
    def __init__(self) -> None:
        self.skills: list[dict[str, Any]] = []
        self.versions: dict[str, list[dict[str, Any]]] = {}
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        self.calls.append((method, path))
        if path.endswith("/skills"):
            if method == "GET":
                return [
                    {**row, "latest_version": len(self.versions.get(str(row["id"]), [])) or None}
                    for row in self.skills
                ]
            assert payload is not None
            row = {"id": str(UUID(int=len(self.skills) + 1)), **payload}
            self.skills.append(row)
            return row
        if path.endswith("/versions"):
            skill_id = path.split("/")[-2]
            if method == "GET":
                return self.versions.get(skill_id, []).copy()
            assert payload is not None
            content = str(payload["content"])
            row = {
                "version": len(self.versions.get(skill_id, [])) + 1,
                "content": content,
                "sha256": hashlib.sha256(content.encode()).hexdigest(),
            }
            self.versions.setdefault(skill_id, []).append(row)
            return row
        if method == "GET" and "/versions/" in path:
            skill_id, version = path.split("/versions/")
            return self.versions[skill_id.split("/")[-1]][int(version) - 1]
        raise AssertionError((method, path))


def test_pack_is_bounded_and_reimport_is_idempotent() -> None:
    skills = load_pack()
    assert len(skills) == 169
    assert {skill.name for skill in skills} >= {
        "work planning",
        "intake triage",
        "QA acceptance",
        "skill authoring",
        "KPI design",
        "release announcement draft",
        "project status brief",
        "stalled-work diagnosis",
        "supplied KPI readout",
        "supplied metric diagnosis",
        "supplied analysis review",
        "documentation drift review",
        "PR briefing draft",
        "visual accessibility brief",
        "public company meeting brief",
        "public catalyst brief",
        "profile improvement proposal",
        "manual engineering review",
        "integration research brief",
        "public issuer baseline",
        "public equity idea triage",
        "short pre-earnings brief",
        "short post-earnings brief",
        "security hardening options",
        "supplied artifact summary",
        "public peer comps screen",
        "public equity shock brief",
        "public equity memo update",
        "public equity scenario screen",
        "public equity thesis update",
        "banking meeting brief",
        "banking company baseline",
        "preliminary buyer universe",
        "capital market window brief",
        "initial private credit screen",
        "distressed screening note",
        "banking quick comps review",
    }
    api = FakeSkillAPI()
    first = import_pack(api, PROJECT, skills)
    assert all(item.startswith("saved v1:") for item in first)
    write_count = len([call for call in api.calls if call[0] == "POST"])
    second = import_pack(api, PROJECT, skills)
    assert all(item.startswith("unchanged:") for item in second)
    assert len([call for call in api.calls if call[0] == "POST"]) == write_count


def test_pack_procedures_use_source_neutral_names_and_text() -> None:
    root = Path(__file__).resolve().parent.parent / "skill_packs" / "adapted"
    entries = json.loads((root / "manifest.json").read_text(encoding="utf-8"))["skills"]
    assert len(entries) == 169
    for entry in entries:
        content = (root / entry["file"]).read_text(encoding="utf-8")
        for value in (entry["name"], entry["description"], entry["file"], content):
            assert re.search(r"paperclip|codex", value, re.IGNORECASE) is None


def test_collision_stops_before_version_write() -> None:
    skills = load_pack()
    api = FakeSkillAPI()
    api.skills.append({"id": str(UUID(int=22)), "name": skills[0].name, "description": "Owned by operator"})
    with pytest.raises(ValueError, match="name collision"):
        import_pack(api, PROJECT, skills)
    assert all(method != "POST" for method, _ in api.calls)


def test_existing_branded_skill_must_be_renamed_before_import() -> None:
    skill = load_pack()[0]
    api = FakeSkillAPI()
    api.skills.append(
        {
            "id": str(UUID(int=23)),
            "name": f"AgentPod {skill.name}",
            "description": f"[AgentPod pack] {skill.description}",
        }
    )
    with pytest.raises(ValueError, match="rename_skill_library"):
        import_pack(api, PROJECT, [skill])
    assert all(method != "POST" for method, _ in api.calls)


def test_newer_library_version_needs_explicit_update() -> None:
    skills = load_pack()
    api = FakeSkillAPI()
    assert import_pack(api, PROJECT, skills[:1]) == [f"saved v1: {skills[0].name}"]
    skill_id = str(api.skills[0]["id"])
    api.request(
        "POST", f"/api/projects/{PROJECT}/skills/{skill_id}/versions", {"content": "# Operator revision"}
    )
    write_count = len([call for call in api.calls if call[0] == "POST"])
    with pytest.raises(ValueError, match="latest version differs"):
        import_pack(api, PROJECT, skills[:1])
    assert len([call for call in api.calls if call[0] == "POST"]) == write_count
    assert import_pack(api, PROJECT, skills[:1], allow_update=True) == [f"saved v3: {skills[0].name}"]
    assert import_pack(api, PROJECT, skills[:1]) == [f"unchanged: {skills[0].name}"]


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8765",
        "http://example.com:8765",
        "http://127.0.0.1:8765/path",
        "http://user:pass@127.0.0.1:8765",
        "http://127.0.0.1",
        "http://127.0.0.1:8765?x=1",
    ],
)
def test_import_api_refuses_nonlocal_or_ambiguous_target(url: str) -> None:
    with pytest.raises(ValueError, match="base URL"):
        LocalSkillAPI(url)


def test_pack_rejects_symlink_and_secret(tmp_path: Path) -> None:
    root = tmp_path / "pack"
    root.mkdir()
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "format": 1,
                "skills": [{"name": "Example", "description": "Example", "file": "example.md"}],
            }
        )
    )
    target = tmp_path / "elsewhere.md"
    target.write_text("# Example")
    (root / "example.md").symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        load_pack(root)
    (root / "example.md").unlink()
    (root / "example.md").write_text("# Example\napi_key=shown-here")
    with pytest.raises(ValueError, match="secret"):
        load_pack(root)


def test_inventory_records_all_local_sources_without_instruction_text(tmp_path: Path) -> None:
    paperclip = tmp_path / "paperclip"
    codex = tmp_path / "codex"
    (paperclip / "skills" / "example").mkdir(parents=True)
    (codex / "skills" / "example").mkdir(parents=True)
    (codex / "plugins" / "cache").mkdir(parents=True)
    (paperclip / "skills" / "example" / "SKILL.md").write_text("PRIVATE SOURCE BODY")
    (codex / "skills" / "example" / "SKILL.md").write_text("CODEX BODY")
    inventory = build_inventory(paperclip, codex, {})
    assert inventory["counts"]["paperclip_skills"] == 1
    assert inventory["counts"]["codex_skills"] == 1
    assert "PRIVATE SOURCE BODY" not in json.dumps(inventory)
    assert all("source_id" in item for item in inventory["sources"])
    assert all("sha256" not in item and "license_review" not in item for item in inventory["sources"])


def test_pack_sources_exist_in_saved_inventory() -> None:
    inventory_path = Path(__file__).resolve().parent.parent / "skill_packs" / "source_inventory.json"
    manifest_path = Path(__file__).resolve().parent.parent / "skill_packs" / "adapted" / "manifest.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mapped_names = {
        item["adapted_as"] for item in inventory["sources"] if item["status"] == "adapted_concept"
    }
    assert mapped_names == {item["name"] for item in manifest["skills"]}
    adapted = {source_id for item in manifest["skills"] for source_id in item["derived_from"]}
    listed = {item["source_id"] for item in inventory["sources"] if item["status"] == "adapted_concept"}
    assert listed == adapted
    engineering_sources = [
        item
        for item in inventory["sources"]
        if item["source_id"]
        in next(
            skill["derived_from"]
            for skill in manifest["skills"]
            if skill["name"] == "manual engineering review"
        )
    ]
    assert len(engineering_sources) == 2
    assert {item["kind"] for item in engineering_sources} == {"workflow_template"}
    source_ids = [source_id for item in manifest["skills"] for source_id in item["derived_from"]]
    assert len(source_ids) == len(set(source_ids))
    assert (
        sum(item["kind"] == "skill" and item["status"] == "adapted_concept" for item in inventory["sources"])
        == 168
    )
    assert all(
        item["status"] == "adapted_concept"
        for item in inventory["sources"]
        if item["kind"] == "skill" and item["source_id"].startswith("paperclip:")
    )
    visual_prefixes = (
        "codex:plugins/cache/openai-bundled/",
        "codex:plugins/cache/openai-curated/build-web-data-visualization/",
        "codex:plugins/cache/openai-curated-remote/openai-templates/",
        "codex:plugins/cache/openai-primary-runtime/",
    )
    visual = [
        item
        for item in inventory["sources"]
        if item["kind"] == "skill" and item["source_id"].startswith(visual_prefixes)
    ]
    # This includes the accessibility source adapted before the selected 50-source batch.
    assert len(visual) == 51
    assert all(item["status"] == "adapted_concept" for item in visual)
    engineering = [
        item
        for item in inventory["sources"]
        if item["kind"] == "skill"
        and (
            item["source_id"].startswith("codex:plugins/cache/openai-curated-remote/superpowers/")
            or item["source_id"].startswith("codex:plugins/cache/openai-curated-remote/codex-security/")
            or item["source_id"]
            in {
                "codex:skills/.system/imagegen/SKILL.md",
                "codex:skills/.system/openai-docs/SKILL.md",
                "codex:skills/.system/plugin-creator/SKILL.md",
                "codex:skills/.system/review-agent/SKILL.md",
                "codex:skills/.system/skill-installer/SKILL.md",
                "codex:skills/playwright/SKILL.md",
            }
        )
    ]
    assert len(engineering) == 36
    assert all(item["status"] == "adapted_concept" for item in engineering)
    assert all(
        item["status"] == "adapted_concept"
        for item in inventory["sources"]
        if item["kind"] in {"workflow_template", "workflow_reference"}
    )


@pytest.mark.parametrize(
    ("pack", "total", "adapted"),
    [
        ("data-analytics", 20, 9),
        ("public-equity-investing", 23, 11),
        ("investment-banking", 23, 7),
    ],
)
def test_finance_data_pack_coverage_matches_inventory(pack: str, total: int, adapted: int) -> None:
    inventory_path = Path(__file__).resolve().parent.parent / "skill_packs" / "source_inventory.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    prefix = f"codex:plugins/cache/openai-curated-remote/{pack}/"
    sources = [
        item
        for item in inventory["sources"]
        if item["kind"] == "skill" and item["source_id"].startswith(prefix)
    ]
    assert len(sources) == total
    assert sum(item["status"] == "adapted_concept" for item in sources) == adapted
