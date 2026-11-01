"""Pluggable email delivery. The announcement system depends on this interface, not on an SMTP vendor."""

from __future__ import annotations

import smtplib
import ssl
from abc import ABC, abstractmethod
from collections.abc import Callable
from email.message import EmailMessage


class TransportError(Exception):
    """A message could not be sent. ``retryable`` says whether trying again later may help."""

    def __init__(self, message: str, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class EmailTransport(ABC):
    @abstractmethod
    def send(self, message: EmailMessage) -> None:
        """Send one message to the addresses in its To header, or raise TransportError."""


class UnconfiguredTransport(EmailTransport):
    """The default. Email is refused rather than pretending to succeed."""

    def send(self, message: EmailMessage) -> None:
        raise TransportError("No email server is configured.", retryable=False)


class SmtpTransport(EmailTransport):
    """Sends through an SMTP server. Port 465 uses implicit TLS; other ports use STARTTLS."""

    def __init__(
        self,
        host: str,
        port: int = 587,
        username: str | None = None,
        password: str | None = None,
        timeout: float = 20,
        smtp_factory: Callable[..., smtplib.SMTP] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.timeout = timeout
        self._factory = smtp_factory

    def _connect(self) -> smtplib.SMTP:
        context = ssl.create_default_context()
        if self._factory is not None:
            return self._factory(self.host, self.port, timeout=self.timeout)
        if self.port == 465:
            return smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=context)
        return smtplib.SMTP(self.host, self.port, timeout=self.timeout)

    def send(self, message: EmailMessage) -> None:
        try:
            with self._connect() as smtp:
                if self.port != 465:
                    smtp.starttls(context=ssl.create_default_context())
                if self.username:
                    smtp.login(self.username, self.password or "")
                smtp.send_message(message)
        except smtplib.SMTPAuthenticationError as exc:
            raise TransportError("The email server refused the login.", retryable=False) from exc
        except smtplib.SMTPRecipientsRefused as exc:
            raise TransportError(
                "The email server refused every recipient.", retryable=False
            ) from exc
        except smtplib.SMTPResponseException as exc:
            # 4xx means try again later; 5xx means the message will not be accepted as sent.
            raise TransportError(
                f"The email server answered {exc.smtp_code}.",
                retryable=400 <= exc.smtp_code < 500,
            ) from exc
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            raise TransportError("Could not reach the email server.", retryable=True) from exc
