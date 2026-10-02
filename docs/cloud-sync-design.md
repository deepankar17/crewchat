# Cloud sync: design for review

Status: **built in 0.3.0.** Written 2 October 2026 for the owner's review; decisions and
changes made while building are in section 15.

## 1. What this adds

Today one machine hosts crewchat and other machines reach it through Tailscale. This design
removes the host:

- Every machine runs its own crewchat server for its local agents.
- Servers that are signed in to the **same Google account** find each other and exchange
  messages through Firestore, pushed in real time.
- Messages are encrypted on the machine before they are sent. Firestore, and whoever runs the
  Firebase project, sees only scrambled data.
- A machine that is off or offline only takes its own agents out. The others carry on.

What does not change: agents still talk only to the server on their own machine, over MCP. The
hooks, the one-time link and listen mode work exactly as now. Cloud sync changes how machines
reach each other, not how an agent wakes up.

Single-machine use needs no account and no cloud, and stays one file with no dependencies.
Tailscale mode keeps working for people who prefer nothing to leave their devices.

## 2. What the experiment proved

Run against the new `crewchat-ae908` project, as a normal signed-in user:

| Question | Result |
|---|---|
| Can a command-line program sign in with Google? | Yes: browser consent once, then silent renewal |
| Can Python hold a live listener as a user, with no polling? | Yes: opened in 0.4 s, documents arrived about 0.5 s after being written |
| Do the security rules confine an account to its own data? | Yes: reads and writes elsewhere were refused |

Two costs showed up: Google's Firestore library is about 70 MB with its dependencies, and it no
longer supports Python 3.9.

## 3. The picture

```
 Machine A                                              Machine B
 ┌────────────────────────┐                            ┌────────────────────────┐
 │ agents ── MCP ── server│── encrypt ─▶ Firestore ─push─▶ decrypt ──│server ── MCP ── agents│
 │           (local)      │◀─ push ───  (ciphertext) ◀─ encrypt ─────│ (local)               │
 └────────────────────────┘     only this Google account can        └────────────────────────┘
                                read or write its own area
```

Servers never connect to each other. Each one only talks to Firestore, so no machine needs a
public address.

## 4. Signing in and devices

- `crewchat cloud login` opens the browser for Google sign-in, once per machine. The sign-in
  renews itself afterwards.
- Each machine is a **device** with a name (default: the machine's name, as used in agent names)
  and its own RSA key pair, created on first login. The private key never leaves the machine.
- **The first device** on an account creates the account's message key (section 6).
- **Every later device must be approved** by one that is already trusted. The new device shows a
  short fingerprint; `crewchat cloud devices` on a trusted machine shows the same fingerprint and
  asks for confirmation. Only then does the new device receive the message key.
- `crewchat cloud remove DEVICE` drops a device and changes the message key so it can read
  nothing new.

Why approval matters: all of an account's devices sign in as the same user, so Firestore's rules
cannot tell them apart, and the project's operator can bypass rules entirely. Approval is
therefore enforced by cryptography on the machines: an approval is a signature by a trusted
device over the new device's public key, and each machine keeps its own list of the devices it
trusts. A key that appears in Firestore without a valid signature is ignored.

## 5. What is stored in Firestore

Everything lives under the account's own ID:

| Path | Contents | In the clear? |
|---|---|---|
| `users/{uid}/devices/{deviceId}` | Device name, public key, the approval signature, the account key wrapped for this device, last-seen time | Names and public keys are readable; the wrapped key is not usable without the device's private key |
| `users/{uid}/messages/{id}` | Sending device, key ID, nonce, ciphertext, server timestamp | Only the envelope; the content is encrypted |
| `users/{uid}/tasks/{taskMessageId}` | Which device took the task, encrypted agent name | Device ID only |
| `users/{uid}/state/{deviceId}` | That device's roster, statuses and read positions, encrypted | No |

Encrypted content includes who a message is from and to, its text, agent names and status lines.
What Firestore can see: that an account has N devices, when they are active, and how many
messages they exchange.

**Firestore is a transit queue, not an archive.** History stays in each machine's local store.
A message is deleted from Firestore once every trusted device has received it, and in any case
after 24 hours (`cloud_ttl_hours`). A machine that was off for longer than that misses those
messages.

## 6. Encryption

One **account key** (AES-256) per account, shared by its trusted devices.

- **Wrapping:** the account key is stored once per device, encrypted with that device's RSA
  public key (RSA-3072, OAEP with SHA-256). A new device gets its copy when it is approved.
- **Messages:** AES-256-GCM with a fresh random nonce for every message. The account ID, message
  ID, key ID and sending device are bound into the encryption, so a stored message cannot be
  altered, moved or replayed as a different one.
- **Key ID:** a short fingerprint of the key travels with each message. A device that does not
  have that key asks for it to be wrapped again instead of failing silently. This is the
  "hash and owner of the key" from the original proposal.
- **Rotation:** removing a device creates a new account key, wrapped for the remaining devices.
  Old keys are kept locally only as long as messages encrypted with them may still arrive.
- **Approvals:** signed with the approving device's RSA key (PSS with SHA-256).
- **Implementation:** all primitives come from the `cryptography` library. No hand-written
  cipher code.

What this protects against: anyone reading the database, including the project's operator, and
anyone tampering with stored messages or slipping in a device. What it does not protect against:
a machine that is itself compromised (it holds a private key and the account key), and the limit
noted above on forward secrecy, which deleting delivered messages keeps small.

## 7. How a message travels

1. An agent on machine A calls `hub_send`. A's server delivers it at once to A's local agents
   and stores it locally.
2. A's server encrypts it and writes it to Firestore. If A is offline it queues and sends later.
3. Firestore pushes it to every other signed-in server on the account. Each decrypts it, stores
   it locally and hands it to its own agents the usual way (inbox, hooks).
4. Each server records what it has received. Once all trusted devices have it, any of them
   deletes it from Firestore.

**Message numbers.** Today one server counts `#1, #2, #3`. With several servers and no leader,
each device numbers its own messages with a short device tag: `A12`, `B7`. They are unique
without coordination, work offline, and are short enough for agents to write `BID A12: yes`.
The chat is ordered by Firestore's server timestamp.

## 8. Shared state without a leader

No server is primary. Each kind of state has one clear owner:

| State | Who decides | How |
|---|---|---|
| Messages | Nobody; they only ever get added | Merge by ID, order by server time |
| An agent's name, status, read position | The server on that agent's machine | Published to the others; the machine label keeps names unique |
| Who took a task | Firestore | Creating `tasks/{id}` succeeds for exactly one server; the others are told it is taken |
| Which devices are trusted | Signatures, checked on every machine | Section 4 |

**Offline behaviour.** Agents on one machine keep talking to each other with no internet.
Messages queue and sync when the connection returns. Taking a task that came from another
machine needs to be online, because Firestore is what makes "only one taker" true.

## 9. Presence and quota

Firestore has no "this device disconnected" signal, so presence is written by each server:

- when it starts, stops, or an agent's status or roster changes;
- otherwise a heartbeat every 15 minutes, and only while it has active agents.

A server that has not written for 25 minutes is shown as offline.

Rough daily use for one person with three machines and 300 messages: about 300 message writes,
600 reads, 300 deletes and 100 presence writes. The free plan allows 20,000 writes and 50,000
reads a day for the whole project. That covers roughly 30 to 50 active people; beyond that the
pay-as-you-go plan costs on the order of a few hundred rupees a month for 100 people.

## 10. Security rules

Already published on `crewchat-ae908`:

```
match /users/{uid}/{document=**} {
  allow read, write: if request.auth != null && request.auth.uid == uid;
}
```

To add: size limits and required fields on message documents, so one account cannot fill the
database, and no listing of other accounts. The rules get their own tests, run against a
separate test project.

## 11. The owner's chat page

Unchanged at first: any machine's server serves the page, signed in with a one-time code. The
owner's messages are synced like everyone else's. Because every machine has the full local
history, the page works from whichever machine is on.

Later, optionally: a hosted page that reads Firestore directly, so the phone works with no
machine switched on. That needs the browser to hold the account key, so it is a separate piece
of work.

## 12. Packaging

- `crewchat.py` stays one dependency-free file for single-machine and Tailscale use, on
  Python 3.9+.
- Cloud sync is a second file, `crewchat_cloud.py`, loaded only when cloud mode is on.
  It needs Python 3.10+ and two libraries: `google-cloud-firestore` and `cryptography`
  (`pip install "crewchat[cloud]"`).
- Start-at-login gains Windows support (Task Scheduler), since every machine now runs a server.

## 13. Build order

1. **Separate the server from its storage and transport.** No behaviour change; all current
   tests keep passing. Needed for everything else.
2. **Sign-in, device keys, approval.** `crewchat cloud login`, `devices`, `approve`, `remove`.
3. **Message sync with encryption**, the offline queue and delete-after-delivery.
4. **Roster, status, read positions and presence.**
5. **Tasks through Firestore.**
6. **Stricter rules and rule tests; Windows start-at-login; documentation.**

Testing: unit tests run two or three servers in one process against an in-memory stand-in for
Firestore, including offline and reconnect cases. A smaller set runs against a real test project.

## 14. Decisions needed from the owner

1. **Whose Firebase project do users connect to?**
   - *Yours, built in as the default.* Easiest for users: install, sign in, done. You become the
     operator: you carry the quota and any bill, and need a short privacy note and terms.
     Because of the encryption you cannot read anyone's messages.
   - *Each user's own.* Nothing for you to run, but every user repeats the console setup done
     for this experiment.
   - The design supports both; this decides the default.
2. **Is 72 hours the right limit** for a machine to be off before it misses messages?
3. **Should the hosted phone page be part of this work or a later step?** Recommended: later.

## 15. Decisions and changes while building

- **Whose project:** each operator sets up their own Firebase project.
  [firebase-setup.md](firebase-setup.md) covers it by hand and as a prompt for an agent.
- **Retention:** 24 hours instead of 72, configurable with `cloud_ttl_hours` in config.json.
  Shorter would lose messages for a laptop closed overnight.
- **Hosted phone page:** later.
- **No Firestore transactions.** The live test showed that an ordinary signed-in user may not
  begin a transaction. Updates to the trusted-device list use a conditional write instead (write
  only if the document is unchanged since it was read; retry otherwise), which gives the same
  guarantee. Task claims use a create that fails if the document exists.
- **Trusted list carries public keys.** Each machine verifies list updates against the keys it
  already trusts, not against keys read from Firestore.
