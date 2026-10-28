# Accessible Channels & Multi-Platform Distribution

This document records channel design and evaluation. It is not a deployment guide or a claim that external delivery is production-ready. At present, the default announcement publisher generates local public HTML, RSS, Atom, and JSON Feed output. Matrix, ActivityPub, and email adapter code is not enabled by default and has not been verified against live services.

---

## 1. Overview and Accessibility Principles

Commercial messaging platforms exclude substantial segments of the public:
- People without smartphones or internet access.
- Elderly, vision-impaired, or non-technical community members.
- People living in areas with severe mobile network throttling or power outages.

The publishing domain is intended to provide independent adapters so the **same canonical announcement** can reach diverse mediums. Only the local web/feed destination is currently enabled by default.

---

## 2. ActivityPub Federation (Fediverse)

The `ActivityPubAdapter` prototype translates canonical announcements into ActivityStreams 2.0 `Create(Note)` objects. It is not configured by the default publisher and has not been verified against a live federated service.
- **Audience:** Users of Mastodon, Misskey, GoToSocial, Pixelfed, and the wider fediverse.
- **Protocol:** W3C ActivityPub standard.
- **Privacy:** Public by design. No private community rooms are federated over ActivityPub.
- **Withdrawal:** Emits an ActivityStreams `Delete(Note)` activity if an announcement is retracted.

---

## 3. Email Delivery & Newsletter Evaluation (Not Live-Enabled)

Email remains the most ubiquitous open communications medium.

### Architectural Approaches
1. **Self-Hosted SMTP (e.g. Postfix container):**
   - *Pros:* Fully independent, zero third-party platform lock-in, zero monthly per-email costs.
   - *Cons:* IP warming and deliverability require strict DNS records (SPF, DKIM, DMARC, reverse PTR). Cloud VPS IP ranges (Hetzner, DigitalOcean) are frequently greylisted by Microsoft/Google.
2. **Dedicated Privacy-Respecting Relays (e.g. Migadu, Postmark, AWS SES):**
   - *Pros:* High deliverability out-of-the-box, reliable TLS transmission.
   - *Cons:* Small operational cost, dependency on relay account.

### Legal & Deliverability Compliance
- Mandatory double opt-in verification.
- Mandatory RFC 8058 `List-Unsubscribe` headers and one-click unsubscribe URL.
- Zero tracking pixels or third-party web fonts.

---

## 4. SMS Delivery Evaluation

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

## 5. Voice Information Line Evaluation

A voice information line allows anyone with a basic landline or feature phone to dial a local phone number and hear the latest recorded announcement.

### Technology: Asterisk / FreeSWITCH IVR
- A lightweight Asterisk container answers incoming calls, plays the latest announcement audio file (generated via TTS or recorded by an organizer), and hangs up.
- **Cost:** Requires a SIP trunk (e.g. ~$1-5/month for a DID phone number plus ~$0.005/minute).
- **Accessibility:** Extremely high value for vision-impaired or elderly community members during public emergencies.

---

## 6. Subscriber Management & Privacy

Subscriber records can be managed via `community-stack subscribers`. This does not mean announcement email delivery is operational: the email adapter's default transport is a mock/local success path and is not enabled by the default publisher.
- Storage: Dedicated local SQLite database (`data/subscriptions/subscribers.db`).
- Privacy: Destination masking (`j***@example.org`, `+1234****567`) in all CLI commands and logs.
- Strict Opt-Out: Immediate suppression upon receiving an unsubscribe request.
