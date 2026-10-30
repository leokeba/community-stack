# WhatsApp announcements

The optional WhatsApp integration uses the upstream
[`mautrix-whatsapp`](https://github.com/mautrix/whatsapp) bridge. It is unofficial,
uses a linked-device session, and may stop working or put the linked phone number
at risk. WhatsApp must remain an auxiliary destination; the Matrix server and
public announcement feed do not depend on it.

## Set it up in the admin console

1. On **Services**, enable **WhatsApp bridge** and review the warning. The server
   generates private appservice credentials, creates the bridge configuration,
   registers the appservice with Synapse, and starts the bridge.
2. On **Channels**, connect a dedicated **Matrix announcements** account. This
   account is used to post announcements into the WhatsApp portal room.
3. Open **WhatsApp setup** from Channels. In Element, start a direct chat with
   `@whatsappbot:<your-matrix-domain>` and send `login qr`. Scan the QR code using
   WhatsApp **Linked devices** on the phone you want to link.
4. Use the bridge bot's help in Element to bridge the intended WhatsApp group.
   Invite the Matrix announcements account to the resulting portal room.
5. On Channels, save the portal room's Matrix room ID (starts with `!`) as the
   WhatsApp destination and test the connection.

Published announcements are posted to that Matrix portal room and forwarded by
mautrix-whatsapp. The connection test verifies that the Matrix account can access
the portal room; it cannot confirm that the WhatsApp phone is still linked.

## Privacy and operations

Mautrix bridging is **bidirectional**: messages in the bridged WhatsApp group may
also appear in Matrix. The announcements connector only sends canonical
announcements, but that does not make the bridge itself one-way. Tell group
members before bridging, and never bridge private coordination or administrator
rooms.

To stop the bridge, turn it off on the Services page. To unlink the phone, also
remove the bridge's linked device in WhatsApp **Settings → Linked devices**.
Bridge data and credentials are kept when the service is stopped so it can be
started again without pairing the phone again.
