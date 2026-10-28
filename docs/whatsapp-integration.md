# WhatsApp Integration & Bridging Guide

## 1. Architectural Baseline & Disclaimer

The Community Communications Stack provides an **optional**, **disabled-by-default** integration with WhatsApp using the upstream [`mautrix-whatsapp`](https://github.com/mautrix/whatsapp) project.

### Critical Operational Disclaimer
- **Unofficial Integration:** `mautrix-whatsapp` connects via reverse-engineered WhatsApp Web protocols. It is not an official WhatsApp API or WhatsApp Business solution.
- **Account Ban Risk:** WhatsApp actively monitors automated and non-official clients. Any phone number used for the bridge is subject to temporary or permanent bans without prior notice.
- **Auxiliary Channel Only:** WhatsApp is treated as an auxiliary convenience channel. The core Matrix homeserver, public web portal, and RSS announcement feeds **must never** depend on WhatsApp availability.

---

## 2. Integration Modes

### Mode A: Public Announcement Forwarder (Recommended)
In Mode A, canonical public announcements created with `community-stack announce` are forwarded unidirectionally to a designated WhatsApp broadcast list or group.
- **Security & Privacy:** No private or two-way messages are bridged. Only public announcements are sent.
- **Low Risk:** Does not expose community member identifiers or message metadata.

### Mode B: Selective Two-Way Room Bridging (Restricted)
In Mode B, explicitly configured Matrix rooms are bridged to designated WhatsApp groups.
- **Strict Allowlisting:** Only rooms listed in `services.whatsapp_bridge.mapped_rooms` are bridged. No automatic bridging of all rooms.
- **No Sensitive Coordination:** Internal organizing rooms, security discussions, or administrator channels must **never** be bridged.
- **Mandatory User Disclosure:** Room topics and welcome notices must clearly state:
  > *"Notice: This room is bridged to a WhatsApp group. Messages sent here are unencrypted on WhatsApp and visible to WhatsApp Web."*

---

## 3. Configuration Contract

In `community-stack.yaml`:
```yaml
services:
  whatsapp_bridge:
    enabled: true
    mode: "selective_bridge" # none | announcements_only | selective_bridge
    mapped_rooms:
      - matrix_room_id: "!public-announcements:example.org"
        whatsapp_chat_jid: "123456789-group@g.us"
        description: "Public Announcements Group"
```

In `.env`:
```bash
WHATSAPP_AS_TOKEN=generate_hex_32_token
WHATSAPP_HS_TOKEN=generate_hex_32_token
```

---

## 4. Operational Runbook

### Checking Bridge Health
```bash
uv run community-stack bridge status
```

### Listing Allowlisted Rooms
```bash
uv run community-stack bridge list-rooms
```

### Emergency Disconnect (Killswitch)
If an unauthorized room is bridged, suspicious bridge activity is detected, or the linked phone is compromised:

```bash
uv run community-stack bridge disable --confirm
```

What the killswitch does:
1. Immediately stops the `community_whatsapp_bridge` container.
2. Halts outgoing and incoming bridge traffic.
3. Preserves all Matrix homeserver data and internal rooms without disruption.

### Unlinking and Re-authenticating
To completely disconnect:
1. Open WhatsApp on the linked smartphone: **Settings > Linked Devices**.
2. Select the **mautrix-whatsapp / Chrome** session and tap **Log Out**.
3. Clear the bridge data directory on the server: `rm -rf data/whatsapp_bridge/*`.
