"""The Gateway recovers remote attempts even without a Python worker."""

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest

from onefoundry import gateway_extension_worker_runtime as runtime
from onefoundry.settings import Settings
from onefoundry.tool_gateway import ToolGateway


def test_remote_only_reconciliation_never_prepares_a_worker_spool(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = cast(ToolGateway, SimpleNamespace(live_text_settings=Settings(db_password="test-only")))
    recovered: list[ToolGateway] = []
    monkeypatch.setattr(runtime, "recover_remote_attempts", recovered.append)
    monkeypatch.setattr(runtime, "prepare_spool", lambda _settings: pytest.fail("worker is disabled"))
    runtime.reconcile_once(gateway)
    assert recovered == [gateway]


def test_remote_recovery_background_runs_and_stops_without_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = cast(ToolGateway, SimpleNamespace(live_text_settings=Settings(db_password="test-only")))
    worker = runtime.ExtensionWorkerRuntime(gateway)
    recovered: list[ToolGateway] = []

    def recover(value: ToolGateway) -> None:
        recovered.append(value)
        worker.stop_event.set()

    async def run() -> None:
        await worker.start()
        assert worker.task is not None
        await worker.task
        await worker.stop()

    monkeypatch.setattr(runtime, "reconcile_once", recover)
    asyncio.run(run())
    assert worker.task is None
    assert recovered == [gateway]


def test_unconfigured_gateway_does_not_start_a_background_task() -> None:
    gateway = cast(ToolGateway, SimpleNamespace(live_text_settings=None))
    worker = runtime.ExtensionWorkerRuntime(gateway)
    asyncio.run(worker.start())
    assert worker.task is None
