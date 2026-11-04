"""Parsing for the ``!safe`` commands sent in a private Matrix conversation.

Only the message body and the event's shape are inspected. The sender comes from the
authenticated event and is never read from the text. Replies, quoted text and forwarded
content are rejected so that an old or copied command cannot be replayed as a new one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

PREFIX = "!safe"
VERBS = ("on", "off", "status", "help")


class CommandError(ValueError):
    """A ``!safe`` message that is malformed or was sent in a way the bot does not accept."""


@dataclass(frozen=True)
class Command:
    verb: str
    code: str | None = None

    def __repr__(self) -> str:  # never let a recovery code reach logs through repr()
        return f"Command(verb={self.verb!r}, code={'<redacted>' if self.code else None})"


def parse(body: Any, content: dict[str, Any] | None = None) -> Command | None:
    """Return the command, ``None`` if the message is not for Safe Mode, or raise CommandError.

    ``None`` means ordinary chat that the bot ignores. ``CommandError`` means the message
    starts with the prefix but must not be acted on.
    """
    if not isinstance(body, str):
        return None
    stripped = body.strip()
    words = stripped.split()
    if not words or words[0].lower() != PREFIX:
        return None

    if content is not None and _is_reply_or_forward(content):
        raise CommandError(
            "Replies and forwarded messages are not accepted. Type the command again."
        )
    if any(line.lstrip().startswith(">") for line in stripped.splitlines()[1:]):
        raise CommandError("Quoted text is not accepted. Type the command again.")

    if len(words) == 1:
        raise CommandError("Send !safe help for the commands.")
    verb = words[1].lower()
    if verb not in VERBS:
        raise CommandError("Unknown command. Send !safe help for the commands.")

    if verb == "off":
        if len(words) == 2:
            return Command("off")
        if len(words) == 3:
            return Command("off", code=words[2])
        raise CommandError("Usage: !safe off <recovery code>")

    if len(words) != 2:
        raise CommandError(f"Usage: !safe {verb}")
    return Command(verb)


def _is_reply_or_forward(content: dict[str, Any]) -> bool:
    if "m.relates_to" in content or "m.new_content" in content:
        return True
    return any(key.startswith("m.forward") or key == "forwarded" for key in content)
