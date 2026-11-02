"""Deterministic, editable text variants for social publishing platforms."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from community_stack.announcements.formatting import to_plain_text
from community_stack.announcements.platforms import PlatformSpec

_HASHTAG_STOP_WORDS = {
    "about",
    "after",
    "also",
    "and",
    "are",
    "back",
    "from",
    "have",
    "into",
    "more",
    "our",
    "that",
    "the",
    "their",
    "this",
    "with",
    "your",
}
_RESERVED_TAGS = {"events", "important"}


def suggested_hashtags(tags: Iterable[str], title: str) -> list[str]:
    """Prefer announcement tags, then add useful title keywords, up to three."""
    suggestions: list[str] = []
    for candidate in (*tags, *title.split()):
        ascii_candidate = (
            unicodedata.normalize("NFKD", candidate).encode("ascii", "ignore").decode("ascii")
        )
        token = re.sub(r"[^A-Za-z0-9_]", "", ascii_candidate).casefold()
        if (
            len(token) < 3
            or token in _HASHTAG_STOP_WORDS
            or token in _RESERVED_TAGS
            or token in suggestions
        ):
            continue
        suggestions.append(token)
        if len(suggestions) == 3:
            break
    return [f"#{tag}" for tag in suggestions]


def _truncate(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    prefix = text[: limit - 1].rstrip()
    if " " in prefix:
        prefix = prefix.rsplit(" ", 1)[0]
    return f"{prefix or text[: limit - 1]}…"


def generate_platform_text(
    *,
    title: str,
    body: str,
    canonical_url: str,
    tags: Iterable[str],
    connect_url: str,
    platform: PlatformSpec,
) -> str:
    """Build a platform-specific variant, truncating only the description."""
    plain_body = to_plain_text(body).strip()
    hashtags = " ".join(suggested_hashtags(tags, title))
    cta = f"Find independent ways to stay connected beyond {platform.name}: {connect_url}"
    limit = platform.capabilities.max_text_length if platform.character_limit_verified else None

    if limit is None:
        return "\n\n".join(
            part for part in (title.strip(), plain_body, canonical_url, cta, hashtags) if part
        )

    fixed_lines = [title.strip(), canonical_url, cta, hashtags]
    fixed_lines = [line for line in fixed_lines if line]
    fixed_length = sum(map(len, fixed_lines)) + 2 * max(0, len(fixed_lines) - 1)
    if fixed_length > limit and hashtags:
        fixed_lines.remove(hashtags)
        hashtags = ""
        fixed_length = sum(map(len, fixed_lines)) + 2 * max(0, len(fixed_lines) - 1)

    if fixed_length > limit:
        title_budget = limit - (
            sum(len(line) for line in fixed_lines if line != title.strip())
            + 2 * max(0, len(fixed_lines) - 1)
        )
        shortened_title = _truncate(title.strip(), title_budget)
        fixed_lines = [shortened_title if line == title.strip() else line for line in fixed_lines]
        fixed_length = sum(map(len, fixed_lines)) + 2 * max(0, len(fixed_lines) - 1)

    if fixed_length > limit or not fixed_lines or not fixed_lines[0]:
        raise ValueError(
            f"{platform.name}'s character limit is too short for the announcement link "
            "and independent-channel call to action."
        )

    body_budget = limit - fixed_length - (2 if plain_body else 0)
    shortened_body = _truncate(plain_body, body_budget) if plain_body else ""
    lines = [*fixed_lines]
    if shortened_body:
        lines.insert(1, shortened_body)
    return "\n\n".join(lines)
