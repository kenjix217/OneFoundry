"""Request boundary for the local unauthenticated service."""

from collections.abc import Awaitable, Callable
from uuid import UUID

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp


def _originless_webhook_receive(request: Request) -> bool:
    if request.method != "POST" or request.headers.get("origin") is not None:
        return False
    prefix = "/api/extensions/webhooks/"
    path = request.url.path
    if not path.startswith(prefix):
        return False
    identity = path[len(prefix) :]
    try:
        return str(UUID(identity)) == identity.lower()
    except ValueError:
        return False


class WriteOriginMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, public_port: int) -> None:
        super().__init__(app)
        self.public_port = public_port

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method not in {"GET", "HEAD", "OPTIONS", "TRACE"}:
            origin = request.headers.get("origin")
            host = request.headers.get("host", "")
            expected = f"http://{host}"
            if host not in {
                f"localhost:{self.public_port}",
                f"127.0.0.1:{self.public_port}",
            } or (origin != expected and not _originless_webhook_receive(request)):
                return Response(status_code=403, content="Forbidden origin")
        return await call_next(request)
