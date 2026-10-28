"""ActivityPub / ActivityStreams 2.0 announcement adapter."""

from __future__ import annotations

import hashlib
import html
import json
import urllib.error
import urllib.request
from collections.abc import Callable

from community_stack.announcements.adapters.base import (
    ChannelAdapter,
    ChannelCapabilities,
    DeliveryResult,
    HealthResult,
    UpdateMode,
    WithdrawMode,
)
from community_stack.announcements.formatting import render_html
from community_stack.announcements.models import Announcement


class ActivityPubAdapter(ChannelAdapter):
    """Federates public announcements to the fediverse using ActivityStreams 2.0."""

    def __init__(
        self,
        actor_id: str = "https://community.example.org/actor",
        outbox_url: str = "https://community.example.org/outbox",
        private_key_pem: str | None = None,
        transport: Callable[[str, str, dict, dict], tuple[int, dict]] | None = None,
        enabled: bool = True,
    ) -> None:
        self.actor_id = actor_id
        self.outbox_url = outbox_url
        self.private_key_pem = private_key_pem
        self.enabled = enabled
        self._custom_transport = transport

    @property
    def name(self) -> str:
        return "activitypub"

    @property
    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            label="Fediverse (ActivityPub)",
            update_mode=UpdateMode.IN_PLACE,
            withdraw_mode=WithdrawMode.DELETE,
        )

    def _http_post(self, url: str, activity_data: dict) -> tuple[int, dict]:
        if self._custom_transport:
            return self._custom_transport(
                "POST", url, {"Content-Type": "application/activity+json"}, activity_data
            )

        data = json.dumps(activity_data).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/activity+json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                return resp.status, body
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8"))
            except Exception:
                body = {"error": str(exc)}
            return exc.code, body
        except Exception as exc:
            return 503, {"error": str(exc)}

    def build_create_activity(self, announcement: Announcement) -> dict:
        """Construct standard ActivityStreams 2.0 Create(Note) activity."""
        note_id = announcement.canonical_url or f"{self.actor_id}/notes/{announcement.id}"
        formatted_content = f"<p><strong>{html.escape(announcement.title)}</strong></p>{render_html(announcement.body)}"
        if announcement.author:
            formatted_content += f"<p><em>— {html.escape(announcement.author)}</em></p>"

        note = {
            "@context": "https://www.w3.org/ns/activitystreams",
            "type": "Note",
            "id": note_id,
            "attributedTo": self.actor_id,
            "content": formatted_content,
            "published": announcement.published_at,
            "to": ["https://www.w3.org/ns/activitystreams#Public"],
            "url": announcement.canonical_url or note_id,
            "tag": [{"type": "Hashtag", "name": f"#{tag}"} for tag in announcement.tags],
        }

        activity = {
            "@context": "https://www.w3.org/ns/activitystreams",
            "type": "Create",
            "id": f"{self.actor_id}/activities/{announcement.id}",
            "actor": self.actor_id,
            "published": announcement.published_at,
            "to": ["https://www.w3.org/ns/activitystreams#Public"],
            "object": note,
        }
        return activity

    def publish(self, announcement: Announcement) -> DeliveryResult:
        if not self.enabled:
            return DeliveryResult(
                success=False,
                error="ActivityPub adapter is disabled.",
                retryable=False,
            )

        activity = self.build_create_activity(announcement)
        status_code, resp = self._http_post(self.outbox_url, activity)

        if 200 <= status_code < 300:
            return DeliveryResult(
                success=True,
                external_id=activity["id"],
                idempotent=True,
            )

        err_msg = resp.get("error", f"HTTP {status_code}")
        return DeliveryResult(
            success=False,
            error=f"ActivityPub federation failed: {err_msg}",
            retryable=status_code >= 500 or status_code == 429,
            idempotent=True,
        )

    def update(self, announcement: Announcement) -> DeliveryResult:
        """Send Update(Note) activity across the fediverse."""
        if not self.enabled:
            return DeliveryResult(success=False, error="ActivityPub adapter is disabled.")

        activity = self.build_create_activity(announcement)
        activity["type"] = "Update"
        revision = hashlib.sha256(
            f"{announcement.title}\0{announcement.body}\0{announcement.author or ''}".encode()
        ).hexdigest()[:16]
        activity["id"] = f"{self.actor_id}/activities/upd_{announcement.id}_{revision}"
        status_code, resp = self._http_post(self.outbox_url, activity)

        if 200 <= status_code < 300:
            return DeliveryResult(success=True, external_id=activity["id"], idempotent=True)

        return DeliveryResult(
            success=False,
            error=f"ActivityPub update failed: {resp.get('error', status_code)}",
            retryable=status_code >= 500 or status_code == 429,
            idempotent=True,
        )

    def withdraw(self, announcement_id: str) -> DeliveryResult:
        """Send Delete(Note) activity across the fediverse."""
        if not self.enabled:
            return DeliveryResult(success=True, idempotent=True)

        delete_activity = {
            "@context": "https://www.w3.org/ns/activitystreams",
            "type": "Delete",
            "id": f"{self.actor_id}/activities/del_{announcement_id}",
            "actor": self.actor_id,
            "to": ["https://www.w3.org/ns/activitystreams#Public"],
            "object": f"{self.actor_id}/notes/{announcement_id}",
        }
        status_code, resp = self._http_post(self.outbox_url, delete_activity)

        if 200 <= status_code < 300:
            return DeliveryResult(success=True)

        return DeliveryResult(
            success=False,
            error=f"ActivityPub withdrawal failed: {resp.get('error', status_code)}",
            retryable=status_code >= 500 or status_code == 429,
            idempotent=True,
        )

    def health_check(self) -> HealthResult:
        if not self.enabled:
            return HealthResult(ok=True, details="ActivityPub adapter is disabled.")
        return HealthResult(ok=True, details=f"Actor configured: {self.actor_id}")
