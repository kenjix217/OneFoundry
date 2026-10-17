"""Local Messages fixture for one synthetic Claude Code calculator repair."""

from __future__ import annotations

import json
import os
import socket
import ssl
from hashlib import sha256
from typing import Any

from runner.codex_local_responses_child import _read_request


def _texts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [text for item in value for text in _texts(item)]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _texts(item)]
    return []


def _message(content: list[dict[str, Any]], stop: str) -> bytes:
    return json.dumps(
        {
            "id": "msg_fixture_repair",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-5",
            "content": content,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 4},
        },
        separators=(",", ":"),
    ).encode()


def _stream(content: list[dict[str, Any]], stop: str) -> bytes:
    message = json.loads(_message([], stop))
    message["stop_reason"] = None
    events: list[dict[str, Any]] = [{"type": "message_start", "message": message}]
    for index, block in enumerate(content):
        if block["type"] == "tool_use":
            start: dict[str, Any] = {
                "type": "tool_use",
                "id": block["id"],
                "name": block["name"],
                "input": {},
            }
            delta = {
                "type": "input_json_delta",
                "partial_json": json.dumps(block["input"], separators=(",", ":")),
            }
        else:
            start = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": block["text"]}
        events.extend(
            (
                {"type": "content_block_start", "index": index, "content_block": start},
                {"type": "content_block_delta", "index": index, "delta": delta},
                {"type": "content_block_stop", "index": index},
            )
        )
    events.extend(
        (
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": 4},
            },
            {"type": "message_stop"},
        )
    )
    return b"".join(
        f"event: {event['type']}\ndata: {json.dumps(event, separators=(',', ':'))}\n\n".encode()
        for event in events
    )


def main() -> None:
    prompt_digest = os.environ["PROMPT_SHA256"]
    if len(prompt_digest) != 64 or any(character not in "0123456789abcdef" for character in prompt_digest):
        raise ValueError("invalid_prompt_digest")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain("/tls/leaf-cert.pem", "/tls/leaf-key.pem")
    stage = 0
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("0.0.0.0", 443))  # noqa: S104 - isolated local fixture network
        listener.listen(8)
        print("READY", flush=True)
        while True:
            connection, _ = listener.accept()
            with connection:
                try:
                    with context.wrap_socket(connection, server_side=True) as secure:
                        line, headers, body = _read_request(secure)
                        request = json.loads(body)
                        if (
                            line != "POST /v1/messages?beta=true HTTP/1.1"
                            or headers.get("x-api-key") != "local-test-only"
                            or not isinstance(request, dict)
                            or request.get("model") != "claude-sonnet-4-5"
                            or request.get("stream") not in (True, False)
                        ):
                            raise ValueError("invalid_fixture_request")
                        messages = request.get("messages")
                        system = request.get("system")
                        tools = request.get("tools")
                        if not isinstance(messages, list) or not isinstance(system, list):
                            raise ValueError("invalid_fixture_messages")
                        title = "You are naming a coding session" in json.dumps(system)
                        prompt_bound = any(
                            sha256(value.encode()).hexdigest() == prompt_digest for value in _texts(messages)
                        )
                        results = [
                            item
                            for item in _walk(messages)
                            if isinstance(item, dict) and item.get("type") == "tool_result"
                        ]
                        content: list[dict[str, Any]]
                        if title:
                            content = [{"type": "text", "text": "Calculator repair"}]
                            stop = "end_turn"
                        elif stage == 0 and prompt_bound and _tool_names(tools) == {"Read", "Edit"}:
                            content = [
                                {
                                    "type": "tool_use",
                                    "id": "toolu_01fixture",
                                    "name": "Read",
                                    "input": {"file_path": "/workspace/calculator.py"},
                                }
                            ]
                            stage = 1
                            stop = "tool_use"
                        elif stage == 1 and any(
                            item.get("tool_use_id") == "toolu_01fixture" and not item.get("is_error")
                            for item in results
                        ):
                            content = [
                                {
                                    "type": "tool_use",
                                    "id": "toolu_02fixture",
                                    "name": "Edit",
                                    "input": {
                                        "file_path": "/workspace/calculator.py",
                                        "old_string": "return value",
                                        "new_string": "return value + 1",
                                    },
                                }
                            ]
                            stage = 2
                            stop = "tool_use"
                        elif stage == 2 and any(
                            item.get("tool_use_id") == "toolu_02fixture" and not item.get("is_error")
                            for item in results
                        ):
                            content = [{"type": "text", "text": "Fixture repair complete"}]
                            stage = 3
                            stop = "end_turn"
                        else:
                            raise ValueError("unexpected_fixture_stage")
                        print(
                            json.dumps(
                                {
                                    "stage": "title" if title else stage,
                                    "prompt_bound": prompt_bound,
                                    "tools": sorted(_tool_names(tools)),
                                    "result_ids": [item.get("tool_use_id") for item in results],
                                },
                                sort_keys=True,
                            ),
                            flush=True,
                        )
                        streaming = request["stream"] is True
                        payload = _stream(content, stop) if streaming else _message(content, stop)
                        secure.sendall(
                            b"HTTP/1.1 200 OK\r\nContent-Type: "
                            + (b"text/event-stream" if streaming else b"application/json")
                            + b"\r\n"
                            + f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode()
                            + payload
                        )
                except (OSError, ValueError, ssl.SSLError) as exc:
                    print(
                        json.dumps(
                            {"fixture_error": str(exc) if isinstance(exc, ValueError) else type(exc).__name__}
                        ),
                        flush=True,
                    )


def _walk(value: Any) -> list[Any]:
    if isinstance(value, list):
        return [part for item in value for part in _walk(item)]
    if isinstance(value, dict):
        return [value, *[part for item in value.values() for part in _walk(item)]]
    return []


def _tool_names(value: Any) -> set[str]:
    return (
        {item["name"] for item in value if isinstance(item, dict) and isinstance(item.get("name"), str)}
        if isinstance(value, list)
        else set()
    )


if __name__ == "__main__":
    main()
