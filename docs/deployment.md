# Deployment Guide: Community Communications Stack

This guide details how to deploy the Community Communications Stack onto a clean Linux server (Debian 12 / Ubuntu 22.04 LTS).

## Quick install

From a workstation with SSH access to a fresh Debian 12/13 or Ubuntu
22.04/24.04 server, run the script without `sudo`. It connects as `debian` by
default and uses non-interactive `sudo` on the server:

```bash
bash scripts/bootstrap.sh example.org
```

For an IP-only setup, pass the server's public IPv4 address. The IP is also
used as the SSH target:

```bash
bash scripts/bootstrap.sh 149.56.210.214
```

If the base domain does not resolve directly to the server, set `VPS_HOST` to
its IP or SSH hostname. `VPS_USER` defaults to `debian` and `VPS_SSH_PORT`
defaults to `22`. Alternatively, SSH into the target first and run
`sudo bash scripts/bootstrap.sh example.org` there.

The script handles system packages, Docker, a `community` service account,
the code in `/opt/community-stack`, configuration and generated secrets, the
firewall (SSH, HTTP and HTTPS only; every port sshd listens on stays open),
the services, and the control agent.

The base domain supplies sensible defaults: the web/admin site is
`community.example.org`, Matrix is `matrix.example.org`, the ACME contact is
`admin@example.org`, and the scheduling time zone is UTC. The Element Web
client is available at `https://community.example.org/app`. Create DNS records
for both service hostnames pointing to the server before running the script.
For a public IPv4 address, pass the address instead; the script adds `.nip.io`
and derives the same service hostnames automatically:

```bash
sudo bash bootstrap.sh 149.56.210.214
```

Optional `--web-domain`, `--matrix-domain`, `--email`, and `--timezone` flags
override those defaults. Add `--dry-run` to see every step without changing
anything. On successful installation, the script prints the one-time admin
setup token and setup URL. It can be run again safely: it keeps existing
configuration, secrets and data. Keep a copy of `RESTIC_PASSWORD` from
`/opt/community-stack/.env` offline.

On first install, `community-stack install` also generates what each service
needs, without ever overwriting existing files: Synapse's `homeserver.yaml`
(PostgreSQL, the configured domain, registration and federation settings),
its signing key and log configuration in `data/matrix`, owned by Synapse's
UID 991, and Element's `data/element/config.json` pointing at this
homeserver. Caddy receives `WEB_DOMAIN`, `MATRIX_DOMAIN` and `ACME_EMAIL` from
`.env`.

The rest of this guide describes the same steps by hand.

---

## 1. Prerequisites

### Hardware Requirements
- **CPU:** 2 vCPU cores minimum (4 vCPU recommended for high community traffic).
- **RAM:** 2 GB minimum (4 GB recommended for Matrix Synapse + PostgreSQL).
- **Storage:** 20 GB SSD minimum for OS, database, and media cache.
- **Network:** Static public IPv4 (and IPv6 if available).

### Domain and DNS Records
Before deploying TLS certificates with Caddy, configure your DNS provider with the following records pointing to your server's public IP address:

| Type | Record / Hostname | Points To | Purpose |
|---|---|---|---|
| A / AAAA | `community.example.org` | `<server-ip>` | Public web portal, Element Web, Announcements |
| A / AAAA | `matrix.example.org` | `<server-ip>` | Matrix homeserver Client-Server API |

---

## 2. Server Preparation

Connect to your target server via SSH:

```bash
ssh root@<server-ip>
```

Clone the repository and install `uv`:

```bash
# Clone the repository
git clone https://github.com/your-org/community-stack.git /opt/community-stack
cd /opt/community-stack

# Install uv (if not already present)
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
```

---

## 3. Configuration & Secrets

Initialize local configuration and generate secure credentials:

```bash
# Create local community-stack.yaml, .env, and data directories
uv run community-stack init
```

Edit `community-stack.yaml` to specify your domains:

```yaml
project:
  name: "my-community"
  profile: "starter"

domain:
  base: "example.org"
  matrix: "matrix.example.org"
  web: "community.example.org"

services:
  matrix:
    enabled: true
    federation: false
    allow_registration: false
  element:
    enabled: true
  announcements:
    enabled: true
    channels:
      web_feed:
        enabled: true
      matrix_room:
        enabled: false

operations:
  backups:
    enabled: true
    schedule: "daily"
```

Verify permissions on your secrets file:

```bash
chmod 600 .env
```

### Secure first administrator setup

Generate a one-time random setup token and place it in `.env` as
`COMMUNITY_STACK_ADMIN_SETUP_TOKEN=<generated-token>`. Keep `.env` mode `0600`.
Deploy the stack with `uv run community-stack install`, then open
`https://community.example.org/admin/setup` and enter that token plus a unique
administrator username and a password of at least 12 characters. Setup is
disabled after the first administrator is created. Remove the setup-token line
from `.env` and run `uv run community-stack install` again to remove it from the
running container environment.

The admin app is routed by Caddy and has no host-published port. It shares the
web domain but requires authentication for all publishing pages. The public
announcement archive remains unauthenticated at `/feed/announcements.html`.
The app's SQLite database is mounted from `data/announcements`; generated public
files are shared read-only with the landing service from `data/landing/feed`.
The app runs as UID/GID `1000:1000` by default so it can write those host
directories without root privileges. If the deployment account owns them under
different IDs, set `COMMUNITY_STACK_UID` and `COMMUNITY_STACK_GID` in `.env`
before deploying.

For Matrix delivery, configure `services.announcements.channels.matrix_room`
only after verifying that the target room is the public announcements room.
Set `public_announcement_room: true`, set `access_token_env` to
`COMMUNITY_STACK_MATRIX_ANNOUNCEMENTS_TOKEN`, define that variable in `.env`,
and keep the token out of YAML. The announcements container does not load
`.env` wholesale; it receives only the setup token and
`COMMUNITY_STACK_MATRIX_ANNOUNCEMENTS_TOKEN`, so a different variable name must
also be added to `services/announcements/docker-compose.yml`. Matrix delivery
is disabled by default and has not been verified against a live server.

---

## 4. Pre-Flight Validation & Dry Run

Run the pre-flight checks:

```bash
uv run community-stack check
```

Inspect the dry-run execution plan:

```bash
uv run community-stack plan
```

---

## 5. Host Automation with Ansible

Apply host preparation, Docker installation, firewall rules, and container deployment:

```bash
# Install Ansible dependencies (if using Ansible directly)
uv run pip install ansible

# Run the deployment playbook locally
ansible-playbook -i "localhost," -c local ansible/playbooks/site.yml
```

---

## 6. Service Verification

Check container health:

```bash
uv run community-stack status
```

Inspect logs:

```bash
uv run community-stack logs matrix
uv run community-stack logs caddy
```

Verify web ingress:
1. Open `https://community.example.org` in a browser — verify that the public landing page loads with HTTPS.
2. Open `https://community.example.org/app` — verify that Element Web loads and connects to `https://matrix.example.org`.
3. Test federation delegation: `curl -s https://matrix.example.org/.well-known/matrix/client`.
4. Open `/admin` and confirm that unauthenticated requests are redirected to login.

### Publishing service upgrades

The announcements app is built from the checked-out source, not pulled from a
registry. From a clean local checkout, deploy the committed source with:

```bash
VPS_HOST=<host-or-ip> VPS_USER=debian ./scripts/deploy-vps.sh
```

The script runs tests and configuration checks, requires a clean worktree,
creates and verifies a restrictive pre-deployment backup, and transfers only
tracked source files. It preserves the VPS `.env`, `community-stack.yaml`,
`data/`, `backups/`, and production-specific Caddy hostnames. It builds the
announcements image, waits for health, and then validates and recreates Caddy.
SSH authentication uses the local SSH agent; the web application receives no
SSH key, Docker socket, or deployment shell access.

The announcement database schema is migrated additively at application startup.
Public files are rebuildable from SQLite and are regenerated after publication,
withdrawal, or re-enabling the web-feed destination.

---

## 7. Firewall and Port Exposure

Only the following public ports should ever be exposed:

| Port | Protocol | Service | Notes |
|---|---|---|---|
| 22 | TCP | SSH | Recommend key-based authentication only |
| 80 | TCP | Caddy | Automated ACME Let's Encrypt challenges |
| 443 | TCP | Caddy | HTTPS ingress for all web & Matrix traffic |

All database and application ports remain internal to Docker networks and are
**never** bound to host ports. Only Caddy publishes ports 80 and 443. Matrix
federation uses port 443 through `/.well-known/matrix/server` delegation.
Docker-published ports bypass UFW, so do not publish extra ports expecting the
host firewall to filter them.
