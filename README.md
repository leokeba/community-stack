# 🌐 Community Communications Stack

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](LICENSE)
[![Python: 3.10+](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue)](pyproject.toml)
[![Tests: 93 passing](https://img.shields.io/badge/tests-93%20passed-success.svg)](tests/)
[![Coverage: 84%](https://img.shields.io/badge/coverage-84%25-brightgreen.svg)](tests/)
[![Architecture: Modular & Self-Hosted](https://img.shields.io/badge/architecture-self--hosted%20%7C%20open--source-orange.svg)](docs/architecture.md)
[![Toolchain: uv](https://img.shields.io/badge/managed%20with-uv-purple.svg)](https://docs.astral.sh/uv/)

> **Resilient, independent, self-hosted communications infrastructure designed for geographically distributed community movements, mutual aid networks, and public organizations.**

When commercial platforms suffer outages, change ownership, ban organizers, or throttle access, a community's voice must not go silent. 

The **Community Communications Stack** is not another chat application—it is an **integrated deployment and operational layer** combining proven, battle-tested open-source software (Matrix Synapse, Element Web, Caddy, Restic, and mautrix-whatsapp) into a unified, easy-to-run system that volunteer non-specialists can deploy, manage, and recover.

---

## 💡 Why Community Communications Stack?

- **📢 Single Source of Truth, Multi-Channel Reach:** Write a public announcement once; broadcast it automatically across a clean web portal, RSS/Atom feeds, JSON Feed, Matrix rooms, the Fediverse (ActivityPub), and opt-in email newsletters.
- **🛡️ Privacy by Separation:** Public bulletins, open community discussion, and sensitive internal coordination live in distinct, strictly isolated zones. Internal rooms are never exposed or bridged.
- **📱 Meet People Where They Are:** Bridge public announcements and allowlisted community rooms to WhatsApp using upstream `mautrix-whatsapp`, without ever making WhatsApp a dependable dependency.
- **📦 Off-Grid & Offline Continuity:** Generate self-contained offline HTML/JSON archives with one command. Distribute them on USB drives or local Wi-Fi captive portals (Raspberry Pi/OpenWrt) during internet blackouts.
- **🔒 True Disaster Recovery:** Automated, client-side encrypted backups (Restic) and verified non-destructive restore drills ensure that complete server loss does not destroy community identity or history.
- **⚙️ Friendly Unified CLI:** Manage the entire infrastructure with a single Python command: `community-stack`. All outputs, logs, and diagnostic reports feature automated secret redaction.

---

## 🏗️ Architecture at a Glance

The stack is organized into four modular layers with isolated Docker networks:

```text
       [ Public Internet ]
               │ (Ports 80, 443, optional 8448)
               ▼
      ┌─────────────────┐
      │  Caddy Ingress  │ (Automated ACME TLS)
      └────────┬────────┘
               │ (proxy_net)
     ┌─────────┼─────────────────────────┐
     ▼         ▼                         ▼
┌─────────┐ ┌─────────┐             ┌─────────┐
│ Landing │ │ Element │             │ Synapse │ (Matrix Homeserver)
│  Page   │ │   Web   │             └────┬────┘
└─────────┘ └─────────┘                  │ (matrix_internal - Isolated)
                                         ▼
                                   ┌──────────┐
                                   │PostgreSQL│ (Database)
                                   └──────────┘
                                         ▲
                                         │ (bridge_net)
                                   ┌──────────┐
                                   │ WhatsApp │ (mautrix-whatsapp, Optional)
                                   │  Bridge  │
                                   └──────────┘
```

| Layer | Components | Role |
|---|---|---|
| **Layer A: Core Services** | Matrix Synapse, Element Web, Public Portal | Open, federated community chat & web client |
| **Layer B: Distribution Adapters** | Web/RSS/Atom, Matrix Room, ActivityPub, Email, WhatsApp | Reaching users across diverse platforms |
| **Layer C: Operations & Ingress** | Caddy, Docker Compose, Ansible, `community-stack` CLI | Automated TLS, networking, host configuration |
| **Layer D: Resilience & Continuity** | Restic, Standby Host Playbook, Offline HTML Exporter | Surviving outages, provider eviction, server loss |

---

## 🚀 Quick Start (Local Setup)

This project uses [`uv`](https://docs.astral.sh/uv/) for high-speed, reproducible Python virtual environments and dependency management.

### 1. Clone and Install Dependencies
```bash
git clone https://github.com/your-org/community-stack.git
cd community-stack

# Sync virtual environment and dependencies
uv sync --all-extras --dev
```

### 2. Initialize Configuration & Secrets
```bash
# Creates community-stack.yaml, data directories, and .env with secure random keys
uv run community-stack init
```

### 3. Validate Configuration & Inspect Plan
```bash
# Run comprehensive configuration and environment checks
uv run community-stack check

# Preview the dry-run execution plan without modifying the host
uv run community-stack plan
```

---

## ⚡ CLI Command Reference

The `community-stack` CLI provides a single, predictable interface for all operational tasks:

```text
Usage: community-stack [OPTIONS] COMMAND [ARGS]...

Infrastructure & Lifecycle:
  init          Initialize local configuration, directories, and secure credentials
  check         Validate configuration against JSON Schema, secrets, and system tools
  plan          Read-only dry-run preview of services, networks, and ingress routes
  install       Deploy enabled services to the host via Docker Compose
  status        Report running container health, status, and port bindings
  logs          Stream or tail service logs (e.g. community-stack logs matrix -f)
  upgrade       Safely upgrade a service with an automatic pre-upgrade snapshot
  doctor        Run non-destructive diagnostics with automated secret redaction

Backup & Recovery:
  backup        Create a client-side encrypted backup snapshot (with DB dump)
  backup verify Verify archive completeness, manifest, and cryptographic integrity
  restore       Safely restore a snapshot to an isolated directory (--confirm required)

Broadcast & Channels:
  announce      Create, list, update, and retract multi-channel announcements
  subscribers   Manage opt-in email and SMS broadcast lists with instant opt-out
  bridge        Inspect WhatsApp bridge health or trigger the emergency killswitch
  archive       Export and verify zero-dependency offline HTML archives
```

### Example: Publish a Multi-Channel Public Bulletin
```bash
uv run community-stack announce create \
  --title "Mutual Aid Distribution Station Open" \
  --body "Supplies and hot meals available at City Hall until 8pm." \
  --author "Logistics Working Group" \
  --tags "mutual-aid,emergency"
```

### Example: Create & Verify an Encrypted Backup
```bash
# Create snapshot
uv run community-stack backup

# Verify integrity
uv run community-stack backup verify
```

### Example: Export Offline Archive for USB / Wi-Fi Distribution
```bash
uv run community-stack archive export -o backups/offline_announcements.zip
uv run community-stack archive verify backups/offline_announcements.zip
```

### Example: Emergency WhatsApp Disconnect (Killswitch)
```bash
# Halts WhatsApp bridge immediately while keeping Matrix 100% operational
uv run community-stack bridge disable --confirm
```

---

## 📖 Comprehensive Documentation

| Document | Description |
|---|---|
| 📋 [**Design Specification**](docs/DESIGN.md) | Architectural baseline, principles, and roadmap milestones |
| 🏛️ [**System Architecture**](docs/architecture.md) | Four-layer architecture, profiles, and service topologies |
| 🛡️ [**Threat Model**](docs/threat-model.md) | 10 threat vectors, mitigations, residual risks, and non-guarantees |
| 🚀 [**Deployment Guide**](docs/deployment.md) | Step-by-step installation on clean Debian 12 / Ubuntu VPS |
| 💾 [**Backup & Recovery Runbook**](docs/backup-restore.md) | Disaster recovery, off-host S3 backups, and restore drills |
| 💬 [**WhatsApp Integration Guide**](docs/whatsapp-integration.md) | mautrix-whatsapp setup, allowlisted bridging, and privacy risks |
| 📡 [**Accessible Channels**](docs/accessible-channels.md) | Fediverse (ActivityPub), Email newsletter, SMS, and IVR lines |
| 🌐 [**Multi-Host Resilience & Offline Continuity**](docs/multi-host-resilience.md) | Warm standby host provisioning, captive portals, and Meshtastic |
| 📘 [**Operator Handbook**](docs/operations.md) | Daily maintenance, incident response runbooks, and decommissioning |
| 🤝 [**Governance & Handover**](docs/governance.md) | Two-person rule, credential escrow, and license attribution |
| 📐 [**Architecture Decisions (ADRs)**](docs/decisions/) | Accepted ADRs for homeserver, ingress, backup, and storage choices |

---

## 🧪 Testing & Quality Assurance

The codebase includes an exhaustive test suite covering schema validation, secret redaction, CLI commands, container service configurations, and disaster recovery:

```bash
# Run full test suite with coverage
uv run pytest --cov=community_stack --cov-report=term-missing

# Run code style & lint checks
uv run ruff check .
uv run ruff format --check .
```

- **93 Automated Tests** (100% passing)
- **84% Code Coverage** across all Python modules
- **Automated CI Workflow** testing Python 3.10, 3.11, 3.12, and 3.13

---

## 🤝 Contributing

We welcome contributions from organizers, developers, and infrastructure specialists! Please review [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md) before opening pull requests or reporting security issues.

---

## 📄 License

This project is licensed under the **GNU Affero General Public License v3.0 or later** ([AGPL-3.0-or-later](LICENSE)).

All upstream container software (Synapse, Element, Caddy, PostgreSQL, mautrix-whatsapp, Restic) retain their respective open-source licenses. See [docs/governance.md](docs/governance.md) for complete attribution.
