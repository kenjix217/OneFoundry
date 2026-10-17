"""Small authoring helpers; the host controls admission and isolation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Protocol, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


class ToolContext(Protocol):
    """Context supplied by the host to an admitted tool invocation."""

    @property
    def project_id(self) -> str: ...

    @property
    def actor_id(self) -> str: ...

    @property
    def run_id(self) -> str: ...

    @property
    def profile_id(self) -> str: ...

    @property
    def connections(self) -> ConnectionInvoker: ...


class ConnectionInvoker(Protocol):
    """Request one declared operation through the host Tool Gateway."""

    async def invoke(
        self, *, slot: str, operation: str, arguments: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...


def tool(identifier: str) -> Callable[[F], F]:
    """Label a function for developer inspection. This never grants or runs it."""

    def decorate(function: F) -> F:
        function.__dict__["__onefoundry_tool_id__"] = identifier
        return function

    return decorate
