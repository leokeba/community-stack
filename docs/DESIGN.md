# Design Document: Resilient Community Communications Stack

**Status:** Proposed  
**Audience:** AI coding agent, maintainers, infrastructure operators  
**Implementation approach:** Modular, self-hosted, open-source, infrastructure-as-code  
**Primary goal:** Make resilient communications infrastructure deployable and maintainable by people who are not infrastructure specialists.

---

## 1. Executive summary

Build an open-source GitHub project that helps a geographically distributed public movement deploy and operate its own communications infrastructure.

The system must remain useful when commercial platforms become unavailable, are blocked, suffer outages, or become unsuitable for a community's needs. At the same time, it must accommodate people who continue using mainstream platforms and people who have limited access to smartphones or the internet.

This is not a new messaging application. It is a deployment and integration layer that combines existing, independently maintained software into a coherent system.

### Core principles

1. **Resilience over feature completeness.** A smaller system that can be recovered is preferable to a large system that cannot.
2. **Accessibility from day one.** Do not require everyone to install a new application.
3. **Independent operation.** Core communications must continue if WhatsApp or another external platform disappears.
4. **Privacy by separation.** Public announcements, community discussions, and sensitive coordination must have different access and exposure policies.
5. **Modularity.** Each service and channel must be independently deployable, upgradeable, replaceable, and removable.
6. **Open standards and upstream software.** Prefer maintained upstream projects over custom implementations.
7. **Reproducibility.** Infrastructure configuration, deployment procedures, and recovery instructions must be version-controlled.
8. **Operational simplicity.** A small volunteer technical team must be able to deploy, update, diagnose, and recover the system.
9. **Data portability.** The community must be able to export its data and move to another host or provider.
10. **No false decentralization.** Multiple applications on one server do not constitute independent infrastructure.

## 2. Scope and non-goals

### In scope

- Automated deployment of a self-hosted communications stack.
- A Matrix homeserver and a user-friendly client.
- A public landing page and announcement stream.
- Optional federation with other independently operated services.
- Selective Matrix-to-WhatsApp bridging.
- Future channel adapters for ActivityPub, RSS, email, SMS, and voice.
- Backup, restore, migration, upgrades, and health monitoring.
- Multi-host deployment and independent failure domains.
- Documentation suitable for volunteer operators.
- Optional offline and local-network continuity tools.

### Explicitly out of scope for the initial release

- Developing a new chat application.
- Reimplementing Matrix, WhatsApp, Mastodon, or other messaging protocols.
- Automatically mirroring every conversation across every service.
- Treating WhatsApp as a dependable or permanent component of the infrastructure.
- Building a custom identity provider before existing authentication capabilities have been evaluated.
- Deploying every possible service in the MVP.
- Promising anonymity, censorship resistance, or end-to-end confidentiality that the architecture cannot guarantee.
- Automatically migrating all historical messages between platforms.

## 3. System architecture

### 3.1 Logical architecture

The system has four layers.

**Layer A — Core community services**

- Matrix homeserver.
- Element Web or another compatible Matrix client.
- Public landing page and service directory.
- Canonical public announcement feed.

**Layer B — Channel adapters**

- Matrix announcement-room adapter.
- WhatsApp bridge or publisher.
- ActivityPub publisher.
- RSS and lightweight web feed.
- Email and SMS adapters in later phases.

**Layer C — Infrastructure and operations**

- Ansible for host configuration.
- Docker Compose for application deployment.
- Caddy for HTTPS and reverse proxying.
- Secrets management.
- Backup and restore tooling.
- Monitoring, logs, and health checks.

**Layer D — Resilience and continuity**

- Independent backups.
- Restore testing.
- Optional second host.
- Exportable public announcements.
- Offline archives and local-network distribution.
- Documented emergency operating procedures.

### 3.2 Recommended initial technology choices

| Concern | Initial choice | Rationale |
|---|---|---|
| Infrastructure automation | Ansible | Reproducible host setup and service lifecycle |
| Application deployment | Docker Compose | Familiar, inspectable service definitions |
| Operator interface | Small Python CLI | One consistent command interface |
| Public reverse proxy | Caddy | Automated TLS and simple routing |
| Messaging | Matrix homeserver | Open protocol and independent operation |
| Web client | Element Web | Established Matrix client |
| WhatsApp bridge | mautrix-whatsapp | Reuse existing bridge implementation |
| Configuration validation | JSON Schema | Machine-readable configuration contract |
| Tests | pytest plus deployment integration tests | Validate configuration, lifecycle, and recovery |

Treat these as initial defaults, not irreversible commitments. Before implementation, inspect the upstream projects and their current deployment requirements. Record any material change in an architecture decision record.

### 3.3 Service boundaries

Use separate Docker Compose projects for independently managed services. A service should have its own configuration, persistent data directory or volume, lifecycle, health check, backup definition, and documented dependencies.

The CLI should orchestrate these projects rather than embedding their application logic.

Do not modify upstream application source code to make the stack work unless there is a compelling, documented reason.

### 3.4 Deployment profiles

Support incremental adoption through profiles:

- **Starter:** host preparation, TLS, public landing page, Matrix, Element, secrets, backups, and recovery procedures.
- **Community:** Starter plus public announcement distribution and selected federation features.
- **Accessible:** Community plus WhatsApp integration and, later, email, SMS, and voice.
- **Resilient:** Community or Accessible plus a second host, independent backups, and continuity procedures.

Profiles should select combinations of modules, not create separate implementations of shared functionality.

The initial implementation must not attempt to deliver every profile at once.

## 4. Configuration and CLI

### 4.1 Configuration

Provide a documented YAML configuration file validated against a JSON Schema.

Example:

```yaml
project:
  name: community
  profile: starter

domain:
  base: example.org
  matrix: matrix.example.org
  web: community.example.org

services:
  matrix:
    enabled: true
  element:
    enabled: true
  announcements:
    enabled: true
  whatsapp_bridge:
    enabled: false

operations:
  backups:
    enabled: true
    schedule: daily
  monitoring:
    enabled: true
```

This is illustrative. The implementation must define the complete schema, sensible defaults, validation rules, and any additional required settings. Do not assume this minimal example is sufficient to deploy a real instance.

Separate non-secret configuration from credentials. Secrets must never be committed to Git, printed in logs, or included in generated diagnostic bundles.

### 4.2 CLI interface

Implement a small Python CLI with a stable, documented command structure.

Required commands:

```text
community-stack init
community-stack check
community-stack plan
community-stack install
community-stack status
community-stack logs <service>
community-stack backup
community-stack backup verify
community-stack upgrade <service>
community-stack restore --target <target>
community-stack doctor
```

Requirements:

- `init` creates an example configuration and required local directories.
- `check` validates configuration, dependencies, DNS assumptions, required ports, storage, and secret availability.
- `plan` describes intended changes without applying them.
- `install` deploys only enabled services.
- `status` reports service health and version information.
- `backup` creates a documented, timestamped backup.
- `backup verify` checks backup integrity and completeness.
- `upgrade` upgrades one service at a time.
- `restore` supports a documented recovery workflow and must not overwrite a production instance without explicit confirmation.
- `doctor` performs non-destructive diagnostics and produces a redacted report.

Commands must return meaningful exit codes and actionable error messages. Destructive operations require explicit confirmation and a clear indication of the target environment.

The CLI should not become a second orchestration engine. Delegate infrastructure operations to Ansible and application lifecycle operations to Compose.

## 5. Public announcements and channel adapters

This is a central architectural component, not an optional integration added at the end.

### 5.1 Canonical announcement model

Define a platform-independent announcement object.

Required fields:

- `id`: stable unique identifier.
- `title`: short human-readable title.
- `body`: announcement content.
- `published_at`: timestamp.
- `language`: language code.
- `status`: draft, published, updated, or withdrawn.
- `canonical_url`: authoritative web location.
- `expires_at`: optional expiry timestamp.
- `audience`: public or another explicitly defined distribution scope.

Optional fields can include attachments, geographic scope, accessibility text, and source attribution.

Validate the model. Define maximum lengths, URL rules, timestamp handling, and attachment restrictions.

### 5.2 Publishing flow

The initial system should use a simple, durable workflow:

1. An authorized publisher creates or updates an announcement.
2. The announcement is validated.
3. A stable identifier and canonical URL are assigned.
4. The publication is stored durably.
5. Each enabled adapter receives a publication event.
6. Each adapter records delivery state independently.
7. Failed deliveries are retried with bounded backoff.
8. Operators can inspect failures and retry them.
9. Updates and withdrawals are propagated where supported.

For the MVP, a small database-backed queue or an equivalent durable mechanism is sufficient. Do not introduce a large distributed messaging platform without evidence that it is needed.

### 5.3 Adapter contract

Each adapter must implement a common interface, conceptually:

```python
class ChannelAdapter:
    async def publish(self, announcement) -> DeliveryResult: ...

    async def update(self, announcement) -> DeliveryResult: ...

    async def withdraw(self, announcement_id) -> DeliveryResult: ...

    async def health_check(self) -> HealthResult: ...
```

This is a conceptual interface, not a demand to implement these exact method signatures without considering the final architecture.

The adapter contract must define:

- Idempotency and duplicate prevention.
- Retryable versus permanent failures.
- Rate limits.
- Message formatting and length limits.
- Link and attachment handling.
- Delivery status and error reporting.
- Channel-specific limitations.
- Authentication and secret handling.
- Logging and redaction.

An unavailable adapter must not block publication to other channels.

### 5.4 Initial adapters

Implement in this order:

1. Public website or static announcement feed.
2. Matrix announcement room.
3. RSS.
4. WhatsApp publishing or bridge integration.
5. ActivityPub.
6. Email and SMS.

The canonical announcement must remain available even if all external adapters fail.

## 6. Matrix-to-WhatsApp integration

### 6.1 Preferred implementation

Evaluate and integrate the existing upstream project:

- Repository: https://github.com/mautrix/whatsapp
- Documentation: https://docs.mau.fi/bridges/go/whatsapp/

Do not implement the WhatsApp protocol from scratch.

`mautrix-whatsapp` is a Matrix-to-WhatsApp bridge based on an unofficial, reverse-engineered WhatsApp Web implementation. It is not an official WhatsApp integration. Its compatibility, account availability, and operation depend on an external service that the project does not control.

Review its current license, releases, deployment requirements, security model, and maintenance status before pinning a version.

### 6.2 Two distinct integration modes

The implementation must distinguish two modes.

**Mode A — Public announcement publishing**

A canonical public announcement is forwarded to a designated WhatsApp destination.

This should be the first WhatsApp integration because it is easier to moderate, limits data exposure, and does not require relaying private conversations.

Use an officially supported WhatsApp Business integration only if its current capabilities, cost, destination types, and applicable policies fit the use case. Do not assume the official Business Platform can send arbitrary messages to ordinary WhatsApp groups.

If using `mautrix-whatsapp`, treat it as an optional unofficial integration and document the associated account and availability risks.

**Mode B — Selected two-way room bridging**

A designated Matrix room is bridged to a designated WhatsApp group using mautrix-whatsapp.

This is optional and must not be enabled by default.

Requirements:

- Explicit operator opt-in.
- Explicit mapping between Matrix rooms and WhatsApp conversations.
- Allowlisting of eligible rooms and destinations.
- No automatic bridging of all rooms or all WhatsApp history.
- No bridging of sensitive rooms by default.
- Clear user-facing disclosure that the bridge operator can access bridged message content.
- Documented handling of edits, deletions, replies, media, reactions, and unsupported message types.
- Loop prevention and duplicate detection.
- Monitoring for disconnection and authentication expiry.
- A documented procedure for revoking the linked account and disabling the bridge.
- An emergency switch that disables outgoing bridge traffic without taking down Matrix.

Do not implement custom reverse-engineered WhatsApp transport code.

### 6.3 Privacy and trust boundaries

Matrix-to-WhatsApp bridging changes the privacy properties of the conversation.

The bridge needs to process messages to relay them. Message content, identifiers, media, and metadata may therefore be visible to the bridge operator or retained in logs and storage.

The implementation must not describe a bridged conversation as having the same confidentiality guarantees as an end-to-end encrypted conversation that stays within one platform.

The bridge is not a security boundary. It is an additional trusted component.

Never bridge private coordination rooms merely because the technical mapping is possible.

### 6.4 WhatsApp failure policy

The project must continue functioning if:

- WhatsApp bans or restricts the linked account.
- WhatsApp changes its protocol.
- The bridge becomes incompatible with a new WhatsApp release.
- A linked device disconnects.
- The bridge operator chooses to disable the integration.
- A legal or policy change makes continued operation unsuitable.

The system must preserve the canonical announcements and Matrix rooms independently.

Document a manual fallback for publishing the same public announcement through the remaining channels.

## 7. Security requirements

Security must be implemented throughout the project, not postponed until the final roadmap phase.

### 7.1 Network exposure

- Expose only required public ports.
- Keep databases, application-service ports, and internal APIs private.
- Use Caddy for public HTTP(S) ingress.
- Document Matrix federation ports and make federation configurable.
- Do not expose administrative interfaces to the public internet by default.
- Separate services through Docker networks where practical.

### 7.2 Secrets

- Never store secrets in Git.
- Provide an `.env.example` containing placeholders only.
- Use restrictive filesystem permissions for credentials.
- Redact secrets, access tokens, session information, and phone numbers from diagnostic reports.
- Do not log message bodies by default.
- Document secret rotation and incident response.

### 7.3 Identity and access

- Require authentication for administration.
- Restrict announcement publishing to authorized accounts.
- Make public Matrix registration a deliberate configuration decision, disabled by default unless explicitly required.
- Document moderation, account recovery, abuse reporting, and account removal.
- Avoid collecting personal information that is not required for operation.

### 7.4 Dependency and supply-chain security

- Pin application images to reviewed versions or immutable digests.
- Never use `latest` in production deployment configuration.
- Track upstream releases and security advisories.
- Document upgrade compatibility and rollback limitations.
- Use least-privilege containers where practical.
- Generate a software bill of materials when feasible.
- Do not blindly auto-upgrade critical services.

### 7.5 Threat model

Document at least these threats:

- Server seizure, compromise, or loss.
- Hosting provider outage or account suspension.
- Domain or DNS disruption.
- Platform censorship or account bans.
- Compromised administrator credentials.
- Malicious or compromised bridge.
- Metadata collection and traffic analysis.
- Malicious links, attachments, spam, and denial-of-service.
- Accidental exposure of a private room through incorrect bridging.
- Backup theft or incomplete recovery.

For each threat, state mitigations, residual risks, and operational actions.

Do not claim that self-hosting alone guarantees anonymity or censorship resistance.

## 8. Backup, restore, and migration

Backups and recovery are MVP requirements.

### 8.1 Requirements

- Back up application databases and persistent data consistently.
- Include the configuration and secrets required for recovery, using a secure storage method.
- Document which data must be backed up together.
- Encrypt off-host backups.
- Support configurable retention.
- Keep at least one backup independent of the production host.
- Avoid storing the only backup on the server it protects.
- Verify backup integrity automatically.
- Provide a restore workflow that can target a separate test environment.
- Document how to recover from complete server loss.

### 8.2 Recovery acceptance criteria

A successful backup command is not sufficient evidence of recoverability.

The MVP must demonstrate that:

1. A fresh test host can be prepared from the repository.
2. A backup can be restored into an isolated environment.
3. Matrix starts successfully and its persistent data is available.
4. The public landing page and announcements can be recovered.
5. The restored configuration is validated.
6. The recovery process is documented and repeatable.
7. The test does not overwrite production data.

Record recovery time and any missing data. The documentation must state that message recovery, federation history, and external-platform history may have limitations.

### 8.3 Migration

Document how to move a deployment to another host, including DNS changes, data transfer, secrets, service shutdown, verification, and rollback.

Keep application-specific migration logic in service modules rather than the central CLI.

## 9. Repository structure

Use the following as an initial proposal. Adjust it after inspecting the repository, but preserve the separation of responsibilities.

```text
.
├── README.md
├── LICENSE
├── SECURITY.md
├── CONTRIBUTING.md
├── CHANGELOG.md
├── pyproject.toml
├── config/
│   ├── schema.json
│   └── example.yaml
├── cli/
│   └── community_stack/
├── ansible/
│   ├── playbooks/
│   └── roles/
│       ├── host/
│       ├── docker/
│       ├── firewall/
│       ├── proxy/
│       ├── backup/
│       └── monitoring/
├── services/
│   ├── landing/
│   ├── matrix/
│   ├── element/
│   ├── announcements/
│   └── whatsapp_bridge/
├── adapters/
│   ├── matrix/
│   ├── rss/
│   ├── whatsapp/
│   ├── activitypub/
│   ├── email/
│   └── sms/
├── tests/
│   ├── unit/
│   ├── integration/
│   └── recovery/
├── docs/
│   ├── architecture.md
│   ├── threat-model.md
│   ├── deployment.md
│   ├── backup-restore.md
│   ├── operations.md
│   └── decisions/
└── examples/
```

Do not create empty placeholder implementations for every future service. Add modules when they have a defined contract and useful tests.

## 10. Implementation roadmap

The milestones are ordered by dependency. Complete each milestone's acceptance criteria before moving to the next.

### Milestone 0 — Repository reconnaissance and architecture decisions

**Goal:** Establish a safe implementation plan before changing code.

Tasks:

- Inspect the existing repository, its language, conventions, CI, and current functionality.
- Identify existing configuration, deployment scripts, and reusable components.
- Check current upstream deployment documentation for Matrix, Element, Caddy, and mautrix-whatsapp.
- Identify licensing constraints and upstream dependencies.
- Record decisions about the homeserver, storage, backup method, and deployment profiles.
- Define the first configuration schema and CLI contract.
- Create an initial threat model.
- Create a milestone-by-milestone implementation plan.

**Acceptance criteria:**

- Existing functionality is understood and preserved where relevant.
- Architecture decisions are recorded.
- Risks and unresolved questions are listed.
- No large implementation begins before the plan is reviewed.

### Milestone 1 — Project foundation

**Goal:** Establish the configuration and automation framework.

Tasks:

- Create Python packaging and CLI entry point.
- Implement `init`, `check`, and `plan`.
- Add JSON Schema validation.
- Add example configuration and documentation.
- Implement safe local state and secret handling.
- Establish linting, formatting, and unit tests.
- Add CI for supported Python versions and configuration validation.

**Acceptance criteria:**

- A clean checkout can initialize a local project.
- Invalid configuration produces actionable errors.
- Missing required secrets are detected without printing their values.
- `plan` does not change the host.
- Unit tests run without requiring a live server.

### Milestone 2 — Minimal deployable infrastructure

**Goal:** Deploy a working public landing page and Matrix service.

Tasks:

- Implement Ansible host preparation.
- Install and configure Docker and Compose.
- Configure Caddy and TLS.
- Deploy the Matrix homeserver and Element Web in separate service modules.
- Configure persistent storage and private internal networks.
- Add health checks and service status reporting.
- Document DNS and firewall requirements.

**Acceptance criteria:**

- A supported clean host can be deployed using documented commands.
- HTTPS works with valid DNS configuration.
- Matrix and Element can communicate.
- Services survive a restart.
- Only documented ports are exposed.
- Disabled services are not deployed.

### Milestone 3 — Backups, restoration, and safe upgrades

**Goal:** Make the core service recoverable before adding more integrations.

Tasks:

- Implement consistent backups of databases and persistent data.
- Add encryption and off-host backup configuration.
- Implement backup verification.
- Implement restore to an isolated target.
- Add service-specific upgrade procedures.
- Document compatibility, rollback limitations, and complete-host-loss recovery.
- Create an automated recovery integration test.

**Acceptance criteria:**

- Backup integrity can be checked.
- A fresh test environment can be restored from a backup.
- The restored Matrix service starts and retains its expected data.
- Production data cannot be overwritten by an unconfirmed restore.
- A documented upgrade and recovery drill succeeds.

### Milestone 4 — Canonical public announcements

**Goal:** Publish information independently of any one messaging platform.

Tasks:

- Define and validate the announcement model.
- Implement durable storage and stable identifiers.
- Build a minimal publishing interface or documented publishing API.
- Publish announcements to a public web page and RSS feed.
- Add the Matrix announcement-room adapter.
- Implement delivery state, retry handling, deduplication, and withdrawal.
- Add authorization for publishers.

**Acceptance criteria:**

- An authorized publisher can publish one announcement.
- It appears on the website, RSS feed, and Matrix room.
- Failure in one adapter does not block the others.
- Retries do not create duplicate announcements.
- Updates and withdrawals behave consistently where supported.
- The public announcement archive remains available without WhatsApp.

### Milestone 5 — Optional WhatsApp compatibility

**Goal:** Allow existing WhatsApp users to access selected public information without making WhatsApp a core dependency.

Tasks:

- Evaluate the current upstream mautrix-whatsapp release and deployment requirements.
- Implement a disabled-by-default deployment profile.
- Provide a documented, isolated authentication and account-linking procedure.
- Implement an allowlisted Matrix-room mapping.
- First validate one-way public announcement publishing.
- Add optional two-way bridging for explicitly mapped rooms.
- Add bridge health checks, disconnection alerts, and an emergency disable command.
- Document privacy implications, operational risks, and account restrictions.
- Test duplicate prevention and failure recovery.

**Acceptance criteria:**

- The core stack works when the bridge is disabled or removed.
- Only explicitly configured rooms are bridged.
- Public announcements can be published independently of the bridge.
- A bridge failure is visible to the operator and does not affect Matrix.
- Logs do not expose credentials or unnecessary personal data.
- Operators can disable the bridge without deleting Matrix data.
- Documentation clearly distinguishes unofficial bridging from official WhatsApp Business integrations.

### Milestone 6 — Additional accessible channels

**Goal:** Reach people who prefer or require alternatives to smartphone messaging apps.

Tasks:

- Implement the ActivityPub adapter using a maintained upstream service where practical.
- Evaluate email and SMS delivery options.
- Design explicit opt-in and opt-out flows for subscriptions.
- Add delivery monitoring, quotas, and rate limits.
- Evaluate a voice information line only after requirements and operating costs are understood.
- Provide a lightweight, low-bandwidth public web interface.

For SMS, evaluate established gateways such as Kannel or Jasmin before building a custom gateway. Treat carrier availability and cost as operational dependencies.

**Acceptance criteria:**

- The same canonical announcement can reach multiple independently configured channels.
- Subscription preferences are scoped by topic or locality where supported.
- Opt-outs are honored.
- Unavailable channels do not interrupt other delivery.
- Costs, delivery limits, and privacy properties are documented.

### Milestone 7 — Multi-host resilience and offline continuity

**Goal:** Reduce dependence on a single server, provider, or network.

Tasks:

- Document a multi-host deployment model with independent failure domains.
- Keep off-host backups independent of the production account where possible.
- Add exportable, timestamped public announcement archives.
- Define a procedure for restoring the core services on another host.
- Evaluate offline HTML archives and local Wi-Fi distribution.
- Evaluate Meshtastic or other offline mechanisms for narrow, suitable use cases.
- Add a disaster-recovery exercise and operator checklist.

**Acceptance criteria:**

- Loss of the primary host does not destroy the only copy of public information.
- A documented recovery path exists for a second host.
- Public announcements can be exported and read offline.
- Each optional offline mechanism has a clear purpose, limitations, and maintenance owner.
- The system does not claim automatic failover unless it has actually been implemented and tested.

### Milestone 8 — Production hardening and maintainability

**Goal:** Make the project sustainable for volunteer operators.

Tasks:

- Review the threat model against the implemented architecture.
- Audit network exposure, secret handling, logs, and dependencies.
- Add update notifications and documented maintenance windows.
- Improve operational diagnostics and redaction.
- Add tested backup retention and storage monitoring.
- Create incident-response and compromised-account procedures.
- Publish a tested compatibility matrix.
- Establish a process for upgrading pinned upstream releases.
- Document governance and maintainer handover.

**Acceptance criteria:**

- A new operator can deploy and maintain the system using the documentation.
- A recovery exercise succeeds without relying on undocumented knowledge.
- Security-sensitive defaults are reviewed.
- Service ownership, maintenance requirements, and end-of-life procedures are documented.

## 11. Testing strategy

Testing must cover more than successful installation.

### Unit tests

- Configuration parsing and validation.
- Secret redaction.
- Announcement schema validation.
- Adapter idempotency.
- Retry classification and backoff.
- URL and attachment validation.
- Access-control decisions.

### Integration tests

- CLI to Ansible invocation.
- Compose configuration validation.
- Matrix service startup and health.
- Announcement delivery to a test adapter.
- Adapter failure isolation.
- Duplicate delivery prevention.
- Upgrade and rollback behavior where supported.

### Recovery tests

- Backup creation and verification.
- Restore to an isolated host.
- Recovery from missing or corrupt backup files.
- Migration to a different host.
- Verification that restore does not overwrite production.

### Manual operational drills

- Simulated WhatsApp disconnection.
- Expired or revoked bridge credentials.
- Unavailable DNS or hosting provider.
- Loss of the production host.
- Compromised administrator account.
- Recovery by a maintainer who did not perform the original deployment.

Do not require live WhatsApp credentials in CI. Test the adapter using mocks and contract tests; reserve real-account validation for an explicitly documented manual test.

## 12. Operational requirements

Provide a short, usable operator handbook covering:

- Initial deployment.
- Adding and removing services.
- Upgrades and maintenance windows.
- Backups and recovery.
- Domain and TLS troubleshooting.
- Matrix registration and moderation.
- WhatsApp bridge authentication and disconnection.
- Incident response.
- Exporting public information.
- Moving to another hosting provider.
- Removing the system and securely disposing of its data.

Include a deployment checklist and a recovery checklist. Commands must be copyable and must clearly distinguish examples from production values.

## 13. Governance and licensing

- Select a license compatible with the project's intended use and dependencies.
- Review the licenses of all bundled code and deployment assets.
- Keep third-party software licenses and attribution available.
- Avoid redistributing upstream software unnecessarily when images or packages can be pulled directly.
- Publish a security reporting process.
- Document how maintainers can rotate responsibilities and revoke access.
- Avoid a single maintainer's personal account becoming a critical dependency.

The project should remain usable if one contributor leaves.

## 14. Definition of done for the MVP

The MVP is complete when a technically competent volunteer can:

1. Clone the repository.
2. Read the prerequisites and threat model.
3. Initialize and validate configuration.
4. Deploy a public landing page, Matrix homeserver, and Element.
5. Publish a public announcement to the website, RSS, and Matrix.
6. Create and verify an off-host backup.
7. Restore the stack to an isolated environment.
8. Upgrade a service using documented commands.
9. Disable an optional integration without disrupting the core.
10. Follow the recovery guide on a clean host.

The WhatsApp bridge is an important early extension, but it must not delay the delivery of a working, recoverable core.

---

## 15. Instructions for the AI coding agent

Treat this document as the architectural baseline.

### Working method

1. **Inspect before editing.** Read the repository, existing documentation, tests, and deployment scripts before proposing changes.
2. **Plan before implementation.** Produce a concise repository assessment and implementation plan for Milestone 0.
3. **Work incrementally.** Implement one milestone at a time. Do not build future modules merely to make the architecture look complete.
4. **Reuse upstream software.** Prefer documented deployment mechanisms and maintained upstream images. Do not reimplement application protocols.
5. **Keep changes reviewable.** Separate infrastructure, CLI, adapters, documentation, and tests into coherent changes.
6. **Test every milestone.** Run relevant tests and report which tests were run, which passed, and which require a real host or credentials.
7. **Document decisions.** If a requirement conflicts with upstream capabilities, explain the conflict and propose a bounded alternative.
8. **Do not silently weaken security.** Never disable authentication, TLS verification, firewall protections, or backup encryption merely to make a deployment pass.
9. **Do not invent successful tests.** Clearly distinguish automated tests, manual tests, mocks, and unverified assumptions.
10. **Preserve user data.** Never perform destructive deployment, migration, or restore operations without explicit authorization.

### Required output before coding

Return:

- A summary of the repository's current state.
- The proposed initial architecture and any changes needed from this document.
- A list of unresolved decisions and risks.
- A milestone plan with concrete files and tests.
- The smallest useful first implementation.

Then begin Milestone 1 once the plan is approved or the available project instructions clearly authorize implementation.

### Final instruction

Optimize for **a small, understandable, recoverable system that a community can actually operate**, not for the largest feature set or the most sophisticated architecture. The public announcement stream and the independent Matrix service must remain functional even if every external channel adapter is unavailable.
