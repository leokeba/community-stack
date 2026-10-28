# ADR 0007: Privileged Control Agent for the Admin Console

## Status
Accepted (2026-10-28). Phase 0 implements the skeleton and the first
operations; later operations are added to the same catalog.

## Context
The roadmap's admin console must enable, configure, back up, upgrade and
restore services without the CLI. Those actions need root-level access: the
Docker daemon, `community-stack.yaml`, `.env`, `data/` (including root-only
Caddy state) and `backups/`.

The console runs inside the public-facing `community_announcements` container
as an unprivileged user (UID 1000) with no Docker socket. Giving it the Docker
socket or write access to `.env` would make any web vulnerability equivalent to
root on the host.

## Decision
A separate **control agent** performs every privileged action. The console asks
it for named operations; it never runs commands itself.

1. **Placement.** The agent is a systemd service on the host
   (`community-stack agent serve`), running as root with systemd sandboxing,
   because backups already require root and the configuration lives on the host.
   It is updated only by the CLI or `deploy-vps.sh`, never from the console.
2. **Channel.** The agent listens on a Unix socket at
   `data/agent/run/agent.sock`, mounted into the console container. There is no
   TCP port. The socket is mode `0660` with the console's group, and on Linux
   the agent also checks the connecting process's UID (`SO_PEERCRED`) against an
   allowlist.
3. **Protocol.** One JSON request per connection, at most 64 KiB, with a
   timeout: `{"op", "params", "actor", "password"?}`. The response is
   `{"ok": true, "result"}` or `{"ok": false, "error"}`.
4. **Operation catalog.** Only operations registered in the agent exist. Each
   one declares its parameters (type, allowed values, length) and a risk level:
   - `read`: status, verification and listings;
   - `change`: reversible changes such as creating a backup;
   - `dangerous`: actions that need the requester's password, re-checked by
     the agent against the admin account store. Two-person approval is added
     for production restore when that operation exists.

   Requests never carry shell commands, file paths, image names or repository
   URLs; the agent derives these from its own configuration.
5. **One engine.** Operations call the same Python functions as the CLI
   (`BackupEngine`, the Docker helpers, and later `install` and `upgrade`), so
   the console and the CLI cannot drift apart.
6. **Change pipeline.** Configuration-changing operations, added after Phase 0,
   follow validate → plan (diff shown in the console) → backup → apply → health
   check → automatic rollback.
7. **Upgrades.** The console may request upgrades only to the image versions
   pinned in the release that is installed. It cannot name an arbitrary image
   or version.
8. **Audit.** The agent writes its own append-only JSON-lines log in
   `data/agent/audit.log`, outside the console's mount, recording the time, the
   actor as claimed by the console, the operation, the redacted parameters and
   the outcome. The console's own activity log records the same actions from its
   side.
9. **Scope.** One agent per server. A central multi-server dashboard is out of
   scope; nothing in the protocol prevents adding one later.

## Consequences
- A bug in the console can at most request catalog operations with validated
  parameters. It cannot run commands, read `.env`, or reach the Docker socket.
- The password re-check protects dangerous operations against stolen sessions,
  CSRF and console logic bugs. It does **not** protect against a fully
  compromised console process. The console can write the admin database the
  agent reads, and it sees passwords as people type them. Against that threat
  the protection is the limited catalog, its validation, the agent's rate limits
  and the agent's separate audit log.
- The agent is a new privileged component and must be kept small and
  reviewed: no new operation without parameter validation and tests.
- Operators must install and enable one more systemd unit. Without it, the
  console's System page explains that the agent is not running, and everything
  else keeps working.
