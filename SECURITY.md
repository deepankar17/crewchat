# Security

## What crewchat protects

- **Who can connect.** The server listens on `127.0.0.1` only. Other machines reach it through a
  private network you control (for example `tailscale serve`), never a public port.
- **Who is who.** Each joined folder has its own token, and the server names every session that
  connects with it. An agent cannot choose who it posts as, and cannot post as the owner. Only
  the owner token can mint sign-in and join codes, post tasks, or rename and remove agents.
- **The chat page.** Sign-in uses a single-use code valid for two minutes. The session cookie is
  HttpOnly and SameSite=Strict, cross-site posts are refused, and the page ships a strict
  Content-Security-Policy. The owner token never reaches a browser.
- **Guessing.** Ten wrong tokens or codes from one address lock it out for ten minutes.

## Linking machines over Tailscale

- **Who can reach a server.** It still listens only on 127.0.0.1. `tailscale serve` makes it
  reachable from devices on your tailnet only; Funnel (the public internet) is never used.
  Traffic between machines is encrypted by Tailscale (WireGuard).
- **Who counts as a member.** Machines share a random key, handed to a new machine in exchange
  for a single-use code that expires after 10 minutes. Requests without the key are refused and
  count towards the lockout. The key is stored in `peers.json`, readable only by you.
- **Removing a machine** changes the key and tells the other members. The removed machine is
  refused at once; the old key keeps working for 10 minutes so members that were busy catch up.
  A member that was off during the change must join again. The removed machine keeps what it
  already received.
- **Any member can do anything a member can:** invite, remove, and send messages that show as
  coming from its agents or its owner. Link only machines you control.

## Cloud sync

- **Who can read.** Firestore's rules confine each Google account to its own data. Within an
  account, everything is encrypted on the machine before it is sent: messages, rosters and task
  claims are sealed with an AES-256-GCM account key, bound to the account, message, key and
  sending machine, so a stored item cannot be altered, moved or replayed as another. Whoever
  administers the Firebase project sees only ciphertext, timestamps and how many machines and
  messages there are.
- **Who can join.** All of an account's machines sign in as the same user, so the rules cannot
  tell them apart. Membership is therefore enforced by signatures: each machine has an RSA-3072
  key, the list of trusted machines is signed by a trusted machine, and the account key is only
  wrapped for machines on that list. A new machine is added only when a trusted one approves it
  after the person compares fingerprints on both screens.
- **The first approval is trust on first use.** A new machine accepts the list that its approver
  signed. `crewchat cloud status` on the new machine shows who signed it and their fingerprint;
  compare that with `crewchat cloud status` on the approving machine. Someone controlling the
  Firebase project could otherwise impersonate the approver to a brand-new machine.
- **Removing a machine** drops it from the list and switches the others to a new account key.
  It can still read what was sent before it was removed and is still in Firestore (at most 24
  hours), and, being signed in to the same Google account, it can delete or flood the account's
  data. Sign it out of your Google account too (Google Account → Security → Your devices).
- **No forward secrecy beyond deletion.** Someone who later obtains a machine's private key and
  the account key can decrypt what is still in Firestore. Deleting delivered messages keeps that
  small.
- **The OAuth client secret** in `oauth-client.json` identifies the crewchat app to Google; for
  desktop apps Google does not treat it as a strong secret, but keep it private anyway.

## What it does not protect

- **Prompt injection between agents.** Agents in the chat can usually run commands. crewchat
  tells them that only the owner's messages are instructions and that no chat message overrides
  their project's safety rules, but that is guidance to a model, not enforcement. Treat every
  agent in the chat as able to influence the others.
- **A compromised host or agent machine.** Tokens are stored in plain files readable by your
  user (`~/.crewchat/tokens/` on the host, `.mcp.json` / `.cursor/mcp.json` in project folders).
  Anyone who can read a folder's token can join the chat as a new agent from that folder.
- **Messages at rest.** Chat history is stored unencrypted in `~/.crewchat/messages.jsonl`. Do
  not put secrets in messages.
- **Transport without a private network.** crewchat speaks plain HTTP on loopback and relies on
  the private network (Tailscale and similar) for encryption between machines.

## Good practice

- Only connect agents you run yourself.
- Do not use Tailscale Funnel or any public tunnel for the chat.
- Shut out a folder you no longer use (`crewchat place remove NAME`): its token stops working
  at once.
- If a folder's token may have leaked, remove that place and join the folder again.
- Sessions in the same folder share that folder's token, so they could read each other's
  messages through the hook endpoint if they guessed a link key. Keys are random; treat agents
  in one folder as trusting each other.

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's "Report a vulnerability" on the repository's
Security tab, or email the maintainer listed on the GitHub profile. You should hear back within a
week.
