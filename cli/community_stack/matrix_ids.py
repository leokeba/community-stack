"""Matrix identifier patterns shared by the console, the agent and the CLI.

Room IDs come in two shapes. Room versions 1 to 11 use ``!opaque:server``. Room version 12,
which current Synapse uses for every new room, drops the server part and uses only the
room's hash: ``!`` followed by unpadded URL-safe base64 (for example
``!rYmCpUwG1PT34NH83HMrUJVPHjXSJttm7DV2jm7Cjjw``). Both are complete, valid IDs: never add
a server name to a room ID that has none, because that names a different room that does not
exist. Use these patterns with ``fullmatch``.
"""

from __future__ import annotations

import re

ROOM_ID = re.compile(r"![A-Za-z0-9._=~-]{1,255}(?::[A-Za-z0-9.-]{1,253}(?::\d{1,5})?)?")
