"""Inspect one supplied static page inside a networkless disposable container."""

from __future__ import annotations

import hashlib
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from urllib.parse import urlsplit

MAX_INPUT = 262_144
MAX_TEXT = 4096
MAX_TITLE = 128


def _no_default_route() -> bool:
    with open("/proc/net/route", encoding="ascii") as routes:
        return all(line.split()[1] != "00000000" for line in list(routes)[1:] if len(line.split()) > 1)


def _no_host_paths() -> bool:
    from pathlib import Path

    forbidden = ("/var/run/docker.sock", "/app", "/work", "/repo", "/spool", "/run/secrets")
    return all(not Path(path).exists() and not Path(path).is_symlink() for path in forbidden)


def inspect(payload: dict[str, object]) -> dict[str, object]:
    if set(payload) != {"url", "html", "css", "width", "height", "timeout_ms"}:
        raise ValueError("input_shape_invalid")
    url, html, css = payload["url"], payload["html"], payload["css"]
    width, height, timeout_ms = payload["width"], payload["height"], payload["timeout_ms"]
    if not isinstance(url, str) or not isinstance(html, str) or not isinstance(css, str):
        raise ValueError("input_type_invalid")
    if any(type(value) is not int for value in (width, height, timeout_ms)):
        raise ValueError("input_type_invalid")
    assert isinstance(width, int) and isinstance(height, int) and isinstance(timeout_ms, int)
    parsed = urlsplit(url)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.port is None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or url != f"http://127.0.0.1:{parsed.port}{parsed.path}"
        or not 1 <= width <= 1920
        or not 1 <= height <= 1200
        or not 1 <= timeout_ms <= 30_000
        or not html
        or len(html.encode()) + len(css.encode()) + len(b"<style></style>") > MAX_INPUT
        or "</style" in css.lower()
    ):
        raise ValueError("input_bounds_invalid")
    if not _no_default_route() or not _no_host_paths():
        raise RuntimeError("isolation_probe_failed")
    from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]

    page_bytes = (f"<style>{css}</style>" + html).encode("utf-8")

    class Page(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != parsed.path:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page_bytes)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(page_bytes)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", parsed.port), Page)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    blocked = 0
    popups = 0
    downloads = 0
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
            context = browser.new_context(
                viewport={"width": width, "height": height},
                java_script_enabled=False,
                accept_downloads=False,
                service_workers="block",
            )

            def route_request(route: object) -> None:
                nonlocal blocked
                request = route.request  # type: ignore[attr-defined]
                if request.url == url:
                    route.continue_()  # type: ignore[attr-defined]
                else:
                    blocked += 1
                    route.abort("blockedbyclient")  # type: ignore[attr-defined]

            def close_popup(page: object) -> None:
                nonlocal popups
                popups += 1
                page.close()  # type: ignore[attr-defined]

            def record_download(_download: object) -> None:
                nonlocal downloads
                downloads += 1

            context.route("**/*", route_request)
            page = context.new_page()
            context.on("page", close_popup)
            page.on("download", record_download)
            response = page.goto(url, wait_until="load", timeout=timeout_ms)
            if response is None or response.status != 200 or response.request.redirected_from:
                raise RuntimeError("navigation_invalid")
            title = page.title()
            text = page.locator("body").inner_text(timeout=timeout_ms)
            final_url = page.url
            if final_url != url or len(title) > MAX_TITLE or len(text) > MAX_TEXT:
                raise RuntimeError("output_invalid")
            if popups or downloads or len(context.pages) != 1:
                raise RuntimeError("unexpected_browser_action")
            context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    return {
        "title": title,
        "text": text,
        "final_url": final_url,
        "status": 200,
        "blocked_request_count": blocked,
        "popup_count": popups,
        "download_count": downloads,
        "no_default_route": True,
        "no_host_paths": True,
        "source_sha256": hashlib.sha256(html.encode() + b"\x00" + css.encode()).hexdigest(),
    }


if __name__ == "__main__":
    raw = sys.stdin.buffer.read(MAX_INPUT + 4096)
    if len(raw) > MAX_INPUT + 4095:
        raise SystemExit("input_too_large")
    result = inspect(json.loads(raw))
    print(json.dumps(result, separators=(",", ":")), flush=True)
