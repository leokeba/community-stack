# ADR 0004: Application Orchestration and Host Provisioning

## Status
Accepted

## Context
The stack must be deployable by small volunteer teams onto standard Linux servers (e.g. Debian 12 / Ubuntu 22.04 LTS). We need to clearly separate host-level setup from application service lifecycle.

## Decision
1. **Host-level provisioning:**
   - **Ansible** playbooks and roles for OS preparation, firewall (UFW/nftables), Docker installation, non-root user setup, and kernel tuning.
   - Ansible roles are modular: `host`, `docker`, `firewall`, `proxy`, `backup`, `monitoring`.
2. **Application service lifecycle:**
   - **Docker Compose** (Compose v2) with separate service definitions:
     - `services/landing/`: static site with public announcements and directory.
     - `services/matrix/`: Synapse homeserver + PostgreSQL database.
     - `services/element/`: Element Web client.
     - `services/announcements/`: Canonical announcements API and feed generator.
     - `services/proxy/`: Caddy reverse proxy.
     - `services/whatsapp_bridge/`: Optional mautrix-whatsapp bridge.
3. **Internal network segregation:**
   - `proxy_net`: Caddy to public-facing HTTP backends (landing, element, synapse client API).
   - `matrix_internal`: Synapse to PostgreSQL (database never exposed to `proxy_net` or host).
   - `bridge_net`: Bridge to Synapse appservice port (isolated from public).

## Consequences
- Clean separation of concerns: host configuration vs container runtime.
- Services can be started, stopped, inspected, and upgraded independently.
- No monolithic Docker Compose file that risks cascading restarts.
