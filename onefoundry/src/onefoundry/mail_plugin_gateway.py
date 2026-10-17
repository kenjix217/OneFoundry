"""OneFoundry-owned mail read actions, scoped resolution, bounds, and event receipts."""

from __future__ import annotations

import base64
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict
from threading import BoundedSemaphore
from typing import Any, Literal, TypeVar
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from onefoundry.gateway_errors import Conflict, NotFound, Unavailable
from onefoundry.mail_imap import MailConfig, MailError
from onefoundry.mail_plugin_contracts import MailRead, MailSearch, MessageIds, mailbox_binding
from onefoundry.mail_plugin_imap import SearchMailbox
from onefoundry.mail_plugin_settings import MailPluginSettings
from onefoundry.mail_store import MailStore
from onefoundry.model_secrets import ModelSecretStore, SecretStoreError
from onefoundry.tool_gateway import ToolGateway

ResultT = TypeVar("ResultT")
MailboxFactory = Callable[[MailConfig, MailPluginSettings, MessageIds], SearchMailbox]


class MailPluginGateway:
    def __init__(
        self,
        gateway: ToolGateway,
        store: MailStore,
        policy: MailPluginSettings,
        ids: MessageIds,
        *,
        factory: MailboxFactory = SearchMailbox,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if gateway.actor_id != policy.actor_id:
            raise MailError("actor_binding_invalid")
        if ids.project_id != policy.project_id or ids.account_id != policy.account_id:
            raise MailError("id_binding_invalid")
        self.gateway, self.store, self.policy, self.ids = gateway, store, policy, ids
        self.factory, self.clock = factory, clock
        self._active = BoundedSemaphore(1)
        self._calls: deque[float] = deque()

    def _audit(self, payload: dict[str, Any]) -> None:
        try:
            with self.gateway.engine.begin() as connection:
                self.gateway._event(
                    connection, self.policy.project_id, None, None, "mail_plugin_action", payload
                )
        except SQLAlchemyError:
            raise MailError("audit_unavailable") from None

    def _config(self, folder: str) -> MailConfig:
        if folder not in self.policy.folders:
            raise MailError("folder_denied")
        try:
            return self.store.read_only_config(
                self.policy.project_id,
                self.policy.account_id,
                host=self.policy.imap_host,
                port=self.policy.imap_port,
                username=self.policy.username,
                folder=folder,
            )
        except NotFound:
            raise MailError("account_unavailable") from None
        except Conflict:
            raise MailError("account_binding_denied") from None
        except Unavailable:
            raise MailError("credential_unavailable") from None
        except SQLAlchemyError:
            raise MailError("database_unavailable") from None

    def check_ready(self) -> None:
        self._config(self.policy.folders[0])

    def _run(self, action: Literal["mail_search", "mail_read"], operation: Callable[[], ResultT]) -> ResultT:
        request_id, started = str(uuid4()), self.clock()
        receipt: dict[str, Any] = {
            "request_id": request_id,
            "action": action,
            "account_id": str(self.policy.account_id),
        }
        if not self._active.acquire(blocking=False):
            self._audit({**receipt, "phase": "denied", "code": "busy"})
            raise MailError("busy")
        try:
            while self._calls and self._calls[0] <= started - 60:
                self._calls.popleft()
            if len(self._calls) >= self.policy.max_calls_per_minute:
                self._audit({**receipt, "phase": "denied", "code": "rate_limited"})
                raise MailError("rate_limited")
            self._calls.append(started)
            self._audit({**receipt, "phase": "started"})
            try:
                result = operation()
            except MailError as failure:
                self._audit(
                    {
                        **receipt,
                        "phase": "finished",
                        "outcome": "failed",
                        "code": failure.code,
                        "elapsed_ms": int((self.clock() - started) * 1000),
                    }
                )
                raise
            except BaseException:
                self._audit({**receipt, "phase": "finished", "outcome": "unknown", "code": "interrupted"})
                raise
            self._audit(
                {
                    **receipt,
                    "phase": "finished",
                    "outcome": "done",
                    "elapsed_ms": int((self.clock() - started) * 1000),
                }
            )
            return result
        finally:
            self._active.release()

    def search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            try:
                query = MailSearch.model_validate(arguments)
            except ValidationError:
                raise MailError("invalid_input") from None
            config = self._config(query.folder)
            return asdict(self.factory(config, self.policy, self.ids).search(query))

        return self._run("mail_search", operation)

    def read(self, arguments: dict[str, Any]) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            try:
                request = MailRead.model_validate(arguments)
            except ValidationError:
                raise MailError("invalid_input") from None
            binding = mailbox_binding(self.policy.imap_host, self.policy.imap_port, self.policy.username)
            folder, validity, uid = self.ids.decode(request.message_id, binding, self.policy.folders)
            config = self._config(folder)
            return asdict(self.factory(config, self.policy, self.ids).read(request.message_id, validity, uid))

        return self._run("mail_read", operation)


def load_message_ids(store: MailStore, policy: MailPluginSettings) -> MessageIds:
    """Read an approved key only; never create directories, keys, or credentials."""
    secrets = ModelSecretStore(str(store.secrets.root.parent / "mail-plugin"))
    try:
        key = base64.b64decode(secrets.read(policy.id_key_id), altchars=b"-_", validate=True)
    except (SecretStoreError, ValueError):
        raise MailError("id_key_unavailable") from None
    return MessageIds(key, policy.project_id, policy.account_id)
