"""Publishing adapters for Facebook Pages, Instagram, and Threads."""

from __future__ import annotations

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

FACEBOOK_GRAPH = "https://graph.facebook.com/v26.0"
INSTAGRAM_GRAPH = "https://graph.instagram.com/v26.0"
THREADS_GRAPH = "https://graph.threads.com/v1.0"
INSTAGRAM_MAX_CHARACTERS = 2200
THREADS_MAX_CHARACTERS = 500


class _MetaAdapter(ChannelAdapter):
    def __init__(self, access_token: str, transport: http.Transport | None = None) -> None:
        self.access_token = access_token
        self._transport = transport or http.send

    @property
    def graph_url(self) -> str:
        raise NotImplementedError

    def _call(self, method: str, path: str, fields: dict[str, str] | None = None):
        values = {**(fields or {}), "access_token": self.access_token}
        url = f"{self.graph_url}{path}"
        if method in {"GET", "DELETE"}:
            url = f"{url}?{urlencode(values)}"
            payload = None
        else:
            payload = http.Form(values)
        return self._transport(method, url, {}, payload)

    def _error(self, status: int, body) -> str:
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            message = error.get("message") or error.get("type")
            text = str(message) if message else http.error_text(status, body)
        else:
            text = http.error_text(status, body)
        return text.replace(self.access_token, "[redacted]")[:300]

    def _failure(self, action: str, status: int, body) -> DeliveryResult:
        message = self._error(status, body)
        if 200 <= status < 300 and message == f"HTTP {status}":
            message = "Meta returned an unexpected response."
        return DeliveryResult(
            success=False,
            error=f"{self.name} {action} failed: {message}",
            retryable=http.retryable(status),
            idempotent=False,
        )

    def _public_image_url(self, announcement: Announcement) -> str | None:
        if not announcement.image:
            return None
        return announcement.image.public_url(announcement.canonical_url)


class FacebookPageAdapter(_MetaAdapter):
    """Publishes text and public images to a Facebook Page."""

    def __init__(
        self,
        page_id: str,
        access_token: str,
        transport: http.Transport | None = None,
    ) -> None:
        super().__init__(access_token, transport)
        self.page_id = page_id

    @property
    def name(self) -> str:
        return "facebook"

    @property
    def graph_url(self) -> str:
        return FACEBOOK_GRAPH

    @property
    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            label="Facebook Page",
            supports_media=True,
            custom_text=True,
            update_mode=UpdateMode.IN_PLACE,
            withdraw_mode=WithdrawMode.DELETE,
        )

    def render_text(self, announcement: Announcement) -> str:
        return http.channel_text(announcement, self.name)

    def publish(self, announcement: Announcement) -> DeliveryResult:
        image_url = self._public_image_url(announcement)
        if announcement.image and not image_url:
            return DeliveryResult(
                success=False,
                error="Facebook image publishing requires a public announcement feed URL.",
            )
        if image_url:
            path = f"/{self.page_id}/photos"
            fields = {"url": image_url, "caption": self.render_text(announcement)}
        else:
            path = f"/{self.page_id}/feed"
            fields = {"message": self.render_text(announcement)}
        status, body = self._call("POST", path, fields)
        if 200 <= status < 300 and isinstance(body, dict):
            external_id = body.get("post_id") or body.get("id")
            if external_id:
                return DeliveryResult(success=True, external_id=str(external_id), idempotent=False)
        return self._failure("post", status, body)

    def update(self, announcement: Announcement) -> DeliveryResult:
        return DeliveryResult(success=False, error="Facebook edits need the published post ID.")

    def update_post(self, announcement: Announcement, external_id: str | None) -> DeliveryResult:
        if not external_id:
            return self.update(announcement)
        status, body = self._call(
            "POST",
            f"/{quote(external_id, safe='')}",
            {"message": self.render_text(announcement)},
        )
        if 200 <= status < 300 and isinstance(body, dict) and body.get("success", True):
            return DeliveryResult(success=True, external_id=external_id, idempotent=False)
        return self._failure("edit", status, body)

    def withdraw(self, announcement_id: str) -> DeliveryResult:
        return DeliveryResult(
            success=False, error="Facebook withdrawal needs the published post ID."
        )

    def withdraw_post(self, announcement_id: str, external_id: str | None) -> DeliveryResult:
        if not external_id:
            return DeliveryResult(success=True, idempotent=True)
        status, body = self._call("DELETE", f"/{quote(external_id, safe='')}")
        if status == 404:
            return DeliveryResult(success=True, idempotent=True)
        if 200 <= status < 300 and isinstance(body, dict) and body.get("success", True):
            return DeliveryResult(success=True, idempotent=True)
        return self._failure("withdrawal", status, body)

    def health_check(self) -> HealthResult:
        status, body = self._call("GET", f"/{self.page_id}", {"fields": "id,name"})
        if status == 200 and isinstance(body, dict):
            if body.get("id") != self.page_id:
                return HealthResult(
                    ok=False,
                    details="The token does not have access to the configured Facebook Page.",
                )
            return HealthResult(
                ok=True, details=f"Connected to Facebook Page {body.get('name', self.page_id)}."
            )
        return HealthResult(ok=False, details=f"Facebook refused: {self._error(status, body)}")


class InstagramAdapter(http.ShortTextChannel, _MetaAdapter):
    """Publishes image posts to an Instagram professional account."""

    max_characters = INSTAGRAM_MAX_CHARACTERS

    def __init__(
        self,
        account_id: str,
        access_token: str,
        transport: http.Transport | None = None,
    ) -> None:
        super().__init__(access_token, transport)
        self.account_id = account_id

    @property
    def name(self) -> str:
        return "instagram"

    @property
    def graph_url(self) -> str:
        return INSTAGRAM_GRAPH

    @property
    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            label="Instagram",
            max_text_length=INSTAGRAM_MAX_CHARACTERS,
            supports_media=True,
            shortens_long_text=True,
            custom_text=True,
            update_mode=UpdateMode.NONE,
            withdraw_mode=WithdrawMode.NONE,
        )

    def publish(self, announcement: Announcement) -> DeliveryResult:
        image_url = self._public_image_url(announcement)
        if not image_url:
            return DeliveryResult(
                success=False,
                error="Instagram publishing requires an image on a public announcement feed.",
            )
        status, body = self._call(
            "POST",
            f"/{self.account_id}/media",
            {
                "image_url": image_url,
                "alt_text": announcement.image.alt,
                "caption": self.preview_text(announcement),
            },
        )
        container_id = str(body.get("id", "")) if isinstance(body, dict) else ""
        if status not in (200, 201) or not container_id:
            return self._failure("image preparation", status, body)
        status, body = self._call(
            "POST",
            f"/{self.account_id}/media_publish",
            {"creation_id": container_id},
        )
        post_id = str(body.get("id", "")) if isinstance(body, dict) else ""
        if status in (200, 201) and post_id:
            return DeliveryResult(success=True, external_id=post_id, idempotent=False)
        return self._failure("post", status, body)

    def update(self, announcement: Announcement) -> DeliveryResult:
        return DeliveryResult(
            success=False, error="Instagram posts cannot be edited through this API."
        )

    def withdraw(self, announcement_id: str) -> DeliveryResult:
        return DeliveryResult(
            success=False,
            error="Instagram posts cannot be withdrawn through this API.",
        )

    def health_check(self) -> HealthResult:
        status, body = self._call("GET", "/me", {"fields": "id,username"})
        if status == 200 and isinstance(body, dict):
            if body.get("id") != self.account_id:
                return HealthResult(
                    ok=False, details="The token does not match the configured Instagram account."
                )
            return HealthResult(
                ok=True, details=f"Connected as @{body.get('username', self.account_id)}."
            )
        return HealthResult(ok=False, details=f"Instagram refused: {self._error(status, body)}")


class ThreadsAdapter(http.ShortTextChannel, _MetaAdapter):
    """Publishes text or public-image posts to a Threads account."""

    max_characters = THREADS_MAX_CHARACTERS

    def __init__(
        self,
        user_id: str,
        access_token: str,
        transport: http.Transport | None = None,
    ) -> None:
        super().__init__(access_token, transport)
        self.user_id = user_id

    @property
    def name(self) -> str:
        return "threads"

    @property
    def graph_url(self) -> str:
        return THREADS_GRAPH

    @property
    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            label="Threads",
            max_text_length=THREADS_MAX_CHARACTERS,
            supports_media=True,
            shortens_long_text=True,
            custom_text=True,
            update_mode=UpdateMode.NONE,
            withdraw_mode=WithdrawMode.DELETE,
        )

    def publish(self, announcement: Announcement) -> DeliveryResult:
        image_url = self._public_image_url(announcement)
        if announcement.image and not image_url:
            return DeliveryResult(
                success=False,
                error="Threads image publishing requires a public announcement feed URL.",
            )
        fields = {
            "media_type": "IMAGE" if image_url else "TEXT",
            "text": self.preview_text(announcement),
        }
        if image_url:
            fields.update(
                image_url=image_url,
                alt_text=announcement.image.alt,
            )
        status, body = self._call("POST", f"/{self.user_id}/threads", fields)
        container_id = str(body.get("id", "")) if isinstance(body, dict) else ""
        if status not in (200, 201) or not container_id:
            return self._failure("post preparation", status, body)
        status, body = self._call(
            "POST",
            f"/{self.user_id}/threads_publish",
            {"creation_id": container_id},
        )
        post_id = str(body.get("id", "")) if isinstance(body, dict) else ""
        if status in (200, 201) and post_id:
            return DeliveryResult(success=True, external_id=post_id, idempotent=False)
        return self._failure("post", status, body)

    def update(self, announcement: Announcement) -> DeliveryResult:
        return DeliveryResult(success=False, error="Threads posts cannot be edited.")

    def withdraw(self, announcement_id: str) -> DeliveryResult:
        return DeliveryResult(
            success=False, error="Threads withdrawal needs the published post ID."
        )

    def withdraw_post(self, announcement_id: str, external_id: str | None) -> DeliveryResult:
        if not external_id:
            return DeliveryResult(success=True, idempotent=True)
        status, body = self._call("DELETE", f"/{quote(external_id, safe='')}")
        if status == 404:
            return DeliveryResult(success=True, idempotent=True)
        if 200 <= status < 300 and isinstance(body, dict) and body.get("success", True):
            return DeliveryResult(success=True, idempotent=True)
        return self._failure("withdrawal", status, body)

    def health_check(self) -> HealthResult:
        status, body = self._call("GET", "/me", {"fields": "id,username"})
        if status == 200 and isinstance(body, dict):
            if body.get("id") != self.user_id:
                return HealthResult(
                    ok=False, details="The token does not match the configured Threads account."
                )
            return HealthResult(
                ok=True, details=f"Connected as @{body.get('username', self.user_id)}."
            )
        return HealthResult(ok=False, details=f"Threads refused: {self._error(status, body)}")
