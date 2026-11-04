"""Change scalar values in community-stack.yaml while keeping its comments and layout."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import yaml

KEY_LINE = re.compile(r"^(?P<indent> *)(?P<key>[A-Za-z0-9_]+):(?P<rest>.*)$")
TRAILING_COMMENT = re.compile(r"\s+#.*$")

VIKUNJA_DEFAULTS = """  vikunja:
    enabled: false
    version: "2.7.1"
    files_path: "data/vikunja/files"
    database:
      type: "postgres"
      data_path: "data/vikunja/postgres"
    allow_registration: false
    enable_link_sharing: false
    memory_limit: "512M"
    cpu_limit: 1
"""

FACILMAP_DEFAULTS = """  facilmap:
    enabled: false
    version: "sha256:bcef0c14e23f8670ea21d18a15d4331348bf253550a5acf39b19d067546f629f"
    cache_path: "data/facilmap/cache"
    database:
      version: "11.4.12"
      data_path: "data/facilmap/mariadb"
      memory_limit: "512M"
      cpu_limit: 1
    memory_limit: "512M"
    cpu_limit: 1
"""


def render_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value))


def set_value(text: str, path: tuple[str, ...], value: Any) -> str:
    """Return ``text`` with the mapping value at ``path`` set, adding the key if missing.

    Only the affected line changes, so operators' comments survive. Raises
    ValueError when the parent block does not exist.
    """
    lines = text.splitlines(keepends=True)
    stack: list[tuple[int, str]] = []
    parent_end: int | None = None
    child_indent: int | None = None
    parent_indent: int | None = None
    left_parent = False
    for index, line in enumerate(lines):
        match = KEY_LINE.match(line.rstrip("\n"))
        if not match:
            # List items and other nested lines still belong to the parent's block.
            stripped = line.strip()
            nested = len(line) - len(line.lstrip(" "))
            if (
                parent_indent is not None
                and not left_parent
                and stripped
                and not stripped.startswith("#")
                and nested > parent_indent
            ):
                parent_end = index
            continue
        indent = len(match["indent"])
        if parent_indent is not None and indent <= parent_indent:
            left_parent = True
        while stack and stack[-1][0] >= indent:
            stack.pop()
        stack.append((indent, match["key"]))
        current = tuple(key for _, key in stack)
        if current == path:
            rest = match["rest"]
            comment = TRAILING_COMMENT.search(rest)
            suffix = comment.group(0) if comment and rest.strip() else ""
            newline = "\n" if line.endswith("\n") else ""
            lines[index] = (
                f"{match['indent']}{match['key']}: {render_value(value)}{suffix}{newline}"
            )
            return _verified("".join(lines), path, value)
        parent = path[:-1]
        if current == parent:
            parent_end, parent_indent = index, indent
        elif parent_end is not None and not left_parent and current[: len(parent)] == parent:
            parent_end = index
            if len(current) == len(parent) + 1 and child_indent is None:
                child_indent = indent
    if parent_end is None or parent_indent is None:
        return _add_missing_block(text, path, value)
    indent = " " * (child_indent if child_indent is not None else parent_indent + 2)
    if not lines[parent_end].endswith("\n"):
        lines[parent_end] += "\n"
    lines.insert(parent_end + 1, f"{indent}{path[-1]}: {render_value(value)}\n")
    return _verified("".join(lines), path, value)


def _add_missing_block(text: str, path: tuple[str, ...], value: Any) -> str:
    """Add the sections that do not exist yet, under the deepest section that does."""
    data = yaml.safe_load(text) or {}
    existing = len(path) - 1
    while existing > 0 and not isinstance(get_value(data, path[:existing], None), dict):
        existing -= 1
    if existing == 0 and not isinstance(data, dict):
        raise ValueError("The configuration must be a YAML mapping.")
    lines = text.splitlines(keepends=True)
    if existing == 0:
        # Top-level section missing: append it at the end of the file.
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        indent = 0
        insert_at = len(lines)
    else:
        # Find the ancestor's own line and where its block ends.
        ancestor = path[:existing]
        stack: list[tuple[int, str]] = []
        start = None
        indent = 0
        insert_at = len(lines)
        for index, line in enumerate(lines):
            match = KEY_LINE.match(line.rstrip("\n"))
            if not match:
                continue
            key_indent = len(match["indent"])
            if start is not None and key_indent <= indent:
                insert_at = index
                break
            while stack and stack[-1][0] >= key_indent:
                stack.pop()
            stack.append((key_indent, match["key"]))
            if tuple(key for _, key in stack) == ancestor and start is None:
                start, indent = index, key_indent
        if start is None:
            raise ValueError(f"Section {'.'.join(ancestor)} is missing from the configuration.")
        # Insert after the last content line of the ancestor's block.
        while insert_at > start + 1 and not lines[insert_at - 1].strip():
            insert_at -= 1
        if not lines[insert_at - 1].endswith("\n"):
            lines[insert_at - 1] += "\n"
    new_lines: list[str] = []
    # Keys nest two spaces deeper than their parent; a new top-level key starts at column 0.
    depth = indent if existing > 0 else -2
    for key in path[existing:-1]:
        depth += 2
        new_lines.append(f"{' ' * depth}{key}:\n")
    new_lines.append(f"{' ' * (depth + 2)}{path[-1]}: {render_value(value)}\n")
    lines[insert_at:insert_at] = new_lines
    return _verified("".join(lines), path, value)


def _verified(new_text: str, path: tuple[str, ...], value: Any) -> str:
    if get_value(yaml.safe_load(new_text), path, object()) != value:
        raise ValueError(f"Could not set {'.'.join(path)} safely.")
    return new_text


def get_value(config: dict, path: tuple[str, ...], default: Any = None) -> Any:
    node: Any = config
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def ensure_vikunja_config(text: str) -> str:
    """Add Vikunja defaults to a legacy config without reserializing operator comments."""
    data = yaml.safe_load(text)
    if not isinstance(data, dict) or not isinstance(data.get("services"), dict):
        raise TypeError("Configuration must contain a services mapping.")
    if "vikunja" in data["services"]:
        return text

    lines = text.splitlines(keepends=True)
    services_index = next(
        (index for index, line in enumerate(lines) if re.fullmatch(r"services:\s*\n?", line)),
        None,
    )
    if services_index is None:
        raise ValueError("Configuration must contain a top-level services mapping.")
    insert_at = len(lines)
    for index in range(services_index + 1, len(lines)):
        line = lines[index]
        if line and not line.startswith((" ", "\t", "#", "\n")):
            insert_at = index
            break
    lines.insert(insert_at, VIKUNJA_DEFAULTS)
    return "".join(lines)


def ensure_facilmap_config(text: str) -> str:
    """Add FacilMap defaults to a legacy config without reserializing operator comments."""
    data = yaml.safe_load(text)
    if not isinstance(data, dict) or not isinstance(data.get("services"), dict):
        raise TypeError("Configuration must contain a services mapping.")
    if "facilmap" in data["services"]:
        return text

    lines = text.splitlines(keepends=True)
    services_index = next(
        (index for index, line in enumerate(lines) if re.fullmatch(r"services:\s*\n?", line)),
        None,
    )
    if services_index is None:
        raise ValueError("Configuration must contain a top-level services mapping.")
    insert_at = len(lines)
    for index in range(services_index + 1, len(lines)):
        line = lines[index]
        if line and not line.startswith((" ", "\t", "#", "\n")):
            insert_at = index
            break
    lines.insert(insert_at, FACILMAP_DEFAULTS)
    return "".join(lines)


def write_in_place(path: Path, text: str) -> None:
    """Overwrite a file keeping its inode.

    Containers mount community-stack.yaml as a single file; replacing it with a
    new file (rename) would leave them reading the old version.
    """
    with open(path, "r+", encoding="utf-8") as handle:
        handle.seek(0)
        handle.write(text)
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())


ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")


def set_env_values(path: Path, values: dict[str, str]) -> None:
    """Set KEY=value lines in a .env file in place, appending keys that are missing."""
    for key, value in values.items():
        if not ENV_KEY.fullmatch(key):
            raise ValueError(f"Invalid environment variable name: {key}")
        if not value or any(ch in value for ch in "\n\r\"'# ") or value != value.strip():
            raise ValueError(f"The value for {key} contains characters .env cannot hold.")
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    remaining = dict(values)
    for index, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if "=" in line and not line.lstrip().startswith("#") and key in remaining:
            lines[index] = f"{key}={remaining.pop(key)}\n"
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines.extend(f"{key}={value}\n" for key, value in remaining.items())
    write_in_place(path, "".join(lines))
