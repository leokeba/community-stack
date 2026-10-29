"""Experimental: announcements to a Signal group through the stack's signal-cli service."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any
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

# The only address this client talks to: the stack's own container on signal_net.
SIGNAL_API = "http://community_signal:8080"
SIGNAL_MAX_CHARACTERS = 2000


def signal_send(method: str, url: str, headers: dict[str, str], payload: Any) -> tuple[int, Any]:
    """Like http.send, but only for the internal signal-cli API, and returning raw images."""
    if not url.startswith(SIGNAL_API + "/"):
        return 0, {"error": "Refusing to contact anything but the Signal service."}
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", **headers},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=http.TIMEOUT_SECONDS) as response:
            raw, kind = response.read(), response.headers.get("Content-Type", "")
            status = response.status
    except urllib.error.HTTPError as exc:
        raw, kind, status = exc.read(), exc.headers.get("Content-Type", ""), exc.code
    except (urllib.error.URLError, OSError) as exc:
        return 0, {"error": f"The Signal service is not reachable: {getattr(exc, 'reason', exc)}"}
    if kind.startswith("image/"):
        return status, raw
    try:
        return status, json.loads(raw or b"{}")
    except ValueError:
        return status, {"raw": raw.decode("utf-8", "replace")[:300]}


class SignalAdapter(http.ShortTextChannel, ChannelAdapter):
    """Posts to one Signal group from a linked account; edits and deletes its message."""

    max_characters = SIGNAL_MAX_CHARACTERS

    def __init__(self, number: str, group_id: str, transport: http.Transport | None = None) -> None:
        self.number = number
        self.group_id = group_id
        self._transport = transport or signal_send

    @property
    def name(self) -> str:
        return "signal"

    @property
    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            label="Signal group (experimental)",
            max_text_length=SIGNAL_MAX_CHARACTERS,
            shortens_long_text=True,
            custom_text=True,
            update_mode=UpdateMode.IN_PLACE,
            withdraw_mode=WithdrawMode.DELETE,
        )

    def _send(self, text: str, edit_timestamp: int | None = None) -> tuple[int, Any]:
        body: dict[str, Any] = {
            "message": text,
            "number": self.number,
            "recipients": [self.group_id],
        }
        if edit_timestamp is not None:
            body["edit_timestamp"] = edit_timestamp
        return self._transport("POST", f"{SIGNAL_API}/v2/send", {}, body)

    @staticmethod
    def _timestamp(body: Any) -> str | None:
        entry = body[0] if isinstance(body, list) and body else body
        value = entry.get("timestamp") if isinstance(entry, dict) else None
        return str(value) if value else None

    def _error(self, status: int, body: Any, action: str, idempotent: bool) -> DeliveryResult:
        return DeliveryResult(
            success=False,
            error=f"Signal {action} failed: {http.error_text(status, body)}",
            retryable=http.retryable(status),
            idempotent=idempotent,
        )

    def publish(self, announcement: Announcement) -> DeliveryResult:
        status, body = self._send(self.preview_text(announcement))
        timestamp = self._timestamp(body)
        if status in (200, 201) and timestamp:
            return DeliveryResult(success=True, external_id=timestamp, idempotent=True)
        # Only a refusal before sending (rate limit) is certain not to have posted.
        return self._error(status, body, "post", idempotent=status == 429)

    def update(self, announcement: Announcement) -> DeliveryResult:
        return self.update_post(announcement, None)

    def update_post(self, announcement: Announcement, external_id: str | None) -> DeliveryResult:
        if not external_id or not external_id.isdigit():
            return self.publish(announcement)
        status, body = self._send(self.preview_text(announcement), int(external_id))
        if status in (200, 201):
            return DeliveryResult(success=True, external_id=external_id, idempotent=True)
        return self._error(status, body, "edit", idempotent=True)

    def withdraw(self, announcement_id: str) -> DeliveryResult:
        return self.withdraw_post(announcement_id, None)

    def withdraw_post(self, announcement_id: str, external_id: str | None) -> DeliveryResult:
        if not external_id or not external_id.isdigit():
            return DeliveryResult(success=True, idempotent=True)
        status, body = self._transport(
            "DELETE",
            f"{SIGNAL_API}/v1/remote-delete/{quote(self.number, safe='')}",
            {},
            {"recipient": self.group_id, "timestamp": int(external_id)},
        )
        if status in (200, 201, 204):
            return DeliveryResult(success=True, idempotent=True)
        return self._error(status, body, "deletion", idempotent=True)

    def health_check(self) -> HealthResult:
        status, accounts = self._transport("GET", f"{SIGNAL_API}/v1/accounts", {}, None)
        if status != 200 or not isinstance(accounts, list):
            return HealthResult(ok=False, details=http.error_text(status, accounts))
        linked = {a.get("number") for a in accounts} | {a.get("aci") for a in accounts}
        if self.number not in linked:
            return HealthResult(ok=False, details="The linked Signal account is no longer there.")
        return HealthResult(ok=True, details="The Signal account is linked and the service is up.")
