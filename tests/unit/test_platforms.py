"""Static platform metadata for automatic and manual announcement publishing."""

from dataclasses import fields

import pytest
from community_stack.announcements.connectors import CONNECTORS
from community_stack.announcements.platforms import (
    PLATFORM_CATALOG,
    PLATFORMS,
    PlatformSpec,
    PublicationMode,
)
from community_stack.community import SOCIAL_PLATFORMS

AUTOMATIC_CONNECTOR_VALUES = {
    "mastodon": {"instance_url": "https://example.org", "access_token": "token"},
    "pixelfed": {"instance_url": "https://example.org", "access_token": "token"},
    "bluesky": {
        "handle": "community.example",
        "app_password": "xxxx-xxxx-xxxx-xxxx",
        "service_url": "https://bsky.social",
    },
    "facebook": {"page_id": "123456", "access_token": "token"},
    "instagram": {"account_id": "123456", "access_token": "token"},
    "threads": {"user_id": "123456", "access_token": "token"},
    "telegram": {"chat_id": "@example", "bot_token": "token"},
    "discord": {"webhook_url": "https://discord.example/webhook"},
    "signal": {"number": "+15555550123", "group_id": "group"},
}


def test_catalogue_covers_the_community_social_platforms_in_display_order():
    assert [platform.identifier for platform in PLATFORM_CATALOG] == list(SOCIAL_PLATFORMS)
    assert set(PLATFORMS) == set(SOCIAL_PLATFORMS)
    assert len(PLATFORM_CATALOG) == len(PLATFORMS)
    assert all(PLATFORMS[key].name == name for key, name in SOCIAL_PLATFORMS.items())


def test_catalogue_contains_no_per_community_account_data():
    per_community_fields = {
        "account_id",
        "access_token",
        "enabled",
        "handle",
        "instance_url",
        "url",
    }
    assert isinstance(PLATFORM_CATALOG[0], PlatformSpec)
    assert not per_community_fields.intersection(field.name for field in fields(PlatformSpec))


@pytest.mark.parametrize("identifier,values", AUTOMATIC_CONNECTOR_VALUES.items())
def test_automatic_catalogue_capabilities_match_the_existing_adapter(identifier, values):
    platform = PLATFORMS[identifier]
    adapter = CONNECTORS[identifier].build(values)
    assert platform.connector_name == identifier
    assert platform.capabilities == adapter.capabilities
    assert platform.publication_modes == {
        PublicationMode.AUTOMATIC,
        PublicationMode.MANUAL,
    }
    assert platform.supported_media == (
        frozenset({"image"}) if adapter.capabilities.supports_media else frozenset()
    )


def test_whatsapp_is_automatic_through_bridged_groups_not_a_connector():
    from community_stack.announcements.adapters.whatsapp import WhatsAppAnnouncementAdapter

    platform = PLATFORMS["whatsapp"]
    adapter = WhatsAppAnnouncementAdapter("http://community_synapse:8008", "!room", "token")
    assert "whatsapp" not in CONNECTORS
    assert platform.capabilities == adapter.capabilities
    assert PublicationMode.AUTOMATIC in platform.publication_modes


def test_unknown_manual_platform_limits_and_media_are_not_guessed():
    for identifier in ("x", "tiktok", "youtube", "linkedin", "other"):
        platform = PLATFORMS[identifier]
        assert platform.connector_name is None
        assert platform.capabilities.max_text_length is None
        assert platform.supported_media is None
        assert platform.character_limit_verified is False
        assert platform.publication_modes == {PublicationMode.MANUAL}
        assert platform.notes


def test_catalogue_has_official_links_for_known_platforms():
    assert all(
        platform.official_url is not None
        for platform in PLATFORM_CATALOG
        if platform.identifier != "other"
    )
    assert PLATFORMS["other"].official_url is None


def test_pixelfed_default_limit_is_marked_as_instance_dependent():
    pixelfed = PLATFORMS["pixelfed"]
    assert pixelfed.capabilities.max_text_length == 2000
    assert pixelfed.character_limit_verified is False
    assert "actual caption limit" in pixelfed.notes
