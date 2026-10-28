# ADR 0005: Backup and Recovery Strategy

## Status
Accepted

## Context
Resilience over feature completeness is the primary design principle. If a community server is lost, seized, or destroyed, the community's communications identity and public announcements must be recoverable on a clean new server.

## Decision
1. **Tooling:**
   - **Restic** (or Borg) for client-side encrypted, deduplicated, snapshot-based backups.
   - Built-in snapshot automation orchestrating PostgreSQL dumps (`pg_dump`) alongside application state directories.
2. **Backup targets:**
   - Support both local storage (for testing / initial setup) and remote S3-compatible object storage (e.g., Wasabi, Backblaze B2, MinIO, AWS S3, or SFTP).
   - Off-host backup is strongly recommended for production profiles.
3. **Integrity and verification:**
   - Automated `community-stack backup verify` command running restic check and snapshot integrity tests.
   - Dedicated restore workflow (`community-stack restore --target <dir>`) that restores to an isolated target directory without risking live production data.
   - Strict explicit confirmation required before any restore operation.

## Consequences
- Data is encrypted before leaving the host.
- Snapshots allow point-in-time recovery.
- Restore drills can be run safely on test systems or separate directories.
