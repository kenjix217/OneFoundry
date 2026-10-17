"""Bounded approval-record parsing independent of the project's history length."""

import json
from collections.abc import Callable
from typing import Any


def matches_approval(
    read: Callable[[int], bytes],
    matches: Callable[[dict[str, Any]], bool],
    *,
    record_limit: int,
    remaining: Callable[[], float],
) -> bool:
    """Validate the complete array with bounded records and a caller-owned deadline."""
    if type(record_limit) is not int or record_limit < 1:
        raise ValueError("activation_approval_record_limit_invalid")
    state = "start"
    record = bytearray()
    depth = 0
    quoted = escaped = found = False
    while True:
        remaining()
        chunk = read(record_limit + 1)
        remaining()
        if not chunk:
            if state != "end":
                raise ValueError("activation_approval_history_incomplete")
            return found
        for byte in chunk:
            if state == "record":
                record.append(byte)
                if len(record) > record_limit:
                    raise ValueError("activation_approval_record_exceeds_bound")
                if quoted:
                    if escaped:
                        escaped = False
                    elif byte == ord("\\"):
                        escaped = True
                    elif byte == ord('"'):
                        quoted = False
                elif byte == ord('"'):
                    quoted = True
                elif byte in b"{[":
                    depth += 1
                elif byte in b"}]":
                    depth -= 1
                    if depth == 0:
                        value = json.loads(record)
                        if not isinstance(value, dict):
                            raise ValueError("activation_approval_record_invalid")
                        found = matches(value) or found
                        record.clear()
                        state = "separator"
                continue
            if byte in b" \r\n\t":
                continue
            if state == "start" and byte == ord("["):
                state = "first"
            elif state in {"first", "value"} and byte == ord("{"):
                record.append(byte)
                depth = 1
                state = "record"
            elif state in {"first", "separator"} and byte == ord("]"):
                state = "end"
            elif state == "separator" and byte == ord(","):
                state = "value"
            else:
                raise ValueError("activation_approval_history_invalid")
