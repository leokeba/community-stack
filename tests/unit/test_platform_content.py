from community_stack.announcements.platform_content import (
    generate_platform_text,
    suggested_hashtags,
)
from community_stack.announcements.platforms import PLATFORMS


def test_suggested_hashtags_prefer_community_tags_and_skip_reserved_tags():
    assert suggested_hashtags(["Mutual Aid", "important"], "Food Drive Saturday") == [
        "#mutualaid",
        "#food",
        "#drive",
    ]


def test_platform_variants_are_distinct_plain_text_with_canonical_and_connect_links():
    shared = {
        "title": "Food drive",
        "body": "Bring **canned food** on Saturday.",
        "canonical_url": "https://community.example.org/feed/announcements/123/",
        "tags": ["mutualaid"],
        "connect_url": "https://community.example.org/connect/",
    }

    mastodon = generate_platform_text(platform=PLATFORMS["mastodon"], **shared)
    pixelfed = generate_platform_text(platform=PLATFORMS["pixelfed"], **shared)

    assert mastodon != pixelfed
    assert "Bring canned food" in mastodon
    assert "**" not in mastodon
    assert shared["canonical_url"] in mastodon
    assert shared["connect_url"] in mastodon
    assert "#mutualaid" in mastodon


def test_verified_platform_limit_shortens_description_but_keeps_links():
    text = generate_platform_text(
        title="Community event",
        body="A useful description " * 100,
        canonical_url="https://community.example.org/feed/announcements/123/",
        tags=[],
        connect_url="https://community.example.org/connect/",
        platform=PLATFORMS["bluesky"],
    )

    assert len(text) <= PLATFORMS["bluesky"].capabilities.max_text_length
    assert text.startswith("Community event\n\n")
    assert "https://community.example.org/feed/announcements/123/" in text
    assert "https://community.example.org/connect/" in text
    assert "…\n\nhttps://community.example.org/feed/announcements/123/" in text


def test_unverified_manual_limit_is_not_guessed():
    text = generate_platform_text(
        title="Community event",
        body="Description " * 100,
        canonical_url="https://community.example.org/feed/announcements/123/",
        tags=[],
        connect_url="https://community.example.org/connect/",
        platform=PLATFORMS["tiktok"],
    )

    assert len(text) > 300
    assert "Description " * 10 in text
