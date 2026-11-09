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
| WhatsApp | mautrix-whatsapp v26.10 | Supported. Linking, room discovery and shared relay are in the admin console. Verified on a live server: linking, login-state check, room discovery, relay, and two-way delivery in a test group. |
| Telegram | mautrix-telegram v26.10 (Go, bridgev2) | Planned. Image confirmed in the registry. Needs your Telegram API credentials and a verified login flow before it is built. |
| Signal | mautrix-signal v26.10 (Go, bridgev2) | Planned. Image confirmed in the registry. The login flow needs a live test with a Signal account. |
| Discord | mautrix-discord v0.7.7 (legacy) | Deferred. Shared relay for bridgev2 is not released yet, and the current bridge needs a bot token. Decision needed. |
| Facebook Messenger | mautrix-meta v26.10 | Planned, experimental. Account restrictions apply. |
| Instagram | mautrix-instagram (separate bridge) | Not validated. Moved out of mautrix-meta in v26.08. |

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
- Removing a bridged portal is available in the WhatsApp console. The bridge's
  `delete-portal` command also removes its Matrix room.
- Chats are bridged automatically only when listed in the bridge configuration
  (`portal_create_filter`); the WhatsApp console manages this allowlist.
- Ghost accounts (Matrix users for platform contacts) are created by the bridge for
  members of bridged chats only.

## Validation for phases 3 and 4

Checked on the upstream release notes, changelogs and the registry. Nothing here is built
or deployed yet.

- **Signal (phase 3):** mautrix-signal v26.10 is maintained on bridgev2. The image is in
  the registry. It needs the same approach as WhatsApp: a QR login from the admin console,
  then a live test with a Signal account. The name `services/signal` is already used by the
  announcements service, so the bridge would need its own service name.
- **Discord (phase 3):** the current release is the legacy architecture. The shared-relay
  login for bridgev2 is not released, and the upstream issue tracking it is still open.
  Community Stack will not use user-token logins. The only option today is a bot-token login
  on the legacy bridge. This needs a decision before it is built.
- **Meta (phase 4):** mautrix-meta v26.10 covers Facebook Messenger, including encrypted
  backfill and blocking. Instagram moved to a separate mautrix-instagram bridge in July 2026,
  and the old Instagram path no longer works. Instagram is not validated. Both are
  experimental and carry account risk.

## Decisions needed before building

1. Signal: go ahead with a test Signal account and phone?
2. Discord: wait for the bridgev2 release, or accept a bot-token setup on the legacy bridge?
3. Meta: start with Messenger only, and accept the experimental account risk?

## Bridges built

The generic bridge path is in place: the catalogue declares each bridge; provisioning,
settings, install, updates, health and console operations are driven from that declaration.

| Bridge | Image | Console page | Status |
|---|---|---|---|
| WhatsApp | mautrix-whatsapp v26.10 | /admin/whatsapp | Verified on a live server (linking, rooms, relay, two-way delivery). |
| Signal | mautrix-signal v26.10 | /admin/bridges/signal | Built and provisioned in tests. The login flow and first-run settings are not yet verified on a live account. |
| Facebook Messenger | mautrix-meta v26.10 | /admin/bridges/messenger | Built and provisioned in tests. Experimental. The login options are read from the bot. Not verified on a live account. |

Each bridge is off by default and is switched on from the Services page. Signal and
Messenger use the generic login flow: "Ask the bot for login options" shows what the
bot offers, and "Start linking" takes the option name from that list.

Discord stays deferred: the bridgev2 release with shared relay logins is not out yet, and
the current bridge needs a bot token on the legacy architecture. Instagram needs its own
bridge, which is not validated.
