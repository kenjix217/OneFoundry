"""The IMAP connector against a local TLS server: read-only, bounded, and duplicate-free."""

from pathlib import Path

import pytest

from onefoundry.mail_imap import ImapMailbox, MailConfig, MailError, parse_message
from tests.fake_imap import Mailbox, message, serve

TEST_PASSWORD = "app-password"  # noqa: S105 - fixture credential for the local fake server


def _config(port: int, password: str | None = None, max_bytes: int = 65536) -> MailConfig:
    return MailConfig(
        "127.0.0.1", port, "owner@example.test", password or TEST_PASSWORD, "INBOX", 5.0, max_bytes
    )


def _box() -> Mailbox:
    return Mailbox(
        messages={
            101: message(
                "Pilot pricing", "Buyer <buyer@acme.example>", "We would like pricing for 50 seats."
            ),
            102: message(
                "Newsletter", "news@lists.example", "<p>Hello <b>there</b></p><script>x()</script>", html=True
            ),
            103: message("Follow up", "buyer@acme.example", "Any update?"),
        }
    )


def test_probe_reports_capabilities_and_mailbox_identity(tmp_path: Path) -> None:
    box = _box()
    with serve(box, tmp_path) as (port, context):
        probe = ImapMailbox(_config(port), context).probe()
    assert probe.idle is True
    assert probe.uidvalidity == 7
    assert probe.uidnext == 104
    assert probe.exists == 3
    assert any(" EXAMINE " in command for command in box.commands)
    assert not any(" SELECT " in command or " STORE " in command for command in box.commands)


def test_fetch_is_read_only_ordered_and_bounded(tmp_path: Path) -> None:
    box = _box()
    with serve(box, tmp_path) as (port, context):
        mailbox = ImapMailbox(_config(port), context)
        first = mailbox.fetch_since(100, 2)
        rest = mailbox.fetch_since(first.highest_uid, 10)
        none = mailbox.fetch_since(rest.highest_uid, 10)
    assert [item.uid for item in first.messages] == [101, 102]
    assert first.highest_uid == 102
    assert [item.uid for item in rest.messages] == [103]
    assert none.messages == () and none.highest_uid == 103
    assert all(
        "BODY.PEEK[]" in command for command in box.commands if "FETCH" in command and "SIZE" not in command
    )
    assert not any(" SELECT " in command or " STORE " in command for command in box.commands)
    parsed = parse_message(first.messages[0].raw)
    assert parsed.from_address == "buyer@acme.example"
    assert parsed.from_domain == "acme.example"
    assert parsed.subject == "Pilot pricing"
    assert parsed.text == "We would like pricing for 50 seats."
    assert parse_message(first.messages[1].raw).text == "Hello there"


def test_large_message_is_truncated_and_flagged(tmp_path: Path) -> None:
    box = Mailbox(messages={101: message("Big", "a@b.example", "x" * 9000)})
    with serve(box, tmp_path) as (port, context):
        batch = ImapMailbox(_config(port, max_bytes=4096), context).fetch_since(100, 5)
    assert batch.messages[0].truncated is True
    assert len(batch.messages[0].raw) == 4096


def test_wrong_password_and_unreachable_server_have_fixed_codes(tmp_path: Path) -> None:
    with serve(_box(), tmp_path) as (port, context):
        with pytest.raises(MailError) as denied:
            ImapMailbox(_config(port, password="wrong"), context).probe()
    assert denied.value.code == "authentication_failed"
    with pytest.raises(MailError) as unreachable:
        ImapMailbox(_config(port), context).probe()
    assert unreachable.value.code == "connection_failed"


def test_untrusted_certificate_is_rejected(tmp_path: Path) -> None:
    with serve(_box(), tmp_path) as (port, _context):
        with pytest.raises(MailError) as failure:
            ImapMailbox(_config(port)).probe()
    assert failure.value.code in {"tls_failed", "connection_failed"}


def test_reply_references_are_bounded_and_keep_the_recent_chain() -> None:
    refs = " ".join(f"<{number}@example.test>" for number in range(40))
    parsed = parse_message(f"In-Reply-To: <39@example.test>\r\nReferences: {refs}\r\n\r\nHi".encode())
    assert parsed.in_reply_to == "<39@example.test>"
    assert len(parsed.references) == 30
    assert parsed.references[0] == "<10@example.test>" and parsed.references[-1] == "<39@example.test>"
