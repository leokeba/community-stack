# Deployment Guide: Community Communications Stack

This guide details how to deploy the Community Communications Stack onto a clean Linux server (Debian 12 / Ubuntu 22.04 LTS).

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
| SRV (optional) | `_matrix-fed._tcp.example.org` | `0 10 8448 matrix.example.org` | Matrix direct federation (if enabled) |

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

operations:
  backups:
    enabled: true
    schedule: "daily"
```

Verify permissions on your secrets file:

```bash
chmod 600 .env
```

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

---

## 7. Firewall and Port Exposure

Only the following public ports should ever be exposed:

| Port | Protocol | Service | Notes |
|---|---|---|---|
| 22 | TCP | SSH | Recommend key-based authentication only |
| 80 | TCP | Caddy | Automated ACME Let's Encrypt challenges |
| 443 | TCP | Caddy | HTTPS ingress for all web & Matrix traffic |
| 8448 | TCP | Matrix Synapse | Federation port (optional, only if federation: true) |

All database ports (e.g. PostgreSQL port 5432) remain strictly internal to the `matrix_internal` Docker network and are **never** bound to host ports.
