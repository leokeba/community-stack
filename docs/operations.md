# Operator Handbook & Operational Procedures

This handbook is written for volunteer infrastructure operators managing a live Community Communications Stack deployment.

---

## 1. Quick Reference & Common Commands

| Task | Command | Notes |
|---|---|---|
| Pre-flight check | `uv run community-stack check` | Validates config, secrets, directories, tools |
| Dry-run plan | `uv run community-stack plan` | Safe preview of what would be deployed |
| Deploy services | `uv run community-stack install` | Deploys all enabled Compose services |
| Service health | `uv run community-stack status` | Reports state of all managed containers |
| View service logs | `uv run community-stack logs matrix -f` | Tail and follow logs for any service |
| Create backup | `uv run community-stack backup` | Creates timestamped snapshot with DB dump |
| Verify backup | `uv run community-stack backup verify` | Cryptographic & structural verification |
| Isolated restore | `uv run community-stack restore --target /tmp/test --confirm` | Non-destructive restore drill |
| Safe upgrade | `uv run community-stack upgrade matrix` | Takes backup, pulls image, restarts |
| Publish announcement | `uv run community-stack announce create -t "Title" -b "Body"` | Dispatches to Web, RSS, Matrix, etc. |
| Export offline bundle | `uv run community-stack archive export` | Creates zero-dependency offline archive |
| Emergency killswitch | `uv run community-stack bridge disable --confirm` | Disconnects WhatsApp bridge immediately |
| Diagnostic report | `uv run community-stack doctor` | Non-destructive, secret-redacted audit |

---

## 2. Maintenance Windows & Upgrades

### Maintenance Rules
1. **Never upgrade without a backup:** `community-stack upgrade` automatically creates a safety snapshot before modifying any container.
2. **One service at a time:** Upgrade services individually (e.g. `caddy` first, then `matrix`, then `element`).
3. **Off-peak scheduling:** Announce maintenance windows to the community at least 24 hours in advance using `community-stack announce create`.

### Upstream Software Release Tracking
We pin all container images to explicit upstream releases:

| Service | Upstream Project | Pinned Release | Security Feed |
|---|---|---|---|
| Proxy | [caddyserver/caddy](https://github.com/caddyserver/caddy) | `2.8.4-alpine` | GitHub Releases / Advisories |
| Homeserver | [matrixdotorg/synapse](https://github.com/element-hq/synapse) | `v1.119.0` | Matrix Security Announcements |
| Database | [docker-library/postgres](https://github.com/docker-library/postgres) | `16-alpine` | PostgreSQL Release Notes |
| Web Client | [element-hq/element-web](https://github.com/element-hq/element-web) | `v1.11.70` | Element Web GitHub Releases |
| WhatsApp Bridge | [mautrix/whatsapp](https://github.com/mautrix/whatsapp) | `v0.11.2` | mautrix-whatsapp Releases |

---

## 3. Incident Response Runbooks

### Incident A: Compromised Administrator Account
1. **Immediately revoke admin access token:**
   - Log into Synapse database or execute admin revocation.
   - Rotate `SYNAPSE_SECRET_KEY` and `SYNAPSE_REGISTRATION_SHARED_SECRET` in `.env`.
2. **Restart Synapse to invalidate existing sessions:**
   ```bash
   docker restart community_synapse
   ```
3. **Audit Matrix room permissions and moderators.**

### Incident B: Suspected WhatsApp Bridge Compromise or Account Ban
1. **Trigger Emergency Killswitch:**
   ```bash
   uv run community-stack bridge disable --confirm
   ```
2. **Unlink WhatsApp Web session** from the mobile device under **Settings > Linked Devices**.
3. **Verify Matrix is unaffected:**
   ```bash
   uv run community-stack status
   ```

### Incident C: Server Seizure, Hardware Loss, or Provider Eviction
1. **Activate Secondary Standby Host:**
   - Execute the standby playbook or launch clean Debian 12 VPS.
2. **Obtain off-host backup archive** from your independent S3/SFTP storage bucket.
3. **Run non-destructive restore:**
   ```bash
   uv run community-stack restore --target /opt/community-stack --confirm
   ```
4. **Repoint DNS records:** Update A/AAAA records to the new host IP.
5. **Start services:**
   ```bash
   cd /opt/community-stack
   uv run community-stack install
   ```

---

## 4. Decommissioning & Secure Data Erasure

When taking down a community infrastructure server permanently:
1. **Export final archive for community records:**
   ```bash
   uv run community-stack archive export -o /tmp/final_community_archive.zip
   uv run community-stack backup
   ```
2. **Halt all containers and remove volumes:**
   ```bash
   docker compose -f services/matrix/docker-compose.yml down -v
   docker compose -f services/proxy/docker-compose.yml down -v
   docker compose -f services/landing/docker-compose.yml down -v
   docker compose -f services/element/docker-compose.yml down -v
   ```
3. **Securely erase persistent data directories:**
   ```bash
   # Shred keys and database files
   find data/ -type f -exec shred -u -z -n 3 {} +
   shred -u -z -n 3 .env
   ```
4. **Delete cloud volumes and request VPS disk sanitization** from provider.
