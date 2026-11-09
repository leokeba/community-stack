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
   registration, then starts the bridge. Initial conversation history sync is
   disabled and no group is bridged until you explicitly select it.
2. **WhatsApp bridge** page, step 1: press **Start QR linking**. The console asks the
   bridge bot for a QR code and shows it. On your phone: WhatsApp &rarr; Settings
   &rarr; Linked devices &rarr; Link a device, then scan the code. Codes expire quickly.
3. Step 2: press **Check linked accounts**. It should list the linked account. Registered
   and running is not the same as linked, so check this before going on.
4. On the **WhatsApp bridge** page, paste an invite link for the one group you want
   to bridge and press **Allow and join this group**. The console resolves the group,
   adds only its ID to the allowlist, and joins it. The invite message is redacted
   from the bridge command chat after processing; revoke the invite in WhatsApp
   afterward if possible. Initial history sync is disabled. The allowlist is intended
   to restrict which chats get Matrix rooms, but it does not currently prevent all
   portal records or contact ghosts; see the privacy issue below.
5. **Channels**: connect a dedicated **Matrix announcements** account. Copy the
   new portal room ID (starts with `!`) from **Bridged rooms**, save it as the
   WhatsApp destination and press **Test connection**. Invite the announcements
   account to the portal room. The test checks Matrix access, not phone linkage.
6. To publish from an **existing Matrix room** to WhatsApp, open **Mirrored rooms**,
   add that source room, choose the WhatsApp destination, and select immediate
   publishing or review. Invite the announcements account to the source room first.

The general command form only sends the allowlisted `help`, `list-logins`,
`login qr`, and `cancel` commands. Group selection is a separate, validated
admin operation; invite links are not written to the admin audit log.

## Two operating models

- **Shared relay (what this page sets up):** the linked session belongs to the
  stack's own admin account (`@community-stack-admin`). It relays the group for the
  community. The phone is a community-owned device, so it must stay charged and online.
- **Announcements from an existing Matrix room:** this is one-way publishing from
  the selected Matrix source room to the WhatsApp portal. Messages arriving from
  WhatsApp appear in the bridge-created portal room, not in the original source room.
  Two-way chat requires using that portal room; the bridge cannot attach an existing
  Matrix room directly to a WhatsApp group.
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
- Members of allowed groups may appear as Matrix ghost accounts. The current
  `max_member_sync: 0` setting disables eager non-admin member sync in every group;
  admins may still sync, and non-admins can appear as they participate. It is not
  a per-group allowlist and does not stop standalone contact/profile events.
- On the WhatsApp bridge page, remove stale saved group records that have no Matrix
  room, or remove a bridged portal. Removing a bridged portal also deletes its Matrix
  room and revokes that group's allowlist entry.
- The pinned mautrix-whatsapp v26 configuration stores history-sync settings under
  `network.history_sync` and member-sync limits under `network.max_member_sync`; the
  admin status check verifies both settings and the portal allowlist.

## Known privacy issue in mautrix-whatsapp v26.10

The allowlist is not currently a complete boundary for remote data retained by the
bridge. Investigation found two independent paths:

- mautrix-go checks `portal_create_filter` after the remote-event queue has already
  created a portal database row. Rejected chats do not get a Matrix room, but new
  rows for them can remain in the bridge database.
- mautrix-whatsapp handles `Contact`, `PushName`, `BusinessName`, and contact-picture
  events independently of bridged-group membership and syncs the corresponding
  Matrix ghosts. This can create or update identities for contacts outside the
  allowlist.

The current deployment uses `network.max_member_sync: 0` as a partial mitigation.
It disables eager sync of non-admin members for every group (admins may still sync),
but does not gate standalone contact/profile events. `history_sync.max_initial_conversations: 0`
only disables initial history backfill.

Proposed upstream fixes address these paths separately:
[mautrix/go#688](https://github.com/mautrix/go/pull/688) checks the filter before
inserting new event-driven portal rows, and
[mautrix/whatsapp#1038](https://github.com/mautrix/whatsapp/pull/1038) adds an
opt-out for unsolicited contact/profile sync. Until the latter is available in the
deployed bridge, do not assume the allowlist prevents all contact identities from
being synced.

## Privacy, storage and operations

- Bridge data lives in `data/whatsapp_bridge/` (database, configuration, registration,
  and the linked session). Its credentials are in `.env`. These are included in
  encrypted backups.
- The console keeps the command chat's room ID in `data/agent/bridges/whatsapp.json`.
  QR images are returned to the administrator who asked for them and are not logged.
  Group invite messages are redacted from that chat after the bridge replies.
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

## Verified on a live server

- Linking a phone from the console, the login-state check, and room discovery.
- Two-way delivery in a test group, with shared relay set for the bridged room.
- Backup and restore: a scratch restore kept the bridge's identity and database intact.

Still outside this test: bridging a large group, and behaviour when the linked phone
has been offline for a long time.
