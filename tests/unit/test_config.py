"""Unit tests for config loader and error formatting."""

from pathlib import Path

import pytest
from community_stack.config import ConfigError, get_default_schema_path, load_config, load_schema


def test_schema_file_found():
    path = get_default_schema_path()
    assert path.is_file()
    schema = load_schema(path)
    assert schema.get("title") == "CommunityStackConfig"


def test_load_config_valid(tmp_path):
    config_file = tmp_path / "test.yaml"
    example_path = Path("config/example.yaml")
    config_file.write_text(example_path.read_text(encoding="utf-8"), encoding="utf-8")

    cfg = load_config(config_file)
    assert cfg["project"]["name"] == "community"
    assert cfg["project"]["profile"] == "starter"


def test_load_config_nonexistent(tmp_path):
    with pytest.raises(ConfigError, match="Configuration file not found"):
        load_config(tmp_path / "does_not_exist.yaml")


def test_load_config_invalid_yaml(tmp_path):
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text("project: [unclosed", encoding="utf-8")

    with pytest.raises(ConfigError, match="YAML parsing error"):
        load_config(bad_yaml)


def test_load_config_schema_violation(tmp_path):
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("project:\n  name: community\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="Configuration validation failed"):
        load_config(invalid)
