"""Suppress private Zoho transport diagnostics without silencing other requests."""

import logging
from contextvars import ContextVar

private_call: ContextVar[bool] = ContextVar("zoho_private_mcp_call", default=False)


class _PrivateDiagnostics(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not private_call.get()


for _logger in (
    "mcp.client.streamable_http",
    "mcp.shared.session",
    "httpx",
    "httpx2",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.http2",
    "httpcore.proxy",
    "httpcore.socks_proxy",
):
    logging.getLogger(_logger).addFilter(_PrivateDiagnostics())
