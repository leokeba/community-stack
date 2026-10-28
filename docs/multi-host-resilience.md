# Multi-Host Resilience, Failover, and Offline Continuity

## 1. Multi-Host Deployment & Failure Domains

Relying on a single cloud VPS or datacenter exposes community infrastructure to hosting provider termination, network isolation, or physical seizure.

### Recommended Multi-Host Architecture
```
+------------------------------------+       +------------------------------------+
| Primary Host (Provider A)          |       | Warm Standby Host (Provider B)     |
| - Active Caddy Ingress             |       | - Pre-provisioned Docker & Playbook|
| - Active Matrix Synapse & Postgres |       | - Pre-pulled Docker Images         |
| - Primary Announcements Engine     |       | - Ready to restore latest snapshot |
+------------------------------------+       +------------------------------------+
                  \                                     /
                   \                                   /
                    v                                 v
          +-----------------------------------------------------+
          | Independent Off-Host Encrypted Storage (Provider C) |
          | - Restic / S3 Repository                            |
          | - Client-side encrypted snapshots                   |
          +-----------------------------------------------------+
```

### Failure Domain Separation
1. **Different Legal Jurisdictions:** Provider A and Provider B should be operated under different corporate and national legal frameworks where feasible.
2. **Independent Credentials:** Standby server SSH keys and cloud access tokens must **never** be identical to the primary host.
3. **No Blind Auto-Failover:** Automated split-brain split-voting clusters add immense operational complexity. For community organizations, a documented, operator-driven failover procedure is more dependable and far less prone to catastrophic partition errors.

---

## 2. Standby Host Provisioning & Failover

### Provisioning the Standby Host
Run the standby playbook against your secondary server:

```bash
ansible-playbook -i standby-host.example.org, ansible/playbooks/standby.yml
```

This installs Docker, firewall rules, and pre-pulls all required container images so that activation takes minutes rather than hours.

### Failover Runbook (Primary Host Lost)
1. **Restore Latest Snapshot on Standby Host:**
   ```bash
   uv run community-stack restore --target /opt/community-stack --confirm
   ```
2. **Start Services on Standby Host:**
   ```bash
   cd /opt/community-stack
   uv run community-stack check
   uv run community-stack install
   ```
3. **Restore PostgreSQL Database:**
   ```bash
   docker exec -i community_postgres pg_restore -U synapse_user -d synapse --clean < /opt/community-stack/backups/postgres_synapse.dump
   docker restart community_synapse
   ```
4. **Update DNS Records:**
   - Update `A`/`AAAA` records for `community.example.org` and `matrix.example.org` to point to the Standby Host IP address.
   - Caddy on the standby host will automatically obtain fresh TLS certificates as soon as DNS propagation completes.

---

## 3. Local Wi-Fi Captive Portal & Offline Distribution

When cellular towers and fiber broadband are severed or shut down, communities need local information distribution.

### The Offline Archive Bundle
The CLI command `community-stack archive export` produces a self-contained `.zip` bundle containing:
- `index.html`: Fully styled HTML reader with zero external dependencies (no Google fonts, no CDNs, no JavaScript).
- `announcements.json`: Structured JSON for automated scrapers or local distribution apps.
- `manifest.json`: Cryptographic SHA256 hashes verifying that the archive has not been tampered with.

### Deployment on Local Wi-Fi (Raspberry Pi / OpenWrt Router)
1. Flash an OpenWrt router or Raspberry Pi with hostapd.
2. Configure DNS to resolve all queries to `192.168.1.1` (captive portal).
3. Unzip the exported offline archive into `/www/` (lighttpd or nginx).
4. Any smartphone connecting to the Wi-Fi network immediately sees the captive portal prompt and reads the latest official community bulletins without mobile data or internet access.

---

## 4. Meshtastic & LoRa Mesh Networking Evaluation

[Meshtastic](https://meshtastic.org/) is an open-source, off-grid mesh communications system operating on unlicensed ISM radio frequencies (433MHz, 868MHz, 915MHz).

### Capabilities & Best Uses
- **Off-Grid Range:** 2km to 30km depending on line-of-sight and repeaters.
- **Independence:** Operates completely off solar/battery power without internet or cell service.
- **Short Emergency Broadcasts:** Ideal for transmitting 150-250 character emergency summaries (e.g. curfew notices, water distribution points).

### Operational Constraints & Limitations
1. **Extremely Low Bandwidth:** Packet payload is capped at ~237 bytes. Cannot transmit photos, long PDFs, or audio.
2. **Duty Cycle Limits:** Radio regulations in the EU/UK (868MHz) enforce a strict 1% duty cycle limit (transmitting max 36 seconds per hour). Flooding the channel with non-emergency chat degrades the entire mesh.
3. **Network Collisions:** Dense mesh nodes with high broadcast volume experience high packet loss.
4. **Encryption vs Public Channel:** Meshtastic supports AES-256 encrypted channels for coordinating teams, as well as a default unencrypted public channel (`LongFast`) for public alerts.
