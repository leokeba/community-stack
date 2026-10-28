# ADR 0006: WhatsApp Integration Strategy

## Status
Accepted

## Context
WhatsApp is the dominant messaging tool in many communities, but it is proprietary, subject to account bans, and unsuited for trusted independent infrastructure. At the same time, forcing everyone to immediately migrate to Matrix would isolate the community.

## Decision
1. **Upstream bridge:**
   - Integrate `mautrix-whatsapp` (official upstream container `dock.mau.dev/mautrix/whatsapp:latest` pinned to a specific release tag).
   - Do NOT implement custom WhatsApp protocol code.
2. **Disabled by default:**
   - The WhatsApp bridge service is disabled in the `starter` profile. It requires explicit opt-in in `accessible` or custom configuration.
3. **Two-phase approach:**
   - **Mode A (Announcement forwarder):** Public announcements forwarded unidirectionally to designated WhatsApp groups or broadcast channels. This is safer, limits exposure, and does not bridge two-way private chat.
   - **Mode B (Selective room bridge):** Explicitly mapped rooms only. No automatic bridging of all rooms. Clear user disclosure in room topics and welcome notices that message content is decrypted and relayed via WhatsApp Web.
4. **Resilience decoupling:**
   - WhatsApp failures (account bans, session expiration, protocol changes) must NOT disrupt the core Matrix homeserver or announcement feed.
   - Dedicated emergency killswitch command to disconnect and revoke the WhatsApp session immediately.

## Consequences
- Protects the core infrastructure from WhatsApp instability.
- Mitigates privacy risks by preventing accidental broad bridging of private coordination spaces.
- Clear separation between public broadcast bridging and private room bridging.
