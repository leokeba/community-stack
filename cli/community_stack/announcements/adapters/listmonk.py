"""Email newsletter through a listmonk server: one campaign per announcement."""

from __future__ import annotations

import hashlib
from urllib.parse import quote, urlencode

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

SENT_STATUSES = {"running", "finished", "scheduled"}


class ListmonkAdapter(ChannelAdapter):
    """Creates and starts a listmonk campaign; edits and withdrawals send follow-up emails."""

    def __init__(
        self,
        base_url: str,
        api_user: str,
        api_token: str,
        list_ids: list[int],
        transport: http.Transport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_user = api_user
        self.api_token = api_token
        self.list_ids = list_ids
        self._transport = transport or http.send

    @property
    def name(self) -> str:
        return "listmonk"

    @property
    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            label="Email newsletter (listmonk)",
            supports_media=True,
            update_mode=UpdateMode.FOLLOW_UP,
            withdraw_mode=WithdrawMode.NOTICE,
        )

    def _call(self, method: str, path: str, payload=None):
        headers = {"Authorization": f"token {self.api_user}:{self.api_token}"}
        status, body = self._transport(method, f"{self.base_url}{path}", headers, payload)
        return status, body if isinstance(body, dict) else {}

    def _failed(self, status: int, body: dict, action: str) -> DeliveryResult:
        return DeliveryResult(
            success=False,
            error=f"listmonk {action} failed: {http.error_text(status, body)}",
            retryable=http.retryable(status),
            # The campaign name carries a unique tag, so a retry finds and reuses it.
            idempotent=True,
        )

    def _find(self, tag: str) -> dict | None:
        query = urlencode({"query": tag, "no_body": "true", "per_page": "50"})
        status, body = self._call("GET", f"/api/campaigns?{query}")
        if status != 200:
            return None
        results = (body.get("data") or {}).get("results") or []
        return next((c for c in results if str(c.get("name", "")).endswith(f"[{tag}]")), None)

    def _send(self, tag: str, subject: str, markdown: str) -> DeliveryResult:
        """Create (or reuse) the campaign named with ``tag`` and start it."""
        campaign = self._find(tag)
        if campaign and campaign.get("status") in SENT_STATUSES:
            return DeliveryResult(success=True, external_id=str(campaign["id"]), idempotent=True)
        if campaign is None:
            status, body = self._call(
                "POST",
                "/api/campaigns",
                {
                    "name": f"{subject[:150]} [{tag}]",
                    "subject": subject,
                    "lists": self.list_ids,
                    "type": "regular",
                    "content_type": "markdown",
                    "body": markdown,
                },
            )
            if status != 200 or not (body.get("data") or {}).get("id"):
                return self._failed(status, body, "campaign creation")
            campaign = body["data"]
        campaign_id = quote(str(campaign["id"]), safe="")
        status, body = self._call(
            "PUT", f"/api/campaigns/{campaign_id}/status", {"status": "running"}
        )
        if status != 200:
            return self._failed(status, body, "sending")
        return DeliveryResult(success=True, external_id=str(campaign["id"]), idempotent=True)

    @staticmethod
    def _markdown(announcement: Announcement) -> str:
        parts = []
        image = announcement.image
        image_url = image.public_url(announcement.canonical_url) if image else None
        if image_url:
            alt = image.alt.replace("]", ")").replace("[", "(")
            parts.append(f"![{alt}]({image_url})")
        parts.append(announcement.body)
        if announcement.author:
            parts.append(f"— {announcement.author}")
        if announcement.canonical_url:
            parts.append(f"[Read this announcement online]({announcement.canonical_url})")
        return "\n\n".join(parts)

    def publish(self, announcement: Announcement) -> DeliveryResult:
        return self._send(announcement.id, announcement.title, self._markdown(announcement))

    def update(self, announcement: Announcement) -> DeliveryResult:
        revision = hashlib.sha256(
            f"{announcement.title}\0{announcement.body}".encode()
        ).hexdigest()[:12]
        return self._send(
            f"{announcement.id}-update-{revision}",
            f"Update: {announcement.title}",
            self._markdown(announcement),
        )

    def withdraw(self, announcement_id: str) -> DeliveryResult:
        return self._send(
            f"{announcement_id}-withdrawn",
            "Correction: an announcement was withdrawn",
            "An announcement we sent earlier has been withdrawn by the organizers and should "
            f"no longer be relied on (reference {announcement_id}).",
        )

    def health_check(self) -> HealthResult:
        status, body = self._call("GET", "/api/lists?per_page=all")
        if status != 200:
            return HealthResult(
                ok=False, details=f"listmonk refused: {http.error_text(status, body)}"
            )
        lists = {
            item.get("id"): item.get("name")
            for item in (body.get("data") or {}).get("results") or []
        }
        missing = [str(i) for i in self.list_ids if i not in lists]
        if missing:
            return HealthResult(ok=False, details=f"List IDs not found: {', '.join(missing)}.")
        names = ", ".join(str(lists[i]) for i in self.list_ids)
        return HealthResult(ok=True, details=f"Connected; announcements go to: {names}.")
