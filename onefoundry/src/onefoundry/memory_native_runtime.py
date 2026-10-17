"""Bounded, serialized native embedding subprocess with stdio and no inherited secrets."""

import hashlib
import json
import os
import select
import stat
import subprocess
import threading
import time
from pathlib import Path
from types import TracebackType
from typing import Any
from uuid import uuid4

from onefoundry.gateway_errors import Unavailable
from onefoundry.memory_embedding_settings import EmbeddingConfiguration

# One inference process at a time per application worker; queueing consumes its deadline.
_INFERENCE_LOCK = threading.Lock()


def verify_asset(value: str, expected: str, *, executable: bool = False) -> Path:
    path = Path(value)
    try:
        info = path.lstat()
        if (
            not path.is_absolute()
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid not in (0, os.getuid())
            or info.st_mode & 0o022
            or (executable and not os.access(path, os.X_OK))
        ):
            raise ValueError("unsafe asset")
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise ValueError("changed asset")
    except (OSError, ValueError):
        raise Unavailable("memory_embedding_asset_invalid") from None
    return path


class NativeSession:
    """DistrictAI's newline-JSON native protocol, isolated from its application code."""

    def __init__(self, settings: EmbeddingConfiguration) -> None:
        self.settings = settings
        self.deadline = time.monotonic() + settings.memory_embedding_timeout_seconds
        self.process: subprocess.Popen[bytes] | None = None
        self.locked = False

    def __enter__(self) -> "NativeSession":
        try:
            self.locked = _INFERENCE_LOCK.acquire(timeout=self._remaining())
            if not self.locked:
                raise Unavailable("memory_embedding_busy")
            runtime = verify_asset(
                self.settings.memory_embedding_runtime_path,
                self.settings.memory_embedding_runtime_sha256,
                executable=True,
            )
            model = verify_asset(
                self.settings.memory_embedding_model_path, self.settings.memory_embedding_model_sha256
            )
            self._remaining()
            self.process = subprocess.Popen(  # noqa: S603 - explicit checksum-verified operator asset
                [str(runtime)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env={"PATH": os.defpath, "LANG": "C.UTF-8"},
                start_new_session=True,
            )
            reply = self.exchange(
                "load",
                {
                    "model_path": str(model),
                    "model": self.settings.memory_embedding_model,
                    "context_length": self.settings.memory_embedding_context_length,
                    "gpu_layers": 0,
                    "rope_scaling": "",
                    "rope_freq_scale": 0.0,
                },
            )
            if (
                reply.get("model") != self.settings.memory_embedding_model
                or reply.get("provider") != "native"
                or reply.get("dimensions") != self.settings.memory_embedding_dimensions
                or reply.get("context_length") != self.settings.memory_embedding_context_length
            ):
                raise Unavailable("memory_embedding_model_mismatch")
            return self
        except OSError:
            self.close()
            raise Unavailable("memory_embedding_unavailable") from None
        except BaseException:
            self.close()
            raise

    def _remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise Unavailable("memory_embedding_timeout")
        return remaining

    def exchange(self, kind: str, body: dict[str, object]) -> dict[str, Any]:
        process = self.process
        if process is None or process.stdin is None or process.stdout is None:
            raise Unavailable("memory_embedding_unavailable")
        identity = str(uuid4())
        outbound = memoryview(json.dumps({**body, "type": kind, "request_id": identity}).encode() + b"\n")
        received = bytearray()
        try:
            while True:
                readable, writable, _ = select.select(
                    [process.stdout], [process.stdin] if outbound else [], [], self._remaining()
                )
                if writable:
                    sent = os.write(process.stdin.fileno(), outbound[:4096])
                    outbound = outbound[sent:]
                if readable:
                    chunk = os.read(process.stdout.fileno(), 4096)
                    if not chunk:
                        raise Unavailable("memory_embedding_unavailable")
                    received.extend(chunk)
                    if len(received) > self.settings.memory_embedding_max_response_bytes:
                        raise Unavailable("memory_embedding_response_invalid")
                    if b"\n" in received:
                        if outbound or not received.endswith(b"\n") or received.count(b"\n") != 1:
                            raise Unavailable("memory_embedding_response_invalid")
                        reply = json.loads(received)
                        if (
                            not isinstance(reply, dict)
                            or reply.get("request_id") != identity
                            or reply.get("type") != kind
                            or reply.get("ok") is not True
                        ):
                            raise Unavailable("memory_embedding_response_invalid")
                        return reply
        except (OSError, ValueError):
            raise Unavailable("memory_embedding_unavailable") from None

    def close(self) -> None:
        if self.process is not None:
            # The native runtime owns no children; kill this owned worker to cancel inference.
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait()
            if self.process.stdin is not None:
                self.process.stdin.close()
            if self.process.stdout is not None:
                self.process.stdout.close()
            self.process = None
        if self.locked:
            _INFERENCE_LOCK.release()
            self.locked = False

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.close()
