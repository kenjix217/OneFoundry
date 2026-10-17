"""Preview or install reviewed OneFoundry procedures through the existing localhost API."""

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID

from onefoundry.skill_schemas import SkillCreate, SkillVersionCreate

PACK_ROOT = Path(__file__).resolve().parent.parent / "skill_packs" / "adapted"


@dataclass(frozen=True)
class PackSkill:
    name: str
    description: str
    content: str
    digest: str


class SkillAPI(Protocol):
    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any: ...


def load_pack(root: Path = PACK_ROOT) -> list[PackSkill]:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != 1
        or not isinstance(manifest.get("skills"), list)
    ):
        raise ValueError("invalid skill pack manifest")
    loaded: list[PackSkill] = []
    names: set[str] = set()
    for item in manifest["skills"]:
        if not isinstance(item, dict) or not isinstance(item.get("file"), str):
            raise ValueError("invalid skill pack entry")
        filename = item["file"]
        if Path(filename).name != filename or not filename.endswith(".md"):
            raise ValueError("skill pack file must be a direct Markdown child")
        if (root / filename).is_symlink():
            raise ValueError("skill pack file cannot be a symlink")
        metadata = SkillCreate.model_validate(
            {"name": item.get("name"), "description": item.get("description")}
        )
        if metadata.name in names or re.search(
            r"\b(?:OneFoundry|AgentPod|Paperclip|Codex)\b", metadata.name, re.I
        ):
            raise ValueError("skill pack name is duplicated or vendor branded")
        content = SkillVersionCreate.model_validate(
            {"content": (root / filename).read_text(encoding="utf-8")}
        ).content
        names.add(metadata.name)
        loaded.append(
            PackSkill(
                metadata.name, metadata.description, content, hashlib.sha256(content.encode()).hexdigest()
            )
        )
    return loaded


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, request: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


class LocalSkillAPI:
    def __init__(self, base_url: str) -> None:
        parts = urlsplit(base_url)
        if parts.scheme != "http" or parts.hostname not in {"localhost", "127.0.0.1"} or parts.port is None:
            raise ValueError("base URL must be an explicit localhost HTTP port")
        if parts.username or parts.password or parts.path not in {"", "/"} or parts.query or parts.fragment:
            raise ValueError("base URL must not contain credentials, a path, query, or fragment")
        self.base_url = f"http://{parts.hostname}:{parts.port}"
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json", "Origin": self.base_url}
        if body is not None:
            headers["Content-Type"] = "application/json"
        # The constructor admits only loopback HTTP and project paths are assembled from a UUID.
        request = Request(self.base_url + path, data=body, headers=headers, method=method)  # noqa: S310
        try:
            with self.opener.open(request, timeout=10) as response:
                data = response.read(1_000_001)
                if len(data) > 1_000_000:
                    raise ValueError("API response exceeds limit")
                return json.loads(data)
        except (HTTPError, URLError) as exc:
            raise RuntimeError(
                f"{method} {path} failed; inspect the Skill Library before retrying: {exc}"
            ) from exc


def import_pack(
    api: SkillAPI, project_id: UUID, skills: list[PackSkill], *, allow_update: bool = False
) -> list[str]:
    base = f"/api/projects/{quote(str(project_id))}/skills"
    existing = api.request("GET", base)
    if not isinstance(existing, list):
        raise ValueError("unexpected skill list response")
    outcomes: list[str] = []
    for skill in skills:
        matches = [item for item in existing if isinstance(item, dict) and item.get("name") == skill.name]
        legacy = [
            item
            for item in existing
            if isinstance(item, dict)
            and item.get("name") in {f"AgentPod {skill.name}", f"OneFoundry {skill.name}"}
        ]
        if legacy:
            raise ValueError(f"existing branded skill requires rename_skill_library first: {skill.name}")
        if len(matches) > 1 or (matches and matches[0].get("description") != skill.description):
            raise ValueError(f"name collision or changed provenance: {skill.name}")
        if matches:
            skill_id = str(UUID(str(matches[0]["id"])))
        else:
            created = api.request("POST", base, {"name": skill.name, "description": skill.description})
            if not isinstance(created, dict) or created.get("name") != skill.name:
                raise ValueError(f"uncertain skill creation: {skill.name}")
            skill_id = str(UUID(str(created["id"])))
            readback = api.request("GET", base)
            if not isinstance(readback, list) or not any(
                isinstance(item, dict) and str(item.get("id")) == skill_id and item.get("name") == skill.name
                for item in readback
            ):
                raise ValueError(f"skill creation readback failed: {skill.name}")
            existing = readback
        versions_path = f"{base}/{quote(skill_id)}/versions"
        latest = matches[0].get("latest_version") if matches else None
        if latest is not None:
            if not isinstance(latest, int) or isinstance(latest, bool) or latest < 1:
                raise ValueError(f"invalid latest version: {skill.name}")
            current = api.request("GET", f"{versions_path}/{latest}")
            if not isinstance(current, dict) or current.get("version") != latest:
                raise ValueError(f"latest version readback failed: {skill.name}")
            if current.get("sha256") == skill.digest and current.get("content") == skill.content:
                outcomes.append(f"unchanged: {skill.name}")
                continue
            if not allow_update:
                raise ValueError(
                    f"latest version differs for {skill.name}; review it before using --apply --update"
                )
        created_version = api.request("POST", versions_path, {"content": skill.content})
        if not isinstance(created_version, dict) or created_version.get("sha256") != skill.digest:
            raise ValueError(f"uncertain version creation: {skill.name}")
        version = created_version.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValueError(f"invalid version response: {skill.name}")
        readback = api.request("GET", f"{versions_path}/{version}")
        if (
            not isinstance(readback, dict)
            or readback.get("sha256") != skill.digest
            or readback.get("content") != skill.content
        ):
            raise ValueError(f"version readback failed: {skill.name}")
        outcomes.append(f"saved v{version}: {skill.name}")
    return outcomes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", type=UUID, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--apply", action="store_true", help="Write through the local OneFoundry API")
    parser.add_argument(
        "--update", action="store_true", help="Append when an existing latest version differs"
    )
    args = parser.parse_args()
    skills = load_pack()
    api = LocalSkillAPI(args.base_url)
    if not args.apply:
        for skill in skills:
            print(f"preview: {skill.name} ({len(skill.content.encode('utf-8'))} bytes, {skill.digest})")
        return
    for outcome in import_pack(api, args.project_id, skills, allow_update=args.update):
        print(outcome)


if __name__ == "__main__":
    main()
