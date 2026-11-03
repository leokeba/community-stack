# Accessible Channels & Multi-Platform Distribution

This document records channel design. It is not a deployment guide. Each
destination's status below is checked against the code; the README and
operations guide hold the operator steps.

At present, the default announcement publisher generates local public HTML, RSS, Atom, and JSON Feed output, and the `/connect` contact page. Matrix, ActivityPub, email and SMS are available once connected in the console.

## Implementation Status

| Status | Destinations |
|---|---|
| **Working and covered by automated tests** | Public HTML archive, stable announcement pages, RSS, Atom, JSON Feed, the `/connect` contact page and its QR codes |
| **Implemented; staging verification required** | Matrix public-room adapter (opt-in). Email through SMTP or Mailgun, with double opt-in sign-up, signed one-click unsubscribe and per-person delivery tracking. Text alerts through an Android SMS gateway |
| **Implemented; not yet verified against a live service** | ActivityPub actor and follower delivery. Pixelfed account publishing through its Mastodon-compatible API. Email delivery through a real SMTP relay or Mailgun API (CI uses fake transports; test credentials before relying on them) |
| **Planned** | Voice/IVR delivery |

---

---

## 1. Overview and Accessibility Principles

Commercial messaging platforms exclude substantial segments of the public:
- People without smartphones or internet access.
- Elderly, vision-impaired, or non-technical community members.
- People living in areas with severe mobile network throttling or power outages.

The publishing domain is intended to provide independent adapters so the **same canonical announcement** can reach diverse mediums. Only the local web/feed destination is currently enabled by default.

---

## 2. ActivityPub Federation (Fediverse)

The `ActivityPubAdapter` translates canonical announcements into ActivityStreams 2.0 `Create(Note)` objects. It is connected from the console (the fediverse connection). It has not been verified against a live federated service.
- **Audience:** Users of Mastodon, Misskey, GoToSocial, Pixelfed, and the wider fediverse.
- **Protocol:** W3C ActivityPub standard.
- **Privacy:** Public by design. No private community rooms are federated over ActivityPub.
- **Withdrawal:** Emits an ActivityStreams `Delete(Note)` activity if an announcement is retracted.

## 3. Pixelfed Account

The Pixelfed connector publishes announcements directly from a connected
Pixelfed account. This is separate from the ActivityPub actor above: Pixelfed
users can already follow that actor, while this connector posts as the
community's Pixelfed account.

- **Setup:** On **Admin → Channels → Pixelfed**, enter the server URL and choose
  **Connect with Pixelfed**, then follow the **Continue to Pixelfed** link. The
  console registers an OAuth client through the Pixelfed API and opens the
  authorization page for approval of `read` and `write` access. The callback URL is
  `https://<community-web-domain>/admin/channels/pixelfed/callback`; the console
  displays its configured URL beside the button. After approval, follow the
  return link to the admin console. Pixelfed must allow app
  registration through its API. If it does not, an existing access token can
  still be entered using the manual option.
- **Publishing:** Posts are public. The announcement's shared image and alt
  text are uploaded to Pixelfed; channel-specific caption text is supported.
- **Limits:** The connection test reads the server's reported caption limit.
  When the server does not report one, previews use a 2,000-character default.
- **Switching:** Connect Pixelfed and Instagram independently, then enable or
  disable either destination in publishing settings. This changes future
  deliveries; it does not migrate followers or previously published posts.
- **Live verification:** CI covers the API contract with a fake transport.
  Verify token scopes and publishing against the community's chosen Pixelfed
  instance before relying on it.

The API is instance-specific; see the [Pixelfed API documentation](https://developers.pixelfed.org/api).

---

## 4. Email Delivery & Newsletter

Email remains the most ubiquitous open communications medium.

### Architectural Approaches
1. **Self-Hosted SMTP (e.g. Postfix container):**
   - *Pros:* Fully independent, zero third-party platform lock-in, zero monthly per-email costs.
   - *Cons:* IP warming and deliverability require strict DNS records (SPF, DKIM, DMARC, reverse PTR). Cloud VPS IP ranges (Hetzner, DigitalOcean) are frequently greylisted by Microsoft/Google.
2. **Dedicated Privacy-Respecting Relays (e.g. Migadu, Postmark, AWS SES):**
   - *Pros:* High deliverability out-of-the-box, reliable TLS transmission.
   - *Cons:* Small operational cost, dependency on relay account.
3. **Mailgun API:**
   - Configure the sending domain name only (for example, `sandbox123.mailgun.org`), private API key and US or EU region on the Channels page. Do not enter the API base URL in the domain field.
   - Sends one API request per active subscriber, preserving the existing delivery ledger and one-click unsubscribe headers.
   - Open and click tracking are explicitly disabled on each API request.
   - The connection test checks the Mailgun domain credentials without sending an email.

### Legal & Deliverability Compliance
- Mandatory double opt-in verification.
- Mandatory RFC 8058 `List-Unsubscribe` headers and one-click unsubscribe URL.
- Zero tracking pixels or third-party web fonts.

---

## 5. SMS Delivery Evaluation

SMS enables critical emergency outreach when mobile data networks are throttled or down.

### Evaluation of Upstream SMS Gateways
1. **Established Open-Source Gateways:**
   - **Kannel:** High-capacity WAP and SMS gateway, mature SMPP client. Complex C codebase and configuration.
   - **Jasmin SMS Gateway:** Python/Twisted-based SMPP gateway with REST API and web UI. Ideal for high-volume SMPP routing.
2. **Local Hardware (USB GSM Modem / SIM800L / Gammu):**
   - *Pros:* Completely off-grid capable, zero external cloud dependencies.
   - *Cons:* Throughput limited to ~6-10 SMS per minute per SIM card. Mobile carriers aggressively throttle or block prepaid SIM cards that exhibit bulk-sending behavior.
3. **Cloud SMS APIs (Twilio, Vonage, Telnyx):**
   - *Pros:* High delivery speed, global carrier routing.
   - *Cons:* Substantial per-message cost (often $0.01 - $0.08 per SMS), mandatory commercial A2P 10DLC registration, government censorship vulnerability.

### SMS Quotas and Rate Limits
- SMS must be restricted to **urgent emergency broadcasts only**.
- Automated rate limiting: max 1 broadcast per 24 hours per recipient.

---

## 6. Voice Information Line Evaluation

A voice information line allows anyone with a basic landline or feature phone to dial a local phone number and hear the latest recorded announcement.

### Technology: Asterisk / FreeSWITCH IVR
- A lightweight Asterisk container answers incoming calls, plays the latest announcement audio file (generated via TTS or recorded by an organizer), and hangs up.
- **Cost:** Requires a SIP trunk (e.g. ~$1-5/month for a DID phone number plus ~$0.005/minute).
- **Accessibility:** Extremely high value for vision-impaired or elderly community members during public emergencies.

---

## 7. Subscriber Management & Privacy

Subscriber records can be managed via `community-stack subscribers`. This does not mean announcement email delivery is operational: the email adapter's default transport is a mock/local success path and is not enabled by the default publisher.
- Storage: Dedicated local SQLite database (`data/subscriptions/subscribers.db`).
- Privacy: Destination masking (`j***@example.org`, `+1234****567`) in all CLI commands and logs.
- Strict Opt-Out: Immediate suppression upon receiving an unsubscribe request.
