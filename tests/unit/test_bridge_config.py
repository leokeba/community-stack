"""A bridge's own config.yaml: the chat allowlist and required settings, for every bridge."""

import stat

import pytest
import yaml
from community_stack.bridges import config as bridge_config
from community_stack.bridges.catalogue import SIGNAL, WHATSAPP

GROUP = "120363411717803463@g.us"


def write(path, config):
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    path.chmod(0o600)


def read(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_chats_are_allowed_and_disallowed_once_keeping_the_file_mode(tmp_path):
    path = tmp_path / "config.yaml"
    write(path, {"bridge": {"portal_create_filter": {"mode": "allow", "list": []}}})

    assert bridge_config.allow_chat(path, WHATSAPP, GROUP) is True
    assert bridge_config.allow_chat(path, WHATSAPP, GROUP) is False
    assert bridge_config.allowed_chats(path) == [GROUP]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert bridge_config.disallow_chat(path, WHATSAPP, GROUP) is True
    assert bridge_config.disallow_chat(path, WHATSAPP, GROUP) is False
    assert bridge_config.allowed_chats(path) == []


def test_items_written_by_hand_as_id_and_receiver_are_understood(tmp_path):
    path = tmp_path / "config.yaml"
    write(
        path,
        {"bridge": {"portal_create_filter": {"mode": "allow", "list": [{"id": GROUP}]}}},
    )
    assert bridge_config.allowed_chats(path) == [GROUP]
    assert bridge_config.disallow_chat(path, WHATSAPP, GROUP) is True
    assert read(path)["bridge"]["portal_create_filter"]["list"] == []


def test_a_deny_mode_filter_allows_nothing_by_name(tmp_path):
    path = tmp_path / "config.yaml"
    write(path, {"bridge": {"portal_create_filter": {"mode": "deny", "list": [GROUP]}}})
    assert bridge_config.allowed_chats(path) == []
    assert bridge_config.safeguards_ok(path, SIGNAL) is False


def test_chat_ids_are_checked_against_the_bridge(tmp_path):
    path = tmp_path / "config.yaml"
    write(path, {"bridge": {"portal_create_filter": {"mode": "allow", "list": []}}})
    with pytest.raises(bridge_config.BridgeConfigError):
        bridge_config.allow_chat(path, WHATSAPP, "not-a-group")
    with pytest.raises(bridge_config.BridgeConfigError):
        bridge_config.allow_chat(path, SIGNAL, "two words")


def test_whatsapp_safeguards_fold_legacy_history_settings_into_the_network_section(tmp_path):
    path = tmp_path / "config.yaml"
    write(
        path,
        {
            "history_sync": {"dispatch_wait": "1m"},
            "bridge": {
                "history_sync": {"max_initial_conversations": 50},
                "portal_create_filter": {"mode": "deny", "list": [GROUP]},
            },
        },
    )
    assert bridge_config.ensure_safeguards(path, WHATSAPP) is True
    config = read(path)
    assert "history_sync" not in config and "history_sync" not in config["bridge"]
    assert config["network"]["history_sync"] == {
        "dispatch_wait": "1m",
        "max_initial_conversations": 0,
        "request_full_sync": False,
    }
    assert config["network"]["max_member_sync"] == 0
    assert config["bridge"]["portal_create_filter"] == {"mode": "allow", "list": [GROUP]}
    assert bridge_config.safeguards_ok(path, WHATSAPP) is True
    assert bridge_config.ensure_safeguards(path, WHATSAPP) is False


def test_bridges_without_required_settings_only_get_the_allow_only_filter(tmp_path):
    path = tmp_path / "config.yaml"
    original = {
        "bridge": {
            "history_sync": {"max_initial_conversations": 0},
            "portal_create_filter": {"mode": "allow", "list": []},
        }
    }
    write(path, original)
    assert bridge_config.ensure_safeguards(path, SIGNAL) is False
    assert read(path) == original


def test_the_command_prefix_comes_from_the_config_then_the_catalogue(tmp_path):
    path = tmp_path / "config.yaml"
    write(path, {"bridge": {"command_prefix": "!w"}})
    assert bridge_config.command_prefix(path, WHATSAPP) == "!w"
    write(path, {"bridge": {}})
    assert bridge_config.command_prefix(path, WHATSAPP) == "!wa"
    assert bridge_config.command_prefix(tmp_path / "missing.yaml", SIGNAL) == "!signal"
