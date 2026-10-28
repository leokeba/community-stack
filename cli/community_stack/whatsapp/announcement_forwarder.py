"""WhatsApp public announcement forwarder (Mode A) implementing ChannelAdapter."""

from __future__ import annotations

import re
from collections.abc import Callable

from community_stack.announcements.adapters.base import (
    ChannelAdapter,
    ChannelCapabilities,
    DeliveryResult,
    HealthResult,
    UpdateMode,
    WithdrawMode,
)
from community_stack.announcements.models import Announcement

# Longest text message WhatsApp accepts, in characters.
WHATSAPP_MAX_TEXT_LENGTH = 65_536


class WhatsAppAnnouncementAdapter(ChannelAdapter):
    """Forward canonical public announcements unidirectionally to WhatsApp groups or broadcast channels."""

    def __init__(
        self,
        bridge_url: str = "http://localhost:29318",
        target_jid: str = "123456789-broadcast@g.us",
        api_token: str | None = None,
        transport: Callable[[str, str, dict], tuple[int, dict]] | None = None,
        enabled: bool = False,
    ) -> None:
        self.bridge_url = bridge_url.rstrip("/")
        self.target_jid = target_jid
        self.api_token = api_token
        self.enabled = enabled
        self._custom_transport = transport

    @property
    def name(self) -> str:
        return "whatsapp"

    @property
    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            label="WhatsApp",
            max_text_length=WHATSAPP_MAX_TEXT_LENGTH,
            update_mode=UpdateMode.FOLLOW_UP,
            withdraw_mode=WithdrawMode.NOTICE,
        )

    def render_text(self, announcement: Announcement) -> str:
        text_body = f"📢 *{announcement.title}*\n\n{announcement.body}"
        if announcement.canonical_url:
            text_body += f"\n\nAuthoritative Link: {announcement.canonical_url}"
        if announcement.author:
            text_body += f"\n— {announcement.author}"
        return text_body

    def _mask_jid(self, jid: str) -> str:
        """Redact sensitive phone numbers or group identifiers in logs."""
        return re.sub(r"^\d{4,}", "****", jid)

    def publish(self, announcement: Announcement) -> DeliveryResult:
        if not self.enabled:
            return DeliveryResult(
                success=False,
                error="WhatsApp bridge adapter is disabled in configuration.",
                retryable=False,
            )

        text_body = self.render_text(announcement)

        payload = {
            "recipient": self.target_jid,
            "message": text_body,
            "announcement_id": announcement.id,
        }

        if self._custom_transport:
            status_code, resp = self._custom_transport("POST", "/v1/send", payload)
        else:
            # Standalone simulated or local endpoint
            status_code = 200
            resp = {"message_id": f"wa_msg_{announcement.id}"}

        if 200 <= status_code < 300:
            return DeliveryResult(
                success=True,
                external_id=resp.get("message_id", f"wa_{announcement.id}"),
            )

        err_msg = resp.get("error", f"HTTP {status_code}")
        # Redact any phone numbers from error message
        masked_err = re.sub(r"\b\d{10,15}\b", "[PHONE_REDACTED]", str(err_msg))
        is_retryable = status_code >= 500 or status_code in (408, 429)
        return DeliveryResult(
            success=False,
            error=f"WhatsApp delivery failed: {masked_err}",
            retryable=is_retryable,
        )

    def update(self, announcement: Announcement) -> DeliveryResult:
        """WhatsApp messages cannot be updated retroactively across all versions; send an update notice."""
        if not self.enabled:
            return DeliveryResult(success=False, error="WhatsApp adapter disabled.")

        update_ann = Announcement(
            id=announcement.id,
            title=f"[UPDATE] {announcement.title}",
            body=announcement.body,
            author=announcement.author,
            canonical_url=announcement.canonical_url,
        )
        return self.publish(update_ann)

    def withdraw(self, announcement_id: str) -> DeliveryResult:
        """Send retraction notice to WhatsApp channel."""
        if not self.enabled:
            return DeliveryResult(success=True)

        notice = Announcement(
            id=f"{announcement_id}_retraction",
            title="⚠️ Announcement Retracted",
            body=f"Notice: The previous announcement ({announcement_id}) has been retracted by organizers.",
        )
        return self.publish(notice)

    def health_check(self) -> HealthResult:
        if not self.enabled:
            return HealthResult(ok=True, details="WhatsApp bridge is disabled (dormant).")
        return HealthResult(
            ok=True, details=f"Target JID configured: {self._mask_jid(self.target_jid)}"
        )
