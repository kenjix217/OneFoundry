"""Synthetic Responses server that asks Codex to repair one disposable fixture."""

from __future__ import annotations

import json
import os
import re
import socket
import ssl
import sys
from hashlib import sha256
from typing import Any

from runner.codex_local_responses_child import _event, _read_request, proxy


def _texts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in _texts(item)]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _texts(item)]
    return []


def _response(model: str, output: list[dict[str, object]], status: str) -> dict[str, object]:
    return {
        "id": "resp_fixture_repair",
        "object": "response",
        "created_at": 1,
        "status": status,
        "model": model,
        "output": output,
        "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
    }


def _next_command(input_items: list[Any]) -> tuple[str, dict[str, Any]] | None:
    outputs = [
        item for item in input_items if isinstance(item, dict) and item.get("type") == "function_call_output"
    ]
    if not outputs:
        return "exec_command", {
            "cmd": "sed -i 's/return value$/return value + 1/' calculator.py",
            "workdir": "/workspace",
            "yield_time_ms": 10000,
        }
    output = outputs[-1].get("output")
    if isinstance(output, str):
        pending = re.search(r"^Process running with session ID ([0-9]+)$", output, re.MULTILINE)
        if pending is not None:
            return "write_stdin", {"session_id": int(pending[1]), "chars": "", "yield_time_ms": 1000}
        if re.search(r"^Process exited with code 0$", output, re.MULTILINE):
            return None
    raise ValueError("fixture_command_not_completed")


def main() -> None:
    nonce = os.environ["PROOF_NONCE"]
    prompt_digest = os.environ["PROMPT_SHA256"]
    if len(nonce) != 32 or any(char not in "0123456789abcdef" for char in nonce):
        raise ValueError("invalid_proof_nonce")
    if len(prompt_digest) != 64 or any(char not in "0123456789abcdef" for char in prompt_digest):
        raise ValueError("invalid_prompt_digest")
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
                    authorized = headers.get("authorization") == "Bearer local-test-only"
                    request = json.loads(body)
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
                    print(
                        json.dumps(
                            {
                                "authorized": authorized,
                                "valid": valid,
                                "tool_names": [tool.get("name") for tool in tools if isinstance(tool, dict)],
                                "input_types": [
                                    item.get("type") for item in input_items if isinstance(item, dict)
                                ],
                                "prompt_bound": prompt_bound,
                            }
                        ),
                        flush=True,
                    )
                    if not valid or (
                        not any(
                            isinstance(item, dict) and item.get("type") == "function_call_output"
                            for item in input_items
                        )
                        and not prompt_bound
                    ):
                        secure.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                        continue
                    next_command = _next_command(input_items)
                    if next_command is None:
                        item: dict[str, object] = {
                            "id": "msg_fixture_repair",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": f"fixture-repair-{nonce}", "annotations": []}
                            ],
                        }
                        response = _response(request["model"], [item], "completed")
                        payload = _event(
                            "response.output_item.done",
                            {
                                "type": "response.output_item.done",
                                "output_index": 0,
                                "item": item,
                            },
                        ) + _event("response.completed", {"type": "response.completed", "response": response})
                    else:
                        names = [tool.get("name") for tool in tools if isinstance(tool, dict)]
                        command_name, arguments = next_command
                        if command_name not in names:
                            secure.sendall(b"HTTP/1.1 422 Unprocessable Entity\r\nContent-Length: 0\r\n\r\n")
                            continue
                        item = {
                            "id": f"fc_fixture_repair_{len(input_items)}",
                            "type": "function_call",
                            "status": "completed",
                            "call_id": f"call_fixture_repair_{len(input_items)}",
                            "name": command_name,
                            "arguments": json.dumps(arguments),
                        }
                        response = _response(request["model"], [item], "in_progress")
                        payload = _event(
                            "response.output_item.done",
                            {
                                "type": "response.output_item.done",
                                "output_index": 0,
                                "item": item,
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
