# Matrix bridges

A Matrix bridge connects an external messaging platform to the community's own Matrix
server. Members keep using the apps they already have, and Matrix becomes an independent
layer that can reach those conversations. Community Stack does not implement any bridge
protocol itself: it deploys and operates the upstream [mautrix](https://github.com/mautrix)
bridges.

## How it fits

- **Matrix (Synapse)** remains the only homeserver. Bridges are application services that
  register with it.
- **Each bridge** is its own container, with its own configuration, registration secrets,
  database, logs and linked session, all under `data/<bridge>/`.
- **The control agent** performs operations on the admin console's behalf. Bridge
  operations are allowlisted: the console can only send the bridge's bot a fixed set of
  commands.
- **Failure isolation.** If a bridge stops, Matrix, the announcements channels and other
  bridges keep working.

## Supported bridges

| Platform | Bridge | Status |
|---|---|---|
| WhatsApp | mautrix-whatsapp v26.10 | Supported. Linking, room discovery and shared relay are in the admin console. Linking is verified on a live server; two-way delivery in a test group is still to be confirmed. |
| Telegram | mautrix-telegram v26.10 (Go, bridgev2) | Planned. Image confirmed in the registry. Needs your Telegram API credentials and a verified login flow before it is built. |
| Signal, Discord, Instagram/Messenger | — | Planned. Not installable yet. |

The catalogue lives in `cli/community_stack/bridges/catalogue.py`. Images are pinned;
`latest` is never used.

## Two operating models

- **Shared relay (what Community Stack sets up).** A dedicated community account is
  linked to the platform, and it relays messages into a group. Members who are not
  linked can send through that account. The link belongs to the community account, not
  a person.
- **Personal puppeting (not offered).** A person links their own account and reaches
  their conversations through their own Matrix login. Do not treat a shared relay as a
  personal account, or the other way round.

## Privacy and encryption

- A bridge reads every message in a bridged room, including messages from encrypted
  rooms, so its operator can read them in plaintext. Community Stack does **not** claim
  end-to-end encryption across a bridged platform.
- The bridge syncs the linked account's contacts and chats. Only chats listed as allowed
  are bridged automatically; everything else stays on the phone. Contacts that are not in
  a bridged chat do not become Matrix accounts.
- Bridged rooms are bidirectional. Tell members before bridging a group, and never bridge
  private coordination rooms.
- Bridge secrets (registration tokens, linked-session data) live in `.env` and
  `data/<bridge>/`, readable only by the service user. Backups include them, encrypted by
  restic.

## Platform risks

A bridge does not transfer ownership of an external account, its membership or its data
to the community. Linked-device sessions can end if the phone stays offline for about two
weeks, and platforms may restrict accounts they consider automated. Matrix adds an access
and continuity layer; it is not a guarantee against platform disruption.

## Operating a bridge

Use the admin console's bridge page for linking, room discovery and relay. Use the CLI
on the host for checks:

```bash
uv run community-stack bridge status      # what is installed, running and registered
uv run community-stack bridge doctor      # each prerequisite, with the fix for failures
uv run community-stack bridge rooms       # Matrix rooms the bridge created for chats
uv run community-stack bridge logs --lines 200
uv run community-stack bridge disable --confirm   # stop it; data and links are kept
```

A container that is running is not the same as a linked account. Confirm linking on the
console ("Check linked accounts").

Lifecycle:
- **Start, stop, restart, enable**: Services page.
- **Upgrade**: change the pinned image in the bridge's compose file, then use the
  service update on the Services page. It takes a backup first and rolls back on failure.
- **Disable**: stops the container and keeps data, credentials and room links.
- **Uninstall**: not automated. Remove the bridge's data directory and its registration
  deliberately, after a backup.

## Backup and restore

Bridge state is part of the normal encrypted backup: the bridge's database is captured
as a consistent snapshot, along with its config, registration and linked session. A
restore brings back the same bridge identity and room links, so the phone does not need
to be linked again unless the platform revoked the session. Test a restore before you
rely on one (`community-stack restore`, with the restore drill on the Backups page).

## Known limitations

- Linking an **existing** Matrix room to a chat is not supported by the bridge version in
  use. Rooms are created by the bridge; the console lists them and can set relay on them.
- Unlinking a single room is not offered: the bridge's delete-portal command removes the
  Matrix room too.
- Chats are bridged automatically only when listed in the bridge configuration
  (`portal_create_filter`). The console does not edit that list yet.
- Ghost accounts (Matrix users for platform contacts) are created by the bridge for
  members of bridged chats only.
