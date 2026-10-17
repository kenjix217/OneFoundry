"""Rename the current project skill library while retaining immutable history and IDs.

Preview is read-only. Apply each project separately after the OneFoundry API is live.
A failed or unknown write must be read back before rerunning this command.
"""

import argparse
import hashlib
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID

from scripts.import_skill_pack import LocalSkillAPI, SkillAPI, load_pack


@dataclass
class RenameCounts:
    skills: int = 0
    metadata: int = 0
    versions: int = 0
    pins: int = 0


def _rows(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise ValueError(f"invalid {label} readback")
    return value


def _exact_version(api: SkillAPI, base: str, skill_id: str, version: int) -> dict[str, Any]:
    row = api.request("GET", f"{base}/skills/{skill_id}/versions/{version}")
    if not isinstance(row, dict) or row.get("version") != version or not isinstance(row.get("content"), str):
        raise ValueError("skill version readback mismatch")
    content = row["content"]
    if hashlib.sha256(content.encode()).hexdigest() != row.get("sha256"):
        raise ValueError("skill version digest mismatch")
    return row


def _append_version(api: SkillAPI, base: str, skill_id: str, content: str) -> int:
    created = api.request("POST", f"{base}/skills/{skill_id}/versions", {"content": content})
    if not isinstance(created, dict) or not isinstance(created.get("version"), int):
        raise ValueError("uncertain skill version creation")
    version = cast(int, created["version"])
    if _exact_version(api, base, skill_id, version)["content"] != content:
        raise ValueError("skill version creation readback mismatch")
    return version


def _desired_content(content: str) -> str:
    updated = content.replace("AgentPod", "OneFoundry")
    if updated.startswith("# OneFoundry "):
        updated = "# " + updated.removeprefix("# OneFoundry ")
    return updated


def rename_project(api: SkillAPI, project_id: UUID, *, apply: bool) -> RenameCounts:
    base = f"/api/projects/{project_id}"
    skills = _rows(api.request("GET", f"{base}/skills"), "skill list")
    profiles = _rows(api.request("GET", f"{base}/profiles"), "profile list")
    bindings = []
    for profile in profiles:
        profile_id = str(UUID(str(profile["id"])))
        bindings.extend(_rows(api.request("GET", f"{base}/profiles/{profile_id}/skills"), "skill pins"))
    by_name = {str(row["name"]): row for row in skills}
    if len(by_name) != len(skills):
        raise ValueError("duplicate skill names in readback")
    counts = RenameCounts()
    for pack in load_pack():
        names = (f"AgentPod {pack.name}", f"OneFoundry {pack.name}", pack.name)
        matches = [by_name[name] for name in names if name in by_name]
        if len(matches) != 1:
            raise ValueError(f"pack skill missing: {pack.name}")
        skill = matches[0]
        descriptions = (
            f"[AgentPod pack] {pack.description.replace('OneFoundry', 'AgentPod')}",
            f"[OneFoundry pack] {pack.description}",
            pack.description,
        )
        if skill.get("description") not in descriptions:
            raise ValueError(f"pack description changed: {pack.name}")
        skill_id = str(UUID(str(skill["id"])))
        counts.skills += 1
        versions = _rows(api.request("GET", f"{base}/skills/{skill_id}/versions"), "skill versions")
        if not versions:
            raise ValueError(f"pack skill has no version: {pack.name}")
        current_latest = _exact_version(api, base, skill_id, max(int(row["version"]) for row in versions))
        desired_latest = _desired_content(current_latest["content"])
        pinned = [row for row in bindings if str(row.get("skill_id")) == skill_id]
        for pin in pinned:
            content = str(pin["content"])
            desired = _desired_content(content)
            if desired == content:
                continue
            counts.pins += 1
            matching = next((row for row in versions if row.get("content") == desired), None)
            if matching is None:
                counts.versions += 1
                if apply:
                    version = _append_version(api, base, skill_id, desired)
                    matching = _exact_version(api, base, skill_id, version)
                else:
                    matching = {
                        "version": max(int(row["version"]) for row in versions) + 1,
                        "content": desired,
                    }
                versions.insert(0, matching)
            if apply:
                assert matching is not None
                path = f"{base}/profiles/{UUID(str(pin['profile_id']))}/skills/{skill_id}"
                result = api.request(
                    "PUT", path, {"version": matching["version"], "expected_binding_id": pin["id"]}
                )
                if not isinstance(result, dict) or result.get("version") != matching["version"]:
                    raise ValueError("skill pin write readback mismatch")
                readback = _rows(
                    api.request("GET", f"{base}/profiles/{UUID(str(pin['profile_id']))}/skills"),
                    "skill pin readback",
                )
                if not any(
                    row.get("skill_id") == skill_id and row.get("content") == desired for row in readback
                ):
                    raise ValueError("skill pin was not confirmed")
        latest_after_pins = max(int(row["version"]) for row in versions)
        latest_content = next(row["content"] for row in versions if row["version"] == latest_after_pins)
        if latest_content != desired_latest:
            counts.versions += 1
            if apply:
                version = _append_version(api, base, skill_id, desired_latest)
                versions.insert(0, _exact_version(api, base, skill_id, version))
        if skill["name"] != pack.name or skill["description"] != pack.description:
            counts.metadata += 1
            if apply:
                result = api.request(
                    "PATCH",
                    f"{base}/skills/{skill_id}",
                    {
                        "expected_name": skill["name"],
                        "expected_description": skill["description"],
                        "name": pack.name,
                        "description": pack.description,
                    },
                )
                if not isinstance(result, dict) or (
                    result.get("id"),
                    result.get("name"),
                    result.get("description"),
                ) != (skill_id, pack.name, pack.description):
                    raise ValueError("skill metadata rename readback mismatch")
    if apply:
        final = _rows(api.request("GET", f"{base}/skills"), "final skill list")
        if not all(any(row.get("name") == pack.name for row in final) for pack in load_pack()):
            raise ValueError("skill rename inventory incomplete")
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", type=UUID, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    counts = rename_project(LocalSkillAPI(args.base_url), args.project_id, apply=args.apply)
    mode = "applied" if args.apply else "preview"
    print(
        f"{mode}: {counts.skills} skills, {counts.metadata} metadata changes, "
        f"{counts.versions} new versions, {counts.pins} pin moves"
    )


if __name__ == "__main__":
    main()
