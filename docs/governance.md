# Governance, Maintainer Handover, and Licensing

## 1. Governance Principles & Continuity

Public community infrastructure must never depend on a single individual's goodwill, memory, or personal accounts.

### The Two-Person Rule
1. **Domain Registrar Access:** At least two designated maintainers must hold administrative access to the domain registrar account with hardware 2FA keys (e.g. YubiKey).
2. **Off-Host Backup Encryption:** The `RESTIC_PASSWORD` and off-host S3 credentials must be stored in a shared password manager (e.g. Bitwarden / KeePassXC) accessible to multiple trusted operators.
3. **Emergency Access Escrow:** A sealed physical copy of recovery credentials should be held in escrow with a trusted community elder or organization treasurer.

---

## 2. Onboarding & Offboarding Technical Volunteers

### Onboarding Procedure
1. Verify agreement with [docs/threat-model.md](threat-model.md) and security principles.
2. Grant access to staging / test environment before touching production.
3. Conduct a recovery drill: the new operator must successfully execute `community-stack restore --target /tmp/test --confirm` and verify all components.
4. Add the operator's public SSH key to the host with individual user accounts (`sudo` access), never sharing a generic `root` key.

### Offboarding / Access Revocation Checklist
When a maintainer steps down or leaves the team:
1. **Remove SSH Public Key** from `/home/<username>/.ssh/authorized_keys` and `/etc/sudoers`.
2. **Rotate Database Password (`POSTGRES_PASSWORD`):** Update `.env`, alter password in PostgreSQL, restart Synapse.
3. **Rotate Matrix Registration Secret (`SYNAPSE_REGISTRATION_SHARED_SECRET`).**
4. **Rotate Off-Host Backup Passwords:** Re-key Restic repository password and rotate S3 access tokens.
5. **Rotate Domain Registrar and DNS API tokens.**
6. Document the departure and date in the operational log.

---

## 3. Third-Party Software License Compliance

All bundled assets, upstream container images, and software components must remain open-source and legally compatible:

| Component | Upstream Project | License | Distribution Mode |
|---|---|---|---|
| Community Stack CLI | This repository | AGPL-3.0-or-later | Direct source |
| Matrix Synapse | Matrix.org / Element | AGPL-3.0 | Upstream container image |
| Element Web | Element.io | Apache-2.0 | Upstream container image |
| Caddy Server | Caddyserver | Apache-2.0 | Upstream container image |
| PostgreSQL | PostgreSQL Global Development Group | PostgreSQL (MIT-style) | Upstream container image |
| mautrix-whatsapp | Tulir Asokan / mau.fi | AGPL-3.0 | Upstream container image |
| Restic | Alexander Neumann | BSD-2-Clause | Upstream binary / package |

No proprietary or non-commercial restricted software is included in the stack.
