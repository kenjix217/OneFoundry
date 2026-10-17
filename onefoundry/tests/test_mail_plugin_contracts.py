"""Synthetic boundary and identity checks for the personal read-only mail plugin."""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from onefoundry.mail_imap import MailError
from onefoundry.mail_plugin_contracts import MailRead, MailSearch, MessageIds


@pytest.mark.parametrize("bad", ["x\r\nSTORE 1 +FLAGS (\\Seen)", "x\x00", "é", "x" * 257])
def test_search_rejects_command_injection_and_unbounded_input(bad: str) -> None:
    with pytest.raises(ValidationError):
        MailSearch(folder="INBOX", text=bad)


def test_search_dates_and_extra_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        MailSearch(folder="INBOX", since="2026-10-02", before="2026-10-01")
    with pytest.raises(ValidationError):
        MailSearch.model_validate({"folder": "INBOX", "password": "must-never-be-a-tool-argument"})
    with pytest.raises(ValidationError):
        MailSearch(folder="INBOX", limit=True)
    with pytest.raises(ValidationError):
        MailRead(message_id="arbitrary-imap-uid")


def test_ids_are_stable_signed_account_folder_and_uidvalidity_bound() -> None:
    ids = MessageIds(b"synthetic-id-key-with-at-least-32-bytes", uuid4(), uuid4())
    token = ids.encode("binding", "INBOX", 7, 101)
    assert token == ids.encode("binding", "INBOX", 7, 101)
    assert ids.decode(token, "binding", ["INBOX"]) == ("INBOX", 7, 101)
    for binding, folders in [("changed-account", ["INBOX"]), ("binding", ["Sent"])]:
        with pytest.raises(MailError):
            ids.decode(token, binding, folders)
    other = MessageIds(b"synthetic-id-key-with-at-least-32-bytes", uuid4(), uuid4())
    with pytest.raises(MailError):
        other.decode(token, "binding", ["INBOX"])
    with pytest.raises(MailError):
        ids.decode(token[:-1] + ("A" if token[-1] != "A" else "B"), "binding", ["INBOX"])
