"""The community's own ActivityPub account: discovery, signed delivery, and followers.

Other servers find the account through WebFinger, follow it by sending a signed
Follow to its inbox, and receive each published announcement as a signed
Create(Note). Signatures follow the HTTP Signatures draft that Mastodon uses.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import formatdate, parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

from community_stack.announcements.adapters import http
from community_stack.announcements.adapters.base import (
    ChannelAdapter,
    ChannelCapabilities,
    DeliveryResult,
    HealthResult,
    UpdateMode,
    WithdrawMode,
)
from community_stack.announcements.formatting import render_html
from community_stack.announcements.models import Announcement, AnnouncementStatus
from community_stack.announcements.storage import AnnouncementStorage
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

ACTIVITY_JSON = "application/activity+json"
PUBLIC = "https://www.w3.org/ns/activitystreams#Public"
CONTEXT = ["https://www.w3.org/ns/activitystreams", "https://w3id.org/security/v1"]
MAX_CLOCK_SKEW = timedelta(hours=12)
USERNAME = re.compile(r"^[a-z0-9_]{1,30}$")


class SignatureError(ValueError):
    """An inbox request whose signature could not be verified."""


class Fediverse:
    """The account's identity, keys and followers, stored with the announcements."""

    def __init__(
        self, storage: AnnouncementStorage, base_url: str, username: str = "announcements"
    ):
        self.storage = storage
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.host = urlsplit(self.base_url).hostname or ""
        with storage._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ap_followers (
                    actor_id TEXT PRIMARY KEY,
                    inbox TEXT NOT NULL,
                    shared_inbox TEXT,
                    followed_at TEXT NOT NULL
                )
                """
            )

    # Identity

    @property
    def actor_id(self) -> str:
        return f"{self.base_url}/ap/actor"

    @property
    def key_id(self) -> str:
        return f"{self.actor_id}#main-key"

    def note_id(self, announcement_id: str) -> str:
        return f"{self.base_url}/ap/notes/{announcement_id}"

    def _private_key(self) -> rsa.RSAPrivateKey:
        pem = self.storage.get_setting("ap_private_key", "")
        if not pem:
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            pem = key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ).decode()
            self.storage.set_setting("ap_private_key", pem)
        return serialization.load_pem_private_key(pem.encode(), password=None)

    def public_key_pem(self) -> str:
        return (
            self._private_key()
            .public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode()
        )

    def webfinger(self, resource: str) -> dict | None:
        if resource.lower() not in (f"acct:{self.username}@{self.host}", self.actor_id):
            return None
        return {
            "subject": f"acct:{self.username}@{self.host}",
            "aliases": [self.actor_id],
            "links": [{"rel": "self", "type": ACTIVITY_JSON, "href": self.actor_id}],
        }

    def actor(self, name: str, summary: str) -> dict:
        return {
            "@context": CONTEXT,
            "id": self.actor_id,
            "type": "Service",
            "preferredUsername": self.username,
            "name": name,
            "summary": html.escape(summary),
            "url": f"{self.base_url}/feed/announcements.html",
            "inbox": f"{self.base_url}/ap/inbox",
            "outbox": f"{self.base_url}/ap/outbox",
            "followers": f"{self.base_url}/ap/followers",
            "manuallyApprovesFollowers": False,
            "discoverable": True,
            "publicKey": {
                "id": self.key_id,
                "owner": self.actor_id,
                "publicKeyPem": self.public_key_pem(),
            },
        }

    def note(self, announcement: Announcement) -> dict:
        content = f"<p><strong>{html.escape(announcement.title)}</strong></p>"
        content += render_html(announcement.body)
        if announcement.canonical_url:
            url = html.escape(announcement.canonical_url, quote=True)
            content += f'<p><a href="{url}">{url}</a></p>'
        note = {
            "id": self.note_id(announcement.id),
            "type": "Note",
            "attributedTo": self.actor_id,
            "content": content,
            "published": announcement.published_at,
            "url": announcement.canonical_url or self.note_id(announcement.id),
            "to": [PUBLIC],
            "cc": [f"{self.base_url}/ap/followers"],
            "contentMap": {announcement.language: content},
            "tag": [{"type": "Hashtag", "name": f"#{tag}"} for tag in announcement.tags],
        }
        if announcement.status == AnnouncementStatus.UPDATED:
            note["updated"] = announcement.updated_at
        image = announcement.image
        image_url = image.public_url(announcement.canonical_url) if image else None
        if image_url:
            note["attachment"] = [
                {
                    "type": "Document",
                    "mediaType": image.content_type,
                    "url": image_url,
                    "name": image.alt,
                    "width": image.width,
                    "height": image.height,
                }
            ]
        return note

    def activity(self, kind: str, announcement: Announcement) -> dict:
        suffix = {"Create": "", "Update": f"/update/{announcement.updated_at}"}.get(kind, "")
        return {
            "@context": CONTEXT,
            "id": f"{self.note_id(announcement.id)}/activity{suffix}",
            "type": kind,
            "actor": self.actor_id,
            "published": announcement.updated_at if kind == "Update" else announcement.published_at,
            "to": [PUBLIC],
            "cc": [f"{self.base_url}/ap/followers"],
            "object": self.note(announcement),
        }

    def delete_activity(self, announcement_id: str) -> dict:
        return {
            "@context": CONTEXT,
            "id": f"{self.note_id(announcement_id)}/delete",
            "type": "Delete",
            "actor": self.actor_id,
            "to": [PUBLIC],
            "object": {"id": self.note_id(announcement_id), "type": "Tombstone"},
        }

    # Followers

    def add_follower(self, actor_id: str, inbox: str, shared_inbox: str | None) -> None:
        with self.storage._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO ap_followers (actor_id, inbox, shared_inbox, followed_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(actor_id) DO UPDATE SET inbox=excluded.inbox,
                    shared_inbox=excluded.shared_inbox
                """,
                (actor_id, inbox, shared_inbox, datetime.now(timezone.utc).isoformat()),
            )

    def remove_follower(self, actor_id: str) -> None:
        with self.storage._get_connection() as conn:
            conn.execute("DELETE FROM ap_followers WHERE actor_id = ?", (actor_id,))

    def follower_count(self) -> int:
        with self.storage._get_connection() as conn:
            return conn.execute("SELECT COUNT(*) FROM ap_followers").fetchone()[0]

    def delivery_inboxes(self) -> list[str]:
        """One inbox per server where possible: shared inboxes cover all its followers."""
        with self.storage._get_connection() as conn:
            rows = conn.execute("SELECT inbox, shared_inbox FROM ap_followers").fetchall()
        return sorted({row["shared_inbox"] or row["inbox"] for row in rows})

    # Signatures

    def sign(self, method: str, url: str, body: bytes | None) -> dict[str, str]:
        parts = urlsplit(url)
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        headers = {"Host": parts.netloc, "Date": formatdate(usegmt=True)}
        signed = ["(request-target)", "host", "date"]
        if body is not None:
            headers["Digest"] = (
                "SHA-256=" + base64.b64encode(hashlib.sha256(body).digest()).decode()
            )
            signed.append("digest")
        lines = [f"(request-target): {method.lower()} {target}"] + [
            f"{name}: {headers[name.title()]}" for name in signed[1:]
        ]
        signature = self._private_key().sign(
            "\n".join(lines).encode(), padding.PKCS1v15(), hashes.SHA256()
        )
        headers["Signature"] = (
            f'keyId="{self.key_id}",algorithm="rsa-sha256",headers="{" ".join(signed)}",'
            f'signature="{base64.b64encode(signature).decode()}"'
        )
        return headers

    def fetch(self, url: str, transport: http.Transport) -> dict | None:
        """GET a remote ActivityPub document, signed for servers that require it."""
        headers = {"Accept": ACTIVITY_JSON, **self.sign("GET", url, None)}
        status, body = transport("GET", url, headers, None)
        return body if status == 200 and isinstance(body, dict) else None

    def verify(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
        transport: http.Transport,
    ) -> dict:
        """Check an inbox request's signature; returns the signing actor's document."""
        lowered = {k.lower(): v for k, v in headers.items()}
        fields = dict(re.findall(r'(\w+)="([^"]*)"', lowered.get("signature", "")))
        signed = fields.get("headers", "date").split()
        if not fields.get("keyId") or not fields.get("signature"):
            raise SignatureError("Missing signature.")
        if not {"(request-target)", "host", "date", "digest"} <= set(signed):
            raise SignatureError("The signature must cover the target, host, date and digest.")
        try:
            sent = parsedate_to_datetime(lowered["date"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SignatureError("Missing or invalid Date header.") from exc
        if abs(datetime.now(timezone.utc) - sent) > MAX_CLOCK_SKEW:
            raise SignatureError("The request is too old.")
        expected_digest = "SHA-256=" + base64.b64encode(hashlib.sha256(body).digest()).decode()
        if lowered.get("digest") != expected_digest:
            raise SignatureError("The body does not match its digest.")

        actor_url = fields["keyId"].split("#", 1)[0]
        actor = self.fetch(actor_url, transport)
        key = (actor or {}).get("publicKey") or {}
        if not actor or key.get("id") != fields["keyId"] or not key.get("publicKeyPem"):
            raise SignatureError("Could not fetch the signing key.")
        lines = []
        for name in signed:
            if name == "(request-target)":
                lines.append(f"(request-target): {method.lower()} {path}")
            elif name in lowered:
                lines.append(f"{name}: {lowered[name]}")
            else:
                raise SignatureError(f"Signed header {name} is missing.")
        try:
            public_key = serialization.load_pem_public_key(key["publicKeyPem"].encode())
            public_key.verify(
                base64.b64decode(fields["signature"]),
                "\n".join(lines).encode(),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except (InvalidSignature, ValueError, TypeError) as exc:
            raise SignatureError("The signature does not match.") from exc
        return actor

    # Inbox

    def handle_inbox(
        self, path: str, headers: dict[str, str], body: bytes, transport: http.Transport
    ) -> tuple[int, str]:
        try:
            activity = json.loads(body)
        except ValueError:
            return 400, "Not JSON."
        if not isinstance(activity, dict):
            return 400, "Not an activity."
        try:
            actor = self.verify("POST", path, headers, body, transport)
        except SignatureError as exc:
            return 401, str(exc)
        if activity.get("actor") != actor.get("id"):
            return 401, "The activity's actor did not sign it."

        kind = activity.get("type")
        target = activity.get("object")
        if kind == "Follow" and target == self.actor_id:
            inbox = actor.get("inbox")
            if not isinstance(inbox, str) or not inbox.startswith("https://"):
                return 400, "The follower has no inbox."
            shared = (actor.get("endpoints") or {}).get("sharedInbox")
            self.add_follower(actor["id"], inbox, shared if isinstance(shared, str) else None)
            accept = {
                "@context": CONTEXT,
                "id": f"{self.actor_id}/accept/{hashlib.sha256(str(activity.get('id')).encode()).hexdigest()[:24]}",
                "type": "Accept",
                "actor": self.actor_id,
                "object": activity,
            }
            self.deliver(inbox, accept, transport)
            return 202, "Followed."
        if kind == "Undo" and isinstance(target, dict) and target.get("type") == "Follow":
            self.remove_follower(actor["id"])
            return 202, "Unfollowed."
        if kind == "Delete" and target == actor.get("id"):
            self.remove_follower(actor["id"])
            return 202, "Removed."
        return 202, "Ignored."

    def deliver(self, inbox: str, activity: dict, transport: http.Transport) -> tuple[int, Any]:
        body = json.dumps(activity).encode()
        headers = self.sign("POST", inbox, body)
        return transport("POST", inbox, headers, http.RawBody(body, ACTIVITY_JSON))


class FediverseAdapter(ChannelAdapter):
    """Delivers announcements to everyone following the community's own account."""

    def __init__(self, fediverse: Fediverse, transport: http.Transport | None = None) -> None:
        self.fediverse = fediverse
        self._transport = transport or http.send

    @property
    def name(self) -> str:
        return "fediverse"

    @property
    def capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            label=f"Fediverse (@{self.fediverse.username}@{self.fediverse.host})",
            supports_media=True,
            update_mode=UpdateMode.IN_PLACE,
            withdraw_mode=WithdrawMode.DELETE,
        )

    def _deliver_all(self, activity: dict, action: str) -> DeliveryResult:
        failures = []
        for inbox in self.fediverse.delivery_inboxes():
            status, body = self.fediverse.deliver(inbox, activity, self._transport)
            # 410 Gone means the follower's server no longer exists.
            if not (200 <= status < 300 or status == 410):
                failures.append(f"{urlsplit(inbox).hostname}: {http.error_text(status, body)}")
        if failures:
            return DeliveryResult(
                success=False,
                error=f"Fediverse {action} reached some servers but not: "
                + "; ".join(failures[:5]),
                retryable=True,
                # Servers deduplicate activities by ID, so delivering again is safe.
                idempotent=True,
            )
        return DeliveryResult(success=True, external_id=activity["id"], idempotent=True)

    def publish(self, announcement: Announcement) -> DeliveryResult:
        return self._deliver_all(self.fediverse.activity("Create", announcement), "delivery")

    def update(self, announcement: Announcement) -> DeliveryResult:
        return self._deliver_all(self.fediverse.activity("Update", announcement), "edit")

    def withdraw(self, announcement_id: str) -> DeliveryResult:
        return self._deliver_all(self.fediverse.delete_activity(announcement_id), "deletion")

    def health_check(self) -> HealthResult:
        count = self.fediverse.follower_count()
        return HealthResult(
            ok=True,
            details=f"@{self.fediverse.username}@{self.fediverse.host} has {count} follower(s).",
        )
