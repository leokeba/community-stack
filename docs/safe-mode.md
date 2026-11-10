# Safe Mode

Safe Mode lets a member temporarily remove their own access to designated sensitive Matrix
rooms, for example before a protest or before entering a place where a phone may be
searched. It works from an ordinary Matrix client, in a direct chat with the Safe Mode bot,
with no custom app. The chat must not be end-to-end encrypted: the bot cannot read
encrypted messages (see [Clients](#clients)).

Read the limits first. Safe Mode stops a suspended account from reading **new** messages in
protected rooms. It does not erase anything, and a phone that stays signed in can still
fetch the room's earlier messages from the server.

## What it does and does not protect

| | Protected | Not protected |
|---|---|---|
| Messages sent in protected rooms while Safe Mode is on | Yes, hidden from the account, also after a restore | |
| Access from every device on the account | Yes, the membership is removed for the account | |
| Being let back in while Safe Mode is on | Yes, the bot removes the account again within about a second | |
| Messages sent **before** Safe Mode was turned on | | Still fetchable from the server by any device that stays signed in. Decrypting them needs keys the device holds or can get from key backup. |
| Messages and media already downloaded to a phone | | Still there. Clear them in the client yourself. |
| The room list, member list up to the removal, and the chat with the bot | | Visible on a signed-in phone. Room members see "removed by safemode: Safe Mode". |
| Anyone who can unlock the phone and read it | | Safe Mode does not stop this. |
| Normal community rooms | Unaffected | |

The second row from the top is the one that matters most: if a phone may be inspected while
it is still signed in, signing that session out (from another device, or by an
administrator) protects far more than Safe Mode does. Removing room membership was verified
not to block reading earlier history (Synapse 1.163.2, 2026-11-10).

## Using it (members)

Open a direct chat with the Safe Mode bot, with encryption off. Send these as plain messages:

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
  without it. After a restore you rejoin by accepting the invitations; messages sent while
  Safe Mode was on stay hidden from you.
- Replies and quoted or forwarded messages are refused, so copying an old command does not
  repeat it.
- The bot does not answer accounts that are not enrolled.

**Offline:** the bot can only act when your phone can reach the server. Until the reply
confirms the change, do not assume access has been suspended.

Messages you send to the bot are stored in your Matrix conversation with it. Do not send a
recovery code you have not yet kept safely, and remember that a used code is spent.

### Clients

The bot has no encryption keys. If the direct chat is encrypted, it replies that it cannot
read your message and that Safe Mode has **not** changed. Which clients can start an
unencrypted direct chat has not been tested here (Element Web is expected to when the other
account has no encryption keys, as the bot has none; Element X may always encrypt). Check on
the clients your members use before relying on Safe Mode.

## Setting it up (administrators)

1. Enable the service in `community-stack.yaml`:

   ```yaml
   services:
     safemode:
       enabled: true
   ```

2. Run `uv run community-stack install`. This creates the bot's tokens in `.env`, its
   configuration under `data/safemode/`, and the Synapse appservice registration. The bot
   creates its database when it first starts; the `access` commands and the console only
   work after that.

3. Create each protected room through the bot, inviting the people who need it:

   ```bash
   uv run community-stack access create-room --name "Organising group" \
       --invite @organiser:example.org --invite @member:example.org
   uv run community-stack access rooms
   uv run community-stack access doctor       # checks every protected room now
   ```

   The room is end-to-end encrypted, invite-only, with `joined` history and no guest access,
   and the bot is its creator. In room version 12 a creator cannot be removed, so the bot
   can always remove and re-invite members. Members can invite others. A room created by a
   person cannot be protected, because the bot could not manage it; `access protect
   '<room id>' --name …` only accepts rooms the bot created.

4. Enrol a member and issue their recovery code:

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
- A command is recorded in the bot's database before Synapse is told it was received; the
  removals, invitations and the reply follow on worker threads. A recorded `!safe on` is
  finished even if the bot restarts before replying.
- On `!safe on` the bot records each account's protected memberships **before** removing
  any. Retries never overwrite that record. A room it cannot read is retried, never skipped.
- On `!safe off` the bot checks the recovery code, marks it spent, and re-invites the member
  only to rooms they had joined. Invitations they never accepted are not restored. The member
  must accept each invitation in their client.
- The newest command always wins: `!safe on` stops an unfinished restore, and the
  administrative restore is carried out by the bot itself, so two changes to one account
  never run at once.
- While an account is suspended, the bot removes it again from any protected room it is
  invited to or joins.
- The bot never logs message content. Its database holds only operational state: the
  protected-room registry, enrolled accounts, state, the membership snapshot and hashed
  recovery codes. It keeps no activation timestamps.
- The bot's database is part of the encrypted backup. The bot holds no homeserver admin
  credentials.

## Verification

Verified on a test homeserver (Synapse 1.163.2, room version 12, 2026-11-10), with the bot
image built from this repository:

- The bot creates protected rooms, and a member with power level 100 cannot remove it; the
  bot can remove that member.
- `!safe on` and `!safe off` work end to end in an unencrypted direct chat; the reply arrives
  within a second.
- A suspended account cannot read messages sent while it is out, also after it rejoins.
  It **can** still read earlier messages from the server, before and after a restore.
- A suspended account re-invited by another member is removed again.
- Administrative restore re-invites the member, and the rejoin is left alone.
- In an encrypted direct chat the bot cannot read commands, and says so.

Still to verify on the clients your members use:

- Whether Element X can start an unencrypted direct chat with the bot.
- Element X and Element Web behaviour for key backup and cross-signing after a restore.

Element local-storage behaviour (clearing a room's cache or keys) is out of scope for this
version. Do not describe it as possible unless it has been tested on the devices you support.

## Related

- Design and decisions: [`docs/safe-mode-design.md`](safe-mode-design.md)
- Bridge architecture, which uses the same appservice pattern: [`docs/matrix-bridges.md`](matrix-bridges.md)
