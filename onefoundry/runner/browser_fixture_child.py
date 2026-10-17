"""Fixed synthetic page/server and offline Playwright probe inside disposable Docker."""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit

MARKER = "ONEFOUNDRY_OFFLINE_BROWSER_FIXTURE_V1"
PATH = "/fixture.html"
HTML = (
    f"<!doctype html><html><head><title>{MARKER}</title></head><body><main>{MARKER}</main></body></html>"
).encode()
MAX_PNG = 512 * 1024
MAX_TEXT = 512


def _no_default_route() -> bool:
    with open("/proc/net/route", encoding="ascii") as routes:
        return all(line.split()[1] != "00000000" for line in list(routes)[1:] if len(line.split()) > 1)


def _no_host_paths() -> bool:
    forbidden = ("/var/run/docker.sock", "/app", "/work", "/repo", "/spool", "/run/secrets")
    from pathlib import Path

    return all(not Path(path).exists() and not Path(path).is_symlink() for path in forbidden)


class _Page(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != PATH:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(HTML)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(HTML)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _fixture() -> None:
    server = HTTPServer(("127.0.0.1", 8765), _Page)
    print("READY", flush=True)
    server.serve_forever()


def _browser(url: str) -> None:
    from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]

    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.path != PATH or parsed.query or parsed.fragment:
        raise ValueError("fixture_url_invalid")
    if not _no_default_route() or not _no_host_paths():
        raise RuntimeError("isolation_probe_failed")
    denied: list[str] = []
    popup_count = 0
    download_count = 0
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            viewport={"width": 800, "height": 600},
            accept_downloads=False,
            service_workers="block",
        )
        context.add_init_script(
            "Object.defineProperty(window, 'open', {value: () => null, writable: false, configurable: false})"
        )

        def route_request(route: object) -> None:
            request = route.request  # type: ignore[attr-defined]
            if request.url == url:
                route.continue_()  # type: ignore[attr-defined]
            else:
                denied.append(request.url)
                route.abort("blockedbyclient")  # type: ignore[attr-defined]

        context.route("**/*", route_request)

        def close_popup(page: object) -> None:
            nonlocal popup_count
            popup_count += 1
            page.close()  # type: ignore[attr-defined]

        page = context.new_page()
        context.on("page", close_popup)

        def record_download(_download: object) -> None:
            nonlocal download_count
            download_count += 1

        page.on("download", record_download)
        response = page.goto(url, wait_until="load", timeout=5000)
        if response is None or response.status != 200 or response.request.redirected_from:
            raise RuntimeError("fixture_navigation_invalid")
        title = page.title()
        text = page.locator("body").inner_text(timeout=2000)
        final_url = page.url
        if title != MARKER or text != MARKER or final_url != url:
            raise RuntimeError("fixture_content_invalid")
        # A hostile URL is attempted from page JS and must be rejected by the
        # request route. The isolated network independently denies egress.
        hostile = page.evaluate("""async () => {
            try { await fetch('http://example.invalid/hostile'); return false; }
            catch { return true; }
        }""")
        if hostile is not True or "http://example.invalid/hostile" not in denied:
            raise RuntimeError("hostile_request_not_blocked")
        wrong_path = page.evaluate("""async () => {
            try { await fetch('http://127.0.0.1:8765/other'); return false; }
            catch { return true; }
        }""")
        if wrong_path is not True or "http://127.0.0.1:8765/other" not in denied:
            raise RuntimeError("wrong_fixture_path_not_blocked")
        if page.evaluate("window.open('http://example.invalid/popup')") is not None:
            raise RuntimeError("popup_not_blocked")
        if popup_count != 0 or download_count != 0 or len(context.pages) != 1:
            raise RuntimeError("unexpected_browser_action")
        png = page.screenshot(type="png", full_page=False, timeout=3000)
        context.close()
        browser.close()
    if len(png) > MAX_PNG or len(title) > 128 or len(text) > MAX_TEXT:
        raise RuntimeError("browser_output_too_large")
    print(
        json.dumps(
            {
                "marker": MARKER,
                "title": title,
                "text": text,
                "final_url": final_url,
                "png_sha256": hashlib.sha256(png).hexdigest(),
                "png_base64": base64.b64encode(png).decode("ascii"),
                "no_default_route": True,
                "no_host_paths": True,
                "hostile_request_blocked": True,
                "popup_count": popup_count,
                "download_count": download_count,
            },
            separators=(",", ":"),
        ),
        flush=True,
    )


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "fixture":
        _fixture()
    elif len(sys.argv) == 3 and sys.argv[1] == "browser":
        _browser(sys.argv[2])
    else:
        raise SystemExit("invalid_role")
