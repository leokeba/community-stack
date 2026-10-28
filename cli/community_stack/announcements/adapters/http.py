"""HTTP and text helpers shared by the social and messaging connectors."""

from __future__ import annotations

import ipaddress
import json
import socket
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from community_stack.announcements.models import Announcement

# (method, url, headers, json payload) -> (HTTP status or 0 for network errors, decoded body)
Transport = Callable[[str, str, dict[str, str], Any], tuple[int, Any]]

TIMEOUT_SECONDS = 15


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
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request_headers = {"User-Agent": "community-stack", **headers}
    if data is not None:
        request_headers.setdefault("Content-Type", "application/json")
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


def compose_post(announcement: Announcement, limit: int) -> str:
    """Title and body, shortened with a link to the full announcement when too long."""
    full = f"{announcement.title}\n\n{announcement.body}"
    if len(full) <= limit:
        return full
    suffix = f"… {announcement.canonical_url}" if announcement.canonical_url else "…"
    return full[: max(limit - len(suffix), 0)].rstrip() + suffix
