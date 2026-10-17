"""Bounded authenticated SMTP submission with mandatory verified TLS."""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    security: str
    username: str
    password: str = field(repr=False)
    timeout: float


def _connect(config: SmtpConfig) -> smtplib.SMTP:
    context = ssl.create_default_context()
    if config.security == "ssl":
        client: smtplib.SMTP = smtplib.SMTP_SSL(
            config.host, config.port, timeout=config.timeout, context=context
        )
    elif config.security == "starttls":
        client = smtplib.SMTP(config.host, config.port, timeout=config.timeout)
        try:
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        except Exception:
            client.close()
            raise
    else:
        raise ValueError("SMTP security mode is invalid")
    return client


def probe(config: SmtpConfig) -> None:
    with _connect(config) as client:
        client.login(config.username, config.password)
        client.noop()


def submit(config: SmtpConfig, sender: str, recipient: str, subject: str, body: str, message_id: str) -> None:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message["Message-ID"] = message_id
    message.set_content(body)
    with _connect(config) as client:
        client.login(config.username, config.password)
        refused = client.send_message(message, from_addr=sender, to_addrs=[recipient])
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
