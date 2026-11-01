"""Email newsletter announcement broadcast adapter with unsubscribe compliance."""

from __future__ import annotations

import hashlib
import html
import time
from email.message import EmailMessage

from community_stack.announcements.adapters.base import (
    ChannelAdapter,
    ChannelCapabilities,
    DeliveryResult,
    HealthResult,
    UpdateMode,
    WithdrawMode,
)
from community_stack.announcements.formatting import render_html, to_plain_text
from community_stack.announcements.models import Announcement
from community_stack.subscriptions.email_signup import make_unsubscribe_token
from community_stack.subscriptions.email_transport import (
    EmailTransport,
    TransportError,
    UnconfiguredTransport,
)
from community_stack.subscriptions.manager import SubscriberManager


class EmailNewsletterAdapter(ChannelAdapter):
    """Broadcasts announcements to verified opt-in email subscribers."""

    def __init__(
        self,
        subscriber_manager: SubscriberManager | None = None,
        from_address: str = "announcements@community.example.org",
        public_base_url: str = "https://community.example.org",
        unsubscribe_secret: bytes | None = None,
        transport: EmailTransport | None = None,
        enabled: bool = True,
        time_budget_seconds: float = 200,
    ) -> None:
        self.subscriber_manager = subscriber_manager or SubscriberManager()
        self.time_budget_seconds = time_budget_seconds
        self.from_address = from_address
        self.public_base_url = public_base_url.rstrip("/")
        # Signs the per-person unsubscribe links. Without it nothing can be sent safely.
        self.unsubscribe_secret = unsubscribe_secret
        self.enabled = enabled
        self.transport = transport or UnconfiguredTransport()

    def unsubscribe_url(self, address: str) -> str | None:
        if self.unsubscribe_secret is None:
            return None
        token = make_unsubscribe_token(self.unsubscribe_secret, address)
        return f"{self.public_base_url}/subscribe/email/unsubscribe?t={token}"

    @property
    def name(self) -> str:
        return "email"

    @property
    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            label="Email newsletter",
            supports_media=True,
            update_mode=UpdateMode.FOLLOW_UP,
            withdraw_mode=WithdrawMode.NOTICE,
        )

    @staticmethod
    def _image_html(announcement: Announcement) -> str:
        image = announcement.image
        url = image.public_url(announcement.canonical_url) if image else None
        if not url:
            return ""
        return (
            f'<p><img src="{html.escape(url, quote=True)}" alt="{html.escape(image.alt, quote=True)}" '
            f'width="{min(image.width, 600)}" style="max-width:100%;height:auto"></p>'
        )

    def build_email_message(
        self, announcement: Announcement, unsubscribe_url: str | None = None
    ) -> EmailMessage:
        if unsubscribe_url is None:
            raise ValueError("Every announcement email needs its own unsubscribe link.")
        msg = EmailMessage()
        msg["Subject"] = f"📢 {announcement.title}"
        msg["From"] = self.from_address
        msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

        # Plain text version
        plain_body = f"{announcement.title}\n\n{to_plain_text(announcement.body)}"
        if announcement.author:
            plain_body += f"\n\n— {announcement.author}"
        plain_body += f"\n\nTo stop receiving these emails: {unsubscribe_url}"
        msg.set_content(plain_body)

        # HTML version
        html_body = f"""<!DOCTYPE html>
<html>
<body>
  <h2>📢 {html.escape(announcement.title)}</h2>
  {self._image_html(announcement)}
  {render_html(announcement.body)}
  {"<p><em>— " + html.escape(announcement.author) + "</em></p>" if announcement.author else ""}
  <hr>
  <p style="font-size: 0.85em; color: #666;">
    You received this email because you opted into community announcements.
    <a href="{html.escape(unsubscribe_url, quote=True)}">Click here to unsubscribe</a>.
  </p>
</body>
</html>"""
        msg.add_alternative(html_body, subtype="html")
        return msg

    def publish(self, announcement: Announcement) -> DeliveryResult:
        return self._deliver(announcement, "publish")

    def _deliver(self, announcement: Announcement, operation: str) -> DeliveryResult:
        """Send one message per person still owed one, within this run's time budget."""
        if not self.enabled:
            return DeliveryResult(
                success=False,
                error="Email newsletter adapter is disabled.",
                retryable=False,
            )
        if self.unsubscribe_secret is None:
            return DeliveryResult(
                success=False,
                error="Email is missing its unsubscribe signing secret.",
                retryable=False,
            )

        active = set(self.subscriber_manager.get_active_recipients(channel="email"))
        self.subscriber_manager.queue_email_send(announcement.id, operation, sorted(active))
        total, already = self.subscriber_manager.email_send_progress(announcement.id, operation)
        if total == 0:
            return DeliveryResult(success=True, external_id="email_no_subscribers", idempotent=True)

        # Only people still subscribed, who were in the audience when sending began.
        owed = [
            address
            for address in self.subscriber_manager.pending_email_sends(announcement.id, operation)
            if address in active
        ]
        started = time.monotonic()
        for address in owed:
            if time.monotonic() - started > self.time_budget_seconds:
                return DeliveryResult(
                    success=False,
                    error=f"Paused after {already} of {total} to stay within the job time limit; "
                    "the rest will be sent on retry.",
                    retryable=True,
                    idempotent=True,
                )
            message = self.build_email_message(announcement, self.unsubscribe_url(address))
            message["To"] = address
            try:
                self.transport.send(message)
            except TransportError as exc:
                return DeliveryResult(
                    success=False,
                    error=f"Email delivery stopped after {already} of {total}: {exc}",
                    retryable=exc.retryable,
                    idempotent=True,
                )
            self.subscriber_manager.mark_email_sent(announcement.id, operation, address)
            already += 1
        return DeliveryResult(
            success=True,
            external_id=f"email_sent_{total}_recipients",
            idempotent=True,
        )

    def update(self, announcement: Announcement) -> DeliveryResult:
        # Each revision is sent once to the people who are subscribed when it is published.
        revision = hashlib.sha256(
            f"{announcement.title}\0{announcement.body}".encode()
        ).hexdigest()[:12]
        return self._deliver(announcement, f"update:{revision}")

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
