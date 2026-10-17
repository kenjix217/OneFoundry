"""Local TLS Responses fixture for the pinned Codex CLI proof only."""

from __future__ import annotations

import json
import os
import select
import socket
import ssl
import sys

from runner.egress_probe_child import _client_hello


def proxy(fixture_ip: str) -> None:
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("0.0.0.0", 3128))  # noqa: S104 - isolated Docker network
        listener.listen(8)
        print("READY", flush=True)
        while True:
            connection, _ = listener.accept()
            with connection:
                try:
                    connection.settimeout(4)
                    first = bytearray()
                    while b"\r\n\r\n" not in first and len(first) < 2048:
                        part = connection.recv(2048 - len(first))
                        if not part:
                            raise ValueError("incomplete_connect_headers")
                        first.extend(part)
                    line = bytes(first).split(b"\r\n", 1)[0]
                    allowed = line == b"CONNECT provider.test:443 HTTP/1.1"
                    print(
                        json.dumps({"connect": line.decode("ascii", errors="replace"), "allowed": allowed}),
                        flush=True,
                    )
                    if not allowed:
                        connection.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                        continue
                    with socket.create_connection((fixture_ip, 443), timeout=4) as upstream:
                        connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                        upstream.sendall(_client_hello(connection))
                        print(json.dumps({"tls_sni": "provider.test"}), flush=True)
                        sockets = (connection, upstream)
                        while True:
                            ready, _, _ = select.select(sockets, (), (), 4)
                            if not ready:
                                break
                            for source in ready:
                                data = source.recv(8192)
                                if not data:
                                    break
                                (upstream if source is connection else connection).sendall(data)
                            else:
                                continue
                            break
                except (OSError, ValueError) as exc:
                    print(
                        json.dumps({"proxy_error": type(exc).__name__, "detail": str(exc)[:80]}), flush=True
                    )


def _read_request(connection: ssl.SSLSocket) -> tuple[str, dict[str, str], bytes]:
    connection.settimeout(4)
    raw = bytearray()
    while b"\r\n\r\n" not in raw and len(raw) < 65536:
        part = connection.recv(4096)
        if not part:
            raise ValueError("incomplete_headers")
        raw.extend(part)
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("ascii").split("\r\n")
    headers = {
        key.lower(): value for key, value in (line.split(": ", 1) for line in lines[1:] if ": " in line)
    }
    length = int(headers.get("content-length", "0"))
    if length > 262144:
        raise ValueError("request_too_large")
    while len(body) < length:
        part = connection.recv(length - len(body))
        if not part:
            raise ValueError("incomplete_body")
        body += part
    return lines[0], headers, bytes(body)


def _event(name: str, data: dict[str, object]) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n".encode()


def main() -> None:
    nonce = os.environ["PROOF_NONCE"]
    if len(nonce) != 32 or any(char not in "0123456789abcdef" for char in nonce):
        raise ValueError("invalid_proof_nonce")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain("/tls/leaf-cert.pem", "/tls/leaf-key.pem")
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("0.0.0.0", 443))  # noqa: S104 - disposable isolated Docker network
        listener.listen(8)
        print("READY", flush=True)
        while True:
            connection, _ = listener.accept()
            try:
                with context.wrap_socket(connection, server_side=True) as secure:
                    request_line, headers, body = _read_request(secure)
                    valid = (
                        request_line == "POST /v1/responses HTTP/1.1"
                        and headers.get("authorization") == "Bearer local-test-only"
                        and headers.get("content-type", "").startswith("application/json")
                    )
                    request = json.loads(body) if valid else None
                    valid = valid and isinstance(request, dict) and request.get("stream") is True
                    print(
                        json.dumps(
                            {
                                "request_line": request_line,
                                "authorized": headers.get("authorization") == "Bearer local-test-only",
                                "stream": isinstance(request, dict) and request.get("stream") is True,
                                "model": request.get("model") if isinstance(request, dict) else None,
                            }
                        ),
                        flush=True,
                    )
                    if not valid or not isinstance(request, dict):
                        secure.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                        continue
                    response = {
                        "id": "resp_local_proof",
                        "object": "response",
                        "created_at": 1,
                        "status": "completed",
                        "model": request["model"],
                        "output": [
                            {
                                "id": "msg_local_proof",
                                "type": "message",
                                "status": "completed",
                                "role": "assistant",
                                "content": [
                                    {"type": "output_text", "text": f"local-proof-{nonce}", "annotations": []}
                                ],
                            }
                        ],
                        "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
                    }
                    payload = _event(
                        "response.output_item.done",
                        {
                            "type": "response.output_item.done",
                            "output_index": 0,
                            "item": response["output"][0],
                        },
                    ) + _event("response.completed", {"type": "response.completed", "response": response})
                    secure.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nCache-Control: no-cache\r\n"
                        + f"Content-Length: {len(payload)}\r\n\r\n".encode()
                        + payload
                    )
            except (OSError, ValueError, ssl.SSLError) as exc:
                print(json.dumps({"fixture_error": type(exc).__name__, "detail": str(exc)[:100]}), flush=True)
                connection.close()


if __name__ == "__main__":
    if sys.argv[1:] and sys.argv[1] == "proxy":
        proxy(sys.argv[2])
    else:
        main()
