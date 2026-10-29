# Backup, Restore, and Disaster Recovery Runbook

## 1. Overview and Core Principles

The Community Communications Stack is designed to survive total server loss, hosting provider termination, or physical hardware seizure.

### Non-Negotiable Operational Principles
1. **Resilience over feature completeness:** A smaller system that can be recovered reliably is preferable to a large system that cannot.
2. **Encrypted at rest and in transit:** Backups must be client-side encrypted before leaving the host.
3. **Independent failure domains:** Production data and backups must not live exclusively on the same server or provider.
4. **Non-destructive restore drills:** Testing a restore must never overwrite or endanger production data.
5. **Verified recoverability:** A backup is only valid if its contents have been verified and restored successfully into an isolated target environment.

---

## 2. Backup Architecture

### What is Backed Up
Every snapshot created by `community-stack backup` captures:
1. **PostgreSQL Database (`postgres_synapse.dump`):** Matrix Synapse state, accounts, joined rooms, key material, and messages.
2. **Persistent Storage (`data/`):**
   - Synapse configuration, media store, and signing keys (`data/matrix`).
   - Caddy certificates and ACME state (`data/caddy`).
   - Landing page assets and custom announcements (`data/landing`).
   - Canonical announcements, delivery history, admin accounts, sessions, and publishing settings (`data/announcements/announcements.db`).
   - Element Web branding and client configuration (`data/element`).
3. **Deployment Configuration (`community-stack.yaml`):** Exact profile, domains, and service parameters.
4. **Secrets (`.env`):** Database passwords, session keys, and registration secrets (restored with restricted `0600` permissions).
5. **Snapshot Manifest (`manifest.json`):** Timestamp, project identifier, and integrity checksum metadata.

The announcement database and generated public feeds are included because the
backup archives the entire `data/` directory. The announcement SQLite file is
captured with SQLite's online backup API so a concurrent organizer write does
not leave a partial database in the snapshot. The SQLite database contains
password hashes and authentication/session records as well as public
announcement content, and `.env` holds every service secret, so snapshots are
sensitive data.

### Encryption

With `operations.backups.encryption: "restic"` (the default for new
configurations), each snapshot goes into a [restic](https://restic.net/)
repository and is encrypted on the host before it is written, locally or to the
S3 or SFTP target. Nothing is left in plaintext: the staging directory is
deleted once restic has the snapshot. Retention uses
`restic forget --keep-within <retention_days>d --prune`.

With `encryption: "none"`, snapshots are gzip archives with owner-only (`0600`)
permissions but **no encryption**, and off-site targets are ignored. Configs
written before this setting existed behave this way, and `community-stack
check` warns about it.

`RESTIC_PASSWORD` in `.env` is the only key to the repository. Keep a copy
offline (for example in a password manager held by two maintainers): without
it, encrypted backups cannot be restored.

### Automatic and off-site backups

With the control agent running, backups happen on their own: the agent checks
every five minutes and makes a backup when the latest one is older than the
`schedule` (hourly, daily or weekly; changeable on the console's Services page),
then verifies it. A failed automatic backup is retried after an hour, shown on
the Health page, and recorded in the agent's audit log. Backups made by hand or
by a deployment count towards the schedule.

The console's **Backups** page sends encrypted backups to any S3-compatible
bucket (for example Backblaze B2, Scaleway, OVHcloud or AWS). The agent writes
`S3_BUCKET_URL`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` (and `S3_REGION` when given)
to `.env`, sets `target: "s3"` and `encryption: "restic"`, creates the restic
repository in the bucket, and makes and verifies a first backup there. If any
step fails, `.env` and the configuration are restored and backups stay where
they were. Use a private bucket for these backups only; the provider only ever
receives encrypted data. The same page can switch back to the server's own disk
and downloads the offline archive of published announcements.

### Turning on encryption for an existing deployment

1. Install restic on the host (`sudo apt install restic`; the Ansible host role
   installs it for new hosts).
2. Check that `RESTIC_PASSWORD` in `.env` is set and stored safely offline.
3. Add `encryption: "restic"` under `operations.backups` in
   `community-stack.yaml`.
4. Run `community-stack backup`, then `community-stack backup verify`, then a
   restore drill (section 4).
5. Once the encrypted snapshot is verified, delete the old plaintext
   `backups/snap_*.tar.gz` archives.

---

## 3. Regular Operations

### Creating a Backup Snapshot
```bash
uv run community-stack backup
```

### Verifying Backup Integrity
```bash
# Verify the latest snapshot
uv run community-stack backup verify

# Verify a specific snapshot by ID
uv run community-stack backup verify --snapshot snap_20261028_120000Z
```

### Configuring Off-Host S3 Backups
In `community-stack.yaml`:
```yaml
operations:
  backups:
    enabled: true
    schedule: "daily"
    target: "s3"
    encryption: "restic"
    retention_days: 30
```

In `.env`:
```bash
RESTIC_PASSWORD=my_strong_passphrase
S3_ACCESS_KEY=my_s3_key
S3_SECRET_KEY=my_s3_secret
S3_BUCKET_URL=s3:s3.wasabisys.com/community-backups
```

---

## 4. Disaster Recovery & Restore Drill

### Testing Restore into an Isolated Directory
To verify recoverability without modifying the active production host:

```bash
# Attempting restore without --confirm is blocked by design
uv run community-stack restore --target /tmp/test-restore --confirm
```

Verify the restored directory:
```bash
ls -la /tmp/test-restore
ls -la /tmp/test-restore/backups
ls -la /tmp/test-restore/data
```

### Complete Host Loss Recovery Runbook
If the primary server is lost or destroyed:
1. **Provision a new Linux VPS** (Debian 12 / Ubuntu 22.04 LTS).
2. **Install `uv` and clone the repository:**
   ```bash
   git clone https://github.com/your-org/community-stack.git /opt/community-stack
   cd /opt/community-stack
   ```
3. **Obtain the latest backup snapshot archive** (from off-host S3 or remote storage).
4. **Restore into a staging directory:**
   ```bash
   uv run community-stack restore --target /opt/community-stack-recovered --confirm
   ```
5. **Copy recovered files into `/opt/community-stack`:**
   ```bash
   cp -r /opt/community-stack-recovered/data /opt/community-stack/
   cp /opt/community-stack-recovered/community-stack.yaml /opt/community-stack/
   cp /opt/community-stack-recovered/.env /opt/community-stack/
   chmod 600 /opt/community-stack/.env
   ```
6. **Repoint DNS records** to the new server's IP address.
7. **Deploy and start services:**
   ```bash
   uv run community-stack check
   uv run community-stack install
   ```
8. **Restore the PostgreSQL database dump:**
   ```bash
   docker exec -i community_postgres pg_restore -U synapse_user -d synapse --clean < /opt/community-stack-recovered/backups/postgres_synapse.dump
   ```
9. **Restart Matrix Synapse:**
   ```bash
   docker restart community_synapse
   ```
10. **Verify service status:**
    ```bash
    uv run community-stack status
    uv run community-stack doctor
    ```

---

## 5. Safe Service Upgrades

To upgrade an individual service without downtime or data risk:

```bash
uv run community-stack upgrade matrix
```

What the upgrade command does:
1. Automatically creates a pre-upgrade snapshot before making any container changes.
2. Pulls the latest reviewed pinned image.
3. Recreates the service container.
4. Performs healthcheck verification.
