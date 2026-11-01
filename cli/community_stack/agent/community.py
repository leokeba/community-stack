"""Changes to the community profile made from the console (CR1.8).

The profile is the optional ``community:`` block of community-stack.yaml. Only that
block is replaced; every other line of the file is kept. The whole file is validated
against the schema before anything is written, and a safety backup is taken first.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

from community_stack.community import INDEPENDENT_KEYS, SOCIAL_PLATFORMS
from community_stack.config import validate_config
from community_stack.config_edit import write_in_place

ALLOWED = {
    "name",
    "description",
    "url",
    "logo",
    "contact_email",
    "emergency_instructions",
    "social",
    "independent",
}
TOP_LEVEL_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*:")


class CommunityError(ValueError):
    """A profile change that cannot be saved. The message is shown to the person who made it."""


def _text(values: dict[str, Any], key: str) -> str:
    value = values.get(key, "")
    if not isinstance(value, str):
        raise CommunityError(f"'{key}' must be text.")
    return value.strip()


def build_block(values: dict[str, Any]) -> dict[str, Any]:
    """Turn the submitted profile into the YAML block, rejecting anything unexpected."""
    unknown = sorted(set(values) - ALLOWED)
    if unknown:
        raise CommunityError(f"Unknown profile field: {', '.join(unknown)}.")
    name = _text(values, "name")
    if not name:
        raise CommunityError("The community needs a name.")

    block: dict[str, Any] = {"name": name}
    for key in ("description", "url", "logo", "emergency_instructions"):
        value = _text(values, key)
        if value:
            block[key] = value
    contact = _text(values, "contact_email")
    if contact:
        block["contact"] = {"email": contact}

    social = []
    for item in values.get("social", []) or []:
        if not isinstance(item, dict):
            raise CommunityError("Each social account must be a platform, address and handle.")
        platform = _text(item, "platform")
        url = _text(item, "url")
        if not platform and not url:
            continue
        if platform not in SOCIAL_PLATFORMS:
            raise CommunityError("Choose a listed platform for each social account.")
        entry: dict[str, Any] = {"platform": platform, "url": url}
        handle = _text(item, "handle")
        if handle:
            entry["handle"] = handle
        social.append(entry)
    if social:
        block["social"] = social

    independent_values = values.get("independent", {}) or {}
    if not isinstance(independent_values, dict):
        raise CommunityError("Channel choices must be on or off for each channel.")
    unknown_channels = sorted(set(independent_values) - set(INDEPENDENT_KEYS))
    if unknown_channels:
        raise CommunityError(f"Unknown channel: {', '.join(unknown_channels)}.")
    if independent_values:
        block["independent"] = {
            key: bool(independent_values.get(key, False)) for key in INDEPENDENT_KEYS
        }
    return block


def splice(text: str, block: dict[str, Any]) -> str:
    """Replace the ``community:`` block, or add one at the end. Other lines are untouched."""
    dumped = yaml.safe_dump({"community": block}, sort_keys=False, allow_unicode=True)
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.startswith("community:")), None)
    if start is None:
        separator = (
            "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        )
        return text + separator + dumped
    end = next(
        (i for i in range(start + 1, len(lines)) if TOP_LEVEL_KEY.match(lines[i])),
        len(lines),
    )
    return "".join(lines[:start]) + dumped + "".join(lines[end:])


def apply(context: Any, values: dict[str, Any]) -> dict[str, Any]:
    """Validate, back up, write and report. Raises CommunityError with nothing changed."""
    from community_stack.backup import BackupError

    block = build_block(values)
    old_text = context.config_path.read_text(encoding="utf-8")
    new_text = splice(old_text, block)
    try:
        parsed = yaml.safe_load(new_text)
    except yaml.YAMLError as exc:
        raise CommunityError(f"The profile could not be saved: {exc}") from exc
    errors = validate_config(parsed)
    if errors:
        raise CommunityError("The profile is not valid: " + "; ".join(errors))

    logo = block.get("logo")
    if logo and not (context.config_path.parent / logo).is_file():
        raise CommunityError(
            f"The logo file '{logo}' was not found next to community-stack.yaml. "
            "Copy it there first."
        )

    steps: list[str] = []
    engine = context.backup_engine()
    if engine.backups_cfg.get("enabled", True):
        try:
            snapshot = engine.create_snapshot()
        except BackupError as exc:
            raise CommunityError(
                f"The safety backup failed, so nothing was changed: {exc}"
            ) from exc
        steps.append(f"Safety backup {snapshot.id} created.")

    previous = context.config_path.with_name("community-stack.yaml.previous")
    previous.write_text(old_text, encoding="utf-8")
    previous.chmod(0o600)
    write_in_place(context.config_path, new_text)
    steps.append("Community profile saved.")
    return {"applied": True, "steps": steps}
