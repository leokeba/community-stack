# Operator Handbook & Operational Procedures

This handbook is written for volunteer infrastructure operators managing a live Community Communications Stack deployment.

---

## 1. Quick Reference & Common Commands

| Task | Command | Notes |
|---|---|---|
| Pre-flight check | `uv run community-stack check` | Validates config, secrets, directories, tools |
| Dry-run plan | `uv run community-stack plan` | Safe preview of what would be deployed |
| Deploy services | `uv run community-stack install` | Deploys all enabled Compose services |
| Service health | `uv run community-stack status` | Reports state of all managed containers |
| View service logs | `uv run community-stack logs matrix -f` | Tail and follow logs for any service |
| Create backup | `uv run community-stack backup` | Creates timestamped snapshot with DB dump |
| Verify backup | `uv run community-stack backup verify` | Cryptographic & structural verification |
| Isolated restore | `uv run community-stack restore --target /tmp/test --confirm` | Non-destructive restore drill |
| Safe upgrade | `uv run community-stack upgrade matrix` | Takes backup, pulls image, restarts |
| Draft announcement | `uv run community-stack announce draft -t "Title" -b "Body"` | Saves privately without dispatch |
| Preview/publish | `uv run community-stack announce preview <id>` then `announce publish <id>` | Default output is local public HTML and feeds |
| Schedule a draft | `uv run community-stack announce schedule <id> --at "2026-11-02 09:30"` | Time is in `project.timezone`; `announce unschedule <id>` cancels |
| Run due retries | `uv run community-stack announce worker --once` | Runs queued deliveries now; the admin app also does this every 15 s |
| Export offline bundle | `uv run community-stack archive export` | Creates zero-dependency offline archive |
| Emergency killswitch | `uv run community-stack bridge disable --confirm` | Disconnects WhatsApp bridge immediately |
| Diagnostic report | `uv run community-stack doctor` | Non-destructive, secret-redacted audit |

The admin UI at `/admin` supports the same publishing lifecycle. Only the local
web-feed adapter is enabled by default. Matrix delivery requires explicit
configuration; ActivityPub/email adapter classes are prototypes and have not
been verified against live services.

Organizers each get their own account. The first account, created at
`/admin/setup`, is an **owner**. Owners and admins add accounts on the
Accounts page with one of four roles:

| Role | Can do |
| --- | --- |
| Owner | Everything, including managing other owners |
| Admin | Manage accounts (except owners) and publishing destinations, and publish |
| Publisher | Write, publish, edit and withdraw announcements |
| Reviewer | Read announcements and delivery history |

Role changes and disabled accounts take effect on the next page load, and a
password reset signs the account out everywhere. At least one active owner must
always remain. Sign-ins, failed sign-ins, account changes, publishing actions
and settings changes are recorded with time, account and client address on
the Activity log page, and kept for about 13 months.

### Review before publishing

Owners and admins can turn on **Review before publishing** on the Settings
page. From then on a draft is submitted for review, and someone other than the
person who submitted it (an owner, admin, publisher or reviewer) approves it or
sends it back with a note. Only approved announcements can be published or
scheduled, and editing a submitted, approved or scheduled announcement sends it
back to draft, so what is published is always what was approved. The dashboard
lists announcements waiting for your review, and each announcement shows its
review history.

For safety notices that cannot wait, owners and admins can publish without
review by giving a reason and their password; the reason is recorded in the
announcement's review history and the activity log. The server command line
(`community-stack announce publish`) is not subject to review.

### Writing announcements

Announcement messages use Markdown: `## Heading`, `**bold**`, `*italic*`,
`- list item` and `[link text](https://…)`. Line breaks are kept as typed. Raw
HTML is not allowed and is shown as text. The public feed, email, Matrix and
ActivityPub get the formatted version; Mastodon, Bluesky, Telegram, Discord,
Slack and WhatsApp get clean plain text without Markdown symbols.

The editor previews the formatted announcement and exactly what each channel
will receive, with character counts, as you type. For channels with a length
limit you can write custom text, for example a short version with hashtags for
Mastodon; leave it empty to send the title and message, shortened with a link
when too long. The announcement page shows the same previews after saving.

Drafts and scheduled announcements can have one image, added on the
announcement page with a required description for people who cannot see it.
Uploads must be JPEG, PNG, WebP or GIF and at most 10 MB; the server removes
location and camera metadata, applies the photo's rotation and resizes it to at
most 1600 pixels. Images stay private until the announcement is published, are
copied to the public feed (`/feed/media/`) at publication, and are removed from
it on withdrawal. The web feed, Mastodon, Bluesky, Discord, Slack, ActivityPub
and email send the image with its description; Matrix and Telegram do not yet,
which the announcement page points out.

Announcements sent often (meeting notices, food-bank days, alerts) can be
kept as **templates**: create them on the Templates page or save any
announcement as one from its page, then pick a template when creating a draft
to start with its title, message, author, tags and per-channel text filled in.

### Connecting channels

Owners and admins connect Mastodon, Bluesky, a Telegram channel, Discord,
Slack and a [listmonk](https://listmonk.app/) newsletter on the console's **Channels** page, which lists the steps for each one.
Credentials are stored in the announcements database on the server, are never
shown again after saving, and are deleted on disconnect; leave a secret field
blank to keep the saved value. "Test connection" checks the credentials without
posting, except for Slack, which offers no such check. Connected channels are
on by default and can be turned off on the Settings page.

| Channel | Long text | After an edit | After withdrawal |
| --- | --- | --- | --- |
| Mastodon | Shortened to 500 characters with a link | Post edited | Post deleted |
| Bluesky | Shortened to 300 characters with a link | Not sent (Bluesky has no edits) | Post deleted |
| Telegram channel | Shortened to 4,096 characters with a link | Message edited | Message deleted |
| Discord channel | Shortened to 2,000 characters with a link | Message edited | Message deleted |
| Slack channel | Shortened to 3,000 characters with a link | Not sent | Not sent |
| Email newsletter (listmonk) | Full text and image, sent to the chosen lists | Follow-up email | Correction email |

Server addresses must use HTTPS and resolve to public addresses, so a
connection cannot be pointed at services inside the server. Telegram, Discord
and Slack posts are only retried automatically after a rate-limit refusal; other
failed posts are not retried, because the platform may already have received
them. listmonk must be reachable over HTTPS from the internet; the connector
uses an API user and sends one Markdown campaign per announcement, named with
the announcement ID so a retry reuses the campaign instead of emailing
everyone twice. These connectors are tested against recorded API behaviour,
not yet against live accounts.

### Two-step login and account recovery

Everyone can turn on two-step login on their **My account** page: after
confirming their password they scan a QR code with an authenticator app (Aegis,
FreeOTP, Google Authenticator, 1Password and others) and receive ten single-use
recovery codes. Logging in then asks for the 6-digit code after the password;
each code works only once, and the code step allows five tries within five
minutes. Owners and admins should all turn it on; the Get started checklist
tracks this. If someone loses their phone and recovery codes, an owner or admin
turns it off for them on the Accounts page.

If nobody can log in, recover an account on the server, as the user that owns
the stack directory (not root):

```bash
cd /opt/community-stack
.venv/bin/community-stack admin list
.venv/bin/community-stack admin reset-password <username> --disable-two-step
```

This prints a temporary password once, signs the account out everywhere,
re-enables it if it was disabled, and records the reset in the activity log.

### Delivery retries

Drafts can be scheduled from the announcement page or with `announce
schedule`. Times are entered in the community's time zone, set as an IANA name
in `project.timezone` (for example `Europe/Paris`; default UTC). The background
worker publishes a scheduled announcement within about 15 seconds of its time.
Editing keeps the schedule, and withdrawing or cancelling it stops publication.

Each channel delivery is stored as a job before it runs. A temporary failure
(a network error, HTTP 5xx or 429) on a channel that can safely repeat the
operation is retried automatically after 30 s, 2 min, 8 min, 32 min and 1 h,
up to 5 attempts in total. The admin app runs due jobs in the background every
15 seconds; set `COMMUNITY_STACK_EMBEDDED_WORKER=0` to turn that off and run
`community-stack announce worker` as a separate process instead, or
`COMMUNITY_STACK_WORKER_INTERVAL` to change the interval. Withdrawing an
announcement or disabling a channel cancels its pending retries. Pending jobs
are listed on the announcement's page under "Scheduled deliveries".

---

### Health and logs

The console's **Health** page (owners and admins, through the control agent)
checks free disk space, how old the latest backup is, the HTTPS certificates of
the web and Matrix domains, and whether every stack container is running and
healthy. It also shows the last 50, 200 or 500 lines of any service's logs.
Before logs leave the agent, every value from `.env`, saved channel
credentials, bearer tokens and Telegram bot tokens are replaced with
`[REDACTED]`; logs can still contain IP addresses and account names, which is
why the page is limited to owners and admins.

### Services and settings

With the control agent installed, owners and admins change settings on the
console's **Services** page: the community time zone, how long backups are
kept, backup encryption, and whether Element and Matrix run. Every change is
shown as a review first, including the exact lines of `community-stack.yaml`
that will change. Applying it:

1. takes a safety backup (if that fails, nothing is changed);
2. saves the previous configuration as `community-stack.yaml.previous` and
   writes the new one in place, keeping its comments;
3. starts or stops the affected service and waits up to three minutes for it
   to be healthy;
4. restores the previous configuration and service state if it is not.

Stopping Matrix stops all community chat, so it asks for the person's password.
Domains, Matrix registration and federation, and the WhatsApp bridge are still
configured from the command line. The **Get started** page lists the remaining
setup steps, and the dashboard links to it until they are done.

### Software updates

Each release of the stack pins tested versions of Caddy, nginx, Synapse,
PostgreSQL, Element and the WhatsApp bridge in its Compose files. After a new
release is installed, the Services page lists every component whose running
version differs from the release, with a link to the upstream release notes,
and an **Update** button per service. Updating takes a safety backup, pulls the
pinned images, restarts the service and waits for it to become healthy; if it
does not, the previous images are started again. Only the pinned versions can
be installed this way, and a PostgreSQL major-version change is refused
because it needs a database migration from the command line. The announcements
service itself is updated by deploying a new release.

### Control agent

The admin console's System page (owners and admins) shows service status and
can create a backup, check the latest backup, and run a restore drill. The
console never touches the server directly: it asks the **control agent**, a
small privileged service on the host, which accepts only a fixed list of
operations (see [ADR 0007](decisions/0007-control-agent.md)). Without the agent,
the System page says so and the rest of the console works as before.

Install it once on the server, from the stack directory:

```bash
sudo install -m 0644 services/agent/community-stack-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now community-stack-agent
sudo systemctl status community-stack-agent
```

The unit assumes the stack lives in `/opt/community-stack`, that the CLI is
installed in its `.venv`, and that the console runs as UID/GID 1000; edit the
`ExecStart` line if yours differ. The agent listens on
`data/agent/run/agent.sock`, which only the console's group can open, and
records every request in `data/agent/audit.log`. The restore drill asks for the
person's password again; the agent checks it itself and allows five wrong
attempts per 15 minutes. `deploy-vps.sh` restarts the agent after a deployment
if it is installed.

## 2. Maintenance Windows & Upgrades

### Maintenance Rules
1. **Never upgrade without a backup:** `community-stack upgrade` automatically creates a safety snapshot before modifying any container.
2. **One service at a time:** Upgrade services individually (e.g. `caddy` first, then `matrix`, `element`, and `announcements`).
3. **Off-peak scheduling:** Announce maintenance windows to the community at least 24 hours in advance using `community-stack announce create`.

The announcements application is built locally from the checked-out source:
take a backup, update the repository, then run
`uv run community-stack upgrade announcements`. This rebuilds the container
without pulling an application image from a registry.

### Upstream Software Release Tracking
We pin all container images to explicit upstream releases:

| Service | Upstream Project | Pinned Release | Security Feed |
|---|---|---|---|
| Proxy | [caddyserver/caddy](https://github.com/caddyserver/caddy) | `2.8.4-alpine` | GitHub Releases / Advisories |
| Homeserver | [matrixdotorg/synapse](https://github.com/element-hq/synapse) | `v1.119.0` | Matrix Security Announcements |
| Database | [docker-library/postgres](https://github.com/docker-library/postgres) | `16-alpine` | PostgreSQL Release Notes |
| Web Client | [element-hq/element-web](https://github.com/element-hq/element-web) | `v1.11.70` | Element Web GitHub Releases |
| WhatsApp Bridge | [mautrix/whatsapp](https://github.com/mautrix/whatsapp) | `v0.11.2` | mautrix-whatsapp Releases |

---

## 3. Incident Response Runbooks

### Incident A: Compromised Administrator Account
1. **Immediately revoke admin access token:**
   - Log into Synapse database or execute admin revocation.
   - Rotate `SYNAPSE_SECRET_KEY` and `SYNAPSE_REGISTRATION_SHARED_SECRET` in `.env`.
2. **Restart Synapse to invalidate existing sessions:**
   ```bash
   docker restart community_synapse
   ```
3. **Audit Matrix room permissions and moderators.**

### Incident B: Suspected WhatsApp Bridge Compromise or Account Ban
1. **Trigger Emergency Killswitch:**
   ```bash
   uv run community-stack bridge disable --confirm
   ```
2. **Unlink WhatsApp Web session** from the mobile device under **Settings > Linked Devices**.
3. **Verify Matrix is unaffected:**
   ```bash
   uv run community-stack status
   ```

### Incident C: Server Seizure, Hardware Loss, or Provider Eviction
1. **Activate Secondary Standby Host:**
   - Execute the standby playbook or launch clean Debian 12 VPS.
2. **Obtain off-host backup archive** from your independent S3/SFTP storage bucket.
3. **Run non-destructive restore:**
   ```bash
   uv run community-stack restore --target /opt/community-stack --confirm
   ```
4. **Repoint DNS records:** Update A/AAAA records to the new host IP.
5. **Start services:**
   ```bash
   cd /opt/community-stack
   uv run community-stack install
   ```

---

## 4. Decommissioning & Secure Data Erasure

When taking down a community infrastructure server permanently:
1. **Export final archive for community records:**
   ```bash
   uv run community-stack archive export -o /tmp/final_community_archive.zip
   uv run community-stack backup
   ```
2. **Halt all containers and remove volumes:**
   ```bash
   docker compose -f services/matrix/docker-compose.yml down -v
   docker compose -f services/proxy/docker-compose.yml down -v
   docker compose -f services/landing/docker-compose.yml down -v
   docker compose -f services/element/docker-compose.yml down -v
   ```
3. **Securely erase persistent data directories:**
   ```bash
   # Shred keys and database files
   find data/ -type f -exec shred -u -z -n 3 {} +
   shred -u -z -n 3 .env
   ```
4. **Delete cloud volumes and request VPS disk sanitization** from provider.
