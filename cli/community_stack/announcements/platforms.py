"""Static platform metadata for automatic and manual announcement publishing."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from community_stack.announcements.adapters.base import (
    ChannelCapabilities,
    UpdateMode,
    WithdrawMode,
)
from community_stack.announcements.adapters.messaging import (
    DISCORD_MAX_CHARACTERS,
    TELEGRAM_MAX_CHARACTERS,
)
from community_stack.announcements.adapters.meta import (
    INSTAGRAM_MAX_CHARACTERS,
    THREADS_MAX_CHARACTERS,
)
from community_stack.announcements.adapters.signal import SIGNAL_MAX_CHARACTERS
from community_stack.announcements.adapters.social import (
    BLUESKY_MAX_CHARACTERS,
    MASTODON_MAX_CHARACTERS,
    PIXELFED_MAX_CHARACTERS,
)
from community_stack.announcements.adapters.whatsapp import (
    WHATSAPP_MAX_TEXT_LENGTH,
)
from community_stack.community import SOCIAL_PLATFORMS


class PublicationMode(str, Enum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"


@dataclass(frozen=True)
class PlatformSpec:
    """Platform facts only; credentials, handles and enabled state remain per-community."""

    identifier: str
    name: str
    capabilities: ChannelCapabilities
    supported_media: frozenset[str] | None
    publication_modes: frozenset[PublicationMode]
    connector_name: str | None
    character_limit_verified: bool
    notes: str = ""
    official_url: str | None = None


_OFFICIAL_URLS = {
    "facebook": "https://www.facebook.com/",
    "instagram": "https://www.instagram.com/",
    "threads": "https://www.threads.net/",
    "x": "https://x.com/",
    "tiktok": "https://www.tiktok.com/",
    "youtube": "https://www.youtube.com/",
    "linkedin": "https://www.linkedin.com/",
    "whatsapp": "https://www.whatsapp.com/",
    "telegram": "https://telegram.org/",
    "mastodon": "https://joinmastodon.org/",
    "pixelfed": "https://pixelfed.org/",
    "bluesky": "https://bsky.app/",
    "discord": "https://discord.com/",
    "signal": "https://signal.org/",
}


def _platform(
    identifier: str,
    capabilities: ChannelCapabilities | None = None,
    supported_media: frozenset[str] | None = None,
    *,
    connector_name: str | None = None,
    character_limit_verified: bool = False,
    notes: str = "",
) -> PlatformSpec:
    name = SOCIAL_PLATFORMS[identifier]
    modes = {PublicationMode.MANUAL}
    if connector_name:
        modes.add(PublicationMode.AUTOMATIC)
    if capabilities is None:
        capabilities = ChannelCapabilities(label=name, custom_text=True)
    return PlatformSpec(
        identifier=identifier,
        name=name,
        capabilities=capabilities,
        supported_media=supported_media,
        publication_modes=frozenset(modes),
        connector_name=connector_name,
        character_limit_verified=character_limit_verified,
        notes=notes,
        official_url=_OFFICIAL_URLS.get(identifier),
    )


_AUTOMATIC_PLATFORMS = {
    "facebook": _platform(
        "facebook",
        ChannelCapabilities(
            label="Facebook Page",
            supports_media=True,
            custom_text=True,
            update_mode=UpdateMode.IN_PLACE,
            withdraw_mode=WithdrawMode.DELETE,
        ),
        frozenset({"image"}),
        connector_name="facebook",
        notes="The current adapter does not declare a character limit.",
    ),
    "instagram": _platform(
        "instagram",
        ChannelCapabilities(
            label="Instagram",
            max_text_length=INSTAGRAM_MAX_CHARACTERS,
            supports_media=True,
            shortens_long_text=True,
            custom_text=True,
        ),
        frozenset({"image"}),
        connector_name="instagram",
        character_limit_verified=True,
        notes="Publishing requires an image on every announcement.",
    ),
    "threads": _platform(
        "threads",
        ChannelCapabilities(
            label="Threads",
            max_text_length=THREADS_MAX_CHARACTERS,
            supports_media=True,
            shortens_long_text=True,
            custom_text=True,
            withdraw_mode=WithdrawMode.DELETE,
        ),
        frozenset({"image"}),
        connector_name="threads",
        character_limit_verified=True,
    ),
    "mastodon": _platform(
        "mastodon",
        ChannelCapabilities(
            label="Mastodon",
            max_text_length=MASTODON_MAX_CHARACTERS,
            supports_media=True,
            shortens_long_text=True,
            custom_text=True,
            update_mode=UpdateMode.IN_PLACE,
            withdraw_mode=WithdrawMode.DELETE,
        ),
        frozenset({"image"}),
        connector_name="mastodon",
        character_limit_verified=True,
    ),
    "pixelfed": _platform(
        "pixelfed",
        ChannelCapabilities(
            label="Pixelfed",
            max_text_length=PIXELFED_MAX_CHARACTERS,
            supports_media=True,
            shortens_long_text=True,
            custom_text=True,
            update_mode=UpdateMode.IN_PLACE,
            withdraw_mode=WithdrawMode.DELETE,
        ),
        frozenset({"image"}),
        connector_name="pixelfed",
        notes=(
            "The 2,000-character value is a fallback; the connection check discovers "
            "the instance's actual caption limit."
        ),
    ),
    "bluesky": _platform(
        "bluesky",
        ChannelCapabilities(
            label="Bluesky",
            max_text_length=BLUESKY_MAX_CHARACTERS,
            supports_media=True,
            shortens_long_text=True,
            custom_text=True,
            withdraw_mode=WithdrawMode.DELETE,
        ),
        frozenset({"image"}),
        connector_name="bluesky",
        character_limit_verified=True,
    ),
    "telegram": _platform(
        "telegram",
        ChannelCapabilities(
            label="Telegram channel",
            max_text_length=TELEGRAM_MAX_CHARACTERS,
            shortens_long_text=True,
            custom_text=True,
            update_mode=UpdateMode.IN_PLACE,
            withdraw_mode=WithdrawMode.DELETE,
        ),
        frozenset(),
        connector_name="telegram",
        character_limit_verified=True,
    ),
    "whatsapp": _platform(
        "whatsapp",
        ChannelCapabilities(
            label="WhatsApp announcements",
            max_text_length=WHATSAPP_MAX_TEXT_LENGTH,
            update_mode=UpdateMode.FOLLOW_UP,
            withdraw_mode=WithdrawMode.NOTICE,
        ),
        frozenset(),
        connector_name="whatsapp",
        character_limit_verified=True,
        notes="Automatic delivery posts in groups chosen on the Channels page, through the "
        "WhatsApp bridge.",
    ),
    "discord": _platform(
        "discord",
        ChannelCapabilities(
            label="Discord channel",
            max_text_length=DISCORD_MAX_CHARACTERS,
            supports_media=True,
            shortens_long_text=True,
            custom_text=True,
            update_mode=UpdateMode.IN_PLACE,
            withdraw_mode=WithdrawMode.DELETE,
        ),
        frozenset({"image"}),
        connector_name="discord",
        character_limit_verified=True,
    ),
    "signal": _platform(
        "signal",
        ChannelCapabilities(
            label="Signal group (experimental)",
            max_text_length=SIGNAL_MAX_CHARACTERS,
            shortens_long_text=True,
            custom_text=True,
            update_mode=UpdateMode.IN_PLACE,
            withdraw_mode=WithdrawMode.DELETE,
        ),
        frozenset(),
        connector_name="signal",
        character_limit_verified=True,
        notes="The adapter uses an unofficial Signal client and is experimental.",
    ),
}

_UNVERIFIED_NOTE = "Character limits and media support have not been verified for this platform."
PLATFORM_CATALOG = tuple(
    _AUTOMATIC_PLATFORMS.get(
        identifier,
        _platform(
            identifier,
            supported_media=None,
            notes=_UNVERIFIED_NOTE,
        ),
    )
    for identifier in SOCIAL_PLATFORMS
)
PLATFORMS: Mapping[str, PlatformSpec] = MappingProxyType(
    {platform.identifier: platform for platform in PLATFORM_CATALOG}
)
