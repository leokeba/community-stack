# Safe Mode

Safe Mode lets a member temporarily remove their own access to designated sensitive Matrix
rooms, for example before a protest or before entering a place where a phone may be
searched. It works from an ordinary Matrix client, including Element X, with no custom app.

Read the limits first. Safe Mode stops **server-side** access. It does not erase anything
already on a device.

## What it does and does not protect

| | Protected | Not protected |
|---|---|---|
| New events in protected rooms | Yes, once Safe Mode is on and confirmed | |
| Access from every device on the account | Yes, the membership is removed for the account | |
| Messages and media already downloaded to a phone | | Still there. Clear them in the client yourself. |
| Encryption keys already held on a device | | Still usable. No key revocation is claimed. |
| Anyone who can unlock the phone and read it | | Safe Mode does not stop this. |
| Normal community rooms | Unaffected | |

Historical messages: after a restore the member receives new messages. Whether a member can
read earlier history depends on the room's history setting (`joined`) and on the keys their
devices already hold. Community Stack does not promise that earlier messages stay hidden.

Before relying on Safe Mode, test the room behaviour on your own server (see
[Verification still required](#verification-still-required)).

## Using it (members)

Open a direct chat with the Safe Mode bot. Send these as plain messages:

```text
!safe on              suspend access to protected rooms
!safe status          show your state (no room names)
!safe off <code>      restore access, with your recovery code
!safe help            show the commands
```

- `!safe on` removes your membership in each protected room. The reply says whether all of
  it worked. If the reply says PARTIAL, some rooms are not yet suspended or could not be
  checked: send `!safe on` again, or ask an administrator. `!safe on` also works while access
  is still being restored.
- `!safe off` needs your **recovery code**. An administrator issues it once and you keep it
  separately from your phone, for example on paper. The bot does not accept a turn-off
  without it.
- Replies and quoted or forwarded messages are refused, so copying an old command does not
  repeat it.
- The bot does not answer accounts that are not enrolled.

**Offline:** the bot can only act when your phone can reach the server. Until the reply
confirms the change, do not assume access has been suspended.

Messages you send to the bot are stored in your Matrix conversation with it. Do not send a
recovery code you have not yet kept safely, and remember that a used code is spent.

## Setting it up (administrators)

1. Enable the service in `community-stack.yaml`:

   ```yaml
   services:
     safemode:
       enabled: true
   ```

2. Run `uv run community-stack install`. This creates the bot's tokens in `.env`, its
   configuration under `data/safemode/`, and the Synapse appservice registration.

3. Create each protected room **through the service account**, not a person. Then invite
   the people who need it. Make the room:
   - end-to-end encrypted,
   - invite-only,
   - with history visibility set to `joined`,
   - with the service account able to invite and remove members.

   Room version 12 gives a room creator infinite power, so a room created by a person cannot
   be managed by the bot. The bot refuses such rooms.

4. Protect the room:

   ```bash
   uv run community-stack access protect '!roomid:example.org' --name "Organising group"
   uv run community-stack access rooms
   uv run community-stack access doctor       # checks every protected room now
   ```

5. Enrol a member and issue their recovery code:

   ```bash
   uv run community-stack access enroll @member:example.org
   uv run community-stack access recovery-code @member:example.org   # shown once
   ```

   Give the code to the member in person or by another channel they trust. Keep no copy in
   the chat.

The same tasks are on the admin console at **Safe Mode** (under People, for accounts with the system permission). The console changes Safe Mode only through the control agent, which runs the same checks and records the same audit entries. Recovery codes are shown once on that page.

Other commands:

```bash
uv run community-stack access status @member:example.org   # state and counts, no room names
uv run community-stack access unprotect '!roomid:example.org'
uv run community-stack access restore @member:example.org --confirm   # administrative restore
uv run community-stack access bot                          # is the bot container running?
uv run community-stack access logs --lines 200
```

`access restore --confirm` restores an account without its recovery code. Use it only when
the member cannot use their code and you have verified who they are. It is logged as an
action with no message content.

## How it works

- The bot is a Matrix application service. Synapse pushes events to it; it does not sync.
  It acts only in direct chats with exactly two members, and only for enrolled accounts.
- The sender comes from the event Synapse delivers, never from message text.
- On `!safe on` the bot records each account's current protected memberships **before**
  removing any. Retries never overwrite that record. If the service restarts midway, it
  finishes the job when it starts again.
- On `!safe off` the bot checks the recovery code, marks it spent, and re-invites the member
  only to rooms they had joined. Invitations they never accepted are not restored. The member
  must accept each invitation in their client.
- The bot never logs message content. Its database holds only operational state: the
  protected-room registry, enrolled accounts, state, the membership snapshot and hashed
  recovery codes. It keeps no activation timestamps.
- The bot's database is part of the encrypted backup. The bot holds no homeserver admin
  credentials.

## Verification still required

These were not verified against a running server when this was written. Test them on your
own homeserver before you tell members that Safe Mode works:

- The bot can kick members from a room that a service account created on your Synapse
  version, and that a person cannot remove the service account from it.
- After a member rejoins, they cannot read earlier messages through normal history requests.
- Element X and Element Web behave as expected for key backup and cross-signing after a
  restore.
- Turning Safe Mode on and off works from Element X with the bot in a direct chat.

Element local-storage behaviour (clearing a room's cache or keys) is out of scope for this
version. Do not describe it as possible unless it has been tested on the devices you support.

## Related

- Design and decisions: [`docs/safe-mode-design.md`](safe-mode-design.md)
- Bridge architecture, which uses the same appservice pattern: [`docs/matrix-bridges.md`](matrix-bridges.md)
