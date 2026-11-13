# LibreBooking Space Reservations

LibreBooking is an optional application for reservable spaces, shared
availability calendars, and approval workflows. It is disabled by default and
uses a separate MariaDB database. The application and database publish no host
ports; only the application is reachable from Caddy.

The current stable pin is LibreBooking `7.2.0` with official Docker release
`3.5.2` (`7.2.0-3.5.2`). Check the
[LibreBooking releases](https://github.com/LibreBooking/librebooking/releases)
and [Docker releases](https://github.com/LibreBooking/docker/releases) before
changing the pin. The local derived image removes shell xtrace from the
upstream entrypoint, so environment-based passwords are not echoed into
container logs.

## Enable and complete first setup

1. Add a DNS record for `booking.<domain.base>` (or the hostname you will use)
   pointing to the Community Stack server.
2. In **Admin → Services and updates**, set the LibreBooking administrator
   email to the address that should receive LibreBooking administrator rights,
   then turn on **LibreBooking space reservations**. Review and apply the
   change. The agent creates stable database and installer credentials,
   prepares persistent storage, deploys the service, and installs the HTTPS
   route. The public app URL ends in `/Web/`.
3. Use the **Show first-setup instructions** form on that page. It requires
   password confirmation and returns the installation password and the
   application-scoped database account. The MariaDB root password is never
   shown to the web console.
4. Temporarily enable **LibreBooking self-registration** in the services
   settings, then open the installer URL from the instructions. Use the
   displayed database host/name/user/password and leave the installer's
   **Create database** and **Create database user** options unchecked: the
   dedicated database and account already exist.
5. Register the initial account using the configured administrator email.
   LibreBooking grants that address administrator privileges. Turn
   self-registration off immediately after registering it; while enabled,
   anyone who can reach the site can create an account.
6. In LibreBooking, add spaces/resources, configure their schedules, permissions
   and approval requirements, then share the schedule with the community.

The default language is French (`fr_fr`); the time zone follows
`project.timezone`. Configure email delivery in LibreBooking itself if you want
reservation notifications. Community Stack does not synchronize LibreBooking
users with Matrix.

## Admin settings and lifecycle

The web console controls enablement, hostname, administrator email,
self-registration, and the pinned image tag. Enabling or changing the image
version takes a safety backup and rolls the configuration back if the service
does not become healthy. Self-registration is off by default. For command-line
maintenance, the service is also available as `librebooking`, `booking`, and
`reservations`:

```sh
uv run community-stack status
uv run community-stack logs librebooking
uv run community-stack service restart librebooking
uv run community-stack upgrade librebooking
```

Changing the image tag in the admin console triggers a local rebuild from the
matching pinned upstream image. Keep the pre-change backup until the updated
application and database migration have been checked.

## Backup, restore, and recovery

When enabled, backup stops the LibreBooking application briefly, writes
`backups/mariadb_librebooking.sql` using a MariaDB logical dump, archives its
configuration, uploads, and logs, then restarts the application. The live
MariaDB data directory is excluded from the archive. A failed dump or restart
fails the backup; `community-stack backup verify` checks the dump is present.

To restore, restore the stack configuration, `.env`, archived LibreBooking
files, and `mariadb_librebooking.sql` together. Import the SQL dump into the
initialized `librebooking` database before starting the application, as
described in the [backup and recovery runbook](backup-restore.md). Keep `.env`
owner-only and preserve the original database and installer passwords;
regenerating them while data exists is refused.

LibreBooking is licensed under AGPL-3.0. See
[Governance & Handover](governance.md) for software attribution.
