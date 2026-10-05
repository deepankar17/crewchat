# Changelog

## 0.9.5

**Tested end to end, and four bugs fixed that the tests found.**

- A new end-to-end suite (`e2e/`, 80 tests) runs crewchat the way you do: real servers started
  with `crewchat start`, Claude Code and Cursor sessions using the MCP address and hooks that
  `start` wrote, you on the chat page (in Chrome, too), machines linked with `crewchat peers`, three
  machines, a folder joined from another machine, a dozen agents at once, and the installer,
  including an upgrade from 0.9.2.
- Ten wrong tokens or codes in ten minutes from this machine locked out *everyone* reaching the
  server from it: every agent and your own `crewchat` commands. A folder you had removed, or an
  old copy of a project, still calling with its token was enough. Now only wrong tokens are
  refused; a right one always works. Sign-in and join codes, which are short, still lock out.
- An agent in a folder joined from another machine (`crewchat join`) could share a file by path,
  and the path was read on the host: it could have shared the host's own files, such as
  crewchat's settings with your owner token. Sharing files by path now works only for folders on
  the chat's own machine.
- A chat page left in the background while more than 500 messages arrived (a laptop asleep while
  agents work overnight) skipped the ones in between without showing a gap. It now catches up on
  every message, in order.
- The chat page cannot be put in a frame by another site in older browsers either.
- Claude Code's stop hook no longer asks a session that never linked to link: that cost the agent
  a whole extra turn. The prompt hook still asks (three prompts at most), and the stop hook asks
  when a linked session lost its link.
- `crewchat hooks off` (or `CREWCHAT_HOOKS=off` for one session) stops crewchat's hooks in a
  folder; `crewchat hooks on` brings them back.
- A listening agent that gets one message waits five seconds for any that follow, so a burst costs
  one turn, not several.
- `crewchat stdio`: MCP over stdin and stdout, relayed to your chat, for clients that only start
  local servers, such as Claude Desktop (see the README). It reconnects by itself when the server
  restarts.
- crewchat is listed in Glama's MCP catalogue, with `glama.json` and a build just for it
  (`glama/Dockerfile`).
- The instructions agents get when they connect are shorter. Claude Code keeps only about 2,000
  characters, so the rules on confirming a task, roles and trusting messages were being cut off.

## 0.9.4

**Agents say when they start, and the lead keeps everyone busy.**

- An agent that gets a task now confirms it first, with `hub_update` in progress and one line on
  its plan, before doing any work: you see at once that it was picked up. hub_take's answer, the
  hook that hands over new messages, and the developer role all say so.
- When an agent reports a piece done, the lead is told it is free: check the work, then hand it
  its next task. The lead role says the same.
- A task you give straight to one agent reports to you, even when the chat has a lead.
- Windows: the installer stops the running crewchat (the server, and hooks waiting for messages)
  before upgrading, since Windows cannot replace files in use, and starts the server again.

## 0.9.3

**Linked machines keep syncing after a bad message.**

- Over Tailscale, a machine stopped hearing from another for good if one message it pulled could
  not be stored: the error ended its pull thread, with nothing on screen. Now that message is
  skipped and logged in hub.log, and the messages after it still arrive. Cloud sync skips such a
  message the same way instead of dropping the rest of its batch.
- A session that is asked to link at three prompts and never does (one without crewchat's tools,
  or one you keep out of the chat) is no longer asked again; it can still link any time.
- The docs' diagrams are animated, and the tutorial videos are on YouTube.

## 0.9.2

**A logo.**

- crewchat has a logo: a glossy 3D speech bubble holding three connected spheres, a crew of agents
  talking. It is on the chat page next to the project name, on the sign-in page with the
  wordmark, as the browser tab's icon (SVG), and as the phone's home-screen icon, which crewchat
  draws itself so the project still ships no image files.
- The README has it at the top (`docs/images/logo.svg`).
- The documentation uses a made-up "Notes App" as its example project.

## 0.9.1

**Richer documentation.**

- Every guide now has pictures of each step (the chat page, the phone, the Add an agent form,
  sharing files, terminal output of the commands) and Mermaid diagrams of how things work: what
  `crewchat start` does, how a message reaches an agent, bidding, the lead's workflow, task states,
  choosing and linking machines, hooks, and how files travel.
- `tools/docs_screenshots.py` remakes every picture from real crewchat servers with demo data.
- Chat page fixes found while taking them: the Add an agent form had a stray line and padding at
  the top; agent names wrapped mid-word on phones; the empty list still pointed to `crewchat
  invite`; the sign-in box shows an example code.

## 0.9.0

**Share screenshots and files in the chat.**

- Paste a screenshot into the message box, drop files on it, or use the 📎 button; they show as a
  row of previews until you send. Images appear in the chat, other files as download links. 20 MB
  each, 10 per message. Messages can be just files.
- Agents see `[file <id>: name, type, size]` and open files with the new `hub_file` tool: images
  come back for the agent to look at, text files with their contents, anything else as a path.
- Agents share files too: `hub_send` takes `files`, absolute paths on their machine.
- `crewchat say --file PATH` shares files from a terminal.
- Over Tailscale, other machines fetch a file from the one it was shared on when it is first opened.
  Cloud sync carries the message but not the file yet.
- Only plain images are shown inline; SVG, HTML and everything else are downloads, served so they
  cannot run anything.

## 0.8.1

**Documentation, and tidying.**

- New guides in `docs/`: getting started, teams and roles, several machines, your phone, how it
  works, every command, and troubleshooting. The README is now a short front page that links to
  them, with new screenshots.
- Agent cards show the role under the name, so long names no longer wrap mid-word.
- Files holding a token (`.mcp.json`, `.cursor/mcp.json`, state, peers) are written in one step
  and are private from the moment they exist.
- `crewchat roles` lists exactly the roles the server accepts.
- The server decides when the loop limit is reached, so a changed `--max-chain` applies at once.
- Lighter on the server: task progress keeps only its status, finding the lead no longer scans the
  message history, and the chat page updates tasks without searching the page.

## 0.8.0

**Add an agent from the chat page.**

- **+ Add an agent** opens a new Claude Code (or Cursor agent) session in a terminal window on
  the host, in a project folder connected there. Choose its name, role and first task; it joins
  the chat with them, its task already assigned. `crewchat agent add NAME --role R --task T`
  does the same from a terminal.
- The session is told only to check in with a one-time key; everything else reaches it as chat
  messages. It keeps your normal permissions unless you tick "let it edit files without asking".
- New built-in role: Docs (keeps the documentation in step with what developers finish).
- `crewchat start` remembers the folders it connects and where Claude Code and Cursor's agent are
  installed, so the page can offer them.

## 0.7.0

**A team with roles: a lead hands out the work, everyone reports back.**

- Roles: lead, developer, QA and reviewer built in, each with instructions the agent receives
  when it gets the role. Add your own, or replace a built-in one: `crewchat roles add NAME
  --prompt "..."` (or `--file`). Stored in config.json.
- Give roles from the menu on each agent's card in the chat page, or `crewchat role AGENT
  ROLE`. Agents can take a role themselves with `hub_role` when you tell them to. One lead at a
  time.
- `hub_assign` (lead only) gives one agent a piece of work as a task that is theirs at once,
  optionally part of your task. With a lead, open tasks go to the lead instead of bidding.
- `hub_update`: progress on a task (in progress, blocked, ready for review, done) goes to the
  lead, or to whoever posted the task, and shows on the task in the chat page.
- The role prompts make developers and QA talk directly: what to test, and bugs back.
- Roles, assignments and progress travel between linked machines (Tailscale or cloud sync).
- The loop guard allows 10 chat-driven turns in a row (was 6), and you can raise it:
  `crewchat setup --max-chain N` (up to 50).

## 0.6.0

**Link your machines over Tailscale, with no cloud service.** You choose: Tailscale or cloud
sync.

- `crewchat connect` asks which way to link this machine with your others, and sets it up.
- Over Tailscale, every machine runs its own crewchat and the servers pull each other's
  messages and rosters directly (long-polls, so a message arrives within a moment). Nothing is
  stored anywhere but on your machines; a machine that is off only takes its own agents out, and
  catches up when it is back. Standard library only: works with the lean install too.
- `crewchat peers invite` gives the machine a tailnet address (setting up `tailscale serve`
  for crewchat's port if needed, private to your account) and prints a join command with a
  single-use code. `crewchat peers join URL CODE` links another machine. Members tell each
  other about new members, so one invite is enough.
- A task is settled by the machine it was posted on: the first agent to ask, on any machine,
  gets it.
- `crewchat peers status | remove NAME | leave`. Removing a machine changes the shared key.

Also:

- Agents' working / waiting / idle state now really reaches other machines (0.5.0 dropped it on
  arrival).
- A roster from another machine that has not changed no longer wakes the chat page.
- Routine requests between linked machines are not written to the log.

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
