"""Messaging connectors: Telegram channels, Discord and Slack webhooks."""

from __future__ import annotations

import json
from urllib.parse import quote

from community_stack.announcements.adapters import http
from community_stack.announcements.adapters.base import (
    ChannelAdapter,
    ChannelCapabilities,
    DeliveryResult,
    HealthResult,
    UpdateMode,
    WithdrawMode,
)
from community_stack.announcements.models import Announcement

TELEGRAM_MAX_CHARACTERS = 4096
DISCORD_MAX_CHARACTERS = 2000
# Slack section blocks, used to show an image under the text, hold 3,000 characters.
SLACK_MAX_CHARACTERS = 3000
# Announcement text must never ping @everyone or roles in Discord.
DISCORD_NO_MENTIONS = {"parse": []}


def _rate_limited(status: int) -> bool:
    # A 429 is refused before anything is posted, so repeating it cannot duplicate.
    return status == 429


class TelegramAdapter(http.ShortTextChannel, ChannelAdapter):
    """Posts to a Telegram channel through a bot that is an administrator there."""

    max_characters = TELEGRAM_MAX_CHARACTERS

    def __init__(
        self, bot_token: str, chat_id: str, transport: http.Transport | None = None
    ) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._transport = transport or http.send

    @property
    def name(self) -> str:
        return "telegram"

    @property
    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            label="Telegram channel",
            max_text_length=TELEGRAM_MAX_CHARACTERS,
            shortens_long_text=True,
            custom_text=True,
            update_mode=UpdateMode.IN_PLACE,
            withdraw_mode=WithdrawMode.DELETE,
        )

    def _call(self, method: str, payload: dict) -> tuple[int, dict]:
        status, body = self._transport(
            "POST", f"https://api.telegram.org/bot{self.bot_token}/{method}", {}, payload
        )
        return status, body if isinstance(body, dict) else {}

    def _error(self, status: int, body: dict, action: str, idempotent: bool) -> DeliveryResult:
        # The bot token is part of the URL; never let it reach stored error messages.
        detail = http.error_text(status, body, "description").replace(self.bot_token, "***")
        return DeliveryResult(
            success=False,
            error=f"Telegram {action} failed: {detail}",
            retryable=http.retryable(status),
            idempotent=idempotent,
        )

    def publish(self, announcement: Announcement) -> DeliveryResult:
        status, body = self._call(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": self.preview_text(announcement),
            },
        )
        if status == 200 and body.get("ok"):
            return DeliveryResult(
                success=True, external_id=str(body["result"]["message_id"]), idempotent=True
            )
        return self._error(status, body, "post", idempotent=_rate_limited(status))

    def update(self, announcement: Announcement) -> DeliveryResult:
        return self.update_post(announcement, None)

    def update_post(self, announcement: Announcement, external_id: str | None) -> DeliveryResult:
        if not external_id:
            return self.publish(announcement)
        status, body = self._call(
            "editMessageText",
            {
                "chat_id": self.chat_id,
                "message_id": int(external_id),
                "text": self.preview_text(announcement),
            },
        )
        if (status == 200 and body.get("ok")) or "message is not modified" in str(
            body.get("description", "")
        ):
            return DeliveryResult(success=True, external_id=external_id, idempotent=True)
        return self._error(status, body, "edit", idempotent=True)

    def withdraw(self, announcement_id: str) -> DeliveryResult:
        return self.withdraw_post(announcement_id, None)

    def withdraw_post(self, announcement_id: str, external_id: str | None) -> DeliveryResult:
        if not external_id:
            return DeliveryResult(success=True, idempotent=True)
        status, body = self._call(
            "deleteMessage", {"chat_id": self.chat_id, "message_id": int(external_id)}
        )
        if status == 200 and body.get("ok"):
            return DeliveryResult(success=True, idempotent=True)
        return self._error(status, body, "delete", idempotent=True)

    def health_check(self) -> HealthResult:
        status, body = self._call("getChat", {"chat_id": self.chat_id})
        if status == 200 and body.get("ok"):
            title = body["result"].get("title") or body["result"].get("username") or "channel"
            return HealthResult(ok=True, details=f"The bot can reach {title}.")
        detail = http.error_text(status, body, "description").replace(self.bot_token, "***")
        return HealthResult(ok=False, details=f"Telegram refused: {detail}")


class DiscordWebhookAdapter(http.ShortTextChannel, ChannelAdapter):
    """Posts to a Discord channel through a channel webhook."""

    max_characters = DISCORD_MAX_CHARACTERS

    def __init__(self, webhook_url: str, transport: http.Transport | None = None) -> None:
        self.webhook_url = webhook_url.rstrip("/")
        self._transport = transport or http.send

    @property
    def name(self) -> str:
        return "discord"

    @property
    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            label="Discord channel",
            max_text_length=DISCORD_MAX_CHARACTERS,
            supports_media=True,
            shortens_long_text=True,
            custom_text=True,
            update_mode=UpdateMode.IN_PLACE,
            withdraw_mode=WithdrawMode.DELETE,
        )

    def _message(self, announcement: Announcement) -> dict:
        return {
            "content": self.preview_text(announcement),
            "allowed_mentions": DISCORD_NO_MENTIONS,
        }

    def _error(self, status: int, body, action: str, idempotent: bool) -> DeliveryResult:
        return DeliveryResult(
            success=False,
            error=f"Discord {action} failed: {http.error_text(status, body)}",
            retryable=http.retryable(status),
            idempotent=idempotent,
        )

    def publish(self, announcement: Announcement) -> DeliveryResult:
        message = self._message(announcement)
        image = announcement.image
        if image:
            # Uploading the file (rather than linking it) lets Discord show the description.
            message["attachments"] = [
                {"id": 0, "filename": image.filename, "description": image.alt}
            ]
            payload = http.Multipart(
                {"payload_json": json.dumps(message)},
                (("files[0]", image.filename, image.content_type, image.read_bytes()),),
            )
        else:
            payload = message
        status, body = self._transport("POST", f"{self.webhook_url}?wait=true", {}, payload)
        if 200 <= status < 300 and isinstance(body, dict) and body.get("id"):
            return DeliveryResult(success=True, external_id=str(body["id"]), idempotent=True)
        return self._error(status, body, "post", idempotent=_rate_limited(status))

    def update(self, announcement: Announcement) -> DeliveryResult:
        return self.update_post(announcement, None)

    def update_post(self, announcement: Announcement, external_id: str | None) -> DeliveryResult:
        if not external_id:
            return self.publish(announcement)
        status, body = self._transport(
            "PATCH",
            f"{self.webhook_url}/messages/{quote(external_id, safe='')}",
            {},
            self._message(announcement),
        )
        if 200 <= status < 300:
            return DeliveryResult(success=True, external_id=external_id, idempotent=True)
        return self._error(status, body, "edit", idempotent=True)

    def withdraw(self, announcement_id: str) -> DeliveryResult:
        return self.withdraw_post(announcement_id, None)

    def withdraw_post(self, announcement_id: str, external_id: str | None) -> DeliveryResult:
        if not external_id:
            return DeliveryResult(success=True, idempotent=True)
        status, body = self._transport(
            "DELETE", f"{self.webhook_url}/messages/{quote(external_id, safe='')}", {}, None
        )
        if 200 <= status < 300 or status == 404:
            return DeliveryResult(success=True, idempotent=True)
        return self._error(status, body, "delete", idempotent=True)

    def health_check(self) -> HealthResult:
        status, body = self._transport("GET", self.webhook_url, {}, None)
        if status == 200 and isinstance(body, dict):
            return HealthResult(ok=True, details=f"Webhook '{body.get('name', '?')}' is active.")
        return HealthResult(ok=False, details=f"Discord refused: {http.error_text(status, body)}")


def slack_escape(text: str) -> str:
    """Slack treats &, < and > as markup in message text."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class SlackWebhookAdapter(http.ShortTextChannel, ChannelAdapter):
    """Posts to a Slack channel through an incoming webhook (no edits or deletion)."""

    max_characters = SLACK_MAX_CHARACTERS

    def __init__(self, webhook_url: str, transport: http.Transport | None = None) -> None:
        self.webhook_url = webhook_url
        self._transport = transport or http.send

    @property
    def name(self) -> str:
        return "slack"

    @property
    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            label="Slack channel",
            max_text_length=SLACK_MAX_CHARACTERS,
            supports_media=True,
            shortens_long_text=True,
            custom_text=True,
            update_mode=UpdateMode.NONE,
            withdraw_mode=WithdrawMode.NONE,
        )

    def publish(self, announcement: Announcement) -> DeliveryResult:
        text = slack_escape(self.preview_text(announcement))
        message: dict = {"text": text}
        image_url = (
            announcement.image.public_url(announcement.canonical_url)
            if announcement.image
            else None
        )
        if image_url:
            message["blocks"] = [
                {"type": "section", "text": {"type": "mrkdwn", "text": text}},
                {"type": "image", "image_url": image_url, "alt_text": announcement.image.alt},
            ]
        status, body = self._transport("POST", self.webhook_url, {}, message)
        if status == 200:
            return DeliveryResult(success=True, idempotent=True)
        return DeliveryResult(
            success=False,
            error=f"Slack post failed: {http.error_text(status, body)}",
            retryable=http.retryable(status),
            idempotent=_rate_limited(status),
        )

    def update(self, announcement: Announcement) -> DeliveryResult:
        return DeliveryResult(success=False, error="Slack webhooks cannot edit messages.")

    def withdraw(self, announcement_id: str) -> DeliveryResult:
        return DeliveryResult(success=False, error="Slack webhooks cannot delete messages.")

    def health_check(self) -> HealthResult:
        return HealthResult(
            ok=True,
            details="Webhook saved. Slack cannot check a webhook without posting to it.",
        )
