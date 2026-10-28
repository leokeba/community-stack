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
| Draft announcement | `uv run community-stack announce draft -t "Title" -b "Body"` | Saves privately without dispatch |
| Preview/publish | `uv run community-stack announce preview <id>` then `announce publish <id>` | Default output is local public HTML and feeds |
| Run due retries | `uv run community-stack announce worker --once` | Runs queued deliveries now; the admin app also does this every 15 s |
| Export offline bundle | `uv run community-stack archive export` | Creates zero-dependency offline archive |
| Emergency killswitch | `uv run community-stack bridge disable --confirm` | Disconnects WhatsApp bridge immediately |
| Diagnostic report | `uv run community-stack doctor` | Non-destructive, secret-redacted audit |

The admin UI at `/admin` supports the same publishing lifecycle. Only the local
web-feed adapter is enabled by default. Matrix delivery requires explicit
configuration; ActivityPub/email adapter classes are prototypes and have not
been verified against live services.

Organizers each get their own account. The first account, created at
`/admin/setup`, is an **owner**. Owners and admins add accounts on the
Accounts page with one of four roles:

| Role | Can do |
| --- | --- |
| Owner | Everything, including managing other owners |
| Admin | Manage accounts (except owners) and publishing destinations, and publish |
| Publisher | Write, publish, edit and withdraw announcements |
| Reviewer | Read announcements and delivery history |

Role changes and disabled accounts take effect on the next page load, and a
password reset signs the account out everywhere. At least one active owner must
always remain. Sign-ins, failed sign-ins, account changes, publishing actions
and settings changes are recorded with time, account and client address on
the Activity log page, and kept for about 13 months.

### Delivery retries

Each channel delivery is stored as a job before it runs. A temporary failure
(a network error, HTTP 5xx or 429) on a channel that can safely repeat the
operation is retried automatically after 30 s, 2 min, 8 min, 32 min and 1 h,
up to 5 attempts in total. The admin app runs due jobs in the background every
15 seconds; set `COMMUNITY_STACK_EMBEDDED_WORKER=0` to turn that off and run
`community-stack announce worker` as a separate process instead, or
`COMMUNITY_STACK_WORKER_INTERVAL` to change the interval. Withdrawing an
announcement or disabling a channel cancels its pending retries. Pending jobs
are listed on the announcement's page under "Scheduled deliveries".

---

### Control agent

The admin console's System page (owners and admins) shows service status and
can create a backup, check the latest backup, and run a restore drill. The
console never touches the server directly: it asks the **control agent**, a
small privileged service on the host, which accepts only a fixed list of
operations (see [ADR 0007](decisions/0007-control-agent.md)). Without the agent,
the System page says so and the rest of the console works as before.

Install it once on the server, from the stack directory:

```bash
sudo install -m 0644 services/agent/community-stack-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now community-stack-agent
sudo systemctl status community-stack-agent
```

The unit assumes the stack lives in `/opt/community-stack`, that the CLI is
installed in its `.venv`, and that the console runs as UID/GID 1000; edit the
`ExecStart` line if yours differ. The agent listens on
`data/agent/run/agent.sock`, which only the console's group can open, and
records every request in `data/agent/audit.log`. The restore drill asks for the
person's password again; the agent checks it itself and allows five wrong
attempts per 15 minutes. `deploy-vps.sh` restarts the agent after a deployment
if it is installed.

## 2. Maintenance Windows & Upgrades

### Maintenance Rules
1. **Never upgrade without a backup:** `community-stack upgrade` automatically creates a safety snapshot before modifying any container.
2. **One service at a time:** Upgrade services individually (e.g. `caddy` first, then `matrix`, `element`, and `announcements`).
3. **Off-peak scheduling:** Announce maintenance windows to the community at least 24 hours in advance using `community-stack announce create`.

The announcements application is built locally from the checked-out source:
take a backup, update the repository, then run
`uv run community-stack upgrade announcements`. This rebuilds the container
without pulling an application image from a registry.

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
