"""Unit tests for the community profile, its schema, and independent channel rules."""

import copy
from pathlib import Path

import pytest
import yaml
from community_stack.community import (
    INDEPENDENT_KEYS,
    SOCIAL_PLATFORMS,
    address_warnings,
    load_profile,
    resolve_independent,
)
from community_stack.config import load_schema, validate_config


@pytest.fixture
def example() -> dict:
    with open(Path("config/example.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture
def with_community(example) -> dict:
    cfg = copy.deepcopy(example)
    cfg["community"] = {
        "name": "Example Community",
        "description": "A community working together.",
        "url": "https://community.example.org",
        "logo": "logo.png",
        "contact": {"email": "hello@example.org"},
        "emergency_instructions": "Use the feed if the chat is down.",
        "social": [
            {"platform": "instagram", "url": "https://instagram.com/example", "handle": "@example"},
            {"platform": "facebook", "url": "https://facebook.com/example"},
        ],
        "independent": {"website": True, "email": True, "matrix": True, "rss": True},
    }
    return cfg


# Schema


def test_example_with_community_block_is_valid(with_community):
    assert validate_config(with_community) == []


def test_schema_platform_list_matches_code():
    community = load_schema()["properties"]["community"]
    schema_platforms = community["properties"]["social"]["items"]["properties"]["platform"]["enum"]
    assert set(schema_platforms) == set(SOCIAL_PLATFORMS)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c["community"].update(url="http://community.example.org"),
        lambda c: c["community"]["social"].append({"platform": "myspace", "url": "https://x.org"}),
        lambda c: c["community"]["social"].append({"platform": "x", "url": "javascript:alert(1)"}),
        lambda c: c["community"]["independent"].update(carrier_pigeon=True),
        lambda c: c["community"].update(logo="../secrets.png"),
        lambda c: c["community"].update(logo="logo.gif"),
        lambda c: c["community"].update(surprise="extra"),
        lambda c: c["community"]["contact"].update(email="not-an-address"),
        lambda c: c["community"].update(name=""),
    ],
)
def test_schema_rejects_invalid_community_values(with_community, mutate):
    mutate(with_community)
    assert validate_config(with_community), "expected a validation error"


def test_config_without_community_block_stays_valid(example):
    assert "community" not in example
    assert validate_config(example) == []


# Profile loading


def test_profile_defaults_for_older_configs(example):
    profile = load_profile(example)
    assert profile.name == example["project"]["name"]
    assert profile.url == f"https://{example['domain']['web']}"
    assert profile.independent["website"] is True
    assert profile.independent["rss"] is True
    assert profile.independent["email"] is False
    assert profile.independent["matrix"] is False


def test_profile_strips_trailing_slashes(with_community):
    with_community["community"]["url"] = "https://community.example.org/"
    profile = load_profile(with_community)
    assert profile.url == "https://community.example.org"
    assert profile.connect_url == "https://community.example.org/connect/"


def test_profile_reads_social_accounts(with_community):
    profile = load_profile(with_community)
    assert [s.label for s in profile.social] == ["Instagram", "Facebook"]
    assert profile.social[0].handle == "@example"


# Address warnings


@pytest.mark.parametrize(
    ("url", "expect_warning"),
    [
        ("https://community.example.org", False),
        ("https://example.org", False),
        ("https://feeds.example.org", False),
        ("https://community.example.net", True),
        ("https://community.hosting-provider.com", True),
    ],
)
def test_address_warnings_flag_addresses_off_the_domain(url, expect_warning):
    profile = load_profile({"domain": {"base": "example.org", "web": "community.example.org"}})
    assert bool(address_warnings(url, profile)) is expect_warning


# Channel resolution


def _resolve(profile_cfg, connections=None, extra_cfg=None):
    cfg = copy.deepcopy(profile_cfg)
    cfg.update(extra_cfg or {})
    profile = load_profile(cfg)
    return resolve_independent(profile, connections or {}, cfg)


def test_website_and_feeds_are_always_shown_by_default(example):
    result = _resolve(example)
    keys = [c.key for c in result.shown]
    assert keys == ["website", "rss"]
    assert result.shown[0].url.endswith("/feed/announcements.html")


def test_email_is_never_shown_before_sign_up_exists(with_community):
    result = _resolve(with_community)
    assert "email" not in [c.key for c in result.shown]
    assert ("email", "Email sign-up is not available yet.") in result.hidden


def test_switched_off_channel_is_hidden_with_reason(with_community):
    with_community["community"]["independent"]["rss"] = False
    result = _resolve(with_community)
    assert ("rss", "switched off in community.independent") in result.hidden


def test_matrix_needs_a_room_even_when_switched_on(with_community):
    result = _resolve(with_community)
    assert "matrix" not in [c.key for c in result.shown]


def test_matrix_shown_when_room_is_connected(with_community):
    connections = {"matrix_room": {"room_id": "!abc:matrix.example.org"}}
    result = _resolve(with_community, connections=connections)
    matrix = next(c for c in result.shown if c.key == "matrix")
    assert matrix.url == "https://matrix.to/#/!abc:matrix.example.org"


def test_activitypub_shows_handle_when_fediverse_is_connected(with_community):
    with_community["community"]["independent"]["activitypub"] = True
    result = _resolve(
        with_community,
        connections={"fediverse": {"username": "announcements"}},
    )
    fediverse = next(c for c in result.shown if c.key == "activitypub")
    assert fediverse.detail == "@announcements@community.example.org"


def test_every_independent_key_is_accounted_for(with_community):
    result = _resolve(with_community)
    accounted = {c.key for c in result.shown} | {key for key, _ in result.hidden}
    assert accounted == set(INDEPENDENT_KEYS)
