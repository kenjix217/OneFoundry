"""A scripted HTTP transport for Microsoft and Google endpoints in connector tests."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import parse_qs
from urllib.request import Request

from onefoundry.inference.http import HTTPResult

Responder = Callable[[Request], tuple[int, object]]


@dataclass
class FakeHTTP:
    routes: list[tuple[str, str, Responder]] = field(default_factory=list)
    calls: list[tuple[str, str, dict[str, list[str]], str]] = field(default_factory=list)

    def on(self, method: str, prefix: str, responder: Responder | tuple[int, object]) -> None:
        handler = responder if callable(responder) else (lambda _request, value=responder: value)
        self.routes.insert(0, (method, prefix, handler))

    def __call__(self, outbound: Request, *, timeout: float) -> HTTPResult:
        method, url = outbound.get_method(), outbound.full_url
        form = parse_qs(outbound.data.decode()) if isinstance(outbound.data, bytes) else {}
        self.calls.append((method, url, form, str(outbound.get_header("Authorization") or "")))
        for route_method, prefix, handler in self.routes:
            if route_method == method and url.startswith(prefix):
                status, body = handler(outbound)
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                return HTTPResult(status, raw)
        return HTTPResult(404, b'{"error":"not found"}')
