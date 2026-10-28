# Changelog

All notable changes to the Community Communications Stack will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Milestone 0: Reconnaissance and Architecture Decisions:**
  - Initial threat model (`docs/threat-model.md`).
  - Architecture documentation (`docs/architecture.md`).
  - Architecture Decision Records (`docs/decisions/0001` through `0006`).
- **Milestone 1: Project Foundation:**
  - Python packaging with `pyproject.toml` and `uv` support.
  - JSON Schema specification (`config/schema.json`) for `community-stack.yaml`.
  - Operator CLI (`community-stack`) with `init`, `check`, and `plan` commands.
  - Secret management with strict `.env` loading, permission checks, and automatic redaction filter.
  - Test suite with pytest and code formatting/linting with ruff.
  - GitHub Actions CI workflow.
- **Milestone 2: Minimal Deployable Infrastructure:**
  - Modular Docker Compose services for Caddy proxy (`services/proxy`), Landing page (`services/landing`), Matrix Synapse with PostgreSQL (`services/matrix`), and Element Web client (`services/element`).
  - Network segmentation isolating database traffic (`matrix_internal`) from public ingress (`proxy_net`).
  - Ansible host preparation roles and master deployment playbook (`ansible/`).
  - Implemented `community-stack install`, `community-stack status`, and `community-stack logs` CLI commands.
  - End-to-end deployment documentation and firewall/DNS guide (`docs/deployment.md`).
  - Integration test suite for container service configurations, network policies, and healthchecks.
- **Milestone 3: Backups, Restoration, and Safe Upgrades:**
  - Implemented encrypted backup engine (`BackupEngine`) backing up PostgreSQL dumps, persistent data, config, and credentials.
  - Snapshot verification command (`community-stack backup verify`) validating manifest and archive integrity.
  - Isolated restore command (`community-stack restore --target <dir> --confirm`) with safety guards preventing unconfirmed overwrite of active environments.
  - Safe service upgrade command (`community-stack upgrade <service>`) with automatic pre-upgrade snapshots.
  - Comprehensive backup, restore, and disaster recovery runbook (`docs/backup-restore.md`).
  - End-to-end disaster recovery integration test suite (`tests/recovery/test_backup_restore.py`).
- **Milestone 4: Canonical Public Announcements:**
  - Canonical announcement domain model (`Announcement`) with strict field and length validations.
  - Durable SQLite storage engine (`AnnouncementStorage`) tracking announcements and per-adapter delivery states.
  - Web & RSS adapter (`WebFeedAdapter`) generating public HTML archive, Atom 1.0, RSS 2.0, and JSON Feed (v1.1).
  - Matrix announcement room adapter (`MatrixRoomAdapter`) posting formatted notices via Client-Server API with idempotency keys.
  - Announcement publishing orchestrator (`AnnouncementPublisher`) with failure isolation, bounded retries, and update/withdrawal propagation.
  - CLI commands: `community-stack announce create`, `list`, `show`, `update`, `withdraw`, and `retry`.
  - Comprehensive unit and integration test suite verifying multi-channel delivery and adapter failure isolation.
- **Milestone 5: Optional WhatsApp Compatibility:**
  - Integrated `mautrix-whatsapp` bridge service container (`dock.mau.dev/mautrix/whatsapp:v0.11.2`) with configuration templates.
  - Implemented `WhatsAppAnnouncementAdapter` (Mode A) for unidirectional forwarder of canonical announcements.
  - Strict room allowlisting logic in `WhatsAppBridgeManager` preventing accidental exposure of non-allowlisted private rooms.
  - Emergency disconnect killswitch command (`community-stack bridge disable --confirm`) immediately stopping bridge traffic while preserving core Matrix.
  - Comprehensive operational and privacy guide in `docs/whatsapp-integration.md`.
  - Unit and integration tests validating that WhatsApp disconnection or failure does not disrupt the core stack.
- **Milestone 6: Additional Accessible Channels:**
  - Implemented `ActivityPubAdapter` federating announcements to Mastodon/GoToSocial via ActivityStreams 2.0 `Create(Note)` and `Delete(Note)` events.
  - Implemented `EmailNewsletterAdapter` with multipart text/HTML formatting, RFC 8058 one-click unsubscribe headers, and topic filtering.
  - Built `SubscriberManager` with SQLite persistence, double opt-in validation, immediate opt-out compliance, and destination privacy masking.
  - Built ultra-lightweight, zero-JS text-mode public web portal (`services/landing/html/lite.html`) for 2G/3G connections.
  - Added CLI subscriber management: `community-stack subscribers list`, `add`, and `unsubscribe`.
  - Comprehensive evaluation of Email, SMS gateways (Kannel, Jasmin, GSM modems), and IVR voice information lines in `docs/accessible-channels.md`.
  - Integration test suite validating multi-channel broadcast and strict opt-out adherence.
- **Milestone 7: Multi-Host Resilience & Offline Continuity:**
  - Implemented `OfflineArchiveExporter` producing self-contained, offline-readable HTML & JSON archives with SHA256 integrity verification.
  - Built `community-stack archive export` and `community-stack archive verify` CLI commands for USB and local Wi-Fi distribution.
  - Added Ansible warm standby provisioning playbook (`ansible/playbooks/standby.yml`) pre-pulling container images and configuring independent hosts.
  - Comprehensive guide in `docs/multi-host-resilience.md` covering independent failure domains, captive portal Wi-Fi setup (OpenWrt/Raspberry Pi), and Meshtastic/LoRa off-grid mesh evaluation.
  - Unit and integration tests verifying offline archive generation and cryptographic tampering detection.
- **Milestone 8: Production Hardening, Operator Handbook & Governance:**
  - Enhanced `community-stack doctor` with live storage capacity checks and container network port isolation audits.
  - Authoritative Operator Handbook in `docs/operations.md` covering daily maintenance, upgrading pinned releases, incident response runbooks, and decommissioning.
  - Maintainer handover and governance guide in `docs/governance.md` covering the two-person rule, access escrow, credential rotation, and upstream license compliance.
  - Unit and regression test suite verifying diagnostic audits and security redactions.
- **Roadmap Phase 0: Durable delivery jobs:**
  - Every channel delivery is recorded as a job in the announcements database before it runs, so a crash mid-dispatch no longer loses deliveries.
  - Temporary, safely repeatable failures are retried automatically with exponential backoff (30 s to 1 h, 5 attempts).
  - Background delivery loop inside the admin app, plus `community-stack announce worker` for running it standalone.
  - Withdrawals and disabled channels cancel pending retries; a newer update supersedes a pending publish.
  - Admin dashboard and announcement pages show deliveries waiting for an automatic retry.
- **Roadmap Phase 0: Connector capability model:**
  - Channel adapters declare a readable label, a text limit, media support, and whether edits and withdrawals change the original post or send a follow-up message.
  - Each announcement page lists the channels it goes to, flags text that is too long for a channel, and says what an edit or withdrawal will do on each one.
- **Roadmap Phase 0: Accounts, roles and activity log:**
  - Several admin accounts with Owner, Admin, Publisher and Reviewer roles; every admin page and action checks the role on the server.
  - The existing single administrator becomes an Owner automatically; at least one active owner always remains.
  - Accounts page to add accounts, change roles, disable or re-enable access, and reset passwords; My account page to change your own password.
  - Role changes, disabling and password resets apply to existing sessions immediately.
  - Activity log of sign-ins, failed sign-ins, account changes, publishing actions and settings changes, kept for about 13 months.
- **Roadmap Phase 0: Encrypted backups with restic:**
  - New `operations.backups.encryption` setting (`restic` or `none`); `community-stack init` and the example configuration default to `restic`.
  - With restic, snapshots are encrypted on the host into a local, S3 or SFTP repository, verified with `restic check` plus a content listing, restored without leaving a scratch copy, and pruned by retention days. No plaintext archive is kept.
  - Restic receives only backup settings, never the other secrets in `.env`.
  - `community-stack check`, `plan` and `backup` say when backups are unencrypted or restic is missing; configurations without the setting keep the previous owner-only archives.
  - The Ansible host role installs restic, and CI runs the encrypted backup tests against a real restic binary.
- **Roadmap Phase 0: Control agent skeleton:**
  - Design record [ADR 0007](docs/decisions/0007-control-agent.md): a privileged host service performs console actions from a fixed catalog over a local Unix socket.
  - `community-stack agent serve` with a hardened systemd unit (`services/agent/community-stack-agent.service`); the socket is limited to the console's group and, on Linux, to allowed peer UIDs.
  - First operations: service status, create backup, verify backup, and a restore drill that restores into a scratch folder, checks it, and deletes it.
  - The agent checks each request's parameters, the account's role (read-only from the admin database), and for the restore drill the person's password, with five attempts per 15 minutes; it runs one change at a time and writes its own audit log.
  - Admin console System page for owners and admins, which explains when the agent is not installed.
  - `deploy-vps.sh` restarts the agent after deploying, when it is installed.
