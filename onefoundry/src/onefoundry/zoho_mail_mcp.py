"""One bounded, authenticated MCP call using the existing SDK; no write retries."""

from __future__ import annotations

import json
from typing import Any, Protocol

import anyio
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from onefoundry.zoho_mail_contracts import endpoint
from onefoundry.zoho_mail_privacy import private_call


class ZohoMcpError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _failure_code(error: BaseException) -> str:
    if isinstance(error, ZohoMcpError):
        return error.code
    if isinstance(error, BaseExceptionGroup):
        codes = [_failure_code(child) for child in error.exceptions]
        return next(
            (code for code in codes if code != "connection_or_authorization_failed"),
            "connection_or_authorization_failed",
        )
    if isinstance(error, TimeoutError):
        return "request_timeout"
    return "connection_or_authorization_failed"


class ZohoClient(Protocol):
    def call(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class _BoundedStream(httpx2.AsyncByteStream):
    def __init__(self, stream: httpx2.AsyncByteStream, limit: int) -> None:
        self.stream, self.limit = stream, limit

    async def __aiter__(self) -> Any:
        size = 0
        async for chunk in self.stream:
            size += len(chunk)
            if size > self.limit:
                raise ZohoMcpError("response_too_large")
            yield chunk

    async def aclose(self) -> None:
        await self.stream.aclose()


def provider_data(result: dict[str, Any]) -> Any:
    """Reject both MCP failures and Zoho's nested failure-with-HTTP-success."""
    # Zoho tools can return the API envelope directly or a success/data wrapper.
    envelope = result if isinstance(result.get("status"), dict) else result.get("data")
    if not isinstance(envelope, dict):
        raise ZohoMcpError("invalid_response")
    status = envelope.get("status")
    if isinstance(status, dict) and status.get("code") in {401, 403}:
        raise ZohoMcpError("authentication_failed")
    if result.get("status") == "failure":
        raise ZohoMcpError("provider_rejected")
    if isinstance(status, dict) and status.get("code") != 200:
        raise ZohoMcpError("provider_rejected")
    if "data" not in envelope or not isinstance(status, dict):
        raise ZohoMcpError("invalid_response")
    return envelope["data"]


class HostedZohoClient:
    def __init__(self, url: str, token: str, timeout: float, max_bytes: int) -> None:
        self.url, self.token = endpoint(url), token
        self.timeout, self.max_bytes = timeout, max_bytes

    def call(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return anyio.run(self._call, operation, arguments)
        except ZohoMcpError:
            raise
        except Exception as error:
            # ExceptionGroup and SDK errors may embed tokens or the keyed URL.
            raise ZohoMcpError(_failure_code(error)) from None

    async def _call(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        marker = private_call.set(True)

        async def bound(response: httpx2.Response) -> None:
            if response.status_code in {401, 403}:
                raise ZohoMcpError("authentication_failed")
            if 300 <= response.status_code < 400:
                raise ZohoMcpError("redirect_rejected")
            if not isinstance(response.stream, httpx2.AsyncByteStream):
                raise ZohoMcpError("invalid_response")
            response.stream = _BoundedStream(response.stream, self.max_bytes)

        try:
            with anyio.fail_after(self.timeout):
                async with create_mcp_http_client(
                    headers={"Authorization": f"Bearer {self.token}"},
                    timeout=httpx2.Timeout(self.timeout),
                ) as http:
                    http.event_hooks["response"].append(bound)
                    async with streamable_http_client(
                        self.url, http_client=http, terminate_on_close=False
                    ) as (reader, writer):
                        async with ClientSession(
                            reader, writer, read_timeout_seconds=self.timeout
                        ) as session:
                            await session.initialize()
                            listed = await session.list_tools()
                            if listed.next_cursor or len(listed.tools) > 100:
                                raise ZohoMcpError("tool_contract_changed")
                            # Select a reviewed operation from actual discovery, never an agent-supplied name.
                            matches = [
                                tool
                                for tool in listed.tools
                                if tool.name.lower() in {operation.lower(), f"zohomail_{operation}".lower()}
                            ]
                            if len(matches) != 1:
                                raise ZohoMcpError("tool_unavailable")
                            tool = matches[0]
                            body = arguments.get("body", {})
                            if body.get("mode") == "draft":
                                properties = tool.input_schema.get("properties", {})
                                modes = properties.get("body", {}).get("properties", {}).get("mode", {})
                                if "draft" not in modes.get("enum", []):
                                    raise ZohoMcpError("draft_not_supported")
                            result = await session.call_tool(tool.name, arguments)
                            if result.is_error:
                                raise ZohoMcpError("provider_rejected")
                            value = result.structured_content
                            if value is None:
                                blocks = [item.text for item in result.content if item.type == "text"]
                                if len(blocks) != 1:
                                    raise ZohoMcpError("invalid_response")
                                value = json.loads(blocks[0])
                            if not isinstance(value, dict):
                                raise ZohoMcpError("invalid_response")
                            encoded = json.dumps(value, allow_nan=False).encode()
                            if len(encoded) > self.max_bytes:
                                raise ZohoMcpError("response_too_large")
                            # Credentials returned by a compromised provider never reach callers.
                            if self.token in encoded.decode() or self.url in encoded.decode():
                                raise ZohoMcpError("protected_response")
                            provider_data(value)
                            return value
        finally:
            private_call.reset(marker)
