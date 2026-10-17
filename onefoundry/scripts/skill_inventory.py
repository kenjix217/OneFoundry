"""Inventory local skill sources without copying instructions or granting capabilities."""

import argparse
import json
from pathlib import Path
from typing import Any

SKIP_PARTS = {".git", ".venv", "node_modules"}
PACK_MANIFEST = Path(__file__).resolve().parent.parent / "skill_packs" / "adapted" / "manifest.json"
WORKFLOW_REFS = (
    "skills/paperclip/references/workflows.md",
    "skills/paperclip/references/routines.md",
    "skills/paperclip/references/company-skills.md",
    "skills/paperclip/references/artifacts.md",
)


def _adapted_sources(manifest_path: Path) -> dict[str, str]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {source_id: str(item["name"]) for item in manifest["skills"] for source_id in item["derived_from"]}


def _entry(root: Path, path: Path, source: str, kind: str, adapted_sources: dict[str, str]) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    if path.is_symlink() or not resolved.is_relative_to(root.resolve(strict=True)):
        raise ValueError(f"source escapes root: {path}")
    data = path.read_bytes()
    relative = path.relative_to(root).as_posix()
    source_id = f"{source}:{relative}"
    return {
        "source_id": source_id,
        "kind": kind,
        "bytes": len(data),
        "status": "adapted_concept" if source_id in adapted_sources else "inventory_only",
        "adapted_as": adapted_sources.get(source_id),
    }


def build_inventory(
    paperclip_root: Path, codex_root: Path, adapted_sources: dict[str, str] | None = None
) -> dict[str, Any]:
    """List all local SKILL.md files plus Paperclip workflow templates and refs."""
    if adapted_sources is None:
        adapted_sources = _adapted_sources(PACK_MANIFEST)
    if not (codex_root / "skills").is_dir() or not (codex_root / "plugins" / "cache").is_dir():
        raise ValueError("Codex root needs skills and plugins/cache directories")
    entries: list[dict[str, Any]] = []
    for path in sorted(paperclip_root.rglob("SKILL.md")):
        if not SKIP_PARTS.intersection(path.relative_to(paperclip_root).parts):
            entries.append(_entry(paperclip_root, path, "paperclip", "skill", adapted_sources))
    for name in ("TEAM.md", "TASK.md"):
        for path in sorted((paperclip_root / "packages" / "teams-catalog").rglob(name)):
            entries.append(_entry(paperclip_root, path, "paperclip", "workflow_template", adapted_sources))
    for relative in WORKFLOW_REFS:
        path = paperclip_root / relative
        if path.is_file():
            entries.append(_entry(paperclip_root, path, "paperclip", "workflow_reference", adapted_sources))
    for directory in (codex_root / "skills", codex_root / "plugins" / "cache"):
        for path in sorted(directory.rglob("SKILL.md")):
            if not SKIP_PARTS.intersection(path.relative_to(codex_root).parts):
                entries.append(_entry(codex_root, path, "codex", "skill", adapted_sources))
    external_ref = (
        paperclip_root / "packages/skills-catalog/catalog/optional/research/last30days/catalog-ref.json"
    )
    if external_ref.is_file():
        entries.append(
            _entry(paperclip_root, external_ref, "paperclip", "external_skill_reference", adapted_sources)
        )
    entries.sort(key=lambda item: str(item["source_id"]))
    found = {str(item["source_id"]) for item in entries}
    if missing := set(adapted_sources) - found:
        raise ValueError(f"adapted sources are absent from inventory: {sorted(missing)}")
    counts = {
        "paperclip_skills": sum(
            x["kind"] == "skill" and str(x["source_id"]).startswith("paperclip:") for x in entries
        ),
        "codex_skills": sum(
            x["kind"] == "skill" and str(x["source_id"]).startswith("codex:") for x in entries
        ),
        "workflow_sources": sum(x["kind"] in {"workflow_template", "workflow_reference"} for x in entries),
        "adapted_concepts": sum(x["status"] == "adapted_concept" for x in entries),
    }
    return {"format": 1, "counts": counts, "sources": entries}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paperclip-root", type=Path, required=True)
    parser.add_argument("--codex-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    inventory = build_inventory(args.paperclip_root, args.codex_root)
    args.output.write_text(json.dumps(inventory, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(inventory["counts"], sort_keys=True))


if __name__ == "__main__":
    main()
