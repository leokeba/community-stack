"""Unit tests for configuration JSON Schema validation."""

import copy
from pathlib import Path

import pytest
import yaml
from community_stack.config import validate_config


@pytest.fixture
def base_config() -> dict:
    example_path = Path("config/example.yaml")
    with open(example_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_example_config_is_valid(base_config):
    errors = validate_config(base_config)
    assert errors == [], f"Expected example.yaml to be valid, got: {errors}"


def test_schema_rejects_missing_project(base_config):
    cfg = copy.deepcopy(base_config)
    del cfg["project"]
    errors = validate_config(cfg)
    assert any("project" in err for err in errors)


def test_schema_rejects_invalid_profile(base_config):
    cfg = copy.deepcopy(base_config)
    cfg["project"]["profile"] = "unsupported_profile"
    errors = validate_config(cfg)
    assert any("profile" in err for err in errors)


def test_schema_rejects_invalid_domain(base_config):
    cfg = copy.deepcopy(base_config)
    cfg["domain"]["base"] = "not a valid domain!"
    errors = validate_config(cfg)
    assert any("domain" in err for err in errors)


def test_schema_rejects_extra_properties(base_config):
    cfg = copy.deepcopy(base_config)
    cfg["unknown_field"] = "bad"
    errors = validate_config(cfg)
    assert any("unknown_field" in err or "additionalProperties" in err for err in errors)


def test_schema_allows_valid_whatsapp_bridge_mapping(base_config):
    cfg = copy.deepcopy(base_config)
    cfg["services"]["whatsapp_bridge"]["enabled"] = True
    cfg["services"]["whatsapp_bridge"]["mode"] = "selective_bridge"
    cfg["services"]["whatsapp_bridge"]["mapped_rooms"] = [
        {
            "matrix_room_id": "!testroom:example.org",
            "whatsapp_chat_jid": "123456789-group@g.us",
            "description": "General Announcements",
        }
    ]
    errors = validate_config(cfg)
    assert errors == []


def test_vikunja_is_optional_and_validates_service_settings(base_config):
    assert base_config["services"]["vikunja"]["enabled"] is False
    config = copy.deepcopy(base_config)
    config["services"]["vikunja"].update(
        enabled=True,
        version="2.7.1",
        hostname="tasks.community.org",
        files_path="data/collaboration/files",
        database={"type": "postgres", "data_path": "data/collaboration/postgres"},
    )
    assert validate_config(config) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", "latest"),
        ("files_path", "../outside"),
        ("enable_link_sharing", "yes"),
        ("cpu_limit", 0),
    ],
)
def test_vikunja_rejects_invalid_settings(base_config, field, value):
    config = copy.deepcopy(base_config)
    config["services"]["vikunja"][field] = value
    assert validate_config(config)
