"""Local Responses fixture asking Codex to edit one known snapshot text file."""

from __future__ import annotations

import json
import os
import socket
import ssl
import sys
from hashlib import sha256
from typing import Any

from runner.codex_local_responses_child import _event, _read_request, proxy

EXPECTED_AFTER = b"synthetic HTTPS fixture repaired\n"


def _texts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [part for item in value for part in _texts(item)]
    if isinstance(value, dict):
        return [part for item in value.values() for part in _texts(item)]
    return []


def _response(model: str, output: list[dict[str, object]], status: str) -> dict[str, object]:
    return {
        "id": "resp_snapshot_file_repair",
        "object": "response",
        "created_at": 1,
        "status": status,
        "model": model,
        "output": output,
        "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
    }


def main() -> None:
    prompt_digest = os.environ["PROMPT_SHA256"]
    if len(prompt_digest) != 64 or any(char not in "0123456789abcdef" for char in prompt_digest):
        raise ValueError("invalid_prompt_digest")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain("/tls/leaf-cert.pem", "/tls/leaf-key.pem")
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("0.0.0.0", 443))  # noqa: S104 - isolated fake Docker network
        listener.listen(8)
        print("READY", flush=True)
        while True:
            connection, _ = listener.accept()
            try:
                with context.wrap_socket(connection, server_side=True) as secure:
                    request_line, headers, body = _read_request(secure)
                    request = json.loads(body)
                    authorized = headers.get("authorization") == "Bearer local-test-only"
                    valid = (
                        request_line == "POST /v1/responses HTTP/1.1"
                        and authorized
                        and isinstance(request, dict)
                        and request.get("stream") is True
                        and isinstance(request.get("model"), str)
                    )
                    tools = request.get("tools", []) if isinstance(request, dict) else []
                    input_items = request.get("input", []) if isinstance(request, dict) else []
                    prompt_bound = any(
                        sha256(value.encode()).hexdigest() == prompt_digest for value in _texts(input_items)
                    )
                    completed = any(
                        isinstance(item, dict) and item.get("type") == "function_call_output"
                        for item in input_items
                    )
                    print(
                        json.dumps(
                            {
                                "valid": valid,
                                "authorized": authorized,
                                "prompt_bound": prompt_bound,
                                "completed": completed,
                                "tool_names": [tool.get("name") for tool in tools if isinstance(tool, dict)],
                            }
                        ),
                        flush=True,
                    )
                    if not valid or (not completed and not prompt_bound):
                        secure.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                        continue
                    if completed:
                        item: dict[str, object] = {
                            "id": "msg_snapshot_file_repair",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "fixture file edit complete",
                                    "annotations": [],
                                }
                            ],
                        }
                        response = _response(request["model"], [item], "completed")
                    else:
                        if "exec_command" not in [
                            tool.get("name") for tool in tools if isinstance(tool, dict)
                        ]:
                            secure.sendall(b"HTTP/1.1 422 Unprocessable Entity\r\nContent-Length: 0\r\n\r\n")
                            continue
                        item = {
                            "id": "fc_snapshot_file_repair",
                            "type": "function_call",
                            "status": "completed",
                            "call_id": "call_snapshot_file_repair",
                            "name": "exec_command",
                            "arguments": json.dumps(
                                {
                                    "cmd": "printf 'synthetic HTTPS fixture repaired\\n' > file.txt",
                                    "workdir": "/workspace",
                                    "yield_time_ms": 1000,
                                }
                            ),
                        }
                        response = _response(request["model"], [item], "in_progress")
                    payload = _event(
                        "response.output_item.done",
                        {"type": "response.output_item.done", "output_index": 0, "item": item},
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
