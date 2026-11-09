"""Read and change a bridge's own config.yaml: its chat allowlist and required settings.

Every mautrix bridgev2 bridge has the same ``bridge.portal_create_filter``. Community Stack
keeps it in allow mode with an explicit list, so a bridge only creates Matrix rooms by
itself for chats an administrator chose. The filter does not govern chats that already
have a room, nor rooms bridged by hand with the bot's ``bridge`` command.

Writes are atomic and keep the file's owner and mode: the file belongs to the bridge's
container user, and this code usually runs as root.
"""

from __future__ import annotations

import os
import stat
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from community_stack.bridges.catalogue import BridgeSpec


class BridgeConfigError(Exception):
    """The bridge configuration is missing, unreadable or has an unexpected shape."""


def load(path: Path) -> tuple[dict[str, Any], os.stat_result]:
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        original_stat = path.stat()
    except (OSError, yaml.YAMLError) as exc:
        raise BridgeConfigError("Could not read the bridge configuration.") from exc
    if not isinstance(config, dict):
        raise BridgeConfigError("The bridge configuration must be a YAML mapping.")
    return config, original_stat


def write(path: Path, config: dict[str, Any], original_stat: os.stat_result) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=".bridge-config-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            yaml.safe_dump(config, stream, sort_keys=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.chown(temporary, original_stat.st_uid, original_stat.st_gid)
        os.chmod(temporary, stat.S_IMODE(original_stat.st_mode))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _section(config: dict[str, Any], key: str) -> dict[str, Any]:
    section = config.setdefault(key, {})
    if not isinstance(section, dict):
        raise BridgeConfigError(f"The bridge configuration's '{key}' section is not a mapping.")
    return section


def _portal_filter(config: dict[str, Any]) -> dict[str, Any]:
    portal_filter = _section(_section(config, "bridge"), "portal_create_filter")
    if not isinstance(portal_filter.get("list", []), list):
        raise BridgeConfigError("The bridge's chat allowlist must be a list.")
    portal_filter.setdefault("list", [])
    return portal_filter


def _entry_id(entry: Any) -> str | None:
    # The bridge accepts plain IDs and {id, receiver} items; this code writes plain IDs.
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict) and isinstance(entry.get("id"), str):
        return entry["id"]
    return None


def allowed_chats(path: Path) -> list[str]:
    """Chat IDs the bridge may create rooms for by itself; empty if not in allow mode."""
    if not path.is_file():
        return []
    config, _ = load(path)
    portal_filter = _portal_filter(deepcopy(config))
    if portal_filter.get("mode") != "allow":
        return []
    return [chat for chat in map(_entry_id, portal_filter["list"]) if chat]


def allow_chat(path: Path, spec: BridgeSpec, chat_id: str) -> bool:
    """Add one chat to the allowlist. True when the file changed."""
    if not spec.chat_id_ok(chat_id):
        raise BridgeConfigError(f"That {spec.name} {spec.chat_noun} ID is not valid.")
    config, original_stat = load(path)
    portal_filter = _portal_filter(config)
    portal_filter["mode"] = "allow"
    if chat_id in map(_entry_id, portal_filter["list"]):
        return False
    portal_filter["list"].append(chat_id)
    write(path, config, original_stat)
    return True


def disallow_chat(path: Path, spec: BridgeSpec, chat_id: str) -> bool:
    """Remove one chat from the allowlist. True when the file changed."""
    if not spec.chat_id_ok(chat_id):
        raise BridgeConfigError(f"That {spec.name} {spec.chat_noun} ID is not valid.")
    config, original_stat = load(path)
    portal_filter = _portal_filter(config)
    kept = [entry for entry in portal_filter["list"] if _entry_id(entry) != chat_id]
    if len(kept) == len(portal_filter["list"]):
        return False
    portal_filter["list"] = kept
    write(path, config, original_stat)
    return True


def _get(config: dict[str, Any], keys: tuple[str, ...]) -> Any:
    value: Any = config
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def safeguards_ok(path: Path, spec: BridgeSpec) -> bool:
    """Whether the allowlist is in allow mode and every required setting holds."""
    try:
        config, _ = load(path)
    except BridgeConfigError:
        return False
    portal_filter = _get(config, ("bridge", "portal_create_filter"))
    if not isinstance(portal_filter, dict) or portal_filter.get("mode") != "allow":
        return False
    if not isinstance(portal_filter.get("list"), list):
        return False
    return all(_get(config, keys) == value for keys, value in spec.required_settings)


def ensure_safeguards(path: Path, spec: BridgeSpec) -> bool:
    """Apply allow-only room creation and the bridge's required settings. True if changed."""
    original, original_stat = load(path)
    config = deepcopy(original)
    portal_filter = _portal_filter(config)
    portal_filter["mode"] = "allow"

    if any(keys[:2] == ("network", "history_sync") for keys, _ in spec.required_settings):
        # Older templates put history_sync at the top level or under bridge:, where current
        # bridges ignore it. Fold those values into network.history_sync.
        history_sync = _section(_section(config, "network"), "history_sync")
        for legacy in (
            config.pop("history_sync", None),
            config["bridge"].pop("history_sync", None),
        ):
            if legacy is not None and not isinstance(legacy, dict):
                raise BridgeConfigError("The bridge's history sync settings are not a mapping.")
            for key, value in (legacy or {}).items():
                history_sync.setdefault(key, value)

    for keys, value in spec.required_settings:
        section = config
        for key in keys[:-1]:
            section = _section(section, key)
        section[keys[-1]] = value

    if config == original:
        return False
    write(path, config, original_stat)
    return True


def command_prefix(path: Path, spec: BridgeSpec) -> str:
    """The prefix the bot needs on commands sent outside its management room."""
    try:
        config, _ = load(path)
    except BridgeConfigError:
        config = {}
    prefix = _get(config, ("bridge", "command_prefix"))
    if isinstance(prefix, str) and prefix.strip():
        return prefix.strip()
    if not spec.command_prefix:
        raise BridgeConfigError(f"The {spec.name} bridge has no known command prefix.")
    return spec.command_prefix
