"""WhatsApp-only bridge actions: joining a group from its invite link.

Everything that works for any bridge (rooms, links, relay, repairs) is in
``community_stack.agent.bridges``; this module only adds what the WhatsApp bot alone can
do. Invite links are bearer credentials, so the commands that carry one are redacted from
the bot chat after use and never echoed back or logged.
"""

from __future__ import annotations

import re
from typing import Any

from community_stack.agent import bridges
from community_stack.agent.bridges import BridgeError
from community_stack.bridges import config as bridge_config
from community_stack.bridges.catalogue import WHATSAPP, WHATSAPP_GROUP_ID

INVITE_LINK_PATTERN = re.compile(
    r"https://chat\.whatsapp\.com/[A-Za-z0-9_-]{10,80}(?:\?[A-Za-z0-9=&_-]+)?"
)
RESOLVED = re.compile(rf"That invite link points at (.+?) \(`?({WHATSAPP_GROUP_ID.pattern})`?\)")
JOINED = "Successfully joined group"


def _apply_safeguards(context) -> None:
    try:
        changed = bridge_config.ensure_safeguards(bridges._config_path(context, WHATSAPP), WHATSAPP)
    except bridge_config.BridgeConfigError as exc:
        raise BridgeError(str(exc)) from exc
    if changed:
        bridges.restart_bridge(context, WHATSAPP)


def add_group(context, invite_link: str, room_id: str = "") -> dict[str, Any]:
    """Join a WhatsApp group from its invite link and give it a Matrix room.

    Without ``room_id`` the group is allowlisted and the bridge creates a new room for it.
    With ``room_id`` the group is linked to that existing room instead, and it is not
    allowlisted, so the bridge never creates a second room for it on its own.
    """
    invite_link = invite_link.strip()
    if not INVITE_LINK_PATTERN.fullmatch(invite_link):
        raise BridgeError("Enter a valid WhatsApp group invite link.")
    if room_id:
        bridges._check_room_id(room_id)
    bridges._require_running(WHATSAPP)
    _apply_safeguards(context)

    resolved = bridges._bot_command(context, f"resolve-link {invite_link}", WHATSAPP, redact=True)
    match = RESOLVED.search(bridges._texts(resolved["replies"]))
    if not match:
        raise BridgeError("The bridge could not resolve that WhatsApp group invite link.")
    name, group_id = match.groups()

    existing = bridges._record(context, WHATSAPP, group_id)
    if existing and existing["room_id"]:
        raise BridgeError(
            f"{name} already has a Matrix room. Use “Move” on that room to link the group to "
            "another room."
        )
    if not room_id:
        bridges._set_allowed(context, WHATSAPP, group_id, True)

    joined = bridges._bot_command(context, f"join {invite_link}", WHATSAPP, redact=True)
    joined_text = bridges._texts(joined["replies"])
    if JOINED not in joined_text and existing is None:
        raise BridgeError(
            f"The bridge did not confirm joining {name}."
            + (f" It answered: {joined_text[:300]}" if joined_text else "")
        )

    if room_id:
        record = bridges._wait_for_record(context, WHATSAPP, group_id, bool)
        if record is None:
            raise BridgeError(
                f"Joined {name}, but the bridge has not recorded the group yet. Send a message "
                "in it, then link it to the room from the list of unlinked groups."
            )
        result = bridges.attach_room(context, WHATSAPP, group_id, room_id)
        return {"group_id": group_id, "name": name, **result}

    record = bridges._wait_for_record(
        context, WHATSAPP, group_id, lambda current: bool(current and current["room_id"])
    )
    if not record or not record["room_id"]:
        return {
            "group_id": group_id,
            "name": name,
            "room_id": "",
            "relay": False,
            "note": "The bridge creates the group's room with its next message.",
        }
    return {
        "group_id": group_id,
        "name": name,
        "room_id": record["room_id"],
        "relay": bridges._turn_on_relay(context, record["room_id"], WHATSAPP),
    }
