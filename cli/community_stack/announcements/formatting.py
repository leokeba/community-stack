"""Announcement bodies are Markdown: safe HTML for the web, email and Matrix, plain text elsewhere."""

from __future__ import annotations

from markdown_it import MarkdownIt
from markdown_it.token import Token

# Raw HTML is off, so organizers cannot inject markup; markdown-it also refuses
# javascript: and similar link targets. Line breaks are kept as typed, which is
# how announcements were displayed before Markdown support. Images go through
# the media library instead, where a description is required.
_markdown = MarkdownIt("commonmark", {"html": False, "breaks": True}).disable("image")


def render_html(body: str) -> str:
    return _markdown.render(body).strip()


def to_plain_text(body: str) -> str:
    """Readable text for channels without formatting: no #, ** or [link](...) syntax."""
    blocks: list[tuple[str, bool]] = []
    prefixes: list[str] = []
    counters: list[int] = []
    pending_prefix = ""
    for token in _markdown.parse(body):
        if token.type == "bullet_list_open":
            counters.append(0)
            prefixes.append("•")
        elif token.type == "ordered_list_open":
            counters.append(int(token.attrGet("start") or 1) - 1)
            prefixes.append("#")
        elif token.type in ("bullet_list_close", "ordered_list_close"):
            counters.pop()
            prefixes.pop()
        elif token.type == "list_item_open":
            depth = len(prefixes) - 1
            if prefixes[-1] == "#":
                counters[-1] += 1
                marker = f"{counters[-1]}."
            else:
                marker = "•"
            pending_prefix = "  " * depth + marker + " "
        elif token.type == "inline":
            blocks.append((pending_prefix + _inline_text(token), bool(pending_prefix)))
            pending_prefix = ""
        elif token.type in ("fence", "code_block"):
            blocks.append((token.content.rstrip("\n"), False))
        elif token.type == "hr":
            blocks.append(("———", False))
    return _join(blocks)


def _inline_text(token: Token) -> str:
    parts: list[str] = []
    link_stack: list[tuple[str, int]] = []
    for child in token.children or []:
        if child.type in ("text", "code_inline"):
            parts.append(child.content)
        elif child.type in ("softbreak", "hardbreak"):
            parts.append("\n")
        elif child.type == "link_open":
            link_stack.append((str(child.attrGet("href") or ""), len(parts)))
        elif child.type == "link_close" and link_stack:
            href, start = link_stack.pop()
            label = "".join(parts[start:])
            if href and href != label:
                parts.append(f" ({href})")
    return "".join(parts)


def _join(blocks: list[tuple[str, bool]]) -> str:
    """Blank lines between blocks, single line breaks between consecutive list items."""
    text = ""
    previous_item = False
    for block, is_item in blocks:
        if text:
            text += "\n" if is_item and previous_item else "\n\n"
        text += block
        previous_item = is_item
    return text
