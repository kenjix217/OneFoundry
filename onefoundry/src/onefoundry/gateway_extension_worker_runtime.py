"""Gateway-side protocol reconciliation. Container execution stays in the host supervisor."""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
from typing import TYPE_CHECKING

from onefoundry.gateway_extension_attempt_recovery import recover_remote_attempts
from onefoundry.gateway_extension_spool import prepare_spool
from onefoundry.gateway_extension_worker_callbacks import process_callback
from onefoundry.gateway_extension_worker_expiry import expire_queued
from onefoundry.gateway_extension_worker_receipts import process_receipt, resume_waiting

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway

logger = logging.getLogger(__name__)


def reconcile_once(gateway: ToolGateway) -> None:
    settings = gateway.live_text_settings
    if settings is None:
        return
    recover_remote_attempts(gateway)
    if not settings.extension_worker_image:
        return
    root = prepare_spool(settings)
    fd = os.open(root / ".gateway.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        expire_queued(gateway, root)
        resume_waiting(gateway, root)
        for path in sorted((root / "callbacks" / "requests").glob("*.json")):
            try:
                process_callback(gateway, root, path)
            except Exception:
                logger.error("extension callback reconciliation failed; inspect the pending receipt")
        for path in sorted((root / "receipts").glob("*.json")):
            try:
                process_receipt(gateway, root, path)
            except Exception:
                logger.error("extension worker receipt reconciliation failed")
        resume_waiting(gateway, root)
    finally:
        os.close(fd)


class ExtensionWorkerRuntime:
    def __init__(self, gateway: ToolGateway) -> None:
        self.gateway = gateway
        self.task: asyncio.Task[None] | None = None
        self.stop_event = asyncio.Event()

    async def start(self) -> None:
        settings = self.gateway.live_text_settings
        if settings is not None:
            if settings.extension_worker_image:
                prepare_spool(settings)
            self.stop_event.clear()
            self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        settings = self.gateway.live_text_settings
        assert settings is not None
        while not self.stop_event.is_set():
            try:
                await asyncio.to_thread(reconcile_once, self.gateway)
            except Exception:
                logger.error("extension worker gateway reconciliation is unavailable")
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=settings.extension_worker_poll_seconds)
            except TimeoutError:
                continue

    async def stop(self) -> None:
        self.stop_event.set()
        if self.task is not None:
            await self.task
            self.task = None
