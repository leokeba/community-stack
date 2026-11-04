# Safe Mode: design and limits

Status: implemented, not yet deployed or verified against a live homeserver. Member and operator guide: [`docs/safe-mode.md`](safe-mode.md).

Safe Mode temporarily removes a person's Matrix membership in designated protected rooms,
across all their devices, and restores it later after an independent confirmation.

## What is protected, and what is not

Server-side access is suspended: the account is removed from protected rooms and cannot
read new events there. This does not erase anything already on a device. Messages and
media downloaded earlier, and any encryption keys already held, stay on the phone.
Safe Mode is not protection against inspection of an unlocked phone. The local-data part
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
- Room creation and ownership: nothing in the repo creates protected rooms yet.

## Matrix findings (verified upstream, 2026-11)

Source: the Room Version 12 specification, <https://spec.matrix.org/v1.16/rooms/v12/>, and
the creator power-level guide, <https://matrix.org/docs/spec-guides/creator-power-level/>.

- In room version 12 the creator has infinite power level. It cannot be set in
  `m.room.power_levels`, cannot be changed after creation, and cannot be demoted.
- Kick is allowed only when the sender's power level is at least the kick level and the
  target's is lower than the sender's. A creator's infinite level therefore cannot be
  kicked by someone else. The spec does not state "creators cannot be kicked" in those
  words; this is an inference from the rule, and it should be confirmed with a live test.
- Consequence: a protected room created by a member cannot be managed by the service
  account. Protected rooms must be created by the service account. Rooms created by a
  person are refused, and the code reports this as a compatibility problem.
- Version 12 adds `additional_creators`. Not used here.

Not yet verified (must be tested on a live homeserver before enabling):

- History visibility `joined` behaviour: that a user who rejoins cannot fetch events from
  an earlier membership period through `/messages` or `/context`.
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
  - Activation writes the state and the membership snapshot in one transaction before any
    kick. Retries never overwrite the snapshot.
  - Each room is recorded as `pending`, `done` or `failed`. The account is `SUSPENDED`
    only when every room is `done`, and `PARTIAL` otherwise.
  - Restore needs a one-time recovery code, which is consumed before any Matrix call.
    Five wrong codes lock restore until an administrator issues a new code.
  - Restore re-invites only rooms the account was joined to before suspension. Invited but
    never-joined rooms are not restored. Disabled rooms are skipped.
  - The account returns to `ACTIVE` only when every restore has succeeded. Otherwise it stays
    `RESTORING`, and `reconcile()` finishes it after a restart.
  - Replies never include room names or other people's state.

Tests: `tests/unit/test_safemode.py` (36 tests, fake gateway). The full suite passes
(748 passed, 8 skipped).

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
snapshot of the bot database, and tests (84 Safe Mode tests; the full suite passes).

Not verified here: the Docker image has not been built, and nothing has run against a real
Synapse. Docker was not running on the development machine. The items under
[Verification still required](safe-mode.md#verification-still-required) must be tested on a
staging server before anyone relies on Safe Mode.

Also built: an admin-console page at `/admin/safe-mode`, backed by eight control-agent operations
(`safemode.*`). Changes are limited to the system permission, and administrative restore needs
the admin's password.

Not built, and why:

- **Second-device confirmation for `!safe off`.** The bot's direct chats are not end-to-end
  encrypted, so a challenge sent there is visible on the phone under inspection. Independent
  confirmation needs a bot that takes part in device verification and encrypted rooms. That is
  a separate project with its own design, so the one-time recovery code remains the second factor.
- **Element local-storage research (Phase 4).** Selective cache or key deletion is not verified
  for either client, so it is not claimed. It does not block server-side Safe Mode.
