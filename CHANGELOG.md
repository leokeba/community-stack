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
