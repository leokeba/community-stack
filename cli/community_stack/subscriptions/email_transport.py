"""Pluggable email delivery. The announcement system depends on this interface, not on an SMTP vendor."""

from __future__ import annotations

import base64
import smtplib
import ssl
from abc import ABC, abstractmethod
from collections.abc import Callable
from email.message import EmailMessage
from email.utils import getaddresses
from typing import ClassVar
from urllib.parse import quote

from community_stack.announcements.adapters import http


class TransportError(Exception):
    """A message could not be sent. ``retryable`` says whether trying again later may help."""

    def __init__(self, message: str, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class EmailTransport(ABC):
    @abstractmethod
    def send(self, message: EmailMessage) -> None:
        """Send one message to the addresses in its To header, or raise TransportError."""

    def verify(self) -> str | None:
        """Check provider credentials when the transport supports a non-sending check."""
        return None


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


class MailgunTransport(EmailTransport):
    """Sends individual messages through Mailgun's HTTPS API."""

    _API_HOSTS: ClassVar[dict[str, str]] = {
        "us": "api.mailgun.net",
        "eu": "api.eu.mailgun.net",
    }

    def __init__(
        self,
        domain: str,
        api_key: str,
        region: str = "us",
        transport: http.Transport | None = None,
    ) -> None:
        if region not in self._API_HOSTS:
            raise ValueError("Mailgun region must be 'us' or 'eu'.")
        self.domain = domain
        self.api_key = api_key
        self.region = region
        self._transport = transport or http.send

    @property
    def _base_url(self) -> str:
        host = self._API_HOSTS[self.region]
        return f"https://{host}"

    def _request(self, method: str, path: str, payload: http.Form | None = None):
        credentials = base64.b64encode(f"api:{self.api_key}".encode()).decode("ascii")
        return self._transport(
            method,
            f"{self._base_url}/{path}",
            {"Authorization": f"Basic {credentials}"},
            payload,
        )

    def verify(self) -> str:
        status, _body = self._request("GET", f"v4/domains/{quote(self.domain, safe='')}")
        if status != 200:
            raise TransportError(
                f"Mailgun connection check failed (HTTP {status}).",
                retryable=http.retryable(status),
            )
        return f"Connected to Mailgun sending domain {self.domain}."

    @staticmethod
    def _message_fields(message: EmailMessage) -> dict[str, str]:
        from_addresses = [
            address for _name, address in getaddresses([str(message.get("From", ""))]) if address
        ]
        recipients = [
            address for _name, address in getaddresses([str(message.get("To", ""))]) if address
        ]
        if not from_addresses or not recipients or not message.get("Subject"):
            raise TransportError("Mailgun needs a sender, recipient, and subject.", retryable=False)

        fields = {
            "from": from_addresses[0],
            "to": ", ".join(recipients),
            "subject": str(message["Subject"]),
            "o:tracking": "no",
            "o:tracking-clicks": "no",
            "o:tracking-opens": "no",
        }
        plain_part = message.get_body(preferencelist=("plain",))
        if plain_part is not None:
            fields["text"] = plain_part.get_content()
        html_part = message.get_body(preferencelist=("html",))
        if html_part is not None:
            fields["html"] = html_part.get_content()
        for header in ("List-Unsubscribe", "List-Unsubscribe-Post"):
            if message.get(header):
                fields[f"h:{header}"] = str(message[header])
        if "text" not in fields and "html" not in fields:
            raise TransportError("Mailgun needs a text or HTML message body.", retryable=False)
        return fields

    def send(self, message: EmailMessage) -> None:
        fields = self._message_fields(message)
        domain = quote(self.domain, safe="")
        status, _body = self._request("POST", f"v3/{domain}/messages", http.Form(fields))
        if not 200 <= status < 300:
            raise TransportError(
                f"Mailgun rejected the message (HTTP {status}).",
                retryable=http.retryable(status),
            )
