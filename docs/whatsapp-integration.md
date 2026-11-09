# WhatsApp bridge

The optional WhatsApp integration uses the upstream
[`mautrix-whatsapp`](https://github.com/mautrix/whatsapp) bridge, pinned to
`dock.mau.dev/mautrix/whatsapp:v26.10` (upstream tag `v0.2610.0`). It is unofficial,
uses a linked-device session, and may stop working or put the linked phone number
at risk. WhatsApp must remain an auxiliary destination; the Matrix server and
public announcement feed do not depend on it.

## Set it up in the admin console

Everything below is done from the admin console, under **Chat → Bridges → WhatsApp**
(`/admin/bridges/whatsapp`). You never edit bridge files or run containers by hand.

1. **Services and updates**: turn on **WhatsApp bridge** and review the warning. The
   server creates the bridge's private credentials, its configuration and its Matrix
   registration, then starts the bridge. History backfill is off and no group is
   bridged until you choose one.
2. **Linked WhatsApp account**: press **Start linking**. The console asks the bridge
   bot for a QR code and shows it. On the phone: WhatsApp &rarr; Settings &rarr; Linked
   devices &rarr; Link a device, then scan the code. Codes expire quickly. Then press
   **Check the linked account**; running is not the same as linked.
3. **Add a group**: paste an invite link for the group and choose its Matrix room:
   - **Create a new room for it**: the group is allowlisted and the bridge creates a
     room named after it.
   - **An existing room**: the group is bridged into that room instead. The stack admin
     joins the room if needed (through Synapse's admin API), invites the bridge bot and
     gives it room-admin power, which the bridge needs to manage the room. If nobody has
     given the stack admin enough power in that room, the server's admin API makes it a
     room admin first, and the page says so.

   In both cases the console turns **relay** on, so messages from every Matrix member
   reach WhatsApp through the linked account. The invite link is redacted from the bot
   chat after use and never written to the activity log; revoke it in WhatsApp
   afterwards if you can.
4. **Linked rooms**: open a room's row to invite Matrix accounts (the bridge bot sends
   the invitation), turn relay on or off, refresh its name and members from WhatsApp,
   or choose **Use for WhatsApp announcements** (see below).
5. **Announcements to WhatsApp**: connect a dedicated **Matrix announcements** account
   on the Channels page once. Then press **Use for WhatsApp announcements** in the
   room's row: the bridge bot invites that account, the account joins, and the room is
   saved as the WhatsApp channel's destination. Turn the channel on under Publishing
   settings.
6. To publish from another **Matrix room** to WhatsApp, open **Mirrored rooms**, add
   that source room and choose the WhatsApp destination.

Groups the bridge has seen but that have no room are listed under **Unlinked groups**.
Each can be linked to an existing room or given a new one.

## Changing and repairing links

Every link is changed through the bridge bot's own commands, never by editing the
bridge's database:

- **Move to another room** links the group to a different room. The old room is kept
  for its Matrix members; WhatsApp members and the bot leave it.
- **Unlink** stops bridging and keeps the room and its history. The group is also
  removed from the allowlist: otherwise its next message would make the bridge create a
  new room for it.
- **Delete room** stops bridging and removes every member from the Matrix room.

**Needs attention** lists what to fix:

- *Broken links*: a group still linked to a room that was deleted, or that the bot has
  left. **Forget link** clears it through the bot; then link the group again.
- *Leftover rooms*: rooms the bot is in that no group is linked to, usually from an
  earlier link. Link a group to one, or let the bot leave (the room is kept).

Under **Status and maintenance**: the bridge's checks with how to fix each, **Restart the
bridge**, **Open a new bot chat** if the bot stops answering, the bot's own command list,
and a link to its logs. **Forget record** under an unlinked group deletes the bridge's
record of it; stop the bridge first.

## Two operating models

- **Shared relay (what the console sets up):** the linked session belongs to the
  stack's own admin account (`@community-stack-admin`). It relays each linked group for
  the community. The phone is a community-owned device, so it must stay charged and
  online.
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
- Matrix room IDs without a `:server` suffix, such as `!rYmCpUwG1PT34…`, are
  complete room version 12 IDs. Synapse creates new rooms in that version. Never add a
  server name to them, and never clear or rewrite a portal's `mxid` in the bridge
  database: mautrix creates a fresh, empty room for a portal it cannot find, and
  members are left behind in the old one.
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
