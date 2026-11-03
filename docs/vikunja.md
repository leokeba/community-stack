# Vikunja Collaborative Tasks

Vikunja is an optional, separately deployed project and task-management
application. Community Stack manages its container lifecycle, private database,
HTTPS route, configuration secrets, health checks, and disaster-recovery
artifacts. Project design, accounts, teams, tasks, permissions, and attachments
remain managed in Vikunja.

Vikunja is disabled by default. It runs in its own Compose project and uses a
dedicated PostgreSQL 16 database on an internal-only network. The application
joins `proxy_net` for Caddy ingress; neither the application nor the database
publishes a host port. Caddy's Vikunja site is generated only when the service
is enabled, so a normal Community Stack install does not request another TLS
certificate.

## Enable and deploy

Add or edit this section in `community-stack.yaml`:

```yaml
services:
  vikunja:
    enabled: true
    version: "2.7.1"
    hostname: "tasks.example.org" # optional; defaults to tasks.<domain.base>
    files_path: "data/vikunja/files"
    database:
      type: "postgres"
      data_path: "data/vikunja/postgres"
    allow_registration: false
    enable_link_sharing: false
    memory_limit: "512M"
    cpu_limit: 1
```

The version is explicitly pinned. Change it in configuration before running
`community-stack upgrade vikunja`; the upgrade takes a required pre-upgrade
backup and updates only Vikunja's Compose project. The storage paths must
remain under `data/` so Community Stack can include the files while excluding
the live PostgreSQL data directory from raw file copying.

Create DNS for the chosen hostname pointing to the server, then run:

```bash
uv run community-stack plan
uv run community-stack install
uv run community-stack check
uv run community-stack status
uv run community-stack doctor
```

The CLI provisions stable database and application secrets in `.env` on the
first install, before running its deployment preflight. Run `check` after this
initial install; checking first will correctly report missing Vikunja secrets.
The CLI preserves existing values and refuses to generate missing secrets if
Vikunja data already exists. Keep `.env` owner-only (`0600`) and include it in
the protected, encrypted backup repository.

System administrators can create accounts in **People → Vikunja users** in the
Community Stack admin console. The page is available only when Vikunja is
enabled and the current control agent supports the operation. Community Stack
generates a temporary password, supplies it to Vikunja's supported CLI
interactively (not in process arguments), and displays it once. Share the
password privately; Community Stack does not store it. The account is a normal
Vikunja user, not a Vikunja administrator. Project/team collaboration and
project-level permissions are configured in Vikunja itself.

The supported container CLI remains available as an operator fallback. Run
this interactively so a password is not placed in shell history or process
arguments:

```bash
docker exec -it community_vikunja /app/vikunja/vikunja user create \
  --username organizer --email organizer@example.org
```

The operator can manage local Vikunja users with its `user` CLI commands.
Project/team collaboration and project-level permissions are configured in
Vikunja itself.

## Enable from the admin web console

An administrator with system permissions can open **Server → Services and
updates**, turn on **Vikunja task management**, review the proposed change, and
confirm with their console password. The control agent takes a safety backup,
validates the updated configuration, generates the stable Vikunja secrets,
creates persistent storage, deploys the Vikunja Compose project, and reloads
Caddy when the route changes. If deployment fails its health check, the
previous configuration and service state are restored.

Before enabling it, point `tasks.<domain.base>` at the Community Stack server.
After deployment, the Services and updates page shows an **Open Vikunja** link.
No shell access or direct configuration-file edits are needed for this default
setup. The web console and its privileged control agent must already be
installed and running.

After Vikunja is enabled, administrators with system permissions can create
ordinary user accounts from **People → Vikunja users**. The generated
temporary password is shown only in the creation response; if it is lost,
create a replacement through Vikunja's own supported account-management
workflow rather than expecting Community Stack to recover it.

## Lifecycle commands

```bash
uv run community-stack service enable vikunja
uv run community-stack install
uv run community-stack service start vikunja
uv run community-stack service stop vikunja
uv run community-stack service restart vikunja
uv run community-stack status
uv run community-stack logs vikunja
uv run community-stack upgrade vikunja
uv run community-stack doctor
uv run community-stack service disable vikunja
```

`service disable` updates configuration, stops and removes only Vikunja's
containers, removes its generated Caddy route, and preserves database and file
directories. `install` deploys it again if re-enabled. The application URL is
shown by `plan` and `status`. The static landing page does not have a
configuration-driven service directory, so no dead link is shown when the
optional service is disabled.

## Authentication and sharing

Vikunja's native account authentication is used. Self-registration and public
link sharing are off by default; registration can be explicitly enabled using
`allow_registration`. Community Stack does not add an identity provider or
modify Vikunja's authorization model.

Vikunja supports OpenID Connect (OIDC), but Community Stack does not currently
provide a central identity provider or configure OIDC. A future identity
provider can be integrated using Vikunja's native OIDC provider configuration
after callback URLs, account linking, and recovery have been tested. Matrix and
Vikunja accounts remain separate in this milestone.

Keep `enable_link_sharing: false` unless a community explicitly chooses
anonymous project access. A link share is not inherently read-only: Vikunja
allows a creator to choose Read, Read & Write, or Admin permission and an
optional password. Anyone holding the link receives the selected permission;
delete the share to revoke access. Review the project contents and permission
before distributing a share URL.

## Notifications and integrations

Vikunja records in-app/database notifications and supports email notifications
when its mailer is configured. Its project webhooks expose task events,
including task creation, updates/completion state, and assignee changes; user
webhooks cover personal events such as reminders and overdue tasks. Webhook
payloads can contain task and user data and can be signed with HMAC.

No Matrix bridge is included. Vikunja attempts each webhook delivery once and
does not retry failures. Its default outgoing-request protections reject
private, loopback, and link-local destinations, including a private Synapse
container address. A separate public receiver or carefully reviewed relay
would be needed for Matrix delivery, with explicit controls against SSRF,
duplicate messages, and notification noise. No polling or unofficial connector
is installed.

## Backup, restore, and upgrade

When enabled, `community-stack backup` briefly stops only the Vikunja
application container to keep database and uploaded-file snapshots aligned,
creates `backups/postgres_vikunja.dump` with `pg_dump`, archives Vikunja's
attachment files, and restarts the application. The PostgreSQL data directory
is excluded from the file archive. If Vikunja's database dump or restart
fails, the backup reports an error instead of presenting a partial snapshot as
successful. `community-stack backup verify` checks for the Vikunja dump when
the service is enabled.

Restore first into an isolated directory:

```bash
uv run community-stack restore --target /tmp/community-restore --confirm
```

For a clean Vikunja deployment, restore the archived `data/vikunja/files`
directory and `backups/postgres_vikunja.dump` to the new host, enable Vikunja
with the matching hostname/version, and deploy its Compose project. Copy the
files into the configured persistent directory, then stop only the application
container (leave PostgreSQL running) before restoring the custom-format dump
into the new dedicated database:

```bash
cp -a /tmp/community-restore/data/vikunja/files/. data/vikunja/files/
docker compose --env-file .env -f services/vikunja/docker-compose.yml stop vikunja
docker compose --env-file .env -f services/vikunja/docker-compose.yml exec -T vikunja-db \
  pg_restore -U vikunja -d vikunja --clean --if-exists --no-owner \
  < /path/to/community-restore/backups/postgres_vikunja.dump
docker compose --env-file .env -f services/vikunja/docker-compose.yml start vikunja
```

Restore only into a clean deployment or after separately preserving its
existing database and files. Database migrations may make a newer database
incompatible with an older Vikunja image; restore the matching pre-upgrade
snapshot rather than downgrading an upgraded database in place.

## Boundaries and references

Community Stack owns deployment, secret persistence, private networks, HTTPS,
backup/restore artifacts, and service diagnostics. Vikunja owns users,
projects, teams, tasks, comments, workflows, sharing grants, and application
notifications. The first implementation deliberately does not add a generic
plugin framework or a custom task UI.

Upstream references:

- [Official Docker walkthrough](https://vikunja.io/docs/docker-walkthrough/)
- [Configuration options](https://vikunja.io/docs/config-options/)
- [OpenID Connect](https://vikunja.io/docs/openid/)
- [Webhook API and delivery behavior](https://vikunja.io/docs/webhooks/)
- [Webhook event types](https://vikunja.io/help/webhooks/)
- [Sharing and link-share permissions](https://vikunja.io/help/sharing-and-teams/)
- [Vikunja CLI](https://vikunja.io/docs/cli/)
