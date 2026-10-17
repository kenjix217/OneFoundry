"""Disposable Responses fixture for an actual Codex app-server dynamic tool turn."""

from __future__ import annotations

import json
import os
import socket
import ssl
from typing import Any

from runner.codex_local_responses_child import _event, _read_request


def _response(model: str, item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "resp_team_fixture",
        "object": "response",
        "created_at": 1,
        "status": "completed",
        "model": model,
        "output": [item],
        "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
    }


def main() -> None:
    nonce = os.environ["PROOF_NONCE"]
    request_path = os.environ["PROOF_REQUEST_PATH"]
    if request_path not in {"/v1/responses", "/backend-api/codex/responses"}:
        raise ValueError("invalid proof provider path")
    if len(nonce) != 32 or any(char not in "0123456789abcdef" for char in nonce):
        raise ValueError("invalid proof nonce")
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
                    line, headers, body = _read_request(secure)
                    request = json.loads(body)
                    valid = (
                        line == f"POST {request_path} HTTP/1.1"
                        and headers.get("authorization") == "Bearer local-test-only"
                        and (
                            headers.get("chatgpt-account-id") == "fixture-account"
                            if request_path == "/backend-api/codex/responses"
                            else "chatgpt-account-id" not in headers
                        )
                        and isinstance(request, dict)
                        and request.get("stream") is True
                        and isinstance(request.get("model"), str)
                    )
                    if not valid:
                        secure.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                        continue
                    tools = [item for item in request.get("tools", []) if isinstance(item, dict)]
                    names = [item.get("name") for item in tools]
                    namespace = next((item for item in tools if item.get("name") == "onefoundry"), None)
                    nested = namespace.get("tools") if isinstance(namespace, dict) else None
                    nested_names = (
                        [item.get("name") for item in nested if isinstance(item, dict)]
                        if isinstance(nested, list)
                        else []
                    )
                    inputs = request.get("input", [])
                    completed = bool(
                        inputs
                        and isinstance(inputs[-1], dict)
                        and inputs[-1].get("type") == "function_call_output"
                    )
                    print(
                        json.dumps(
                            {
                                "tool_names": names,
                                "tool_types": [item.get("type") for item in tools],
                                "nested_names": nested_names,
                                "completed": completed,
                                "input_types": [
                                    item.get("type") for item in inputs if isinstance(item, dict)
                                ],
                            }
                        ),
                        flush=True,
                    )
                    if completed:
                        item = {
                            "id": "msg_team_fixture",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": f"team-proof-{nonce}", "annotations": []}
                            ],
                        }
                    else:
                        matched = [name for name in nested_names if name == "publish_update"]
                        if len(matched) != 1:
                            secure.sendall(b"HTTP/1.1 422 Unprocessable Entity\r\nContent-Length: 0\r\n\r\n")
                            continue
                        item = {
                            "id": "fc_team_fixture",
                            "type": "function_call",
                            "status": "completed",
                            "call_id": "call_team_fixture",
                            "name": matched[0],
                            "namespace": "onefoundry",
                            "arguments": json.dumps({"body": f"public plan {nonce}"}),
                        }
                    response = _response(request["model"], item)
                    payload = _event(
                        "response.output_item.done",
                        {"type": "response.output_item.done", "output_index": 0, "item": item},
                    ) + _event("response.completed", {"type": "response.completed", "response": response})
                    secure.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                        b"Cache-Control: no-cache\r\n"
                        + f"Content-Length: {len(payload)}\r\n\r\n".encode()
                        + payload
                    )
            except (OSError, ValueError, ssl.SSLError) as exc:
                print(json.dumps({"fixture_error": type(exc).__name__}), flush=True)
                connection.close()


if __name__ == "__main__":
    main()
