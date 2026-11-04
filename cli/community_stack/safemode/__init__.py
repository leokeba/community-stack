"""Safe Mode: temporary, account-wide suspension of a person's access to protected Matrix rooms.

Three parts, kept apart so the bot is only one interface to the service:

- ``commands``: parses ``!safe`` messages. It reads only the body and the event shape,
  never display names, and refuses replies and quoted text.
- ``store``: SQLite persistence of protected rooms, enrolled accounts, suspension state,
  the membership snapshot and recovery-code hashes. No activation timestamps are kept.
- ``manager``: the Access Manager. It talks to Matrix only through the ``RoomGateway``
  protocol, so the Matrix transport can be added without touching the rules here.

See ``docs/safe-mode-design.md`` for the design and its limits.
"""
