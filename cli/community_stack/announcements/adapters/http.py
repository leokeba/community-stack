"""HTTP and text helpers shared by the social and messaging connectors."""

from __future__ import annotations

import ipaddress
import json
import secrets
import socket
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from community_stack.announcements.formatting import to_plain_text
from community_stack.announcements.models import Announcement

# (method, url, headers, json payload) -> (HTTP status or 0 for network errors, decoded body)
Transport = Callable[[str, str, dict[str, str], Any], tuple[int, Any]]

TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class Multipart:
    """A multipart/form-data payload: text fields plus (field, filename, type, bytes) files."""

    fields: dict[str, str]
    files: tuple[tuple[str, str, str, bytes], ...]

    def encode(self) -> tuple[bytes, str]:
        boundary = secrets.token_hex(16)
        parts = []
        for name, value in self.fields.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n".encode()
            )
        for name, filename, content_type, content in self.files:
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
                f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'.encode()
                + content
                + b"\r\n"
            )
        parts.append(f"--{boundary}--\r\n".encode())
        return b"".join(parts), f"multipart/form-data; boundary={boundary}"


@dataclass(frozen=True)
class RawBody:
    content: bytes
    content_type: str


class UnsafeURLError(ValueError):
    """A URL the console must not contact: not HTTPS, or not on the public internet."""


def resolve(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)]


def assert_public_https(url: str, allowed_hosts: set[str] | None = None) -> str:
    """Return ``url`` if it is HTTPS and every address it resolves to is public.

    Connector URLs are typed in by organizers; without this check the console
    could be pointed at services that are only meant to be reachable inside
    the server, such as Synapse or other containers.
    """
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise UnsafeURLError("Use an https:// address.")
    if parts.username or parts.password:
        raise UnsafeURLError("Addresses must not contain a user name or password.")
    host = parts.hostname.lower()
    if allowed_hosts is not None and host not in allowed_hosts:
        raise UnsafeURLError(f"Use an address on {', '.join(sorted(allowed_hosts))}.")
    try:
        addresses = resolve(host)
    except OSError as exc:
        raise UnsafeURLError(f"Could not find the server {host}.") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
        if not ip.is_global:
            raise UnsafeURLError(f"{host} points to a private network address.")
    return url


def send(method: str, url: str, headers: dict[str, str], payload: Any) -> tuple[int, Any]:
    """Default transport: JSON over HTTPS to public addresses only."""
    try:
        assert_public_https(url)
    except UnsafeURLError as exc:
        return 0, {"error": str(exc)}
    request_headers = {"User-Agent": "community-stack", **headers}
    if isinstance(payload, Multipart):
        data, request_headers["Content-Type"] = payload.encode()
    elif isinstance(payload, RawBody):
        data, request_headers["Content-Type"] = payload.content, payload.content_type
    elif payload is not None:
        data = json.dumps(payload).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    else:
        data = None
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, _decode(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _decode(exc.read())
    except (urllib.error.URLError, OSError) as exc:
        return 0, {"error": f"Network error: {getattr(exc, 'reason', exc)}"}


def _decode(raw: bytes) -> Any:
    text = raw.decode("utf-8", errors="replace")
    if not text:
        return {}
    try:
        return json.loads(text)
    except ValueError:
        return {"raw": text[:500]}


def retryable(status: int) -> bool:
    return status == 0 or status == 429 or status >= 500


def error_text(status: int, body: Any, *keys: str) -> str:
    if isinstance(body, dict):
        for key in (*keys, "error", "message", "description", "raw"):
            if body.get(key):
                return f"{body[key]}"[:300]
    return f"HTTP {status}"


def channel_text(announcement: Announcement, channel: str | None = None) -> str:
    """The channel's custom text if one was written, else the title and plain-text body."""
    custom = announcement.channel_text.get(channel or "", "").strip()
    return custom or f"{announcement.title}\n\n{to_plain_text(announcement.body)}"


def compose_post(announcement: Announcement, limit: int, channel: str | None = None) -> str:
    """The channel's text, shortened with a link to the full announcement when too long."""
    full = channel_text(announcement, channel)
    if len(full) <= limit:
        return full
    suffix = f"… {announcement.canonical_url}" if announcement.canonical_url else "…"
    return full[: max(limit - len(suffix), 0)].rstrip() + suffix


class ShortTextChannel:
    """Mixin for channels with a text limit that take custom per-channel text."""

    name: str
    max_characters: int

    def render_text(self, announcement: Announcement) -> str:
        return channel_text(announcement, self.name)

    def preview_text(self, announcement: Announcement) -> str:
        return compose_post(announcement, self.max_characters, self.name)
