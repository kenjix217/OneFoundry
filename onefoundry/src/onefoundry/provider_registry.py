"""OneFoundry connector directory, adapted from DistrictAI's provider catalog.

The directory is metadata. A provider is connectable only when OneFoundry owns
its discovery and text transport; the remaining entries stay visible as such.
"""

import json
from dataclasses import dataclass
from importlib.resources import files
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    name: str
    kind: str
    family: str | None
    base_url: str | None
    connectable: bool
    text_runtime_available: bool
    catalog_supported: bool


def _load() -> dict[str, ProviderSpec]:
    raw = json.loads(files("onefoundry").joinpath("provider_catalog.json").read_text())
    result: dict[str, ProviderSpec] = {}
    for item in raw["providers"]:
        provider_id = item["id"]
        if provider_id in result:
            raise ValueError("duplicate provider catalog entry")
        family = item.get("family")
        base_url = item.get("base_url")
        parsed = urlsplit(base_url) if base_url else None
        compatible = bool(
            item["kind"] == "api_key"
            and family == "openai_compatible"
            and parsed is not None
            and parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and base_url is not None
            and "${" not in base_url
        )
        existing = provider_id in {
            "openai",
            "anthropic",
            "google",
            "grok",
            "ollama",
            "openai_codex_runtime",
            "xai",
        }
        # DistrictAI uses `google` and `xai` IDs. Existing OneFoundry accounts
        # use `gemini` and `grok`; preserve those IDs across this migration.
        if provider_id == "google":
            continue
        result[provider_id] = ProviderSpec(
            id=provider_id,
            name=item["name"],
            kind=item["kind"],
            family=family,
            base_url=base_url,
            connectable=compatible or existing,
            text_runtime_available=compatible
            or provider_id in {"openai", "anthropic", "grok", "ollama", "openai_codex_runtime", "xai"},
            catalog_supported=compatible or existing,
        )
    result["gemini"] = ProviderSpec("gemini", "Google Gemini", "api_key", "google", None, True, True, True)
    result["grok"] = ProviderSpec("grok", "xAI Grok API", "api_key", "xai", None, True, True, True)
    return result


PROVIDER_REGISTRY = _load()


def provider_spec(provider_id: str) -> ProviderSpec | None:
    return PROVIDER_REGISTRY.get(provider_id)


def provider_directory() -> list[ProviderSpec]:
    return sorted(PROVIDER_REGISTRY.values(), key=lambda item: item.name.casefold())
