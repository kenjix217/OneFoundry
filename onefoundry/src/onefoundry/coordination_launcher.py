"""In-app worker for coordination steps that need model credentials.

The credential-free coordinator matches signals and creates drafts. Steps that
call a model or a mail server, such as Chief of Staff triage and mailbox
polling, run here inside the app process, which already owns the protected
credential store. Each step is bounded and never retries an uncertain model call.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from onefoundry import chief_followup, coordination_autostart, coordination_triage
from onefoundry.mail_idle_runtime import MailIdleRuntime
from onefoundry.task_session_attempts import TaskSessionAttempts
from onefoundry.task_session_decisions import TaskSessionDecisions
from onefoundry.task_session_sdk_turn import TaskSessionSdkTurn

if TYPE_CHECKING:
    from onefoundry.document_store import DocumentStore
    from onefoundry.mail_store import MailStore
    from onefoundry.settings import Settings
    from onefoundry.tool_gateway import ToolGateway

LOG = logging.getLogger(__name__)


class CoordinationLauncher:
    def __init__(
        self,
        gateway: ToolGateway,
        settings: Settings,
        mail: MailStore | None = None,
        documents: DocumentStore | None = None,
        maintenance: object | None = None,
    ) -> None:
        self.gateway = gateway
        self.settings = settings
        self.mail = mail
        self.documents = documents
        self.maintenance = maintenance
        self._task: asyncio.Task[None] | None = None
        self._team_task: asyncio.Task[None] | None = None
        self._mail_idle_task: asyncio.Task[None] | None = None

    def step(self) -> bool:
        """Run one bounded step. Returns whether any work was claimed."""
        from onefoundry.runtime_maintenance import RuntimeMaintenance

        if isinstance(self.maintenance, RuntimeMaintenance):
            if self.maintenance.active():
                return False
            with self.maintenance.guard():
                return self._coordination_step()
        return self._coordination_step()

    def _coordination_step(self) -> bool:
        polled = self.mail.poll_next() if self.mail is not None else False
        polled = (self.documents.poll_next() if self.documents is not None else False) or polled
        triaged = coordination_triage.step(self.gateway, self.settings)
        followed_up = chief_followup.step(self.gateway, self.settings)
        launched = coordination_autostart.step(self.gateway, self.settings)
        expired = TaskSessionAttempts(self.gateway).expire_stale(self.settings.onefoundry_max_elapsed_seconds)
        team_decision = TaskSessionDecisions(self.gateway).dispatch_next()
        return polled or triaged or followed_up or launched or expired > 0 or team_decision is not None

    def team_step(self) -> bool:
        """Queue due Chief decisions, then run at most one claimed team turn."""
        from onefoundry.runtime_maintenance import RuntimeMaintenance

        if isinstance(self.maintenance, RuntimeMaintenance):
            if self.maintenance.active():
                return False
            with self.maintenance.guard():
                return self._team_step()
        return self._team_step()

    def _team_step(self) -> bool:
        from onefoundry.capability_setup_resume import step as setup_step
        from onefoundry.self_extension_continuation import step as extension_step
        from onefoundry.self_extension_dispatch import step as dispatch_extension
        from onefoundry.self_extension_release import step as release_step
        from onefoundry.self_extension_resume import step as resume_step

        extension_progressed = (
            setup_step(self.gateway)
            or release_step(self.gateway)
            or dispatch_extension(self.gateway)
            or extension_step(self.gateway)
            or resume_step(self.gateway)
        )
        expired = TaskSessionAttempts(self.gateway).expire_stale(self.settings.onefoundry_max_elapsed_seconds)
        team_decision = TaskSessionDecisions(self.gateway).dispatch_next()
        settled = TaskSessionSdkTurn(self.gateway, self.settings).run_next()
        return extension_progressed or expired > 0 or team_decision is not None or settled is not None

    async def start(self) -> None:
        if self.settings.team_worker_enabled and self._team_task is None:
            self._team_task = asyncio.create_task(self._team_loop(), name="team-worker")
        if self.settings.coordination_mode == "off" or self._task is not None:
            return
        if self.settings.mail_idle_enabled and self.mail is not None:
            self._mail_idle_task = asyncio.create_task(
                MailIdleRuntime(self.mail, self.maintenance).run(), name="mail-idle"
            )
        self._task = asyncio.create_task(self._loop(), name="coordination-launcher")

    async def stop(self) -> None:
        for task in (self._task, self._team_task, self._mail_idle_task):
            if task is None:
                continue
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._task = None
        self._team_task = None
        self._mail_idle_task = None

    async def _team_loop(self) -> None:
        # Separate from the coordination loop so a long agent turn never delays mail or triage.
        while True:
            try:
                progressed = await asyncio.to_thread(self.team_step)
            except Exception:  # The saved attempt state is reconciled on a later pass.
                LOG.exception("team worker step failed")
                progressed = False
            if not progressed:
                await asyncio.sleep(self.settings.onefoundry_coordinator_poll_seconds)

    async def _loop(self) -> None:
        while True:
            try:
                progressed = await asyncio.to_thread(self.step)
            except Exception:  # A failed step leaves its saved state for the next pass.
                LOG.exception("coordination launcher step failed")
                progressed = False
            if not progressed:
                await asyncio.sleep(self.settings.onefoundry_coordinator_poll_seconds)
