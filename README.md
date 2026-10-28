# Community Communications Stack

A resilient, self-hosted, modular communications infrastructure designed for geographically distributed community movements and public organizations.

## Overview

The Community Communications Stack brings together established, independently maintained open-source software into a coherent, easily operated system:

- **Matrix Homeserver (Synapse):** Open, federated, secure messaging backend.
- **Element Web:** User-friendly web messaging interface.
- **Caddy:** Modern reverse proxy providing automated TLS certificates and secure ingress routing.
- **Canonical Announcement Feed:** Platform-independent public announcements broadcast via Web, RSS, and Matrix rooms.
- **Optional WhatsApp Bridge:** Unidirectional announcement publishing and selective, allowlisted room bridging via `mautrix-whatsapp`.
- **Encrypted Backups:** Automated, client-side encrypted backups (Restic) with verified, non-destructive restore workflows.
- **Operator CLI (`community-stack`):** Unified Python command line interface for deployment, checking, planning, backups, and diagnostics.

## Quick Start (Local Setup)

This project uses [`uv`](https://docs.astral.sh/uv/) for Python dependency and virtual environment management.

```bash
# Clone the repository
git clone https://github.com/your-org/community-stack.git
cd community-stack

# Install dependencies and CLI with uv
uv sync --dev

# Initialize configuration
uv run community-stack init

# Validate configuration and environment
uv run community-stack check

# Inspect the deployment plan without changing the host
uv run community-stack plan
```

## Documentation

- [Design Specification](docs/DESIGN.md)
- [System Architecture](docs/architecture.md)
- [Threat Model](docs/threat-model.md)
- [Architecture Decision Records (ADRs)](docs/decisions/)

## License

This project is licensed under the GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later). See [LICENSE](LICENSE) for details.
