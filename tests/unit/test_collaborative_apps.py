"""Shared operational metadata for collaborative applications."""

from community_stack.collaborative_apps import (
    COLLABORATIVE_APPS,
    COLLABORATIVE_APPS_BY_KEY,
    COLLABORATIVE_SERVICE_ALIASES,
)
from community_stack.commands.logs_cmd import SERVICE_DIR_MAP


def test_collaborative_app_registry_covers_shared_operational_metadata():
    assert tuple(app.key for app in COLLABORATIVE_APPS) == (
        "vikunja",
        "facilmap",
        "librebooking",
    )
    assert COLLABORATIVE_APPS_BY_KEY["vikunja"].container_name == "community_vikunja"
    assert COLLABORATIVE_APPS_BY_KEY["facilmap"].database_container_name == "community_facilmap_db"
    assert COLLABORATIVE_APPS_BY_KEY["librebooking"].database_container_name == (
        "community_librebooking_db"
    )
    assert COLLABORATIVE_SERVICE_ALIASES == {
        "vikunja": "vikunja",
        "tasks": "vikunja",
        "facilmap": "facilmap",
        "maps": "facilmap",
        "librebooking": "librebooking",
        "booking": "librebooking",
        "reservations": "librebooking",
    }


def test_collaborative_app_public_urls_respect_hostname_overrides():
    config = {
        "domain": {"base": "community.example.org"},
        "services": {
            "vikunja": {"enabled": True},
            "facilmap": {"enabled": True, "hostname": "custom-maps.example.org"},
            "librebooking": {"enabled": True},
        },
    }

    assert COLLABORATIVE_APPS_BY_KEY["vikunja"].public_url(config) == (
        "https://tasks.community.example.org/"
    )
    assert COLLABORATIVE_APPS_BY_KEY["facilmap"].public_url(config) == (
        "https://custom-maps.example.org/"
    )
    assert COLLABORATIVE_APPS_BY_KEY["librebooking"].public_url(config) == (
        "https://booking.community.example.org/Web/"
    )
    assert SERVICE_DIR_MAP["tasks"] == "vikunja"
    assert SERVICE_DIR_MAP["maps"] == "facilmap"
    assert SERVICE_DIR_MAP["reservations"] == "librebooking"
