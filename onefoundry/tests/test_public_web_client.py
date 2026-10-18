"""Public web transport policy without external DNS or network traffic."""

import http.client
import json
import socket
from threading import Event
from typing import Any, cast

import pytest
from pydantic import ValidationError

from onefoundry.public_web_client import (
    PublicWebClient,
    PublicWebRejected,
    PublicWebTimedOut,
    PublicWebUnavailable,
    _PinnedHTTPSConnection,
)
from onefoundry.role_web_tools import FetchOutput


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        status: int = 200,
        content_type: str = "text/html",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.body = body
        self.status = status
        self.headers = {"Content-Type": content_type, **(headers or {})}

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name, default)

    def read(self, amount: int = -1) -> bytes:
        return self.body[:amount]

    def read1(self, amount: int = -1) -> bytes:
        chunk = self.body[:amount]
        self.body = self.body[len(chunk) :]
        return chunk


class FakeConnection(http.client.HTTPConnection):
    def __init__(self, response: FakeResponse) -> None:
        super().__init__("unused.invalid")
        self.response = response
        self.requested: list[tuple[str, str, dict[str, str]]] = []
        self.closed = False

    def request(  # type: ignore[override]
        self,
        method: str,
        url: str,
        body: Any = None,
        headers: dict[str, str] | None = None,
        *,
        encode_chunked: bool = False,
    ) -> None:
        self.requested.append((method, url, headers or {}))

    def getresponse(self) -> http.client.HTTPResponse:
        return cast(http.client.HTTPResponse, self.response)

    def close(self) -> None:
        self.closed = True


def _resolver(host: str, port: int, *, type: int) -> list[tuple[Any, ...]]:
    assert port == 443 and type == socket.SOCK_STREAM
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443))]


def _client(response: FakeResponse) -> tuple[PublicWebClient, FakeConnection]:
    connection = FakeConnection(response)
    return PublicWebClient(resolver=_resolver, connection_factory=lambda h, a, t: connection), connection


def test_fetch_bounded_html_with_no_redirect_or_ambient_headers() -> None:
    client, connection = _client(
        FakeResponse(b"<title>Example</title><script>ignore me</script><p>Hello <b>world</b></p>")
    )
    result = client.fetch("https://example.org/news#fragment", 5, 1024, 100)
    assert result["url"] == "https://example.org/news"
    assert result["title"] == "Example"
    assert result["text"] == "Hello world"
    assert result["text_truncated"] is False
    assert result["published_at"] is None
    assert result["fetched_at"].endswith("+00:00")
    assert connection.requested[0][0:2] == ("GET", "/news")
    assert set(connection.requested[0][2]) == {"Host", "Accept", "Accept-Encoding", "User-Agent"}
    assert connection.closed


def test_fetch_marks_bounded_text_excerpt_without_expanding_response_limit() -> None:
    client, connection = _client(FakeResponse(b"<p>" + b"x" * 150 + b"</p>"))
    result = client.fetch("https://example.org/page", 5, 1024, 100)
    assert result["text"] == "x" * 100
    assert result["text_truncated"] is True
    assert FetchOutput.model_validate(result).text_truncated is True
    assert (
        FetchOutput.model_validate({k: v for k, v in result.items() if k != "text_truncated"}).text_truncated
        is False
    )
    with pytest.raises(ValidationError):
        FetchOutput.model_validate({**result, "text_truncated": "yes"})
    assert connection.closed


def test_fetch_prefers_main_content_over_navigation_and_retains_secret_guard() -> None:
    html = (
        "<html><head><title>Pricing</title></head><body><nav>"
        + "Navigation links " * 100
        + "</nav><main><h1>Pricing</h1><p>Prices per 1M tokens: input $1.00.</p>"
        + "<style>.hidden{display:none}</style></main></body></html>"
    ).encode()
    client, _ = _client(FakeResponse(html))
    result = client.fetch("https://example.org/pricing", 5, 4096, 100)
    assert result["text"] == "Pricing Prices per 1M tokens: input $1.00."
    assert result["text_truncated"] is False
    secret, _ = _client(FakeResponse(html.replace(b"Navigation links", b"password=supersecret")))
    with pytest.raises(PublicWebRejected) as error:
        secret.fetch("https://example.org/pricing", 5, 4096, 100)
    assert error.value.code == "web_content_sensitive"


def test_fetch_accepts_bounded_markdown_text() -> None:
    markdown = b"# Pricing\n\nPrices per 1M tokens.\n\n| Model | Input |\n| A | $1.00 |\n"
    client, connection = _client(FakeResponse(markdown, content_type="text/markdown; charset=utf-8"))
    result = client.fetch("https://example.org/pricing.md", 5, 1024, 100)
    assert result["text"].startswith("# Pricing Prices per 1M tokens.")
    assert "$1.00" in result["text"]
    assert result["text_truncated"] is False
    assert connection.requested[0][2]["Accept"].startswith("text/markdown")
    assert connection.closed


def test_fetch_uses_article_when_main_is_absent() -> None:
    client, _ = _client(
        FakeResponse(b"<nav>Navigation " + b"item " * 100 + b"</nav><article>Only article.</article>")
    )
    result = client.fetch("https://example.org/article", 5, 2048, 100)
    assert result["text"] == "Only article."


def test_fetch_skips_whitespace_only_main_before_article_or_body() -> None:
    article, _ = _client(FakeResponse(b"<main> \n </main><article>Useful article.</article>"))
    assert article.fetch("https://example.org/article", 5, 1024, 100)["text"] == "Useful article."
    body, _ = _client(FakeResponse(b"<main> \n </main><p>Useful body.</p>"))
    assert body.fetch("https://example.org/body", 5, 1024, 100)["text"] == "Useful body."


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org/",
        "https://user:pass@example.org/",
        "https://127.0.0.1/",
        "https://[::1]/",
        "https://169.254.169.254/latest/meta-data/",
        "https://example.org:8443/",
    ],
)
def test_fetch_rejects_invalid_destinations_before_connect(url: str) -> None:
    client, connection = _client(FakeResponse(b"safe"))
    with pytest.raises(PublicWebRejected):
        client.fetch(url, 5, 1024, 100)
    assert connection.requested == []


def test_dns_mixed_public_private_answer_is_rejected() -> None:
    def mixed(host: str, port: int, *, type: int) -> list[tuple[Any, ...]]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", 443)),
        ]

    client = PublicWebClient(resolver=mixed)
    with pytest.raises(PublicWebRejected):
        client.fetch("https://example.org/", 5, 1024, 100)


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(b"", status=302, headers={"Location": "http://127.0.0.1/"}),
        FakeResponse(b"A" * 1025),
        FakeResponse(b"short", headers={"Content-Length": "99999"}),
        FakeResponse(b"short", headers={"Content-Encoding": "gzip"}),
        FakeResponse(b"binary", content_type="application/octet-stream"),
    ],
)
def test_fetch_rejects_redirect_large_compressed_or_binary_response(response: FakeResponse) -> None:
    client, connection = _client(response)
    with pytest.raises((PublicWebRejected, PublicWebUnavailable)):
        client.fetch("https://example.org/", 5, 1024, 100)
    assert connection.closed


def test_fetch_rejects_secret_in_page_before_text_truncation() -> None:
    client, _ = _client(FakeResponse(b"Safe first. password=supersecret later."))
    with pytest.raises(PublicWebRejected) as error:
        client.fetch("https://example.org/", 5, 1024, 10)
    assert "supersecret" not in str(error.value)


@pytest.mark.parametrize(
    "response,code",
    [
        (FakeResponse(b"short", headers={"Content-Length": "99999"}), "web_response_too_large"),
        (FakeResponse(b"short", headers={"Content-Encoding": "gzip"}), "web_response_compressed"),
        (FakeResponse(b"binary", content_type="application/octet-stream"), "web_response_content_type"),
        (FakeResponse(b"password=private-value"), "web_content_sensitive"),
    ],
)
def test_fetch_policy_rejection_has_content_free_owner_code(response: FakeResponse, code: str) -> None:
    client, _ = _client(response)
    with pytest.raises(PublicWebRejected) as error:
        client.fetch("https://example.org/private", 5, 1024, 100)
    assert error.value.code == code
    assert "example.org" not in str(error.value)
    assert "private-value" not in str(error.value)


def test_fetch_credential_and_nonpublic_target_have_distinct_owner_codes() -> None:
    client, connection = _client(FakeResponse(b"safe"))
    with pytest.raises(PublicWebRejected) as credential:
        client.fetch("https://user:private@example.org/", 5, 1024, 100)
    with pytest.raises(PublicWebRejected) as nonpublic:
        client.fetch("https://127.0.0.1/", 5, 1024, 100)
    assert credential.value.code == "web_target_credentials"
    assert nonpublic.value.code == "web_target_not_public"
    assert connection.requested == []


def test_search_fixed_endpoint_filters_bad_result_urls_and_dates() -> None:
    payload = json.dumps(
        {
            "results": [
                {"url": "http://internal/", "title": "bad"},
                {
                    "url": "https://example.org/a",
                    "title": "A",
                    "content": "Public result",
                    "publishedDate": "2026-09-26T12:00:00Z",
                },
            ]
        }
    ).encode()
    client, connection = _client(FakeResponse(payload, content_type="application/json"))
    result = client.search("school finance", "https://search.example.org/search", 3, 5, 4096)
    assert result["results"] == [
        {
            "url": "https://example.org/a",
            "title": "A",
            "snippet": "Public result",
            "published_at": "2026-09-26T12:00:00+00:00",
        }
    ]
    assert connection.requested[0][1] == "/search?q=school+finance&format=json"
    assert connection.requested[0][2]["Accept"] == "application/json"


def test_search_supports_only_fixed_internal_http_origin() -> None:
    payload = json.dumps({"results": []}).encode()
    connection = FakeConnection(FakeResponse(payload, content_type="application/json"))
    client = PublicWebClient(
        resolver=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no DNS")),
        internal_connection_factory=lambda timeout: connection,
    )
    assert client.search("public query", "http://searxng:8080/search", 2, 5, 2048)["results"] == []
    assert connection.requested[0][2]["Accept"] == "application/json"
    with pytest.raises(PublicWebRejected):
        client.search("public query", "http://other:8080/search", 2, 5, 2048)


def test_search_requires_setup_and_rejects_secret_query() -> None:
    client, connection = _client(FakeResponse(b"{}", content_type="application/json"))
    with pytest.raises(PublicWebUnavailable):
        client.search("public query", "", 2, 5, 2048)
    with pytest.raises(PublicWebRejected):
        client.search("password=supersecret", "https://search.example.org/search", 2, 5, 2048)
    assert connection.requested == []


def test_pinned_connection_rejects_changed_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    class Socket:
        closed = False

        def getpeername(self) -> tuple[str, int]:
            return ("127.0.0.1", 443)

        def close(self) -> None:
            self.closed = True

    sock = Socket()
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: sock)
    connection = _PinnedHTTPSConnection("example.org", "93.184.215.14", 5)
    with pytest.raises(PublicWebRejected):
        connection.connect()
    assert sock.closed


def test_read_timeout_is_classified_without_url_echo() -> None:
    class TimeoutConnection(FakeConnection):
        def getresponse(self) -> http.client.HTTPResponse:
            raise TimeoutError("https://example.org/private")

    connection = TimeoutConnection(FakeResponse(b""))
    client = PublicWebClient(resolver=_resolver, connection_factory=lambda h, a, t: connection)
    with pytest.raises(PublicWebTimedOut) as error:
        client.fetch("https://example.org/private", 5, 1024, 100)
    assert "example.org" not in str(error.value)


def test_slow_stream_stops_at_total_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    import onefoundry.public_web_client as module

    clock = [0.0]
    monkeypatch.setattr(module, "monotonic", lambda: clock[0])

    class Trickle(FakeResponse):
        def read1(self, amount: int = -1) -> bytes:
            clock[0] = 5.1
            return b"x"

    client, connection = _client(Trickle(b"ignored", content_type="text/plain"))
    with pytest.raises(PublicWebTimedOut):
        client.fetch("https://example.org/", 5, 1024, 100)
    assert connection.closed


def test_stalled_dns_returns_at_deadline_without_starting_request() -> None:
    release = Event()
    entered = Event()

    def stalled(*_args: object, **_kwargs: object) -> list[tuple[Any, ...]]:
        entered.set()
        release.wait(5)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443))]

    connection = FakeConnection(FakeResponse(b"safe"))
    client = PublicWebClient(resolver=stalled, connection_factory=lambda h, a, t: connection)
    try:
        with pytest.raises(PublicWebTimedOut):
            client.fetch("https://example.org/", 1, 1024, 100)
        assert entered.is_set()
        assert connection.requested == []
    finally:
        release.set()


def test_stalled_response_headers_return_at_deadline() -> None:
    release = Event()

    class StalledHeaders(FakeConnection):
        def getresponse(self) -> http.client.HTTPResponse:
            release.wait(5)
            return super().getresponse()

    connection = StalledHeaders(FakeResponse(b"safe"))
    client = PublicWebClient(resolver=_resolver, connection_factory=lambda h, a, t: connection)
    try:
        with pytest.raises(PublicWebTimedOut):
            client.fetch("https://example.org/", 1, 1024, 100)
        assert connection.closed
    finally:
        release.set()
