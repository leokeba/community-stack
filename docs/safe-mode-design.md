# Safe Mode: design and limits

Status: implemented and tested against a local Synapse 1.163.2 (2026-11-10); not yet deployed. Member and operator guide: [`docs/safe-mode.md`](safe-mode.md).

Safe Mode temporarily removes a person's Matrix membership in designated protected rooms,
across all their devices, and restores it later after an independent confirmation.

## What is protected, and what is not

The account is removed from protected rooms and cannot read events sent while it is out,
including after it rejoins. It **can** still fetch events from before its removal: Synapse
lets a user who left read history up to their leave event (verified, see below). Nothing on a
device is erased, and encryption keys already held, or in key backup, still work.
Safe Mode is not protection against inspection of an unlocked, signed-in phone. The local-data part
(Phase 4) is unverified and is not claimed anywhere.

## Audit findings (repository)

- Synapse runs as `community_synapse` (`services/matrix/docker-compose.yml`), pinned to
  `v1.163.2`, with Postgres. Its config comes from `services/matrix/homeserver.template.yaml`.
  Registration is off by default.
- Bridges already run as Synapse application services (`docs/matrix-bridges.md`,
  `cli/community_stack/bridges/catalogue.py`). Each has its own bot localpart and a
  registration file under `data/<bridge>/`.
- The control agent (`cli/community_stack/agent/matrix_admin.py`) calls the Synapse admin
  API from inside the Synapse container. The admin API is never exposed. Safe Mode should
  not reuse that admin account: the plan forbids unrestricted homeserver admin for the bot.
- The project has no Matrix SDK dependency. `httpx` is a dev-only dependency. Adding
  `mautrix-python` would be a new dependency and needs a decision (see below).
- Room creation and ownership: `access create-room` creates protected rooms as the bot.

## Matrix findings (verified upstream, 2026-11)

Source: the Room Version 12 specification, <https://spec.matrix.org/v1.16/rooms/v12/>, and
the creator power-level guide, <https://matrix.org/docs/spec-guides/creator-power-level/>.

- In room version 12 the creator has infinite power level. It cannot be set in
  `m.room.power_levels`, cannot be changed after creation, and cannot be demoted.
- Kick is allowed only when the sender's power level is at least the kick level and the
  target's is lower than the sender's. A creator's infinite level therefore cannot be
  kicked by someone else. Confirmed on Synapse 1.163.2: a power-level-100 member gets
  `M_FORBIDDEN` kicking the creator bot, and the bot can kick that member.
- Consequence: a protected room created by a member cannot be managed by the service
  account. Protected rooms must be created by the service account. Rooms created by a
  person are refused, and the code reports this as a compatibility problem.
- Version 12 adds `additional_creators`. Not used here.

Verified on Synapse 1.163.2 with `joined` history (2026-11-10):

- A kicked user can still `/messages` events from before the kick, and `/members`.
- Events sent while the user was out stay invisible to them after they rejoin; events from
  their earlier membership are visible again.
- After the user calls `/forget`, `/messages` returns `M_FORBIDDEN`. Forgetting needs the
  user's own access token; Synapse has no admin endpoint for it. This is the only server
  mechanism found that hides pre-suspension history from a signed-in device. Using it would
  need the bot to act as members (a non-exclusive user namespace, or the admin login API),
  which the "no unrestricted admin" rule excludes for now.
- Another member can re-invite a suspended account and it can rejoin. The bot now removes it
  again on the membership event (measured: well under a second), but it is briefly back in.

Not yet verified:

- Megolm key behaviour: whether a kicked member's already-received session keys are still
  usable, and whether the room rotates keys after a leave. Element's behaviour here is not
  established, and this design does not claim any key-revocation guarantee.
- Element X and Element Web key backup and cross-signing behaviour on restore.

## Implemented so far

`cli/community_stack/safemode/`:

- `commands.py`: parses `!safe on|off [code]|status|help`. Only the body and event shape
  are read; the sender comes from the authenticated event. Replies (`m.relates_to`),
  quoted lines (`>`) and forwarded content are refused, so an old command cannot be
  replayed. Recovery codes are redacted in `repr`.
- `store.py`: SQLite. Tables: protected rooms, enrolled accounts, per-account state,
  membership snapshot, salted recovery-code hashes. No activation timestamps.
- `manager.py`: the Access Manager. It talks to Matrix only through a `RoomGateway`
  protocol.
  - Commands run in two steps. `submit` records the change in one `BEGIN IMMEDIATE`
    transaction (state, a new operation id, snapshot rows, spent code) without calling Matrix;
    the bot does this before acknowledging the homeserver transaction. `drive` does the Matrix
    work, one drive at a time per account, on a worker pool. Writes made by a drive apply only
    while its operation id is current, so a newer command always wins.
  - Until 2026-11-10 the store ran in SQLite autocommit mode, so its "transactions" committed
    statement by statement. All writes now go through `Store.transaction()`.
  - Activation adds every protected room to the snapshot as `unknown` and records the real
    membership just before the kick. Retries never overwrite a known value.
  - A room whose membership cannot be read stays outstanding, and the account `PARTIAL`,
    until a later attempt reads it.
  - Each room is recorded as `pending`, `done` or `failed`. The account is `SUSPENDED`
    only when every room is `done`, and `PARTIAL` otherwise.
  - Restore needs a one-time recovery code, which is consumed before any Matrix call. Only the
    code that was checked is spent; one reissued in between is left alone.
    Five wrong codes lock restore until an administrator issues a new code.
  - Restore re-invites only rooms the account was joined to before suspension. Invited but
    never-joined rooms are not restored. Disabled rooms are skipped.
  - The account returns to `ACTIVE` only when every restore has succeeded. Otherwise it stays
    `RESTORING`, and `reconcile()` finishes it after a restart.
  - `!safe on` always works, including during an unfinished restore: it suspends the account
    again from the original snapshot. The spent code is not revived; the reply says a new one
    is needed.
  - While an account is suspended, a membership event inviting or joining it to a protected
    room puts that room back on its outstanding list and the bot removes it again.
  - Administrative restore is recorded by the operator command, then carried out by the
    running bot (`POST /_safemode/v1/drive`, homeserver-token authenticated), so every
    membership change for an account comes from one process.
  - The host tools open the database with `mode=rw` and never create it; the control agent
    runs as root and a root-owned file would lock the bot out.
  - Replies never include room names or other people's state.


## Decisions

1. **Bot transport:** an application service, the same pattern as the mautrix bridges. It
   needs no sync loop and no stored bot password. It uses only the standard library, so no
   new dependency was added.
2. **Confirmation for `!safe off`:** a one-time recovery code, issued by an administrator and
   stored separately by the member. Confirmation from a second trusted device needs key
   verification and is not built.
3. **Who creates protected rooms:** the service account only. The bot and the CLI refuse
   rooms created by a person.
4. **Administrative restore:** `access restore --confirm` exists for when a member cannot use
   their code. It is explicit, and it invalidates any outstanding code.
5. **Activation offline:** not solved. The bot cannot receive a command without connectivity.
   The status text says suspension is not confirmed until it is.

## Built, and what is still to verify

Built: the bot (appservice) with a Synapse gateway, the operator commands and host CLI,
provisioning into `install`, the Dockerfile and compose service on `bridge_net`, a backup
snapshot of the bot database, and unit tests with fake gateways.

The image was built and run against a local Synapse 1.163.2; what that showed is listed under
[Verification](safe-mode.md#verification). Client behaviour (Element X direct chats, key
backup after restore) is still untested.

Also built: an admin-console page at `/admin/safe-mode`, backed by nine control-agent operations
(`safemode.*`). Changes are limited to the system permission, and administrative restore needs
the admin's password.

Not built, and why:

- **Second-device confirmation for `!safe off`.** The bot's direct chats are not end-to-end
  encrypted, so a challenge sent there is visible on the phone under inspection. Independent
  confirmation needs a bot that takes part in device verification and encrypted rooms. That is
  a separate project with its own design, so the one-time recovery code remains the second factor.
- **Element local-storage research (Phase 4).** Selective cache or key deletion is not verified
  for either client, so it is not claimed. It does not block server-side Safe Mode.
