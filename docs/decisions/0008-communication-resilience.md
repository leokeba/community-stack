# ADR 0008: Communication Resilience Design

## Status
Accepted for decisions D1 and D2 (2026-11-01), on the recommended options, as
the basis for phase CR1 and CR2. Decision D3 (legal review of consent wording)
is open and must be owned by a named person before any public subscription
form ships. See [COMMUNICATION_RESILIENCE_ROADMAP.md](../COMMUNICATION_RESILIENCE_ROADMAP.md).

## Context
Communities should stay reachable when a proprietary network restricts them or
goes away. The stack already has announcements, feeds, an offline archive and
an SMS sign-up. It has no community identity page, no consent records, no
native email, and two subscriber databases.

## Decisions

**D1. Native email is the default; listmonk is optional.** Double opt-in and
SMTP sending live in the stack, so consent records stay in the community's own
data and `/subscribe` works without another service. listmonk remains a
connector for communities that already run it. A relay account is still an
external dependency, but it can be replaced.

**D2. English first.** Public pages use one text catalogue, so French can be
added without rework. Full interface translation is out of scope.

**D3. Legal review.** Open. Engineering does not sign off consent or privacy
wording on its own.

**Identity lives in YAML; runtime state lives in SQLite.** The `community:`
block holds the profile. Channel states and continuity history go in
`announcements.db`. Subscribers stay in `subscribers.db`.

**The `/connect` page is static.** It is written to `data/landing/connect` and
served by the landing nginx, so it survives an announcements outage. Dynamic
routes stay under `/subscribe*`, which the production Caddyfile already routes.

**The page follows canonical state, not channel success.** It is rebuilt after
every publish, edit or withdrawal, even if no channel accepts the job.

**One subscriber list.** `data/announcements/subscribers.db` is the default for
the CLI, the publisher and the sign-up pages. Rows from the old CLI database
(`data/subscriptions/subscribers.db`) are imported on first use, and existing
rows are never overwritten. The old file is left in place.

## Consequences
- The CLI and the console share one list, so the CLI can see sign-ups.
- CR2 must add consent timestamps, consent text version and confirmation time
  before any email is sent. Legacy rows are marked, not given invented dates.
- Email is never shown on `/connect` as available until the sign-up form exists.
- A future connector that needs a new service must justify it in its own ADR.
