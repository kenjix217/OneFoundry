"""A procedure rename keeps IDs, distinct pinned content, and latest content."""

import hashlib
from typing import Any
from uuid import UUID

from scripts import rename_skill_library
from scripts.import_skill_pack import PackSkill

PROJECT = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
PROFILE = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
SKILL = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
BINDING = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")


class FakeAPI:
    def __init__(self) -> None:
        self.skill = {
            "id": str(SKILL),
            "name": "AgentPod review",
            "description": "[AgentPod pack] Review AgentPod work.",
        }
        self.versions = [
            self._version(1, "# AgentPod first\nPreserve this pinned variant."),
            self._version(2, "# AgentPod review\nPreserve this latest variant."),
        ]
        self.pin = {"id": str(BINDING), "profile_id": str(PROFILE), "skill_id": str(SKILL), "version": 1}
        self.writes = 0

    @staticmethod
    def _version(version: int, content: str) -> dict[str, Any]:
        return {
            "version": version,
            "content": content,
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
        }

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        prefix = f"/api/projects/{PROJECT}"
        assert path.startswith(prefix)
        suffix = path[len(prefix) :]
        if method == "GET" and suffix == "/skills":
            return [{**self.skill, "latest_version": max(row["version"] for row in self.versions)}]
        if method == "GET" and suffix == "/profiles":
            return [{"id": str(PROFILE)}]
        if method == "GET" and suffix == f"/profiles/{PROFILE}/skills":
            version = next(row for row in self.versions if row["version"] == self.pin["version"])
            return [{**self.pin, **version}]
        if method == "GET" and suffix == f"/skills/{SKILL}/versions":
            return list(reversed(self.versions))
        if method == "GET" and suffix.startswith(f"/skills/{SKILL}/versions/"):
            number = int(suffix.rsplit("/", 1)[1])
            return next(row for row in self.versions if row["version"] == number)
        assert payload is not None
        self.writes += 1
        if method == "POST" and suffix == f"/skills/{SKILL}/versions":
            row = self._version(max(row["version"] for row in self.versions) + 1, payload["content"])
            self.versions.append(row)
            return row
        if method == "PUT" and suffix == f"/profiles/{PROFILE}/skills/{SKILL}":
            assert payload["expected_binding_id"] == self.pin["id"]
            self.pin["version"] = payload["version"]
            self.pin["id"] = str(UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"))
            return {**self.pin, "version": payload["version"]}
        if method == "PATCH" and suffix == f"/skills/{SKILL}":
            assert (payload["expected_name"], payload["expected_description"]) == (
                self.skill["name"],
                self.skill["description"],
            )
            self.skill.update(name=payload["name"], description=payload["description"])
            return self.skill
        raise AssertionError((method, path))


def test_rename_preview_apply_and_repeat_preserve_pin_and_latest(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        rename_skill_library,
        "load_pack",
        lambda: [PackSkill("review", "Review OneFoundry work.", "", "")],
    )
    api = FakeAPI()
    preview = rename_skill_library.rename_project(api, PROJECT, apply=False)
    assert (preview.skills, preview.metadata, preview.versions, preview.pins) == (1, 1, 2, 1)
    assert api.writes == 0
    applied = rename_skill_library.rename_project(api, PROJECT, apply=True)
    assert (applied.skills, applied.metadata, applied.versions, applied.pins) == (1, 1, 2, 1)
    assert api.skill["name"] == "review"
    assert api.versions[-1]["content"] == "# review\nPreserve this latest variant."
    assert next(row for row in api.versions if row["version"] == api.pin["version"])["content"] == (
        "# first\nPreserve this pinned variant."
    )
    assert rename_skill_library.rename_project(api, PROJECT, apply=True).versions == 0
