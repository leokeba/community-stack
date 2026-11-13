# Field Map

Field Map is an optional mobile-first field-observation app maintained in
[its own repository](https://github.com/leokeba/field-map). Community Stack
builds the selected upstream Git commit into a local image, then manages its
private PostGIS database, photo storage, HTTPS route, lifecycle, diagnostics,
and backups.

## Enable and manage

1. Add the Field Map hostname's DNS record to the server.
2. Open **Admin → Services and updates**. Enable **Field Map mobile mapping**,
   set the public hostname and tile URL, and review the anonymous map creation
   setting.
3. Review the proposed changes and confirm with an administrator password.
   The console creates the database secret, persistent directories, and Caddy
   route, then builds and starts the pinned application.
4. Open Field Map from the same Services page. `community-stack status` and
   `community-stack doctor` report service state and readiness.

The default app source is pinned to a full commit SHA. Review a new commit
before changing **Field Map source commit**; applying the change takes a
safety backup and rebuilds the service. `community-stack upgrade field-map`
rebuilds the currently configured commit.

Field Map's database is not published to the host. It runs on a private
network; only the app joins the existing proxy network. The app and database
containers have health checks, and neither publishes a host port.

## Access and sensitive data

The initial app does not use Community Stack accounts or an identity provider.
Map owner/editor/viewer links are bearer credentials: anyone holding a link
can use the permission encoded by it. Maps may contain precise locations and
photos; do not put sensitive observations on an internet-accessible test
instance.

Anonymous map creation is enabled in the initial configuration so a test user
can create a map without an account. When enabled, anyone who reaches the
hostname can create maps and consume storage. Disable it in Services when
anonymous creation is no longer intended. Disabling it is **not** an account
system or a site-wide login gate; existing share links remain the app's
access mechanism.

The configured OpenStreetMap tile URL is for low-volume testing only. Choose a
tile provider that permits the deployment's expected traffic before regular
or production use. The tile URL is embedded in the public frontend build and
must not contain private credentials.

The app's upload limit is 10 MB per photo and the proxy caps request bodies at
12 MB. The app stores protected originals and generated thumbnails. Review
photo metadata and location sensitivity before sharing a map.

## Backup, restore, and disable

When enabled, `community-stack backup` briefly stops the Field Map app,
creates `backups/postgres_field_map.dump`, archives
`data/field_map/uploads`, and restarts the app. The live PostgreSQL directory
is excluded from the file archive; the logical PostGIS dump and photo files
are both required for recovery. A failed dump or app restart fails the
snapshot. `community-stack backup verify` checks for the database dump.

To restore to a clean deployment:

1. Restore a snapshot to an isolated directory with
   `community-stack restore --target /tmp/field-map-restore --confirm`.
2. Restore the matching `community-stack.yaml` and `.env` to the new host,
   then keep `.env` mode `0600`.
3. Copy the photos and start the configured services:

   ```bash
   cp -a /tmp/field-map-restore/data/field_map/uploads/. data/field_map/uploads/
   uv run community-stack install
   ```

4. Stop the app, import the PostGIS dump, then restart it:

   ```bash
   docker compose --env-file .env -f services/field_map/docker-compose.yml stop field_map
   docker compose --env-file .env -f services/field_map/docker-compose.yml exec -T field_map-db \
     pg_restore -U fieldmap -d fieldmap --clean --if-exists --no-owner \
     < /tmp/field-map-restore/backups/postgres_field_map.dump
   docker compose --env-file .env -f services/field_map/docker-compose.yml start field_map
   ```

Restore only to a clean deployment or after separately preserving its current
database and uploads. Verify with `community-stack status` and
`community-stack doctor`.

Disabling Field Map from the admin console stops its containers and removes
its public route, but preserves the database, uploaded photos, and generated
credentials. Do not remove those persistent files unless the data is no
longer needed and a verified backup exists.
