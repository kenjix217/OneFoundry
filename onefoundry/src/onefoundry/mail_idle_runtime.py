"""Separate notification waits so slow mail servers never delay model turns."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text

from onefoundry.gateway_errors import Conflict, Unavailable
from onefoundry.mail_idle import IdleWait
from onefoundry.mail_imap import MailError

if TYPE_CHECKING:
    from onefoundry.mail_store import MailStore


class MailIdleRuntime:
    def __init__(self, store: MailStore, maintenance: object | None = None) -> None:
        self.store = store
        self.maintenance = maintenance
        self.tasks: dict[UUID, asyncio.Task[None]] = {}

    def eligible(self) -> list[dict[str, Any]]:
        from onefoundry.runtime_maintenance import RuntimeMaintenance

        if isinstance(self.maintenance, RuntimeMaintenance) and self.maintenance.active():
            return []
        with self.store.gateway.engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT a.* FROM mail_accounts a JOIN projects p ON p.id=a.project_id "
                    "WHERE a.enabled AND a.status='ready' AND a.protocol='imap' AND p.status='active' "
                    "AND a.capabilities ? 'IDLE' AND NOT EXISTS(SELECT 1 FROM coordination_controls c "
                    "WHERE c.project_id=a.project_id AND c.paused) ORDER BY a.id LIMIT :limit"
                ),
                {"limit": self.store.settings.mail_max_fetch_per_poll},
            ).mappings()
            return [dict(row) for row in rows]

    async def run(self) -> None:
        try:
            while True:
                rows = await asyncio.to_thread(self.eligible)
                ids = {row["id"] for row in rows}
                for account_id, task in list(self.tasks.items()):
                    if account_id not in ids:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                        del self.tasks[account_id]
                for row in rows:
                    if row["id"] not in self.tasks or self.tasks[row["id"]].done():
                        self.tasks[row["id"]] = asyncio.create_task(self._watch(row))
                await asyncio.sleep(self.store.settings.onefoundry_coordinator_poll_seconds)
        finally:
            for task in self.tasks.values():
                task.cancel()
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)
            self.tasks.clear()

    async def _watch(self, row: dict[str, Any]) -> None:
        wait: IdleWait | None = None
        try:
            config = await asyncio.to_thread(self.store._config, row)
            wait = IdleWait(self.store.mailbox_factory(config), self.store.settings.mail_idle_wait_seconds)
            notified = await asyncio.to_thread(wait.wait)
            if notified:
                await asyncio.to_thread(self._nudge, row)
        except (MailError, Unavailable, Conflict):
            # Periodic polling remains active and owns connection status. Back
            # off this optional optimization without recording server text.
            await asyncio.sleep(self.store.settings.mail_poll_seconds)
        finally:
            if wait is not None:
                wait.cancel()

    def _nudge(self, row: dict[str, Any]) -> None:
        from onefoundry.runtime_maintenance import RuntimeMaintenance

        if isinstance(self.maintenance, RuntimeMaintenance):
            with self.maintenance.guard():
                self._save_nudge(row)
            return
        self._save_nudge(row)

    def _save_nudge(self, row: dict[str, Any]) -> None:
        with self.store.gateway.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE mail_accounts SET last_poll_at=NULL WHERE project_id=:p AND id=:id "
                    "AND enabled AND status='ready' AND version=:version AND poll_lease_until IS NULL "
                    "AND EXISTS(SELECT 1 FROM projects WHERE id=:p AND status='active') "
                    "AND NOT EXISTS(SELECT 1 FROM coordination_controls WHERE project_id=:p AND paused)"
                ),
                {"p": row["project_id"], "id": row["id"], "version": row["version"]},
            )
