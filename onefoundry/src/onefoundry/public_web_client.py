"""Bounded public HTTPS reads for the Researcher tool owner.

The client never uses ambient proxies, cookies, credentials, or redirects. It
resolves the target once, rejects non-public answers, and connects to the
validated address while TLS still verifies the requested hostname.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import re
import socket
import ssl
from collections.abc import Callable
from datetime import UTC, datetime
from html.parser import HTMLParser
from queue import Empty, Queue
from threading import BoundedSemaphore, Event, Thread
from time import monotonic
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

from onefoundry.workflow_schemas import SECRET_PATTERN, local_text


class PublicWebError(Exception):
    """A public read was rejected or unavailable; details never echo input."""


class PublicWebUnavailable(PublicWebError):
    """The configured source or network dependency is unavailable."""

    def __init__(self, message: str, *, code: str = "web_dependency_unavailable") -> None:
        super().__init__(message)
        self.code = code


class PublicWebRejected(PublicWebError):
    """The destination, argument, or response violated public-read policy."""

    def __init__(self, message: str, *, code: str = "web_policy_rejected") -> None:
        super().__init__(message)
        self.code = code


class PublicWebTimedOut(PublicWebError):
    """The bounded public read ran out of time."""


Resolver = Callable[..., list[tuple[Any, ...]]]
ConnectionFactory = Callable[[str, str, float], http.client.HTTPConnection]
_READ_SLOTS = BoundedSemaphore(4)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, timeout: float) -> None:
        self._ssl_context = ssl.create_default_context()
        super().__init__(host, port=443, timeout=timeout, context=self._ssl_context)
        self._address = address
        self._deadline = monotonic() + timeout
        self._stream_socket: ssl.SSLSocket | None = None

    def _remaining(self) -> float:
        remaining = self._deadline - monotonic()
        if remaining <= 0:
            raise PublicWebTimedOut("public web request timed out")
        return remaining

    def connect(self) -> None:
        plain = socket.create_connection((self._address, 443), timeout=self._remaining())
        try:
            if ipaddress.ip_address(plain.getpeername()[0]) != ipaddress.ip_address(self._address):
                raise PublicWebRejected("public web peer address changed", code="web_target_peer_changed")
            plain.settimeout(self._remaining())
            self.sock = self._ssl_context.wrap_socket(plain, server_hostname=self.host)
            self.sock.settimeout(self._remaining())
            self._stream_socket = self.sock
        except Exception:
            plain.close()
            raise


class _PageText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.in_title = False
        self.title: list[str] = []
        self.body: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self.hidden += 1
        elif tag == "title" and not self.hidden:
            self.in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self.hidden:
            self.hidden -= 1
        elif tag == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.hidden:
            return
        if self.in_title:
            self.title.append(data)
        else:
            self.body.append(data)


def _clean(value: str, limit: int) -> str:
    return _clean_excerpt(value, limit)[0]


def _clean_excerpt(value: str, limit: int) -> tuple[str, bool]:
    if SECRET_PATTERN.search(value):
        raise PublicWebRejected("public web content was rejected", code="web_content_sensitive")
    normalized = re.sub(r"\s+", " ", value).strip()
    return normalized[:limit], len(normalized) > limit


def _target(url: str) -> tuple[str, str, str]:
    if not isinstance(url, str) or len(url.encode("utf-8")) > 2048 or any(ord(c) < 32 for c in url):
        raise PublicWebRejected("invalid public HTTPS URL", code="web_target_invalid")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise PublicWebRejected("invalid public HTTPS URL", code="web_target_invalid") from None
    if parsed.username is not None or parsed.password is not None:
        raise PublicWebRejected("invalid public HTTPS URL", code="web_target_credentials")
    if parsed.scheme != "https" or host is None or port not in (None, 443) or not parsed.netloc:
        raise PublicWebRejected("invalid public HTTPS URL", code="web_target_invalid")
    try:
        ascii_host = host.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError:
        raise PublicWebRejected("invalid public HTTPS URL", code="web_target_invalid") from None
    if not ascii_host or len(ascii_host) > 253 or any(c in ascii_host for c in "/%\\"):
        raise PublicWebRejected("invalid public HTTPS URL", code="web_target_invalid")
    # Reject literal local addresses before the resolver is invoked.
    try:
        literal = ipaddress.ip_address(ascii_host)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise PublicWebRejected("public HTTPS target is unavailable", code="web_target_not_public")
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    normalized = urlunsplit(("https", ascii_host, parsed.path or "/", parsed.query, ""))
    return ascii_host, path, normalized


class PublicWebClient:
    """Public-only transport with injectable DNS and connection for deterministic tests."""

    def __init__(
        self,
        *,
        resolver: Resolver = socket.getaddrinfo,
        connection_factory: ConnectionFactory = _PinnedHTTPSConnection,
        internal_connection_factory: Callable[[float], http.client.HTTPConnection] | None = None,
    ) -> None:
        self._resolver = resolver
        self._connection_factory = connection_factory
        self._internal_connection_factory = internal_connection_factory or (
            lambda timeout: http.client.HTTPConnection("searxng", 8080, timeout=timeout)
        )

    def _address(self, host: str) -> str:
        try:
            answers = self._resolver(host, 443, type=socket.SOCK_STREAM)
            addresses = [str(answer[4][0]) for answer in answers]
        except (OSError, ValueError, IndexError, TypeError):
            raise PublicWebUnavailable(
                "public HTTPS target is unavailable", code="web_dns_unavailable"
            ) from None
        if not addresses:
            raise PublicWebUnavailable("public HTTPS target is unavailable", code="web_dns_unavailable")
        try:
            if any(not ipaddress.ip_address(ip).is_global for ip in addresses):
                raise PublicWebRejected("public HTTPS target is unavailable", code="web_target_not_public")
        except ValueError:
            raise PublicWebUnavailable(
                "public HTTPS target is unavailable", code="web_dns_unavailable"
            ) from None
        return addresses[0]

    def _read(
        self,
        url: str,
        *,
        timeout_seconds: int,
        max_response_bytes: int,
        internal_search: bool = False,
    ) -> tuple[str, str, bytes]:
        if not 1 <= timeout_seconds <= 30 or not 1024 <= max_response_bytes <= 2_000_000:
            raise PublicWebRejected("public web limits are invalid", code="web_limits_invalid")
        deadline = monotonic() + timeout_seconds
        if not _READ_SLOTS.acquire(timeout=timeout_seconds):
            raise PublicWebTimedOut("public web request timed out")
        completed: Queue[tuple[bool, object]] = Queue(maxsize=1)
        cancelled = Event()
        connections: list[http.client.HTTPConnection] = []

        def read() -> None:
            try:
                completed.put(
                    (
                        True,
                        self._read_blocking(
                            url,
                            deadline=deadline,
                            max_response_bytes=max_response_bytes,
                            internal_search=internal_search,
                            cancelled=cancelled,
                            connections=connections,
                        ),
                    )
                )
            except Exception as error:
                completed.put((False, error))
            finally:
                _READ_SLOTS.release()

        try:
            Thread(target=read, daemon=True, name="onefoundry-public-web").start()
        except RuntimeError:
            _READ_SLOTS.release()
            raise PublicWebUnavailable(
                "public web worker is unavailable", code="web_worker_unavailable"
            ) from None
        try:
            success, value = completed.get(timeout=max(0, deadline - monotonic()))
        except Empty:
            cancelled.set()
            for connection in connections:
                try:
                    connection.close()
                except OSError:
                    pass
            raise PublicWebTimedOut("public web request timed out") from None
        if not success:
            if isinstance(value, PublicWebError):
                raise value
            raise PublicWebUnavailable("public web request failed", code="web_network_unavailable")
        if not isinstance(value, tuple):
            raise PublicWebUnavailable("public web request failed", code="web_network_unavailable")
        return value

    def _read_blocking(
        self,
        url: str,
        *,
        deadline: float,
        max_response_bytes: int,
        internal_search: bool,
        cancelled: Event,
        connections: list[http.client.HTTPConnection],
    ) -> tuple[str, str, bytes]:
        if internal_search:
            parsed = urlsplit(url)
            if parsed.scheme != "http" or parsed.netloc != "searxng:8080" or parsed.path != "/search":
                raise PublicWebRejected(
                    "public search endpoint is invalid", code="web_search_endpoint_invalid"
                )
            host, path, normalized = "searxng", "/search?" + parsed.query, url
            address = None
        else:
            host, path, normalized = _target(url)
            address = self._address(host)
        remaining = deadline - monotonic()
        if remaining <= 0 or cancelled.is_set():
            raise PublicWebTimedOut("public web request timed out")
        connection = (
            self._internal_connection_factory(remaining)
            if internal_search
            else self._connection_factory(host, str(address), remaining)
        )
        connections.append(connection)
        try:
            if cancelled.is_set():
                raise PublicWebTimedOut("public web request timed out")
            connection.request(
                "GET",
                path,
                headers={
                    "Host": "searxng:8080" if internal_search else host,
                    "Accept": "text/html,text/plain,application/json",
                    "Accept-Encoding": "identity",
                    "User-Agent": "OneFoundry/1",
                },
            )
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise PublicWebTimedOut("public web request timed out")
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            response = connection.getresponse()
            if monotonic() >= deadline:
                raise PublicWebTimedOut("public web request timed out")
            if response.status != 200:
                raise PublicWebUnavailable(
                    "public web request was unsuccessful", code="web_http_status_unavailable"
                )
            encoding = response.getheader("Content-Encoding", "identity")
            if encoding is None or encoding.lower() != "identity":
                raise PublicWebRejected(
                    "compressed public web content is unavailable", code="web_response_compressed"
                )
            declared = response.getheader("Content-Length")
            if declared is not None and (not declared.isdigit() or int(declared) > max_response_bytes):
                raise PublicWebRejected("public web content exceeded limit", code="web_response_too_large")
            content_type = (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type not in {"text/html", "text/plain", "application/json"}:
                raise PublicWebRejected(
                    "public web content type is unavailable", code="web_response_content_type"
                )
            chunks: list[bytes] = []
            total = 0
            while True:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise PublicWebTimedOut("public web request timed out")
                stream_socket = (
                    connection._stream_socket  # noqa: SLF001 - owned transport socket
                    if isinstance(connection, _PinnedHTTPSConnection)
                    else connection.sock
                )
                if stream_socket is not None:
                    stream_socket.settimeout(remaining)
                chunk = response.read1(min(8192, max_response_bytes + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_response_bytes:
                    raise PublicWebRejected(
                        "public web content exceeded limit", code="web_response_too_large"
                    )
                chunks.append(chunk)
            payload = b"".join(chunks)
            if monotonic() >= deadline:
                raise PublicWebTimedOut("public web request timed out")
            return normalized, content_type, payload
        except TimeoutError:
            raise PublicWebTimedOut("public web request timed out") from None
        except (OSError, http.client.HTTPException, ssl.SSLError):
            raise PublicWebUnavailable("public web request failed", code="web_network_unavailable") from None
        finally:
            connection.close()

    def search(
        self,
        query: str,
        endpoint: str,
        max_results: int,
        timeout_seconds: int,
        max_response_bytes: int,
    ) -> dict[str, Any]:
        if not endpoint:
            raise PublicWebUnavailable("public search is not configured", code="web_search_not_configured")
        if not 1 <= max_results <= 20:
            raise PublicWebRejected("public search limit is invalid", code="web_limits_invalid")
        try:
            safe_query = local_text(query)
        except ValueError:
            raise PublicWebRejected(
                "public search query is invalid", code="web_search_query_invalid"
            ) from None
        if len(safe_query.encode("utf-8")) > 240:
            raise PublicWebRejected("public search query is invalid", code="web_search_query_invalid")
        internal = endpoint == "http://searxng:8080/search"
        if not internal:
            host, path, _ = _target(endpoint)
            if path != "/search":
                raise PublicWebRejected(
                    "public search endpoint is invalid", code="web_search_endpoint_invalid"
                )
        else:
            host = "searxng:8080"
        parsed = urlsplit(endpoint)
        if parsed.query or parsed.fragment or (parsed.scheme == "http" and not internal):
            raise PublicWebRejected("public search endpoint is invalid", code="web_search_endpoint_invalid")
        request_url = urlunsplit(
            (parsed.scheme, host, "/search", urlencode({"q": safe_query, "format": "json"}), "")
        )
        _, content_type, payload = self._read(
            request_url,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
            internal_search=internal,
        )
        if content_type != "application/json":
            raise PublicWebRejected("public search response is invalid", code="web_search_response_invalid")
        try:
            document = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            raise PublicWebRejected(
                "public search response is invalid", code="web_search_response_invalid"
            ) from None
        if not isinstance(document, dict) or not isinstance(document.get("results"), list):
            raise PublicWebRejected("public search response is invalid", code="web_search_response_invalid")
        results: list[dict[str, str | None]] = []
        for item in document["results"]:
            if not isinstance(item, dict):
                continue
            candidate = item.get("url")
            if not isinstance(candidate, str):
                continue
            try:
                _, _, result_url = _target(candidate)
            except PublicWebRejected:
                continue
            title = item.get("title")
            snippet = item.get("content")
            published = item.get("publishedDate")
            published_at: str | None = None
            if isinstance(published, str):
                try:
                    parsed_date = datetime.fromisoformat(published.replace("Z", "+00:00"))
                    if parsed_date.tzinfo is not None:
                        published_at = parsed_date.astimezone(UTC).isoformat()
                except ValueError:
                    pass
            results.append(
                {
                    "url": result_url,
                    "title": _clean(title if isinstance(title, str) else "", 240),
                    "snippet": _clean(snippet if isinstance(snippet, str) else "", 500),
                    "published_at": published_at,
                }
            )
            if len(results) >= max_results:
                break
        return {"fetched_at": datetime.now(UTC).isoformat(), "results": results}

    def fetch(
        self,
        url: str,
        timeout_seconds: int,
        max_response_bytes: int,
        max_text_chars: int,
    ) -> dict[str, Any]:
        if not 1 <= max_text_chars <= 16000:
            raise PublicWebRejected("public page text limit is invalid", code="web_limits_invalid")
        source, content_type, payload = self._read(
            url, timeout_seconds=timeout_seconds, max_response_bytes=max_response_bytes
        )
        if content_type not in {"text/html", "text/plain"}:
            raise PublicWebRejected(
                "public page content type is unavailable", code="web_response_content_type"
            )
        decoded = payload.decode("utf-8", errors="replace")
        if content_type == "text/html":
            parser = _PageText()
            parser.feed(decoded)
            title = _clean(" ".join(parser.title), 240)
            page_text, text_truncated = _clean_excerpt(" ".join(parser.body), max_text_chars)
        else:
            title = ""
            page_text, text_truncated = _clean_excerpt(decoded, max_text_chars)
        return {
            "url": source,
            "fetched_at": datetime.now(UTC).isoformat(),
            "title": title,
            "text": page_text,
            "text_truncated": text_truncated,
            "published_at": None,
        }
