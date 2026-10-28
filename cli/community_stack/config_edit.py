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
        raise ValueError(f"Section {'.'.join(path[:-1])} is missing from the configuration.")
    indent = " " * (child_indent if child_indent is not None else parent_indent + 2)
    if not lines[parent_end].endswith("\n"):
        lines[parent_end] += "\n"
    lines.insert(parent_end + 1, f"{indent}{path[-1]}: {render_value(value)}\n")
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
