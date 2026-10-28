"""Open social web connectors: Mastodon-compatible servers and Bluesky."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
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

MASTODON_MAX_CHARACTERS = 500
BLUESKY_MAX_CHARACTERS = 300
TID_ALPHABET = "234567abcdefghijklmnopqrstuvwxyz"


class MastodonAdapter(http.ShortTextChannel, ChannelAdapter):
    """Posts to a Mastodon (or Mastodon API compatible) account with a user access token."""

    max_characters = MASTODON_MAX_CHARACTERS

    def __init__(
        self,
        instance_url: str,
        access_token: str,
        transport: http.Transport | None = None,
    ) -> None:
        self.instance_url = instance_url.rstrip("/")
        self.access_token = access_token
        self._transport = transport or http.send

    @property
    def name(self) -> str:
        return "mastodon"

    @property
    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            label="Mastodon",
            max_text_length=MASTODON_MAX_CHARACTERS,
            shortens_long_text=True,
            custom_text=True,
            update_mode=UpdateMode.IN_PLACE,
            withdraw_mode=WithdrawMode.DELETE,
        )

    def _call(self, method: str, path: str, payload=None, idempotency_key: str | None = None):
        headers = {"Authorization": f"Bearer {self.access_token}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return self._transport(method, f"{self.instance_url}{path}", headers, payload)

    def _status(self, announcement: Announcement) -> dict:
        return {
            "status": self.preview_text(announcement),
            "visibility": "public",
            "language": announcement.language.split("-")[0],
        }

    def _result(self, status: int, body, idempotent: bool, action: str) -> DeliveryResult:
        if 200 <= status < 300:
            return DeliveryResult(
                success=True, external_id=str(body.get("id", "")) or None, idempotent=True
            )
        return DeliveryResult(
            success=False,
            error=f"Mastodon {action} failed: {http.error_text(status, body)}",
            retryable=http.retryable(status),
            idempotent=idempotent,
        )

    def publish(self, announcement: Announcement) -> DeliveryResult:
        # Mastodon keeps idempotency keys for an hour, so a retry does not post twice.
        status, body = self._call(
            "POST",
            "/api/v1/statuses",
            self._status(announcement),
            idempotency_key=f"community-stack-{announcement.id}",
        )
        return self._result(status, body, idempotent=True, action="post")

    def update(self, announcement: Announcement) -> DeliveryResult:
        return self.update_post(announcement, None)

    def update_post(self, announcement: Announcement, external_id: str | None) -> DeliveryResult:
        if not external_id:
            return self.publish(announcement)
        status, body = self._call(
            "PUT", f"/api/v1/statuses/{quote(external_id, safe='')}", self._status(announcement)
        )
        return self._result(status, body, idempotent=True, action="edit")

    def withdraw(self, announcement_id: str) -> DeliveryResult:
        return self.withdraw_post(announcement_id, None)

    def withdraw_post(self, announcement_id: str, external_id: str | None) -> DeliveryResult:
        if not external_id:
            return DeliveryResult(success=True, idempotent=True)
        status, body = self._call("DELETE", f"/api/v1/statuses/{quote(external_id, safe='')}")
        if status == 404:
            return DeliveryResult(success=True, idempotent=True)
        return self._result(status, body, idempotent=True, action="delete")

    def health_check(self) -> HealthResult:
        status, body = self._call("GET", "/api/v1/accounts/verify_credentials")
        if status == 200:
            return HealthResult(ok=True, details=f"Connected as @{body.get('acct', '?')}.")
        return HealthResult(ok=False, details=f"Mastodon refused: {http.error_text(status, body)}")


def deterministic_tid(announcement: Announcement) -> str:
    """A stable AT Protocol record key for an announcement, so retries overwrite, not duplicate."""
    published = datetime.fromisoformat(announcement.published_at.replace("Z", "+00:00"))
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    micros = int(published.timestamp() * 1_000_000) & ((1 << 53) - 1)
    clock = int.from_bytes(hashlib.sha256(announcement.id.encode()).digest()[:2], "big") & 0x3FF
    value = (micros << 10) | clock
    return "".join(TID_ALPHABET[(value >> shift) & 31] for shift in range(60, -1, -5))


class BlueskyAdapter(http.ShortTextChannel, ChannelAdapter):
    """Posts to a Bluesky account using an app password."""

    max_characters = BLUESKY_MAX_CHARACTERS

    def __init__(
        self,
        handle: str,
        app_password: str,
        service_url: str = "https://bsky.social",
        transport: http.Transport | None = None,
    ) -> None:
        self.handle = handle.lstrip("@")
        self.app_password = app_password
        self.service_url = service_url.rstrip("/")
        self._transport = transport or http.send

    @property
    def name(self) -> str:
        return "bluesky"

    @property
    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            label="Bluesky",
            max_text_length=BLUESKY_MAX_CHARACTERS,
            shortens_long_text=True,
            custom_text=True,
            update_mode=UpdateMode.NONE,
            withdraw_mode=WithdrawMode.DELETE,
        )

    def _xrpc(self, method: str, payload: dict, token: str | None = None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self._transport("POST", f"{self.service_url}/xrpc/{method}", headers, payload)

    def _session(self) -> tuple[str, str] | DeliveryResult:
        status, body = self._xrpc(
            "com.atproto.server.createSession",
            {"identifier": self.handle, "password": self.app_password},
        )
        if status == 200 and body.get("accessJwt") and body.get("did"):
            return body["accessJwt"], body["did"]
        return DeliveryResult(
            success=False,
            error=f"Bluesky sign-in failed: {http.error_text(status, body)}",
            retryable=http.retryable(status),
            idempotent=True,
        )

    def _record(self, announcement: Announcement) -> dict:
        text = self.preview_text(announcement)
        record = {
            "$type": "app.bsky.feed.post",
            "text": text,
            "createdAt": datetime.fromisoformat(announcement.published_at.replace("Z", "+00:00"))
            .astimezone(timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "langs": [announcement.language],
        }
        link = announcement.canonical_url
        if link and link in text:
            # Bluesky only makes links clickable through facets, indexed in UTF-8 bytes.
            start = text.encode("utf-8").rindex(link.encode("utf-8"))
            record["facets"] = [
                {
                    "index": {"byteStart": start, "byteEnd": start + len(link.encode("utf-8"))},
                    "features": [{"$type": "app.bsky.richtext.facet#link", "uri": link}],
                }
            ]
        return record

    def publish(self, announcement: Announcement) -> DeliveryResult:
        session = self._session()
        if isinstance(session, DeliveryResult):
            return session
        token, did = session
        status, body = self._xrpc(
            "com.atproto.repo.putRecord",
            {
                "repo": did,
                "collection": "app.bsky.feed.post",
                "rkey": deterministic_tid(announcement),
                "record": self._record(announcement),
            },
            token,
        )
        if status == 200:
            return DeliveryResult(success=True, external_id=body.get("uri"), idempotent=True)
        return DeliveryResult(
            success=False,
            error=f"Bluesky post failed: {http.error_text(status, body)}",
            retryable=http.retryable(status),
            idempotent=True,
        )

    def update(self, announcement: Announcement) -> DeliveryResult:
        return DeliveryResult(success=False, error="Bluesky posts cannot be edited.")

    def withdraw(self, announcement_id: str) -> DeliveryResult:
        return self.withdraw_post(announcement_id, None)

    def withdraw_post(self, announcement_id: str, external_id: str | None) -> DeliveryResult:
        if not external_id:
            return DeliveryResult(success=True, idempotent=True)
        session = self._session()
        if isinstance(session, DeliveryResult):
            return session
        token, did = session
        status, body = self._xrpc(
            "com.atproto.repo.deleteRecord",
            {
                "repo": did,
                "collection": "app.bsky.feed.post",
                "rkey": external_id.rsplit("/", 1)[-1],
            },
            token,
        )
        if status == 200:
            return DeliveryResult(success=True, idempotent=True)
        return DeliveryResult(
            success=False,
            error=f"Bluesky delete failed: {http.error_text(status, body)}",
            retryable=http.retryable(status),
            idempotent=True,
        )

    def health_check(self) -> HealthResult:
        session = self._session()
        if isinstance(session, DeliveryResult):
            return HealthResult(ok=False, details=session.error or "Bluesky sign-in failed.")
        return HealthResult(ok=True, details=f"Signed in as @{self.handle}.")
