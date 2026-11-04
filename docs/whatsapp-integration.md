# WhatsApp bridge

The optional WhatsApp integration uses the upstream
[`mautrix-whatsapp`](https://github.com/mautrix/whatsapp) bridge, pinned to
`dock.mau.dev/mautrix/whatsapp:v26.10` (upstream tag `v0.2610.0`). It is unofficial,
uses a linked-device session, and may stop working or put the linked phone number
at risk. WhatsApp must remain an auxiliary destination; the Matrix server and
public announcement feed do not depend on it.

## Set it up in the admin console

Everything below is done from the admin console. You never edit bridge files or run
containers by hand.

1. **Services**: turn on **WhatsApp bridge** and review the warning. The server
   creates the bridge's private credentials, its configuration and its Matrix
   registration, then starts the bridge.
2. **Channels**: connect a dedicated **Matrix announcements** account. It is used to
   post into the bridged WhatsApp group.
3. **WhatsApp bridge** page, step 1: press **Start QR linking**. The console asks the
   bridge bot for a QR code and shows it. On your phone: WhatsApp &rarr; Settings
   &rarr; Linked devices &rarr; Link a device, then scan the code. Codes expire quickly.
4. Step 2: press **Check linked accounts**. It should list the linked account. Registered
   and running is not the same as linked, so check this before going on.
5. Step 2: press **Show bridge commands** to read the bot's own help. Use the bot's
   instructions to bridge the intended WhatsApp group into a Matrix room, and invite
   the announcements account to that room.
6. **Channels**: save the room ID (starts with `!`) as the WhatsApp destination and
   press **Test connection**. It checks that the announcements account can reach the
   room; it cannot confirm the phone is still linked.

The admin console sends only three bot commands: `help`, `list-logins` and
`login qr`. Other commands, such as logout, are not available from the console.

## Two operating models

- **Shared relay (what this page sets up):** the linked session belongs to the
  stack's own admin account (`@community-stack-admin`). It relays the group for the
  community. The phone is a community-owned device, so it must stay charged and online.
- **Personal puppeting (not offered here):** a person links their own WhatsApp and
  reaches their chats through their own Matrix account. Do not mix the two in one
  session.

## What this does not guarantee

- A bridge does not transfer ownership of the WhatsApp account, its group, its
  members or its history to the community. The group still depends on WhatsApp and
  on the linked phone.
- Linked devices disconnect if the phone is offline for more than about two weeks;
  the bridge warns after 12 days without data from the phone. WhatsApp may restrict
  accounts it considers suspicious.
- The bridge reads messages in bridged rooms. Its operator, which is the server
  operator, can read the plaintext. Community Stack does not claim end-to-end
  encryption across WhatsApp.
- Bridging is **bidirectional**: messages in the WhatsApp group also appear in Matrix.
  Tell group members before bridging, and never bridge private coordination rooms.

## Privacy, storage and operations

- Bridge data lives in `data/whatsapp_bridge/` (database, configuration, registration,
  and the linked session). Its credentials are in `.env`. These are included in
  encrypted backups.
- The console keeps the command chat's room ID in `data/agent/bridges/whatsapp.json`.
  QR images are returned to the administrator who asked for them and are not logged.
- To stop the bridge, turn it off on the Services page. Its data and linked session
  are kept so it can start again without pairing. To unlink the phone for good, also
  remove the linked device in WhatsApp under **Settings &rarr; Linked devices**.
- Upgrading: change the pinned version in `services/whatsapp_bridge/docker-compose.yml`
  and check the release notes first. The config template follows the v26 schema.

## Verified on the live server

- The bridge (mautrix-whatsapp v26.10) talks to Synapse and receives appservice traffic. Its
  appservice and homeserver addresses use DNS-safe aliases (`whatsapp-bridge`,
  `matrix-synapse`); underscored container names are rejected.
- The command chat is marked as the management room; commands are sent bare (`help`,
  `login qr`, `list-logins`). If the bridge resets its records, the console opens a new
  chat once and retries.
- `login qr` returns a QR code image to the console.

## Not yet verified

The end-to-end path is not complete until a real phone links, a group is bridged, messages
flow both ways, and a backup and restore has been tested.
