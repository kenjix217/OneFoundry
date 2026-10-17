"""IMAP IDLE over local TLS, cancellation and quiet-period polling fallback."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from onefoundry.mail_idle import IdleWait
from onefoundry.mail_imap import ImapMailbox
from tests.fake_imap import serve
from tests.test_mail_imap import _box, _config


def test_idle_coalesces_notification_and_never_fetches_or_writes(tmp_path: Path) -> None:
    box = _box()
    box.idle_notification = True
    with serve(box, tmp_path) as (port, context):
        assert IdleWait(ImapMailbox(_config(port), context), 1).wait()
    assert any("EXAMINE" in command for command in box.commands)
    assert any("IDLE" in command for command in box.commands)
    assert "DONE" in box.commands
    assert all("FETCH" not in command and "STORE" not in command for command in box.commands)


def test_idle_timeout_closes_connection_and_allows_poll_fallback(tmp_path: Path) -> None:
    box = _box()
    with serve(box, tmp_path) as (port, context):
        mailbox = ImapMailbox(_config(port), context)
        assert not IdleWait(mailbox, 0.1).wait()
        assert mailbox.probe().idle


def test_idle_cancellation_unblocks_wait(tmp_path: Path) -> None:
    box = _box()
    with serve(box, tmp_path) as (port, context), ThreadPoolExecutor(max_workers=1) as pool:
        wait = IdleWait(ImapMailbox(_config(port), context), 5)
        future = pool.submit(wait.wait)
        assert box.idle_entered.wait(3)
        wait.cancel()
        assert future.result(timeout=3) is False
