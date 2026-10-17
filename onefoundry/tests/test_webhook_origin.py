"""Originless intake is narrow; operator writes still require their local origin."""

import asyncio

from fastapi import Request, Response
from starlette.types import Receive, Scope, Send

from onefoundry.security import WriteOriginMiddleware


async def _unused_app(scope: Scope, receive: Receive, send: Send) -> None:
    raise AssertionError("dispatch should call the supplied handler")


def _status(path: str, *, origin: str | None = None, host: str = "localhost:8765") -> int:
    headers = [(b"host", host.encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    request = Request(
        {"type": "http", "method": "POST", "path": path, "headers": headers, "query_string": b""}
    )

    async def call_next(_request: Request) -> Response:
        return Response(status_code=200)

    middleware = WriteOriginMiddleware(_unused_app, public_port=8765)
    return asyncio.run(middleware.dispatch(request, call_next)).status_code


def test_only_exact_webhook_receive_allows_missing_origin() -> None:
    target = "/api/extensions/webhooks/12345678-1234-4123-8123-123456789abc"
    assert _status(target) == 200
    assert _status(target, origin="https://other.example") == 403
    assert _status(target + "/extra") == 403
    assert _status("/api/extensions/webhooks/not-a-uuid") == 403
    assert _status("/api/projects/123/connections") == 403
    assert _status("/api/projects/123/connections", origin="http://localhost:8765") == 200
    assert _status(target, host="outside.example:8765") == 403
