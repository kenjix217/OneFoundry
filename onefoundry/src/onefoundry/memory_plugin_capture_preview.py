"""Bounded ephemeral receipts bind a submission to the exact selected preview."""

import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Callable
from threading import Lock
from typing import Any

from onefoundry.memory_plugin_contracts import MemoryCapture


def selection_digest(selection: MemoryCapture, actor_id: str) -> str:
    payload = selection.model_dump(mode="json", exclude={"preview_sha256"})
    return hashlib.sha256(
        json.dumps({"actor_id": actor_id, "selection": payload}, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class CapturePreviews:
    def __init__(self, seconds: int, capacity: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.seconds, self.capacity, self.clock = seconds, capacity, clock
        self._lock = Lock()
        self._actors: dict[str, OrderedDict[str, tuple[str, float]]] = {}

    def preview(self, actor_id: str, selection: MemoryCapture) -> dict[str, Any]:
        digest = selection_digest(selection, actor_id)
        identity = str(selection.client_capture_id)
        with self._lock:
            receipts = self._actors.setdefault(actor_id, OrderedDict())
            receipts.pop(identity, None)
            receipts[identity] = (digest, self.clock() + self.seconds)
            while len(receipts) > self.capacity:
                receipts.popitem(last=False)
        return {
            "selection": selection.model_dump(mode="json"),
            "preview_sha256": digest,
            "valid_for_seconds": self.seconds,
            "submission_requires_confirmation": True,
            "review_required": True,
            "source_archive_retained_on_rejection": True,
            "company_content_stored": False,
        }

    def matches(self, actor_id: str, selection: MemoryCapture, digest: str) -> bool:
        with self._lock:
            receipts = self._actors.get(actor_id)
            receipt = None if receipts is None else receipts.get(str(selection.client_capture_id))
            return bool(
                receipt is not None
                and self.clock() < receipt[1]
                and digest == receipt[0] == selection_digest(selection, actor_id)
            )
