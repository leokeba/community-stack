"""Matrix announcement room adapter implementing the ChannelAdapter contract."""

from __future__ import annotations

import hashlib
import html
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from urllib.parse import quote

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


class MatrixRoomAdapter(ChannelAdapter):
    """Publishes canonical announcements to a designated Matrix announcement room."""

    def __init__(
        self,
        homeserver_url: str = "http://localhost:8008",
        room_id: str = "!announcements:example.org",
        access_token: str | None = None,
        transport: Callable[[str, str, dict, dict], tuple[int, dict]] | None = None,
    ) -> None:
        self.homeserver_url = homeserver_url.rstrip("/")
        self.room_id = room_id
        self.access_token = access_token
        self._custom_transport = transport

    @property
    def name(self) -> str:
        return "matrix_room"

    @property
    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            label="Matrix room",
            update_mode=UpdateMode.FOLLOW_UP,
            withdraw_mode=WithdrawMode.NOTICE,
        )

    def render_text(self, announcement: Announcement) -> str:
        return f"📢 {announcement.title}\n\n{to_plain_text(announcement.body)}"

    def _http_request(
        self, method: str, path: str, payload: dict | None = None
    ) -> tuple[int, dict]:
        """Perform HTTP request or invoke custom transport callable."""
        headers = {
            "Content-Type": "application/json",
        }
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        if self._custom_transport:
            return self._custom_transport(method, path, headers, payload or {})

        url = f"{self.homeserver_url}{path}"
        data = json.dumps(payload).encode("utf-8") if payload else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)

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
        except (OSError, TimeoutError, urllib.error.URLError, json.JSONDecodeError) as exc:
            return 503, {"error": str(exc)}

    def publish(self, announcement: Announcement) -> DeliveryResult:
        """Post a new announcement event to the Matrix room with idempotency txnId."""
        # Use announcement ID in txnId for idempotency
        txn_id = f"pub_{announcement.id}"
        path = (
            f"/_matrix/client/v3/rooms/{quote(self.room_id, safe='')}/send/m.room.message/{txn_id}"
        )

        formatted_html = f"<h3>📢 {html.escape(announcement.title)}</h3>" + render_html(
            announcement.body
        )
        if announcement.author:
            formatted_html += f"<p><em>— {html.escape(announcement.author)}</em></p>"

        payload = {
            "msgtype": "m.notice",
            "body": self.render_text(announcement),
            "format": "org.matrix.custom.html",
            "formatted_body": formatted_html,
            "org.community_stack.announcement_id": announcement.id,
        }

        status_code, resp = self._http_request("PUT", path, payload)
        if 200 <= status_code < 300:
            event_id = resp.get("event_id")
            return DeliveryResult(success=True, external_id=event_id, idempotent=True)

        error_msg = resp.get("error", f"HTTP {status_code}")
        is_retryable = status_code >= 500 or status_code == 429
        return DeliveryResult(
            success=False,
            error=f"Matrix send failed: {error_msg}",
            retryable=is_retryable,
            idempotent=True,
        )

    def update(self, announcement: Announcement) -> DeliveryResult:
        """Post an updated notice or edit to the room."""
        revision = hashlib.sha256(
            f"{announcement.title}\0{announcement.body}\0{announcement.author or ''}".encode()
        ).hexdigest()[:16]
        txn_id = f"upd_{announcement.id}_{revision}"
        path = (
            f"/_matrix/client/v3/rooms/{quote(self.room_id, safe='')}/send/m.room.message/{txn_id}"
        )

        formatted_html = f"<h3>📢 [UPDATED] {html.escape(announcement.title)}</h3>" + render_html(
            announcement.body
        )
        payload = {
            "msgtype": "m.notice",
            "body": f"📢 [UPDATED] {announcement.title}\n\n{to_plain_text(announcement.body)}",
            "format": "org.matrix.custom.html",
            "formatted_body": formatted_html,
            "org.community_stack.announcement_id": announcement.id,
        }

        status_code, resp = self._http_request("PUT", path, payload)
        if 200 <= status_code < 300:
            return DeliveryResult(success=True, external_id=resp.get("event_id"), idempotent=True)

        return DeliveryResult(
            success=False,
            error=f"Matrix update failed: {resp.get('error', status_code)}",
            retryable=status_code >= 500 or status_code == 429,
            idempotent=True,
        )

    def withdraw(self, announcement_id: str) -> DeliveryResult:
        """Post a withdrawal notice to the room."""
        txn_id = f"withdraw_{announcement_id}"
        path = (
            f"/_matrix/client/v3/rooms/{quote(self.room_id, safe='')}/send/m.room.message/{txn_id}"
        )

        payload = {
            "msgtype": "m.notice",
            "body": f"⚠️ Notice: Announcement {announcement_id} has been withdrawn by the organizers.",
            "org.community_stack.announcement_id": announcement_id,
        }

        status_code, resp = self._http_request("PUT", path, payload)
        if 200 <= status_code < 300:
            return DeliveryResult(success=True, idempotent=True)

        return DeliveryResult(
            success=False,
            error=f"Matrix withdrawal notice failed: {resp.get('error', status_code)}",
            retryable=status_code >= 500 or status_code == 429,
            idempotent=True,
        )

    def verify_access(self, require_public: bool = True) -> HealthResult:
        """Check credentials, room membership and optionally public-room visibility."""
        if not self.access_token:
            return HealthResult(ok=False, details="A Matrix access token is required.")

        status, account = self._http_request("GET", "/_matrix/client/v3/account/whoami")
        if not 200 <= status < 300 or not account.get("user_id"):
            return HealthResult(ok=False, details=f"Matrix login failed (HTTP {status}).")

        status, joined = self._http_request("GET", "/_matrix/client/v3/joined_rooms")
        if not 200 <= status < 300:
            return HealthResult(
                ok=False, details=f"Could not list joined Matrix rooms (HTTP {status})."
            )
        if self.room_id not in joined.get("joined_rooms", []):
            return HealthResult(
                ok=False,
                details=f"{account['user_id']} must join {self.room_id} before this channel can post.",
            )

        room_path = quote(self.room_id, safe="")
        status, encryption = self._http_request(
            "GET", f"/_matrix/client/v3/rooms/{room_path}/state/m.room.encryption"
        )
        if status == 200 and encryption:
            return HealthResult(
                ok=False,
                details="This sender cannot post to an end-to-end encrypted room. Use an unencrypted room.",
            )
        if status != 404:
            return HealthResult(
                ok=False,
                details=f"Could not verify the Matrix room's encryption state (HTTP {status}).",
            )

        if require_public:
            status, visibility = self._http_request(
                "GET", f"/_matrix/client/v3/directory/list/room/{room_path}"
            )
            if status != 200 or visibility.get("visibility") != "public":
                return HealthResult(
                    ok=False,
                    details="The announcements room must be published in the public Matrix room directory.",
                )
        return HealthResult(ok=True, details=f"Matrix access confirmed for {account['user_id']}.")

    def health_check(self) -> HealthResult:
        """Check Matrix credentials, membership and public announcement room visibility."""
        return self.verify_access(require_public=True)
