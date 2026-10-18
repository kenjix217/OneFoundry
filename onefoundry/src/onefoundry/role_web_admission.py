"""Durable admission around bounded Researcher web requests.

Network I/O occurs after the intent transaction commits and before a fresh
permission check. No database lock is held while contacting a web source.
"""

from __future__ import annotations

import hashlib
import json
from time import monotonic
from typing import TYPE_CHECKING, Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from onefoundry.gateway_errors import Conflict
from onefoundry.onefoundry_budget import linked_workflow, mark_block, reserve, settle
from onefoundry.public_web_client import (
    PublicWebClient,
    PublicWebRejected,
    PublicWebTimedOut,
    PublicWebUnavailable,
)
from onefoundry.role_tool_admission import (
    RunContext,
    ToolDenied,
    ToolDependencyFailure,
    ToolInputError,
    ToolOutcomeUnknown,
    ToolOutputRejected,
    ToolPolicyRejected,
    ToolTimedOut,
    _argument_bytes,
    _context,
    _db_bounds,
    _event,
    _prior_attempt,
    _reject_sensitive_output,
    _researcher,
)
from onefoundry.role_web_tools import FETCH_TOOL_ID, WEB_TOOLS

if TYPE_CHECKING:
    from onefoundry.tool_gateway import ToolGateway


_WEB_POLICY_STEPS = {
    "web_policy_rejected": "Inspect the public-web policy and source before retrying with a new call.",
    "web_target_invalid": "Use an exact public HTTPS URL returned by this run's search.",
    "web_target_credentials": "Choose a public URL without embedded credentials.",
    "web_target_not_public": "Choose a public HTTPS source; private destinations are blocked.",
    "web_target_peer_changed": "The resolved peer changed; inspect the source before any new call.",
    "web_limits_invalid": "Inspect the configured public-web limits.",
    "web_search_endpoint_invalid": "Inspect the configured trusted search endpoint.",
    "web_search_query_invalid": "Use public search terms within the query bound.",
    "web_search_response_invalid": "Inspect the configured search service's JSON response contract.",
    "web_response_too_large": "The source exceeded the configured response-byte limit.",
    "web_response_compressed": "The source returned compressed content, which this reader blocks.",
    "web_response_content_type": "Choose a public HTML or plain-text source.",
    "web_content_sensitive": "Secret screening blocked the source; do not route it through another read.",
}
_WEB_DEPENDENCY_STEPS = {
    "web_dependency_unavailable": "Inspect the configured public-web dependency before another call.",
    "web_dns_unavailable": "Public DNS resolution failed; inspect the source or network dependency.",
    "web_network_unavailable": "The public HTTPS connection failed; inspect network readiness.",
    "web_http_status_unavailable": "The source returned a non-success HTTP status.",
    "web_worker_unavailable": "The bounded public-web worker could not start; inspect local runtime health.",
    "web_search_not_configured": "Configure the trusted public search endpoint before searching.",
}


class WebPolicyFailure(ToolPolicyRejected):
    """Policy rejection with a fixed, content-free owner code."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _WEB_POLICY_STEPS else "web_policy_rejected"
        super().__init__(self.code)

    def as_correction(self) -> dict[str, str]:
        return {
            "reason": "tool_policy_rejected",
            "owner_reason": self.code,
            "next_step": _WEB_POLICY_STEPS[self.code],
        }


class WebDependencyFailure(ToolDependencyFailure):
    """Dependency failure with a fixed, content-free owner code."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _WEB_DEPENDENCY_STEPS else "web_dependency_unavailable"
        super().__init__(self.code)

    def as_correction(self) -> dict[str, str]:
        return {
            "reason": "tool_dependency_failure",
            "owner_reason": self.code,
            "next_step": _WEB_DEPENDENCY_STEPS[self.code],
        }


def saved_web_correction(code: str, tool_id: str) -> dict[str, str] | None:
    """Recreate a first-call correction from a saved reason without raw source data."""
    if tool_id not in WEB_TOOLS:
        return None
    if code in _WEB_POLICY_STEPS:
        return WebPolicyFailure(code).as_correction()
    if code in _WEB_DEPENDENCY_STEPS:
        return WebDependencyFailure(code).as_correction()
    return None


def _admitted(context: RunContext, tool_id: str) -> bool:
    return _researcher(context) and tool_id in context.snapshot_tools & context.current_tools


def _checked_result(result: object, tool_id: str, max_bytes: int) -> tuple[dict[str, Any], bytes]:
    if not isinstance(result, dict):
        raise ToolOutputRejected("web tool result must be a JSON object")
    try:
        WEB_TOOLS[tool_id].validate_output(result)
    except ValueError:
        raise ToolOutputRejected("web tool result failed its registered contract") from None
    _reject_sensitive_output(result)
    encoded = json.dumps(result, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > max_bytes and tool_id == FETCH_TOOL_ID:
        result, encoded = _fit_fetch_excerpt(result, max_bytes)
    if len(encoded) > max_bytes:
        raise ToolOutputRejected("web tool result exceeds the configured byte limit")
    return result, encoded


def _fit_fetch_excerpt(result: dict[str, Any], max_bytes: int) -> tuple[dict[str, Any], bytes]:
    """Shorten only fetched text so its complete JSON result fits the live envelope."""
    original_text = result["text"]
    if not isinstance(original_text, str):
        raise ToolOutputRejected("web tool result failed its registered contract")
    fitted = {**result, "text": "", "text_truncated": True}
    encoded = json.dumps(fitted, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > max_bytes:
        raise ToolOutputRejected("web tool result exceeds the configured byte limit")
    low, high = 0, len(original_text)
    while low < high:
        midpoint = (low + high + 1) // 2
        fitted["text"] = original_text[:midpoint]
        candidate = json.dumps(fitted, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(candidate) <= max_bytes:
            low, encoded = midpoint, candidate
        else:
            high = midpoint - 1
    fitted["text"] = original_text[:low]
    return fitted, encoded


def _searched_url(connection: Any, project_id: UUID, run_id: UUID, url: str) -> bool:
    """Require an exact URL from a successful search in this run, without storing URLs."""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return bool(
        connection.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM events WHERE project_id=:project_id "
                "AND run_id=:run_id AND event_type='role_tool_finished' "
                "AND payload->>'tool_id'='public_web_search_v1' "
                "AND payload->>'status'='done' "
                "AND (payload->'result_url_sha256') ? :digest)"
            ),
            {"project_id": project_id, "run_id": run_id, "digest": digest},
        )
    )


def invoke_web_tool(
    gateway: ToolGateway,
    project_id: UUID,
    run_id: UUID,
    profile_id: UUID,
    call_id: str,
    tool_id: str,
    arguments: object,
    tool_version: int,
) -> dict[str, Any]:
    """Record intent, fetch without a DB lock, then recheck and receipt."""
    settings = gateway.live_text_settings
    if settings is None:
        raise ToolDenied("web tool settings are unavailable")
    tool = WEB_TOOLS[tool_id]
    deadline = monotonic() + min(settings.live_text_timeout_seconds, settings.public_web_timeout_seconds + 5)
    parsed: dict[str, Any] = {}
    web_reservation_id: UUID | None = None
    denial_reason: str | None = None
    try:
        with gateway.engine.begin() as connection:
            _db_bounds(connection, deadline)
            context = _context(
                connection,
                project_id,
                run_id,
                profile_id,
                lock=True,
                max_items=settings.role_context_max_items,
            )
            prior = _prior_attempt(connection, project_id, run_id, call_id)
            if prior == "role_tool_started":
                raise ToolOutcomeUnknown("web tool outcome is unknown; inspect the run timeline")
            if prior is not None:
                raise ToolDenied("web tool call already attempted; inspect the run timeline")
            available = (
                settings.public_web_enabled
                and _admitted(context, tool_id)
                and tool.version == tool_version
                and (tool_id != "public_web_search_v1" or settings.public_web_search_endpoint)
            )
            if not available:
                denial_reason = "version_mismatch" if tool.version != tool_version else "not_admitted"
                _event(
                    gateway,
                    connection,
                    context,
                    "role_tool_denied",
                    {
                        "call_id": call_id,
                        "tool_id": tool_id,
                        "reason": denial_reason,
                    },
                )
            else:
                try:
                    parsed, encoded = _argument_bytes(
                        arguments, settings.live_text_max_prompt_bytes, tool.parse_arguments
                    )
                except ToolInputError:
                    _event(
                        gateway,
                        connection,
                        context,
                        "role_tool_denied",
                        {"call_id": call_id, "tool_id": tool_id, "reason": "invalid_arguments"},
                    )
                    available = False
                    denial_reason = "invalid_arguments"
                else:
                    if tool_id == "public_page_fetch_v1" and not _searched_url(
                        connection, project_id, run_id, parsed["url"]
                    ):
                        _event(
                            gateway,
                            connection,
                            context,
                            "role_tool_denied",
                            {"call_id": call_id, "tool_id": tool_id, "reason": "source_not_searched"},
                        )
                        available = False
                        denial_reason = "source_not_searched"
                    if available:
                        workflow_id = linked_workflow(connection, project_id, context.work_order_id)
                        if workflow_id is not None:
                            try:
                                reservation = reserve(
                                    connection,
                                    gateway,
                                    project_id,
                                    workflow_id,
                                    f"web:{run_id}:{call_id}",
                                    "public_web_call",
                                )
                            except Conflict as error:
                                reason = (
                                    "usage_unknown"
                                    if str(error).startswith("usage_unknown")
                                    else "budget_exceeded"
                                )
                                mark_block(connection, gateway, project_id, workflow_id, reason)
                                _event(
                                    gateway,
                                    connection,
                                    context,
                                    "role_tool_denied",
                                    {"call_id": call_id, "tool_id": tool_id, "reason": reason},
                                )
                                available = False
                            else:
                                web_reservation_id = reservation["id"]
                    if available:
                        _event(
                            gateway,
                            connection,
                            context,
                            "role_tool_started",
                            {
                                "call_id": call_id,
                                "tool_id": tool_id,
                                "version": tool.version,
                                "arguments_sha256": hashlib.sha256(encoded).hexdigest(),
                                "argument_bytes": len(encoded),
                                "timeout_seconds": settings.public_web_timeout_seconds,
                            },
                        )
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) in ("55P03", "57014"):
            raise ToolTimedOut("web tool admission timed out") from None
        raise ToolDependencyFailure("web tool admission dependency failed") from None
    if not available:
        raise ToolDenied("web tool call was denied", reason=denial_reason)

    result_ready = False
    try:
        with gateway.engine.begin() as connection:
            connection.exec_driver_sql("SET TRANSACTION READ ONLY")
            _db_bounds(connection, deadline)
            current = _context(
                connection,
                project_id,
                run_id,
                profile_id,
                lock=False,
                max_items=settings.role_context_max_items,
            )
            if not _admitted(current, tool_id):
                raise ToolDenied("web tool permission changed before execution")

        result = tool.execute(PublicWebClient(), settings, parsed)

        with gateway.engine.begin() as connection:
            _db_bounds(connection, deadline)
            current = _context(
                connection,
                project_id,
                run_id,
                profile_id,
                lock=True,
                max_items=settings.role_context_max_items,
            )
            if not _admitted(current, tool_id):
                raise ToolDenied("web tool permission changed during execution")
            checked, encoded_result = _checked_result(result, tool_id, settings.live_text_max_output_bytes)
            if monotonic() > deadline:
                raise ToolTimedOut("web tool exceeded its configured timeout")
            digest = hashlib.sha256(encoded_result).hexdigest()
            result_ready = True
            _event(
                gateway,
                connection,
                context,
                "role_tool_finished",
                {
                    "call_id": call_id,
                    "tool_id": tool_id,
                    "status": "done",
                    "result_sha256": digest,
                    "result_bytes": len(encoded_result),
                    **(
                        {
                            "result_url_sha256": [
                                hashlib.sha256(item["url"].encode("utf-8")).hexdigest()
                                for item in checked["results"]
                            ]
                        }
                        if tool_id == "public_web_search_v1"
                        else {}
                    ),
                },
            )
            if web_reservation_id is not None:
                settle(connection, gateway, project_id, web_reservation_id, run_id, "settled", None, None)
    except Exception as error:
        if result_ready:
            raise ToolOutcomeUnknown("web tool outcome could not be recorded") from None
        failure: ToolDenied | ToolOutputRejected | ToolPolicyRejected | ToolTimedOut | ToolDependencyFailure
        if isinstance(error, (ToolDenied, ToolOutputRejected, ToolPolicyRejected, ToolTimedOut)):
            failure = error
        elif isinstance(error, PublicWebRejected):
            failure = WebPolicyFailure(error.code)
        elif isinstance(error, (PublicWebTimedOut, TimeoutError)):
            failure = ToolTimedOut("web tool timed out")
        elif isinstance(error, PublicWebUnavailable):
            failure = WebDependencyFailure(error.code)
        elif isinstance(error, DBAPIError) and getattr(error.orig, "sqlstate", None) in ("55P03", "57014"):
            failure = ToolTimedOut("web tool database check timed out")
        else:
            failure = ToolDependencyFailure("web tool dependency failed")
        reason = (
            "permission_changed"
            if isinstance(failure, ToolDenied)
            else "invalid_result"
            if isinstance(failure, ToolOutputRejected)
            else "policy_rejected"
            if isinstance(failure, ToolPolicyRejected)
            else "timeout"
            if isinstance(failure, ToolTimedOut)
            else "dependency_failure"
        )
        try:
            with gateway.engine.begin() as connection:
                _db_bounds(connection, monotonic() + settings.live_text_timeout_seconds)
                _event(
                    gateway,
                    connection,
                    context,
                    "role_tool_finished",
                    {
                        "call_id": call_id,
                        "tool_id": tool_id,
                        "status": "failed",
                        "reason": reason,
                        **(
                            {"owner_reason": failure.code}
                            if isinstance(failure, (WebPolicyFailure, WebDependencyFailure))
                            else {}
                        ),
                    },
                )
                if web_reservation_id is not None:
                    settle(
                        connection,
                        gateway,
                        project_id,
                        web_reservation_id,
                        run_id,
                        "unknown"
                        if isinstance(failure, (ToolTimedOut, ToolDependencyFailure))
                        else "settled",
                        None,
                        None,
                    )
        except Exception:
            raise ToolOutcomeUnknown("web tool outcome could not be recorded") from None
        raise failure from None
    return {
        "call_id": call_id,
        "tool_id": tool_id,
        "version": tool.version,
        "status": "done",
        "result": checked,
        "result_sha256": digest,
    }
