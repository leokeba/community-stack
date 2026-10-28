"""Email newsletter announcement broadcast adapter with unsubscribe compliance."""

from __future__ import annotations

import html
from collections.abc import Callable
from email.message import EmailMessage

from community_stack.announcements.adapters.base import (
    ChannelAdapter,
    ChannelCapabilities,
    DeliveryResult,
    HealthResult,
    UpdateMode,
    WithdrawMode,
)
from community_stack.announcements.models import Announcement
from community_stack.subscriptions.manager import SubscriberManager


class EmailNewsletterAdapter(ChannelAdapter):
    """Broadcasts announcements to verified opt-in email subscribers."""

    def __init__(
        self,
        subscriber_manager: SubscriberManager | None = None,
        from_address: str = "announcements@community.example.org",
        unsubscribe_base_url: str = "https://community.example.org/unsubscribe",
        smtp_host: str = "localhost",
        smtp_port: int = 587,
        transport: Callable[[EmailMessage, list[str]], tuple[bool, str | None]] | None = None,
        enabled: bool = True,
    ) -> None:
        self.subscriber_manager = subscriber_manager or SubscriberManager()
        self.from_address = from_address
        self.unsubscribe_base_url = unsubscribe_base_url
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.enabled = enabled
        self._custom_transport = transport

    @property
    def name(self) -> str:
        return "email"

    @property
    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            label="Email newsletter",
            update_mode=UpdateMode.FOLLOW_UP,
            withdraw_mode=WithdrawMode.NOTICE,
        )

    def build_email_message(self, announcement: Announcement) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = f"📢 {announcement.title}"
        msg["From"] = self.from_address
        msg["List-Unsubscribe"] = f"<{self.unsubscribe_base_url}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

        # Plain text version
        plain_body = f"{announcement.title}\n\n{announcement.body}"
        if announcement.author:
            plain_body += f"\n\n— {announcement.author}"
        plain_body += (
            f"\n\nTo unsubscribe from this community broadcast: {self.unsubscribe_base_url}"
        )
        msg.set_content(plain_body)

        # HTML version
        html_body = f"""<!DOCTYPE html>
<html>
<body>
  <h2>📢 {html.escape(announcement.title)}</h2>
  <p>{html.escape(announcement.body)}</p>
  {"<p><em>— " + html.escape(announcement.author) + "</em></p>" if announcement.author else ""}
  <hr>
  <p style="font-size: 0.85em; color: #666;">
    You received this email because you opted into community announcements.
    <a href="{self.unsubscribe_base_url}">Click here to unsubscribe</a>.
  </p>
</body>
</html>"""
        msg.add_alternative(html_body, subtype="html")
        return msg

    def publish(self, announcement: Announcement) -> DeliveryResult:
        if not self.enabled:
            return DeliveryResult(
                success=False,
                error="Email newsletter adapter is disabled.",
                retryable=False,
            )

        recipients = self.subscriber_manager.get_active_recipients(channel="email")
        if not recipients:
            return DeliveryResult(
                success=True,
                external_id="email_no_subscribers",
            )

        msg = self.build_email_message(announcement)

        if self._custom_transport:
            success, err = self._custom_transport(msg, recipients)
            if not success:
                return DeliveryResult(
                    success=False,
                    error=f"Email delivery error: {err}",
                    retryable=True,
                )
        # Default mock / local success
        return DeliveryResult(
            success=True,
            external_id=f"email_sent_{len(recipients)}_recipients",
        )

    def update(self, announcement: Announcement) -> DeliveryResult:
        return self.publish(announcement)

    def withdraw(self, announcement_id: str) -> DeliveryResult:
        # Email cannot be un-sent; send retraction notice to list
        notice = Announcement(
            id=f"{announcement_id}_retraction",
            title="Notice: Announcement Retracted",
            body=f"The previous announcement (ID: {announcement_id}) has been retracted by organizers.",
        )
        return self.publish(notice)

    def health_check(self) -> HealthResult:
        if not self.enabled:
            return HealthResult(ok=True, details="Email newsletter adapter disabled.")
        recipients_count = len(self.subscriber_manager.get_active_recipients(channel="email"))
        return HealthResult(
            ok=True,
            details=f"From: {self.from_address}, Active subscribers: {recipients_count}",
        )
