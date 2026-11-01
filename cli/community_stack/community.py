"""Community identity and independent channel resolution for the /connect page.

The profile comes from the optional ``community:`` block in community-stack.yaml.
Channels are shown only when they are both switched on and actually working, so
the page never advertises a channel that cannot be used.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from community_stack.config import load_config

SOCIAL_PLATFORMS: dict[str, str] = {
    "facebook": "Facebook",
    "instagram": "Instagram",
    "threads": "Threads",
    "x": "X",
    "tiktok": "TikTok",
    "youtube": "YouTube",
    "linkedin": "LinkedIn",
    "whatsapp": "WhatsApp",
    "telegram": "Telegram",
    "mastodon": "Mastodon",
    "bluesky": "Bluesky",
    "discord": "Discord",
    "signal": "Signal",
    "other": "Other",
}

INDEPENDENT_KEYS = ("website", "email", "matrix", "activitypub", "rss")


class ProfileError(ValueError):
    """Raised when the community profile cannot be used as configured."""


@dataclass(frozen=True)
class SocialAccount:
    platform: str
    url: str
    handle: str | None = None

    @property
    def label(self) -> str:
        return SOCIAL_PLATFORMS.get(self.platform, self.platform.title())


@dataclass(frozen=True)
class CommunityProfile:
    name: str
    url: str
    domain: str
    base_domain: str
    description: str = ""
    logo: str | None = None
    contact_email: str | None = None
    emergency_instructions: str = ""
    social: tuple[SocialAccount, ...] = ()
    independent: dict[str, bool] = field(default_factory=dict)

    @property
    def connect_url(self) -> str:
        return f"{self.url}/connect/"


@dataclass(frozen=True)
class IndependentChannel:
    key: str
    label: str
    description: str
    url: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class ChannelResolution:
    shown: list[IndependentChannel]
    hidden: list[tuple[str, str]]  # (key, reason) for channels not shown


def load_profile(config: dict[str, Any], domain_web: str | None = None) -> CommunityProfile:
    """Build the profile from a loaded configuration, with defaults for older configs."""
    project = config.get("project", {})
    domain = config.get("domain", {})
    community = config.get("community") or {}
    web_domain = domain_web or domain.get("web") or "community.example.org"
    base_domain = domain.get("base") or web_domain

    url = (community.get("url") or f"https://{web_domain}").rstrip("/")
    contact = community.get("contact") or {}
    independent = dict(community.get("independent") or {})
    independent.setdefault("website", True)
    independent.setdefault("rss", True)
    for key in INDEPENDENT_KEYS:
        independent.setdefault(key, False)

    social = tuple(
        SocialAccount(
            platform=item["platform"],
            url=item["url"].rstrip("/"),
            handle=item.get("handle"),
        )
        for item in community.get("social", [])
    )
    return CommunityProfile(
        name=community.get("name") or project.get("name") or "Community",
        url=url,
        domain=web_domain,
        base_domain=base_domain,
        description=community.get("description", ""),
        logo=community.get("logo"),
        contact_email=contact.get("email"),
        emergency_instructions=community.get("emergency_instructions", ""),
        social=social,
        independent=independent,
    )


def load_community(config_path: Path | str) -> tuple[CommunityProfile, dict[str, Any]]:
    """Load the profile from disk. A missing configuration file gives the defaults."""
    path = Path(config_path)
    if not path.is_file():
        return load_profile({}), {}
    config = load_config(path)
    return load_profile(config), config


def logo_path(config_path: Path | str, profile: CommunityProfile) -> Path | None:
    """The logo file, resolved next to the configuration file. Never outside it."""
    if not profile.logo:
        return None
    return Path(config_path).parent / profile.logo


def address_warnings(url: str, profile: CommunityProfile) -> list[str]:
    """Warn when an address will not survive a move to another host."""
    host = urlsplit(url).hostname or ""
    base = profile.base_domain
    on_domain = host in (profile.domain, base) or host.endswith("." + base)
    if on_domain:
        return []
    return [
        f"{url} is not on {profile.domain} or the community's own domain. Links will break if the hosting provider changes."
    ]


def matrix_room_url(connections: dict[str, Any], config: dict[str, Any]) -> str | None:
    """A matrix.to link to the public announcements room, when the room is set up."""
    channels = config.get("services", {}).get("announcements", {}).get("channels", {})
    configured = channels.get("matrix_room", {})
    room = connections.get("matrix_room") or (configured if configured.get("enabled") else None)
    if not room or not room.get("room_id"):
        return None
    return f"https://matrix.to/#/{quote(room['room_id'], safe='!:')}"


def resolve_independent(
    profile: CommunityProfile,
    connections: dict[str, Any],
    config: dict[str, Any],
) -> ChannelResolution:
    """Work out which independent channels can be shown, and why others cannot."""
    candidates: dict[str, IndependentChannel | str] = {}
    base = profile.url
    candidates["website"] = IndependentChannel(
        key="website",
        label="Our website",
        description="Read the latest announcements on our website, with no account needed.",
        url=f"{base}/feed/announcements.html",
    )
    candidates["rss"] = IndependentChannel(
        key="rss",
        label="Feeds (RSS, Atom and JSON Feed)",
        description="Let a feed reader or app tell you when something new is published.",
        url=f"{base}/feed/rss.xml",
    )
    candidates["email"] = (
        IndependentChannel(
            key="email",
            label="Email",
            description="Get announcements in your inbox. You confirm before anything is sent.",
            url=f"{base}/subscribe/email",
        )
        if "email" in connections
        else "No email server is connected."
    )

    matrix = matrix_room_url(connections, config)
    candidates["matrix"] = (
        IndependentChannel(
            key="matrix",
            label="Matrix chat room",
            description="Follow announcements in a Matrix app, such as Element.",
            url=matrix,
        )
        if matrix
        else "No public Matrix room is connected."
    )

    fediverse = connections.get("fediverse")
    candidates["activitypub"] = (
        IndependentChannel(
            key="activitypub",
            label="Fediverse (Mastodon and compatible apps)",
            description="Follow us from Mastodon or another fediverse app.",
            detail=f"@{fediverse['username']}@{profile.domain}",
        )
        if fediverse and fediverse.get("username")
        else "No fediverse account is connected."
    )

    shown: list[IndependentChannel] = []
    hidden: list[tuple[str, str]] = []
    for key in INDEPENDENT_KEYS:
        candidate = candidates[key]
        if not profile.independent.get(key):
            hidden.append((key, "switched off in community.independent"))
        elif isinstance(candidate, str):
            hidden.append((key, candidate))
        else:
            shown.append(candidate)
    return ChannelResolution(shown=shown, hidden=hidden)


def connection_warnings(connections: dict[str, Any]) -> list[str]:
    """Warn about setups that would send the same email twice."""
    if "email" in connections and "listmonk" in connections:
        message = (
            "Both your own email server and listmonk are connected. A person on a listmonk "
            "list can also be subscribed here, so they may receive each announcement twice. "
            "Keep one of them."
        )
        return [message]
    return []
