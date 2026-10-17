"""Bounded read-only discovery against fixed official model-list endpoints."""

import json
import re
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from onefoundry.provider_registry import provider_spec
from onefoundry.settings import Settings

MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")


class DiscoveryError(Exception):
    """Provider discovery failed; message contains no credential or response body."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, request: Request, fp: object, code: int, msg: str, headers: object, newurl: str
    ) -> None:
        return None


@dataclass(frozen=True)
class DiscoveredModel:
    model_id: str
    display_name: str
    category: str
    reasoning_efforts: tuple[str, ...] = ()


def _category(provider: str, model_id: str, raw: dict[str, object]) -> str:
    name = model_id.lower()
    if provider == "gemini":
        methods = raw.get("supportedGenerationMethods")
        if isinstance(methods, list) and "embedContent" in methods and "generateContent" not in methods:
            return "embedding"
    if "embed" in name:
        return "embedding"
    if "realtime" in name or "live" in name:
        return "realtime"
    if any(part in name for part in ("audio", "speech", "transcribe", "tts", "whisper")):
        return "audio"
    if any(part in name for part in ("image", "dall-e", "imagen")):
        return "image"
    spec = provider_spec(provider)
    if (
        (spec is not None and spec.family == "openai_compatible")
        or provider in {"anthropic", "grok", "ollama"}
        or name.startswith(("gpt-", "o1", "o3", "o4", "gemini-", "models/gemini-"))
    ):
        return "text"
    return "other"


def _headers(provider: str, api_key: str) -> dict[str, str]:
    if provider in {"openai", "grok"}:
        return {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    if provider == "anthropic":
        return {"x-api-key": api_key, "anthropic-version": "2023-06-01", "Accept": "application/json"}
    if provider == "gemini":
        return {"x-goog-api-key": api_key, "Accept": "application/json"}
    return {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}


def discover_models(
    provider: str, api_key: str, settings: Settings, endpoint: str | None = None
) -> list[DiscoveredModel]:
    endpoints = {
        "openai": settings.model_openai_list_url,
        "anthropic": settings.model_anthropic_list_url,
        "gemini": settings.model_gemini_list_url,
        "grok": settings.model_grok_list_url,
        "ollama": endpoint,
    }
    spec = provider_spec(provider)
    if (
        provider not in endpoints
        and spec is not None
        and spec.connectable
        and spec.family == "openai_compatible"
    ):
        assert spec.base_url is not None
        endpoints[provider] = spec.base_url.rstrip("/") + "/models"
    if provider not in endpoints or endpoints[provider] is None:
        raise ValueError("unsupported model provider")
    found: dict[str, DiscoveredModel] = {}
    cursor: str | None = None
    seen_cursors: set[str] = set()
    for _page in range(settings.model_discovery_max_pages):
        params: dict[str, str] = {}
        if provider == "anthropic":
            params["limit"] = "100"
            if cursor:
                params["after_id"] = cursor
        elif provider == "gemini":
            params["pageSize"] = "100"
            if cursor:
                params["pageToken"] = cursor
        base = endpoints[provider]
        assert base is not None
        url = (
            base + ("/api/tags" if provider == "ollama" else "") + ("?" + urlencode(params) if params else "")
        )
        request = Request(  # noqa: S310 - fixed HTTPS or validated local HTTP origin
            url, headers={} if provider == "ollama" else _headers(provider, api_key), method="GET"
        )
        try:
            with build_opener(ProxyHandler({}), _NoRedirect).open(
                request, timeout=settings.model_discovery_timeout_seconds
            ) as response:
                if response.status != 200:
                    raise DiscoveryError("provider model list failed")
                payload = response.read(settings.model_discovery_max_response_bytes + 1)
        except (HTTPError, URLError, TimeoutError, OSError):
            raise DiscoveryError("provider model list unavailable") from None
        if len(payload) > settings.model_discovery_max_response_bytes:
            raise DiscoveryError("provider model list exceeded response limit")
        try:
            body = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            raise DiscoveryError("provider returned invalid model JSON") from None
        if not isinstance(body, dict):
            raise DiscoveryError("provider returned invalid model list")
        key = "models" if provider in {"gemini", "ollama"} else "data"
        rows = body.get(key)
        if not isinstance(rows, list):
            raise DiscoveryError("provider returned invalid model list")
        for raw in rows:
            if not isinstance(raw, dict):
                raise DiscoveryError("provider returned invalid model record")
            value = raw.get("name" if provider in {"gemini", "ollama"} else "id")
            if not isinstance(value, str) or not MODEL_ID.fullmatch(value):
                raise DiscoveryError("provider returned invalid model identifier")
            display = raw.get("displayName" if provider == "gemini" else "display_name")
            display_name = display.strip() if isinstance(display, str) and display.strip() else value
            found[value] = DiscoveredModel(value, display_name[:200], _category(provider, value, raw))
            if len(found) > settings.model_discovery_max_models:
                raise DiscoveryError("provider model list exceeded item limit")
        next_cursor: object = None
        if provider == "anthropic" and body.get("has_more") is True:
            next_cursor = body.get("last_id")
            if not next_cursor:
                raise DiscoveryError("provider returned invalid pagination")
        elif provider == "gemini":
            next_cursor = body.get("nextPageToken")
        if provider not in {"anthropic", "gemini"} or not next_cursor:
            return sorted(found.values(), key=lambda item: (item.category, item.display_name.lower()))
        if not isinstance(next_cursor, str) or next_cursor in seen_cursors or len(next_cursor) > 1000:
            raise DiscoveryError("provider returned invalid pagination")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    raise DiscoveryError("provider model list exceeded page limit")
