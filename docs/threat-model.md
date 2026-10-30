# Threat Model: Community Communications Stack

## 1. Overview and Trust Boundaries

The Community Communications Stack is designed for public movements and community groups operating in potentially contested or high-pressure environments. The system prioritizes resilience, independent operation, and clear privacy separation.

### Trust Zones
1. **Public Internet:** Untrusted users, potential attackers, automated scrapers, network observers.
2. **Reverse Proxy (Caddy):** Public ingress terminating TLS and routing HTTP traffic.
3. **Internal Application Network:** Services communicating with each other (Synapse, Element, Announcements API).
4. **Internal Database Network:** PostgreSQL database containing user accounts, room history, and keys. Isolated from public ingress.
5. **Bridge Zone:** WhatsApp bridge container connecting to external WhatsApp servers. High risk of session revocation; trusted only for designated mapped rooms.
6. **Host Environment:** Linux operating system, Docker daemon, encrypted backup storage, and local secrets file (`.env`).

---

## 2. Threat Analysis and Mitigations

### 2.1 Server Seizure, Hardware Theft, or Total Host Loss
- **Threat:** Physical server seizure by authorities, hardware theft, or physical destruction.
- **Impact:** Loss of service, potential exposure of unencrypted local data.
- **Mitigation:**
  - Full-disk encryption (LUKS) on the host operating system.
  - Automated off-host, client-side encrypted backups (Restic) stored on separate infrastructure.
  - No master unencrypted credentials stored in Git or unencrypted backup locations.
  - Documented disaster recovery runbook to rebuild on a fresh host within hours.
- **Residual Risk:** Live memory contains running keys while the server is active; server power-off is necessary to protect at-rest disk encryption.

### 2.2 Hosting Provider Outage or Account Suspension
- **Threat:** Cloud/VPS provider terminates account or experiences major datacenter outage.
- **Impact:** Abrupt service outage, potential loss of server instance and attached volumes.
- **Mitigation:**
  - Automated off-host backups to an independent storage provider (different legal jurisdiction / entity).
  - Infrastructure-as-code (Ansible + Docker Compose) allows deploying onto an alternate provider quickly.
  - Short DNS TTLs (e.g. 300 seconds) for domain records to enable rapid traffic repointing.
- **Residual Risk:** DNS registrar or top-level domain may also be targeted or delayed during failover.

### 2.3 Domain or DNS Disruption / Censorship
- **Threat:** Domain registrar suspension, DNS hijacking, or national/regional DNS poisoning.
- **Impact:** Users unable to reach domain endpoints.
- **Mitigation:**
  - Use of DNSSEC where supported.
  - Secondary DNS providers or backup domains documented in emergency procedures.
  - Static announcements exported to IPFS, RSS mirrors, and local Wi-Fi / offline HTML archives (Milestones 4 and 7).
- **Residual Risk:** Matrix federation relies heavily on domain names for server identity. Changing the base Matrix server name requires identity re-federation.

### 2.4 Platform Censorship or WhatsApp Account Bans
- **Threat:** WhatsApp detects bridge activity (mautrix-whatsapp uses unofficial WhatsApp Web protocol) and bans the phone number or linked account.
- **Impact:** WhatsApp bridge disconnects.
- **Mitigation:**
  - WhatsApp is explicitly treated as an uncredited, auxiliary channel, never a core dependency.
  - Synapse and canonical announcements remain 100% operational when WhatsApp drops.
  - Fast-fail bridge alerting: operators are notified if the bridge disconnects.
  - Canonical public announcements remain accessible via Web, RSS, and Matrix rooms.
- **Residual Risk:** WhatsApp may ban burner SIM cards rapidly.

### 2.5 Compromised Administrator Credentials
- **Threat:** Leaked SSH keys, Matrix admin credentials, or `.env` secrets file.
- **Impact:** Unauthorized access to Matrix administration, database, or host system.
- **Mitigation:**
  - SSH key-only authentication; root login disabled; optional 2FA / hardware security keys.
  - Matrix registration disabled by default (`enable_registration: false`); admin accounts created manually via CLI.
  - Secrets file restricted to `0600` permissions and excluded from version control and diagnostic bundles (`community-stack doctor` redacts all secrets).
  - Documented credential rotation procedures.

### 2.6 Malicious or Compromised Bridge
- **Threat:** The bridge container is exploited or acts maliciously.
- **Impact:** Decryption of bridged room messages and exfiltration to unauthorized endpoints.
- **Mitigation:**
  - The bridge is isolated from public ingress on a dedicated Docker bridge network (`bridge_net`) shared with Synapse; outbound internet access remains necessary.
  - The admin console selects one portal room for canonical announcement delivery; this does not restrict which rooms the bridge bot can create or bridge.
  - Sensitive internal coordination rooms must NEVER be mapped to WhatsApp.
  - Clear user disclosure in room topics indicating the room is bridged to WhatsApp.
  - Emergency disable command (`community-stack bridge disable`) immediately severs container networking and kills bridge processes.

### 2.7 Metadata Collection and Traffic Analysis
- **Threat:** Network observers monitoring IP addresses, TLS SNI, and traffic volumes.
- **Impact:** Identification of community members communicating with the server.
- **Mitigation:**
  - TLS 1.3 enforced by Caddy.
  - Web client (Element Web) and Matrix API served over standard HTTPS port (443), blending with ordinary web traffic.
  - Clear documentation advising high-risk users to use Tor or VPNs.
- **Residual Risk:** Self-hosting does not provide metadata anonymity or prevent ISP-level observation of connections to the server IP.

### 2.8 Malicious Links, Attachments, Spam, and Denial-of-Service
- **Threat:** Bad actors flood public registration, upload malware attachments, or exhaust server bandwidth.
- **Impact:** Storage exhaustion, degraded performance, phishing.
- **Mitigation:**
  - Public registration disabled by default.
  - Media upload size limits in Synapse (`max_upload_size: 50M`).
  - Caddy rate-limiting and connection caps.
  - Anti-spam and message rate limits in Synapse configuration.

### 2.9 Accidental Exposure of Private Rooms Through Bridging
- **Threat:** Operator inadvertently maps an internal or sensitive coordination room to an open WhatsApp group.
- **Impact:** Confidential discussions exposed to WhatsApp ecosystem and participants.
- **Mitigation:**
  - Bridging requires explicit dual-sided mapping with human confirmation in the CLI.
  - Automated safety check prevents bridging rooms with high security classifications.
  - CLI `check` and `plan` show all active bridge mappings prominently before deployment.

### 2.10 Backup Theft or Incomplete Recovery
- **Threat:** Backup repository stolen from off-site storage or corrupted during transit.
- **Impact:** Exposure of database dump or failure to restore when disaster strikes.
- **Mitigation:**
  - Mandatory client-side encryption (Restic repository password) before any data leaves the host.
  - Regular automated integrity check via `community-stack backup verify`.
  - Non-destructive test restore drill (`community-stack restore --target /tmp/drill-restore`).

---

## 3. Explicit Operational Assumptions & Non-Guarantees

1. **Not an Anonymous Whistleblowing Platform:** This stack is built for open community coordination. It does not replace SecureDrop or Tor-based anonymity tools.
2. **Upstream Software Dependencies:** We rely on upstream projects (Synapse, Element, Caddy, Restic, mautrix-whatsapp). Security patches released by upstream must be applied promptly using `community-stack upgrade`.
3. **Physical Host Security:** Host security depends on proper hardware hygiene, strong SSH keys, and encrypted storage.
