# System Architecture: Community Communications Stack

## 1. Architectural Overview

The Community Communications Stack is organized into four distinct architectural layers designed for progressive adoption, operational simplicity, and failure domain isolation:

```
+--------------------------------------------------------------------------+
| Layer A: Core Community Services                                         |
| - Matrix Homeserver (Synapse)    - Element Web Client                    |
| - Public Landing Page            - Canonical Announcement Stream         |
| - (Optional) Vikunja Tasks     - (Optional) FacilMap Maps               |
+--------------------------------------------------------------------------+
                                     |
+--------------------------------------------------------------------------+
| Layer B: Channel Adapters                                                |
| - Matrix Room Adapter            - Static Web / RSS Feed                 |
| - WhatsApp Publisher / Bridge    - (Future: ActivityPub, Email, SMS)     |
+--------------------------------------------------------------------------+
                                     |
+--------------------------------------------------------------------------+
| Layer C: Infrastructure and Operations                                   |
| - Operator CLI (community-stack) - Caddy Reverse Proxy & Automated TLS   |
| - Docker Compose Services        - Ansible Host Provisioning             |
| - Safe Secret Management (.env)  - Health & Diagnostics (doctor)         |
+--------------------------------------------------------------------------+
                                     |
+--------------------------------------------------------------------------+
| Layer D: Resilience and Continuity                                       |
| - Encrypted Backups (Restic)     - Isolated Restore Verification         |
| - Independent Failure Domains    - Offline Static Exports                |
+--------------------------------------------------------------------------+
```

---

## 2. Service Topology and Network Segmentation

Services are packaged as independent Docker Compose modules coordinated by the CLI. Monolithic compose files are avoided to keep failure domains isolated.

```
       [ Public Internet ]
               |
               | (Ports 80, 443)
               v
      +-----------------+
      |  Caddy Ingress  | (proxy_net)
      +-----------------+
        |       |      \
        |       |       \
        v       v        v
   [Landing] [Element] [Synapse Matrix]
                            | (matrix_internal network)
                            v
                       [PostgreSQL]
          [Synapse] <--- (bridge_net) ---> [mautrix-whatsapp] (Optional)
  [Caddy] <--- proxy_net ---> [Vikunja] ---> (vikunja_internal) [PostgreSQL]
  [Caddy] <--- proxy_net ---> [FacilMap] ---> (facilmap_internal) [MariaDB]
```

- **`proxy_net` (Bridge):** Connects Caddy to the HTTP ports of Landing Page, Element Web, Announcements API, and Synapse client endpoints.
- **`matrix_internal` (Internal Bridge, `internal: true`):** Connects Synapse to PostgreSQL. No external internet or host port mapping.
- **`bridge_net` (Dedicated Bridge):** Connects only Synapse and mautrix-whatsapp. It is not attached to public ingress; the bridge still needs outbound internet access.
- **`vikunja_internal` (Internal Bridge, `internal: true`):** Connects Vikunja to its dedicated PostgreSQL database. The database is never attached to ingress or published on the host. Vikunja itself is an independent optional Compose project.
- **`facilmap_internal` (Internal Bridge, `internal: true`):** Connects FacilMap to its dedicated MariaDB database. Only the app joins `proxy_net`; neither service publishes a host port. FacilMap is an independent optional Compose project.

---

## 3. Deployment Profiles

1. **`starter`:**
   - Host configuration + Docker.
   - Caddy reverse proxy with automated TLS.
   - Public Landing Page.
   - Matrix Synapse + PostgreSQL.
   - Element Web.
   - Encrypted local backups & restore tooling.
2. **`community`:**
   - Everything in `starter`.
   - Canonical Announcement Engine (Web, RSS, Matrix Room adapter).
   - Federation enabled (optional).
3. **`accessible`:**
   - Everything in `community`.
   - WhatsApp integration (Mode A announcements first, optional Mode B room bridging).
4. **`resilient`:**
   - Everything in `community` or `accessible`.
   - Off-host backup sync to secondary provider.
   - Second standby host / warm spare provisioning playbook.
   - Offline announcements archive.

---

## 4. Configuration Contract

The system configuration is defined in a single human-readable YAML document (`community-stack.yaml`), validated strictly against `config/schema.json`.

Non-secret configuration (domains, ports, profiles, enabled services) is kept in `community-stack.yaml`.
Secrets (database passwords, Synapse secret keys, backup repository passwords) are kept in `.env` with file permissions `0600`.

Vikunja is disabled by default. When enabled, its pinned image version,
hostname, storage paths, PostgreSQL service, registration/sharing policy, and
resource limits are configured under `services.vikunja`. The operator CLI
generates the Caddy route only while the service is enabled. See
[docs/vikunja.md](vikunja.md) for deployment and recovery details.

FacilMap is also disabled by default. Its image is pinned by immutable OCI
digest, and its hostname, cache/database storage, MariaDB version, and resource
limits live under `services.facilmap`. The operator CLI generates its Caddy
route only while enabled and includes its database dump and cache in backups.
FacilMap has no user accounts or OIDC: map URLs are bearer credentials, not
authenticated private access. See [docs/facilmap.md](facilmap.md) for the
permission model and operational limits.
