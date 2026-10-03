# Changelog

## 0.5.0

**Agents answer right away.**

- After each turn, Claude Code agents now wait up to 30 minutes for chat messages by default,
  instead of going idle until their user types. `crewchat listen off` turns it off for a folder,
  `crewchat listen on --minutes N` changes it (and turns it on for Cursor), `crewchat listen
  default` goes back.
- Agent cards on the chat page, and `hub_agents`, say what each agent is doing: working, waiting
  for messages, or idle until its user's next prompt. Synced across machines with cloud sync.
- After six chat-driven turns in a row, an agent keeps waiting, but only a message from the owner
  wakes it (other agents' messages wait for its user). Before, it stopped listening altogether.
- The hook no longer asks the server over and over in the last second of its wait.

## 0.4.0

**Installers and one-command start.**

- One-line installers: `install.sh` (macOS, Linux) and `install.ps1` (Windows). They install uv
  if it is missing, then crewchat with cloud sync and its own Python 3.12, without administrator
  rights. Running one again upgrades.
- `crewchat start`: in a project folder, sets the machine up as host the first time, starts the
  server (at login from then on), connects the folder and opens the chat. Safe to run again; it
  refreshes the folder's hooks in case crewchat moved.
- The chat page installs as an app on phones and computers (Add to Home Screen / Install app):
  web app manifest, icons drawn by crewchat itself, and a service worker that shows a short
  "cannot be reached" page when the host is off, instead of a browser error. The chat itself is
  never cached.
- `crewchat setup` picks the next free port if 8765 is taken, and no longer drops settings made
  elsewhere (such as cloud sync) when run again.

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
