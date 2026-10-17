"""Local stdio transport; credentials stay in an operator-owned private file."""

import argparse
import json
import os
import re
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID


def configuration(path: Path) -> dict[str, Any]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("private client configuration required")
        raw = os.read(fd, 4097)
    finally:
        os.close(fd)
    if len(raw) > 4096:
        raise ValueError("client configuration exceeds bounds")
    data = json.loads(raw)
    fields = {
        "server_url",
        "api_key",
        "project_ids",
        "timeout_seconds",
        "max_request_bytes",
        "max_response_bytes",
    }
    if not isinstance(data, dict) or set(data) != fields:
        raise ValueError("invalid client configuration fields")
    if (
        not isinstance(data["project_ids"], list)
        or not 1 <= len(data["project_ids"]) <= 20
        or any(not isinstance(value, str) for value in data["project_ids"])
    ):
        raise ValueError("explicit project selection required")
    projects = [str(UUID(value)) for value in data["project_ids"]]
    if projects != data["project_ids"] or len(set(projects)) != len(projects):
        raise ValueError("unique canonical project IDs required")
    if not isinstance(data["server_url"], str) or any(
        ord(char) < 33 or ord(char) > 126 for char in data["server_url"]
    ):
        raise ValueError("use a printable ASCII memory endpoint without whitespace")
    url = urlsplit(data["server_url"])
    if (
        not url.hostname
        or url.path != "/mcp"
        or url.username
        or url.password
        or url.query
        or url.fragment
        or (url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"127.0.0.1", "::1"}))
        or not isinstance(data["api_key"], str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{43}", data["api_key"])
    ):
        raise ValueError("invalid memory endpoint or credential")
    for name, low, high in (
        ("timeout_seconds", 2, 60),
        ("max_request_bytes", 4096, 131072),
        ("max_response_bytes", 4096, 2097152),
    ):
        if type(data[name]) is not int or not low <= data[name] <= high:
            raise ValueError("invalid client resource bounds")
    return data


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def exchange(data: dict[str, Any], request: dict[str, Any]) -> dict[str, Any] | None:
    body = json.dumps(request).encode("utf-8")
    if len(body) > data["max_request_bytes"]:
        raise ValueError("request exceeds bounds")
    headers = {
        "Authorization": "Bearer " + data["api_key"],
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if request.get("method") != "initialize":
        headers["MCP-Protocol-Version"] = "2025-11-25"
    # Neither ambient proxies nor redirects may receive the private bearer credential.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    outgoing = urllib.request.Request(data["server_url"], body, headers, method="POST")  # noqa: S310
    with opener.open(outgoing, timeout=data["timeout_seconds"]) as response:  # noqa: S310 - URL validated
        raw = response.read(data["max_response_bytes"] + 1)
    if len(raw) > data["max_response_bytes"]:
        raise ValueError("response exceeds bounds")
    if "id" not in request:
        return None
    decoded = json.loads(raw)
    if not isinstance(decoded, dict) or decoded.get("jsonrpc") != "2.0" or decoded.get("id") != request["id"]:
        raise ValueError("invalid MCP response identity")
    if request.get("method") == "initialize" and isinstance(decoded.get("result"), dict):
        result = decoded["result"]
        result["instructions"] = (
            "Operator-selected project IDs for this connection: "
            + ", ".join(data["project_ids"])
            + ". "
            + result.get("instructions", "")
        )
    return decoded


def serve(path: Path) -> None:
    configured = configuration(path)
    while line := sys.stdin.buffer.readline(configured["max_request_bytes"] + 1):
        request: dict[str, Any] = {}
        try:
            if len(line) > configured["max_request_bytes"] or not line.endswith(b"\n"):
                raise ValueError("request exceeds bounds")
            parsed = json.loads(line)
            if (
                not isinstance(parsed, dict)
                or parsed.get("jsonrpc") != "2.0"
                or not isinstance(parsed.get("method"), str)
            ):
                raise ValueError("invalid MCP request")
            request = parsed
            # Reload the credential file for every operation so rotation needs no plugin restart.
            response = exchange(configuration(path), request)
        except urllib.error.HTTPError as exc:
            code = "memory_authentication_failed" if exc.code in {401, 403} else "memory_http_failed"
            response = error(request, code)
        except (OSError, ValueError, TypeError):
            response = error(request, "memory_connection_failed")
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
        if len(line) > configured["max_request_bytes"]:
            return  # Stop rather than interpret the remainder as another request.


def error(request: dict[str, Any], code: str) -> dict[str, Any] | None:
    if "method" in request and "id" not in request:
        return None
    return {"jsonrpc": "2.0", "id": request.get("id"), "error": {"code": -32000, "message": code}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        serve(args.config)
    except (OSError, ValueError, TypeError):
        raise SystemExit("memory_bridge_startup_failed: check private configuration") from None


if __name__ == "__main__":
    main()
