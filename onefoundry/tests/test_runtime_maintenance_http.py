"""HTTP write fencing survives included routers and preserves boundary errors."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from onefoundry.gateway_errors import Conflict
from onefoundry.runtime_maintenance import RuntimeMaintenance
from onefoundry.runtime_maintenance_http import attach_write_guards


class Input(BaseModel):
    count: int


class Gate:
    def __init__(self) -> None:
        self.active = False
        self.calls = 0

    @contextmanager
    def guard(self) -> Iterator[None]:
        self.calls += 1
        if self.active:
            raise Conflict("App update in progress.")
        yield


@pytest.mark.parametrize("asynchronous", [False, True])
def test_included_router_blocks_valid_writes_after_input_validation(asynchronous: bool) -> None:
    app, router, gate = FastAPI(), APIRouter(prefix="/company"), Gate()
    effects: list[int] = []

    def save(value: Input) -> dict[str, int]:
        effects.append(value.count)
        return {"count": value.count}

    async def save_async(value: Input) -> dict[str, int]:
        return save(value)

    router.add_api_route("/items", save_async if asynchronous else save, methods=["POST"])
    router.add_api_route("/items", lambda: effects, methods=["GET"])
    attach_write_guards(router, cast(RuntimeMaintenance, gate))
    app.include_router(router)
    with TestClient(app) as client:
        assert client.post("/company/items", json={"count": 2}).json() == {"count": 2}
        gate.active = True
        blocked = client.post("/company/items", json={"count": 3})
        assert blocked.status_code == 409 and "update in progress" in blocked.text
        assert effects == [2]
        calls = gate.calls
        assert client.post("/company/items", json={"count": "invalid"}).status_code == 422
        assert gate.calls == calls
        assert client.get("/company/items").json() == [2]
        assert gate.calls == calls
        gate.active = False
        assert client.post("/company/items", json={"count": 4}).status_code == 200
        assert effects == [2, 4]


def test_activation_lease_owners_keep_their_exclusive_guard() -> None:
    app, router, gate = FastAPI(), APIRouter(), Gate()
    gate.active = True
    for action in ("quiesce", "configure", "release"):
        router.add_api_route(
            f"/extension-activation/approved/{action}", lambda: {"handled": True}, methods=["POST"]
        )
    attach_write_guards(router, cast(RuntimeMaintenance, gate))
    app.include_router(router)
    with TestClient(app) as client:
        for action in ("quiesce", "configure", "release"):
            assert client.post(f"/extension-activation/approved/{action}").json() == {"handled": True}
    assert gate.calls == 0


def test_endpoint_database_failure_keeps_its_owner_identity() -> None:
    app, router, gate = FastAPI(), APIRouter(), Gate()

    @router.post("/items")
    def owner_failure() -> None:
        raise SQLAlchemyError("fixture owner failure")

    attach_write_guards(router, cast(RuntimeMaintenance, gate))
    app.include_router(router)
    with TestClient(app) as client:
        with pytest.raises(SQLAlchemyError, match="fixture owner failure"):
            client.post("/items")
