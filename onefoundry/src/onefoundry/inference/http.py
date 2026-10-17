"""Bounded HTTP transport with no environment proxy or redirect following."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .contracts import TransportError

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_ACTIVE_SENDS = threading.BoundedSemaphore(16)


@dataclass(frozen=True, slots=True)
class HTTPResult:
    status: int
    body: bytes


class HTTPTransport(Protocol):
    def __call__(self, outbound: Request, *, timeout: float) -> HTTPResult: ...


class _ReadableResponse(Protocol):
    status: int

    def read(self, size: int) -> bytes: ...
    def readline(self, size: int) -> bytes: ...
    def close(self) -> None: ...
    def __enter__(self) -> _ReadableResponse: ...
    def __exit__(self, exc_type: object, exc: object, tb: object) -> object: ...


class _RejectRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, request: Request, fp: object, code: int, msg: str, headers: object, newurl: str
    ) -> None:
        return None


class _SendState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.cancelled = False
        self.response: _ReadableResponse | None = None
        self.result: HTTPResult | None = None
        self.error: TransportError | None = None

    def set_response(self, response: _ReadableResponse) -> None:
        with self.lock:
            self.response = response
            cancelled = self.cancelled
        if cancelled:
            response.close()

    def cancel(self) -> None:
        with self.lock:
            self.cancelled = True
            response = self.response
        if response is not None:
            threading.Thread(target=response.close, name="onefoundry-inference-abort", daemon=True).start()


def _send_once(
    outbound: Request,
    timeout: float,
    state: _SendState,
    on_event: Callable[[bytes], None] | None = None,
) -> HTTPResult:
    opener = build_opener(ProxyHandler({}), _RejectRedirect())
    try:
        try:
            response = opener.open(outbound, timeout=timeout)  # noqa: S310 - endpoint validated by adapter
        except HTTPError as error:
            response = error
        state.set_response(response)
        with response:
            if on_event is None or response.status != 200:
                body = response.read(MAX_RESPONSE_BYTES + 1)
            else:
                chunks: list[bytes] = []
                event_lines: list[bytes] = []
                size = 0
                while line := response.readline(MAX_RESPONSE_BYTES + 1):
                    size += len(line)
                    if size > MAX_RESPONSE_BYTES:
                        raise TransportError("provider response exceeded the size limit")
                    chunks.append(line)
                    if line.strip():
                        event_lines.append(line)
                    elif event_lines:
                        on_event(b"".join(event_lines))
                        event_lines.clear()
                if event_lines:
                    on_event(b"".join(event_lines))
                body = b"".join(chunks)
            if len(body) > MAX_RESPONSE_BYTES:
                raise TransportError("provider response exceeded the size limit")
            return HTTPResult(response.status, body)
    except (URLError, TimeoutError, OSError):
        raise TransportError("provider transport failed") from None


def _send(
    outbound: Request, *, timeout: float, on_event: Callable[[bytes], None] | None = None
) -> HTTPResult:
    """Return by the absolute deadline; an expired provider attempt is never retried.

    A daemon worker lets the caller return if DNS or a body read ignores socket
    timeouts. The active response is closed at the deadline; a semaphore caps
    workers still unwinding after cancellation.
    """
    if timeout <= 0 or not _ACTIVE_SENDS.acquire(blocking=False):
        raise TransportError("provider transport is unavailable")
    state = _SendState()

    def run() -> None:
        try:
            state.result = _send_once(outbound, timeout, state, on_event)
        except TransportError as error:
            state.error = error
        except Exception:
            state.error = TransportError("provider transport failed")
        finally:
            state.done.set()
            _ACTIVE_SENDS.release()

    worker = threading.Thread(target=run, name="onefoundry-inference-http", daemon=True)
    try:
        worker.start()
    except RuntimeError:
        _ACTIVE_SENDS.release()
        raise TransportError("provider transport is unavailable") from None
    if not state.done.wait(timeout):
        state.cancel()
        raise TransportError("provider transport deadline exceeded")
    if state.error is not None:
        raise state.error
    if state.result is None:
        raise TransportError("provider transport failed")
    return state.result


def send_http(outbound: Request, *, timeout: float) -> HTTPResult:
    return _send(outbound, timeout=timeout)


def send_http_stream(outbound: Request, *, timeout: float, on_event: Callable[[bytes], None]) -> HTTPResult:
    """Read an SSE response under the same absolute deadline and size cap."""
    return _send(outbound, timeout=timeout, on_event=on_event)
