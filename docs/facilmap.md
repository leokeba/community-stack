# FacilMap Collaborative Maps

FacilMap is an optional, independent collaborative mapping service. Community
Stack manages its containers, MariaDB database, HTTPS route, persistent cache,
diagnostics, and backups. Map design, links, feature data, and sharing remain
inside FacilMap.

## Enable from the web console

1. Point the desired hostname (default `maps.<domain.base>`) at the server.
2. In **Admin → Services and updates**, enable **FacilMap collaborative maps**.
   Change the public hostname if needed.
3. Review the warnings and proposed change. The console makes a safety backup,
   provisions database credentials and storage, deploys the service, and checks
   container health. No VPS shell or manual YAML edit is needed to enable it.
4. Open the link shown on the Services page.

The CLI also supports `community-stack service enable|disable|start|stop|restart
facilmap`, `community-stack status`, `community-stack logs facilmap`, and
`community-stack upgrade facilmap`. CLI `enable` updates configuration; run
`community-stack install` to provision and deploy it.

## Deployment and configuration

The service is disabled by default. Configuration is under `services.facilmap`:

```yaml
services:
  facilmap:
    enabled: false
    version: "sha256:bcef0c14e23f8670ea21d18a15d4331348bf253550a5acf39b19d067546f629f"
    hostname: "maps.example.org" # optional; defaults to maps.<domain.base>
    cache_path: "data/facilmap/cache"
    database:
      version: "11.4.12"
      data_path: "data/facilmap/mariadb"
      memory_limit: "512M"
      cpu_limit: 1
    memory_limit: "512M"
    cpu_limit: 1
```

The app and MariaDB run in a separate Compose project. Caddy reaches the app
over `proxy_net`; the app and MariaDB share a private `facilmap_internal`
network. Neither container publishes a host port. MariaDB uses `utf8mb4`,
as required by FacilMap's Docker instructions. The official image's health
check exercises its Socket.IO endpoint; the app waits for the database health
check before starting. Caddy's `reverse_proxy` supports WebSocket upgrades and
forwards the proxy headers FacilMap is configured to trust.

The `version` is an immutable OCI digest, not a mutable `latest` tag. The
default digest is the official image index published from upstream `main` on
2026-10-30 (Node 22; amd64 and arm64). Upstream does not identify this build as
a stable release (`package.json` reports `0.0.0`); this was selected instead
of the last stable `4.1` image, whose 2024 Dockerfile uses end-of-life Node 21.
The exact digest is pinned so later upstream pushes cannot silently change the
running image. Review upstream releases before changing it. The MariaDB image
uses the explicit `11.4.12` tag.

On first provisioning, generated database passwords are stored in the
owner-only `.env`. Restore those exact values with the database files; do not
regenerate credentials for an existing database. The application persists its
cache at `cache_path`. Map records and calculated routes live in MariaDB.

## Access, permissions, and privacy

FacilMap has no user accounts, group permissions, or OpenID Connect. Community
Stack does not add a proxy login. The HTTPS hostname is publicly reachable, and
anyone who can reach the service can open the interface and create maps.

Each collaborative map has separate **admin**, **editable**, and **read-only**
URLs. A URL is a bearer credential: anyone who obtains it receives its
permissions. Admin links can change settings and delete the map; editable links
can change map features; read-only links can view the map. This is not
authenticated private access, and an unlisted link is not equivalent to an
account-protected map.

Keep admin and edit links private. Search-engine listing is a separate,
per-map opt-in. To revoke a leaked URL, change that link in the map's settings
and distribute the replacement. FacilMap does not redirect the old URL; it
becomes available for a new map, so handle it as a secret even after rotation.
Avoid putting sensitive locations in this publicly reachable service.

FacilMap does not upload or store photos. A map description can refer to an
image hosted at a public URL, but the image is controlled by the external host,
is not included in Community Stack backups, and is not protected by the map's
sharing links. Do not use this as private photo storage.

## Collaboration, exports, and portability

Multiple visitors collaborate through the app's Socket.IO connection. Map data
and calculated routes are database-backed. FacilMap currently models markers
and lines (points and polylines); imported polygons and multipolygons are
converted to lines.

Use **Tools → Export** in FacilMap:

- **GeoJSON** is the preferred geographic-data export. It includes map
  features, custom types, and saved views, but is not a full application
  backup: map settings, permissions, and edit history are not fully represented.
- **GPX**, **CSV**, and **HTML/table** exports are available for their
  corresponding use cases.
- **KML is import-only** in the documented UI. It is not a KML export target.

Use GeoJSON to move geographic features between maps or applications. For a
complete restoration of a map, use the Community Stack database backup; do not
treat GeoJSON as a full backup.

## Backup, restore, and upgrade

When enabled, Community Stack briefly stops the FacilMap app to prevent edits
during the snapshot, creates a MariaDB logical dump, archives the cache, and
restarts the app. The live MariaDB data directory is excluded from raw file
copying. A failed dump or restart fails the backup. Each snapshot contains
`mariadb_facilmap.sql`, the FacilMap cache, configuration, and `.env`; verify
snapshots with `community-stack backup verify`.

To restore into a **clean** deployment:

1. Restore the snapshot to an isolated directory with
   `community-stack restore --target /path/to/restore --confirm`.
2. Copy the restored `community-stack.yaml`, `.env`, and `data/` into a clean
   checkout of the matching Community Stack version. Keep `.env` mode `0600`.
3. Start only the empty MariaDB service, then import the dump before starting
   FacilMap:

   ```bash
   docker compose --env-file .env -f services/facilmap/docker-compose.yml up -d facilmap-db
   docker compose --env-file .env -f services/facilmap/docker-compose.yml exec -T facilmap-db sh -ec '
   credentials="$(mktemp)"
   trap '\''rm -f "$credentials"'\'' EXIT
   umask 077
   printf "[client]\nuser=root\npassword=%s\n" "$MARIADB_ROOT_PASSWORD" > "$credentials"
   mariadb --defaults-extra-file="$credentials" "$MARIADB_DATABASE"
   ' < backups/mariadb_facilmap.sql
   ```

4. Deploy the restored services with `community-stack install`, then check
   `community-stack status` and `community-stack doctor`, and open the map URLs.

Do not import into an existing database without first taking and verifying a
separate backup. A restore drill is complete only after the app starts against
the restored database and maps are verified through FacilMap. Routine automated
tests validate dump creation, snapshot contents, and isolated extraction; they
do not replace a live Docker restore drill.

To update the app, change `services.facilmap.version` to a reviewed image
digest and run `community-stack upgrade facilmap`. The CLI requires a successful
pre-upgrade backup. Do not update the database major version as part of an app
upgrade without a MariaDB migration plan.

## Notifications and future integrations

FacilMap provides live collaborative updates over Socket.IO. Its documented
server interface does not provide an outbound webhook/notification mechanism
for task creation or map edits, and no native Matrix/email notification
integration is configured here. Community members can share map links manually
in Matrix, announcements, or Vikunja. Community Stack does not poll FacilMap or
synchronize map data with those services.

## Validation notes

Automated tests cover configuration, generated routes/secrets, lifecycle
controls, health-check behavior, and backup/restore artifacts. Live image
startup, HTTPS/WebSocket behavior through Caddy, mobile sharing, and a complete
clean-host map restore require a Docker-enabled environment and must be checked
before calling a deployment production-ready.

Upstream references:
[Docker deployment](https://docs.facilmap.org/developers/server/docker.html),
[server configuration](https://docs.facilmap.org/developers/server/config.html),
[collaborative maps](https://docs.facilmap.org/users/collaborative/),
[map settings](https://docs.facilmap.org/users/map-settings/),
[imports](https://docs.facilmap.org/users/import/),
[exports](https://docs.facilmap.org/users/export/),
[license](https://github.com/FacilMap/facilmap/blob/main/LICENSE.md).
