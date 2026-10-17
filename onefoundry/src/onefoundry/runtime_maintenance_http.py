"""Fence HTTP mutations after FastAPI validates inputs and resolves dependencies."""

from __future__ import annotations

import inspect
from contextlib import ExitStack
from functools import wraps
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from onefoundry.gateway_errors import Conflict
from onefoundry.runtime_maintenance import RuntimeMaintenance


def guarded_endpoint(endpoint: Any, gate: RuntimeMaintenance) -> Any:
    if inspect.iscoroutinefunction(endpoint):

        @wraps(endpoint)
        async def asynchronous(*args: Any, **kwargs: Any) -> Any:
            context = gate.guard()
            try:
                await run_in_threadpool(context.__enter__)
            except Conflict as exc:
                return JSONResponse({"detail": str(exc)}, status_code=409)
            except SQLAlchemyError:
                return JSONResponse(
                    {"detail": "Runtime write readiness could not be confirmed."}, status_code=503
                )
            try:
                return await endpoint(*args, **kwargs)
            finally:
                await run_in_threadpool(context.__exit__, None, None, None)

        return asynchronous

    @wraps(endpoint)
    def synchronous(*args: Any, **kwargs: Any) -> Any:
        context = ExitStack()
        try:
            context.enter_context(gate.guard())
        except Conflict as exc:
            return JSONResponse({"detail": str(exc)}, status_code=409)
        except SQLAlchemyError:
            return JSONResponse(
                {"detail": "Runtime write readiness could not be confirmed."}, status_code=503
            )
        with context:
            return endpoint(*args, **kwargs)

    return synchronous


def attach_write_guards(router: APIRouter, gate: RuntimeMaintenance) -> None:
    """Wrap validated endpoint calls before FastAPI includes their router.

    Included routers can build their effective dependency graph lazily from the
    endpoint. Wrapping both owners before inclusion preserves that behavior.
    """
    for route in router.routes:
        if not isinstance(route, APIRoute) or not (route.methods or set()).intersection(
            {"POST", "PUT", "PATCH", "DELETE"}
        ):
            continue
        if "/extension-activation/" in route.path and route.path.endswith(
            ("/quiesce", "/configure", "/release")
        ):
            continue  # These exact approval-bound owners acquire the exclusive guard themselves.
        if "/capability-setup/" in route.path and route.path.endswith(("/quiesce", "/register", "/release")):
            continue
        route.endpoint = guarded_endpoint(route.endpoint, gate)
        route.dependant.call = route.endpoint
