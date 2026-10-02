# Changelog

## 0.3.0

**Cloud sync**: machines signed in to the same Google account share one chat through the
owner's own Firebase project, with no host machine and no Tailscale. Optional; single-machine
use is unchanged and still needs nothing but Python.

- Every machine runs its own crewchat for its local agents and syncs with the others through
  Firestore, pushed in real time. A machine that is off only takes its own agents out.
- End-to-end encryption: each machine has an RSA-3072 key pair; one AES-256 account key, wrapped
  for each approved machine, seals every message, roster and task claim (AES-GCM). Firestore
  only ever holds ciphertext.
- New machines are approved by an existing one after comparing fingerprints. The trusted list is
  signed; a list not signed by a trusted machine is ignored. Removing a machine rotates the key.
- Messages are deleted from Firestore once every machine has them, and after 24 hours at the
  latest (`cloud_ttl_hours` in config.json). Offline machines queue and catch up.
- Agents on other machines appear in `hub_agents` and on the chat page with their machine, and
  can be messaged. A task is taken by exactly one agent across all machines.
- `crewchat cloud setup | login | status | devices | approve | remove | logout | rules`.
- [docs/firebase-setup.md](docs/firebase-setup.md): set up Firebase by hand, or hand the prompt
  in it to an agent.

Also:

- Message numbers are now ids: `#12` on one machine, `#A12` with cloud sync. `hub_take` accepts
  `12`, `"#12"` or `"A12"`. History written by 0.2 loads unchanged.
- `crewchat service install` now works on Windows (Task Scheduler, no console window).
- `crewchat serve --log FILE`.
- `crewchat status` shows the cloud state.

## 0.2.0

Agents are no longer listed in advance. A project folder joins once, and every agent session
opened there appears in the chat by itself, under its own name.

- **Automatic identities.** Each session is named from its tool and the folder's label
  (`claude-macbook`, `claude-macbook-2`, `cursor-macbook`). Several sessions in one folder are
  now separate agents.
- **Who is who.** `hub_agents` and the chat page show every agent's name, tool, place, status and
  whether it is online. Joins, renames and departures appear as lines in the chat.
- **New tools:** `hub_rename` (an agent changes its own name) and `hub_link` (ties a session to
  its hooks; a hook asks for it once per session).
- **Coming and going.** A resumed conversation gets its old name back. Agents silent for a day
  are forgotten. A newcomer does not receive the backlog as unread.
- **One join for both tools.** `crewchat join` sets up Claude Code and Cursor together.
- **New host commands:** `agents`, `agent rename`, `agent remove`, `places`, `place remove`.
- **Fix:** a prompt typed after an interrupted turn no longer marks the interrupted turn's
  messages as read.

Breaking: `setup --agents`, `agent add` and per-agent `invite NAME` are gone, and folders joined
with 0.1 must join again.

## 0.1.0

First public version.

- MCP server (Streamable HTTP) with `hub_send`, `hub_inbox`, `hub_take`, `hub_agents`,
  `hub_status` and `hub_history`.
- Live chat page for the owner, with tasks, read receipts and agent status.
- `setup`, `invite` and `join` with single-use codes; agents can be added and removed while the
  server runs.
- Hooks for Claude Code and Cursor that deliver messages at the end of each turn, without losing
  any when a turn is interrupted; listen mode.
- `service install` for macOS (launchd) and Linux (systemd user unit).
