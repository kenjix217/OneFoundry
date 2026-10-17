"""Server-owned memory grants and receipts; no arbitrary app or database access."""

from __future__ import annotations

import time
from collections import deque
from threading import Lock
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from onefoundry.company_memory_schemas import CaptureCreate, CaptureOut
from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.memory_plugin_capture_preview import CapturePreviews, selection_digest
from onefoundry.memory_plugin_contracts import MemoryCapture, MemoryCaptureConfirmed, MemoryRead, MemorySearch
from onefoundry.memory_plugin_settings import MemoryGrant, MemoryPluginSettings
from onefoundry.memory_schemas import MemoryOut
from onefoundry.role_sensitive_text import contains_sensitive_text
from onefoundry.tool_gateway import ToolGateway


class MemoryFacadeError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class MemoryPluginGateway:
    def __init__(
        self,
        policy: MemoryPluginSettings,
        gateways: dict[str, ToolGateway],
        *,
        capture_gateways: dict[str, ToolGateway] | None = None,
    ) -> None:
        self.policy, self.gateways = policy, gateways
        self.capture_gateways = capture_gateways or {}
        self.previews = CapturePreviews(policy.capture_preview_seconds, policy.max_calls_per_minute)
        self._lock = Lock()
        self._calls: dict[str, deque[float]] = {}

    def _admit(self, grant: MemoryGrant, project_id: UUID, scope: str | None) -> str | None:
        if grant not in self.policy.grants or project_id not in grant.project_ids:
            raise MemoryFacadeError("authorization_denied")
        if scope == "company" and not grant.company_read:
            raise MemoryFacadeError("authorization_denied")
        now = time.monotonic()
        with self._lock:
            calls = self._calls.setdefault(grant.actor_id, deque())
            while calls and calls[0] <= now - 60:
                calls.popleft()
            if len(calls) >= self.policy.max_calls_per_minute:
                raise MemoryFacadeError("rate_limited")
            calls.append(now)
        return scope if grant.company_read else "project"

    def invoke(self, grant: MemoryGrant, action: str, arguments: dict[str, object]) -> dict[str, Any]:
        try:
            models: dict[str, type[BaseModel]] = {
                "memory_search": MemorySearch,
                "memory_read": MemoryRead,
                "memory_capture": MemoryCaptureConfirmed
                if self.policy.auth_mode == "api_key"
                else MemoryCapture,
                "memory_capture_preview": MemoryCapture,
            }
            model = models[action]
            parsed = model.model_validate(arguments)
            if not isinstance(parsed, (MemorySearch, MemoryRead, MemoryCapture)):
                raise MemoryFacadeError("invalid_input")
            data = parsed
        except (ValidationError, KeyError):
            raise MemoryFacadeError("invalid_input") from None
        scope = self._admit(grant, data.project_id, data.scope)
        if isinstance(data, MemorySearch) and data.limit > self.policy.max_results:
            raise MemoryFacadeError("limit_exceeded")
        if isinstance(data, MemoryCapture) and (
            not grant.capture or (data.scope == "company" and not grant.company_capture)
        ):
            raise MemoryFacadeError("authorization_denied")
        gateway = self.gateways[grant.actor_id]
        if isinstance(data, MemoryCaptureConfirmed):
            if not self.previews.matches(grant.actor_id, data, data.preview_sha256):
                raise MemoryFacadeError("capture_preview_required")
            if grant.actor_id not in self.capture_gateways:
                raise MemoryFacadeError("dependency_unavailable")
            gateway = self.capture_gateways[grant.actor_id]
        call_id = uuid4()
        try:
            self._receipt(gateway, data.project_id, call_id, action, "started")
            result = (
                self.previews.preview(grant.actor_id, data)
                if action == "memory_capture_preview" and isinstance(data, MemoryCapture)
                else self._execute(gateway, data, scope)
            )
            if isinstance(data, MemoryCaptureConfirmed):
                result["selection_sha256"] = selection_digest(data, grant.actor_id)
            self._receipt(
                gateway, data.project_id, call_id, action, "finished", result_count=result.get("count")
            )
            return result
        except (NotFound, Conflict, Unavailable, SQLAlchemyError, MemoryFacadeError) as failure:
            code = (
                "not_found"
                if isinstance(failure, NotFound)
                else "conflict"
                if isinstance(failure, Conflict)
                else "dependency_unavailable"
                if isinstance(failure, (SQLAlchemyError, Unavailable))
                else failure.code
            )
            try:
                self._receipt(gateway, data.project_id, call_id, action, "failed", code=code)
            except (SQLAlchemyError, NotFound):
                raise MemoryFacadeError("receipt_unavailable") from None
            raise MemoryFacadeError(code) from None

    @staticmethod
    def _execute(
        gateway: ToolGateway, data: MemorySearch | MemoryRead | MemoryCapture, scope: str | None
    ) -> dict[str, Any]:
        if isinstance(data, MemorySearch):
            rows = gateway.retrieve_memory(data.project_id, data.query, data.limit, scope)
            items = [MemoryOut.model_validate(row).model_dump(mode="json") for row in rows]
            if any(contains_sensitive_text(item["text"]) for item in items):
                raise MemoryFacadeError("content_unavailable")
            return {"items": items, "count": len(items), "current_only": True}
        if isinstance(data, MemoryRead):
            item = MemoryOut.model_validate(gateway.read_memory(data.project_id, data.memory_id, scope=scope))
            if contains_sensitive_text(item.text):
                raise MemoryFacadeError("content_unavailable")
            return {"item": item.model_dump(mode="json"), "count": 1, "current_only": True}
        capture = CaptureCreate.model_validate(data.model_dump(exclude={"project_id", "preview_sha256"}))
        receipt = CaptureOut.model_validate(gateway.capture_company_memory(data.project_id, capture))
        return {"capture": receipt.model_dump(mode="json"), "review_required": True}

    @staticmethod
    def _receipt(
        gateway: ToolGateway,
        project_id: UUID,
        call_id: UUID,
        action: str,
        phase: str,
        *,
        code: str | None = None,
        result_count: Any = None,
    ) -> None:
        with gateway.engine.begin() as connection:
            gateway._require_project(connection, project_id)
            gateway._event(
                connection,
                project_id,
                None,
                None,
                "memory_facade_call",
                {
                    "call_id": str(call_id),
                    "action": action,
                    "phase": phase,
                    "reason_code": code,
                    "result_count": result_count,
                },
            )
