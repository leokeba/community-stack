"""Configuration loading and JSON Schema validation for community-stack."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import yaml


class ConfigError(Exception):
    """Raised when configuration is invalid or missing."""


def get_default_schema_path() -> Path:
    """Find schema.json within the project or package distribution."""
    # Look in current working directory first (e.g. config/schema.json)
    cwd_path = Path.cwd() / "config" / "schema.json"
    if cwd_path.is_file():
        return cwd_path

    # Look relative to repository root (two levels up from this file)
    repo_path = Path(__file__).resolve().parent.parent.parent / "config" / "schema.json"
    if repo_path.is_file():
        return repo_path

    raise FileNotFoundError("Could not locate config/schema.json")


def load_schema(schema_path: Path | None = None) -> dict[str, Any]:
    """Load and parse the JSON Schema for community-stack configuration."""
    target_path = schema_path or get_default_schema_path()
    with open(target_path, "r", encoding="utf-8") as f:
        return json.load(f)


def validate_config(data: dict[str, Any], schema: dict[str, Any] | None = None) -> list[str]:
    """Validate configuration dictionary against JSON Schema.

    Returns a list of human-readable error messages (empty if valid).
    """
    if schema is None:
        schema = load_schema()

    validator_cls = jsonschema.validators.validator_for(schema)
    validator = validator_cls(schema)

    errors = sorted(validator.iter_errors(data), key=lambda e: e.path)
    messages: list[str] = []

    for err in errors:
        path = " -> ".join(str(p) for p in err.path) if err.path else "root"
        messages.append(f"Field '{path}': {err.message}")

    return messages


def load_config(config_path: Path | str) -> dict[str, Any]:
    """Load and validate configuration file from disk."""
    path = Path(config_path)
    if not path.is_file():
        raise ConfigError(f"Configuration file not found: {path}")

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML parsing error in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError(f"Configuration file {path} must contain a YAML mapping/dictionary.")

    errors = validate_config(data)
    if errors:
        formatted = "\n  - ".join(errors)
        raise ConfigError(f"Configuration validation failed for {path}:\n  - {formatted}")

    return data
