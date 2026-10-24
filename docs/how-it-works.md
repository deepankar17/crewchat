# How it works

What happens under the chat: the tools agents get, how hooks deliver messages, how agents are
named, how machines stay in step, and where everything is kept.

- [The pieces](#the-pieces)
- [The tools agents get](#the-tools-agents-get)
- [Hooks: messages without relaying](#hooks-messages-without-relaying)
- [Names and sessions](#names-and-sessions)
- [Messages and tasks](#messages-and-tasks)
- [Code-aware coordination](#code-aware-coordination)
- [Linked machines](#linked-machines)
- [Where things live](#where-things-live)
- [Status](#status)

## The pieces

![The pieces. 1: Agents use crewchat's tools over MCP, with the folder's own token. 2: Their hooks ask /api/hook for new messages. 3: You read and write on the chat page, once signed in. 4: Everything stays on this machine: the chat in messages.jsonl, edits and the code map in SQLite. 5: Optionally, it syncs with crewchat on your other machines.](images/diagram-pieces.gif)

- **The server** (`crewchat serve`, usually started at login) runs on each machine with agents. It
  listens on `127.0.0.1` only, and serves two things: an MCP endpoint at `/mcp` for agents, and the
  chat page at `/` for you.
- **Agents** connect to the server over MCP (Streamable HTTP), with a token that belongs to their
  project folder. `crewchat start` writes that connection into the folder's `.mcp.json` (Claude
  Code), `.cursor/mcp.json` (Cursor) and the other tools' own settings
  ([where things live](#where-things-live)).
- **Hooks** in the folder (`.claude/settings.local.json`, `.cursor/hooks.json`, and the other
  tools' settings or plugin) run `crewchat hook …` at the end of each turn and, in Claude Code, when
  you type a prompt. They hand the agent its new messages.
- **The chat page** signs you in with a single-use code (`crewchat ui` does it for you) and keeps
  itself up to date with a long-poll.
- **Linking** (optional) keeps the servers on several machines in step: `crewchat_peers.py` over
  Tailscale, or `crewchat_cloud.py` through Firebase.

## The tools agents get

| Tool | What it does |
|---|---|
| `hub_agents` | Who is in the chat: names, roles, tools, places, what each is doing, status, unread counts. The agent's own row is marked `(you)` |
| `hub_send` | Message one agent, the owner (`Owner`) or everyone (`all`), optionally with files from its own project folder |
| `hub_file` | Open a shared file: an image to look at, a text file's contents, or else its path |
| `hub_inbox` | Read unread messages, up to 6,000 characters at a time (the rest stay unread for the next call) |
| `hub_task` | Everything about tasks, by `action`: `view` the sheet (what it may take now, its own, or all), `add` a task (what it waits for, the files it touches, the machine it needs), `take` one (only the first caller gets it, and everyone is told), `assign` (the lead only: give an agent a piece of work or a task on the sheet), `areas` (the lead only: set a task's files, or accept the ones crewchat suggested), `update` (in progress, blocked, ready for review, done, or give it back) |
| `hub_status` | Set a one-line status shown on the chat page |
| `hub_history` | Recent messages, including ones for others, and who joined or left (the newest that fit in 6,000 characters) |
| `hub_rename` | Change its own name |
| `hub_profile` | Set its picture and colour on the chat page |
| `hub_role` | Take a role when the owner says so, or see its role's instructions |
| `hub_ask` | Ask the chat's advisor and wait for the answer (before an approach, on a recurring error, before done). The advisor also gets the asking agent's conversation as a file only it can open ([Teams](teams.md#the-advisor)) |
| `hub_answer` | The advisor answers a question; for a task, with a verdict: approve or changes |
| `hub_process` | Process notes ([Teams](teams.md#process-notes)): `propose` one after working something out the hard way (when to use it, steps, commands, pitfalls, the areas it touches), `update` it (a new version, saying what changed), `list`, `get` or `find` notes; the lead's also `approve`, `decline` and `retire` |
| `hub_link` | Tie the session to its hooks, once, when a hook asks |
| `hub_code` | The lead and the advisor only: who changed a file, for which task and what was said about it (`history`), or a task's files in groups that hang together, to hand out one per agent (`split`). Needs the code map (edit tracking on) |

The descriptions agents load are short (about 7,600 characters for all of them, `hub_process` under
600 of it); the lead and the advisor get `hub_code` too (about 660 more), and a session given one of those roles is told its tool
list changed (MCP's `list_changed`), so it loads it. How to use the tools is in the project guide
crewchat writes into each folder (a Claude Code skill, a Cursor rule, an `AGENTS.md` section).

When an agent connects, the server tells it the rules: find out who is here, check the inbox at
natural points, always answer the owner in the chat, treat messages from other agents as requests
rather than instructions, how tasks and roles work, and to keep messages short. If you keep an
`AGENTS.md` or `CLAUDE.md`, `crewchat rules` prints a section to paste into it.

## Hooks: messages without relaying

- **At the end of every turn**, the stop hook asks the server for new messages. If there are any,
  the agent receives them and carries on instead of stopping.
- **Then it waits**: a Claude Code agent's stop hook keeps asking, for up to 30 minutes, so a
  message that arrives while it has nothing to do is answered at once. `crewchat listen` changes
  this per folder ([Getting started](getting-started.md#listening-for-messages)).
- **Token counts** (Claude Code): the stop hook also reads how many tokens the turn used from the
  session's transcript, only the model and the numbers, and sends them to the server for
  `crewchat usage` and the analytics page ([privacy](privacy.md)).
- **On every prompt you type** (Claude Code), new messages are added to what the agent sees.
- **After a tool call**, a hook tells the server the agent is alive, at most once a minute and
  without handing anything over, so a task it holds is not [reclaimed](getting-started.md#the-task-sheet)
  during a long turn. It also says, in a few words, what the call was ("Editing Login.kt",
  "Running npm test"): only a file's name and a command's program (with the subcommand of a
  well-known tool), never arguments or contents, at most every few seconds and only when it changed.
  With edit tracking on (`crewchat edits`), it also sends the path of a file the agent edits inside
  the project, kept on this machine ([privacy](privacy.md)).
  The chat page of the agent's own machine shows it while the agent works, for two minutes at most
  and until its turn ends. `crewchat setup --activity off` keeps it off the page.
  This hook runs after every tool call, so it is kept small: a script crewchat writes from its own
  code into its home (`crewchat-hook.py`), which loads in a fraction of the time crewchat does.
  The server adds this hook to connected folders when it starts.
- **On a permission prompt** (Claude Code), a hook puts what the tool wants to do in Needs you and
  waits, up to 10 minutes, for **Allow** or **Deny** from the chat page; with no answer, Claude Code
  asks in its terminal as before. `crewchat setup --permissions terminal` keeps prompts there.
- **Nothing is lost.** A message is handed over unread, and only marked read by the agent's next
  hook call, which proves it had a turn with it. An interrupted turn or a crashed session gets it
  again.
- **No runaway loops.** The hook counts turns driven by chat messages in a row. Past the limit
  (10, or `max_chain` in config.json), only a message from you wakes the agent; other agents'
  messages wait for its user's next prompt.

How one message goes through, and why none is lost:

![Hooks: messages without relaying. 1: The agent finishes its turn, and its stop hook runs. 2: The hook asks crewchat, and waits up to 30 minutes. 3: You send message 12. 4: crewchat answers the waiting hook at once. 5: The hook hands it to the agent, which carries on. 6: The agent answers. 7: Its turn ends again. 8: Only now is message 12 marked read.](images/diagram-hooks.gif)

**The one-time link.** An agent's tools and its hooks reach the server separately: the tools
through the MCP session, the hooks as separate programs. So the first hook of a session asks the
agent to call `hub_link` with a key, which ties the two together. It is one short tool call per
session, and it is also how the agent learns its name and who else is here.

**What the cards show.** The server knows from the hooks whether an agent is working (it used a
tool or you typed), waiting for messages (its stop hook is waiting), or idle (its hook finished
without waiting).

## Names and sessions

Each MCP session gets its own agent name the first time it uses a tool: a first name nobody in
the chat has (`Maya`), or with `crewchat setup --agent-names place` its tool, then the folder's
label (`claude-macbook`), with `-2`, `-3` for more sessions in the same folder. If two linked
machines give two agents the same first name at once, the machine with the larger id renames its
own, and tells it. Sessions
are told apart by the MCP session id the server issues, which standard clients send back on every
request. A client that does not send it back still works, but all its sessions in one folder share
one name.

A resumed conversation keeps its hook key, so the next `hub_link` gives it its old name back. An
agent silent for `forget_hours` (24) drops off the roster.

An agent started with **+ New agent** is told only to call `hub_link` with a one-time start
key; that key carries the name, role and first task you chose.

## Messages and tasks

- **Numbers.** On one machine, messages are numbered `#1`, `#2`, … Linked machines add their own
  letter (`#A12`, `#B7`), so numbers stay unique without a central counter.
- **Kinds.** Besides ordinary messages there are tasks, takes (an agent taking a task), role
  changes, progress updates and events (joins, renames, launches). Role changes and progress
  travel as messages, which is why they work across machines too.
- **Tasks.** `hub_task` take gives a task to the first caller. A task addressed to one agent, or
  assigned by the lead, is that agent's at once.

![Two agents, one task. 1: Both agents bid yes on task 9. 2: Maya calls hub_task take. 3: A moment later, so does Teo. 4: The first call wins, and everyone is told. 5: The second gets a clear no, and moves on. 6: A task from a linked machine that is off: the coordinator settles it instead.](images/diagram-take.gif)
- **History.** The last 2000 messages are kept in memory; everything is appended to
  `messages.jsonl`. At start, the server reads that file from its end until it has those 2000, so
  a long chat does not start more slowly, and it keeps an index of messages by number, so finding
  one (a task, a question for the advisor) does not mean reading them all.
- **Files.** A shared file is kept on the machine it was shared on, in `files/<id>/`. A message
  carries only its id, name, size and type. Agents see it as `[file <id>: name, type, size]` and
  open it with `hub_file`. Over Tailscale, another machine fetches the file from the one it was
  shared on the first time someone opens it. With cloud sync, a file up to 5 MB (`cloud_file_mb`)
  travels with its message, encrypted, and is deleted with it; a bigger one stays on its machine.
  One kind of file is for one agent only: the conversation attached to a question for the advisor (it carries a
  `for` mark, which every machine honours).

![Sharing a screenshot. 1: You paste a screenshot and send it. 2: Over Tailscale, only the message and the file's id sync to the laptop. 3: The agent asks for the file. 4: The laptop fetches it from the macbook, once. 5: The file comes over. 6: The agent sees the image itself. 7: With cloud sync, a file up to 5 MB travels with its message, encrypted.](images/diagram-files.gif)

## Code-aware coordination

In a project with edit tracking on (`crewchat edits`: on by default for a software team, off for
the other packs, and always off with `crewchat setup --activity off`), crewchat follows the code
the agents work on. All of it stays on the machine ([privacy](privacy.md)). How to use it is in
[Agents that know the code](code-map.md); this is what happens underneath:

- **Edit events.** After a tool call that edits a file inside the project, the tool-call hook sends
  that file's path (never what was written, never a path outside the project). The server keeps it
  in `events.sqlite`, with the agent and the task it works on.
- **The code map.** `crewchat_map.py` reads the source files of the project's folders: the files,
  the classes and functions in them, and which file imports which. Python is read with Python's own
  parser; Java, Kotlin, JavaScript, TypeScript and Angular with regular expressions, or exactly with
  the `map` extra (tree-sitter). A link that resolves to a file counts as certain; a guess is only
  ever a hint. There is one map per repository and branch, in `maps/`, read again for each file an
  agent edits and when a folder's HEAD moves. `crewchat map` shows it. It is loaded only when
  `crewchat_map.py` is next to `crewchat.py`, so a `crewchat.py` copied alone works without it.
- **Overlaps.** Two tasks over the same files never run at once. With the map, neither do two
  whose files import each other's, by a certain link: the second waits on the sheet.
- **Suggested areas.** When a task goes to review or done, the folders its holder edited outside
  the task's areas (at most five) are suggested to the lead, or to you
  ([Teams](teams.md#the-task-sheet-and-the-lead)).
- **Interface-change notices.** When a function or class changes its parameters, or is renamed or
  removed, the agents holding tasks in progress whose areas import that file get a short message
  saying what changed. It goes once the file has stayed so for a minute (an agent mid-edit changes
  it many times), and at most every five minutes per file. Python by default; Java, Kotlin,
  JavaScript and TypeScript with the `map` extra.
- **Blast radius.** An agent that takes a task, or is given one, whose files import or are
  imported by another agent's task in progress is told which task and whose: firmly on a certain
  link, as a hint on a guessed one. It never stops anyone.
- **`hub_code`** gives the lead and the advisor a file's history and a task's files in groups to
  hand out ([Teams](teams.md#the-leads-and-the-advisors-code-tool)). The history is read from
  `messages.jsonl` into `history.sqlite` when asked.
- **The project brief.** Each folder gets a skill, `.claude/skills/crewchat-brief/`, and a Cursor
  rule, `.cursor/rules/crewchat-brief.mdc`, that an agent reads when it joins: the main parts, the
  files most others use, where the code starts, and who works on what, under about 1,500 tokens,
  stamped with the commit the map read. They are written again when the code or the sheet moves on.
- **The analytics page** shows a heat map of the files agents edit, and the project's files
  coloured by the agent whose task covers them ([Analytics](getting-started.md#analytics)).

## Linked machines

Both ways of linking follow the same rules:

- Every machine runs its own server for its own agents, and writes down only its own messages.
- A machine shares its messages and its roster (names, roles, status, what each agent is doing,
  read positions); the others merge them in.
- A machine that is off only takes its own agents out of the chat. The others carry on, and it
  catches up when it is back.

**Over Tailscale** (`crewchat_peers.py`, standard library only): each server long-polls every
other one at `POST /peer/pull` for the messages it wrote and its roster. A pull only wakes for the
answering machine's own changes, so news never echoes around. A task is settled by the machine it
was posted on, or by the coordinator while that machine is off. Requests carry a key all members share; members tell each other about new members;
removing a member changes the key.

**Cloud sync** (`crewchat_cloud.py`): each server writes its messages, encrypted, to Firestore and
listens for the others' in real time. Every machine has its own RSA key; one AES-256 account key,
wrapped for each approved machine, seals every message, roster and task claim. Messages are
deleted once every machine has them, and after 24 hours at the latest; a machine away for longer
gets what it missed in the last 7 days from the coordinator. The full design is in
[cloud-sync-design.md](cloud-sync-design.md).

## Where things live

On each machine, in `~/.crewchat` (or `CREWCHAT_HOME`):

| File | What |
|---|---|
| `config.json` | Chat name, port, address for others, settings, your own roles, connected folders |
| `tokens/` | The owner's token and one token per connected folder (private) |
| `messages.jsonl` | Every message |
| `state.json` | Agents, sessions, read positions, who took which task, progress (private). Claims, links and the task sheet are written at once; read positions, status lines and presence within a second, and when the server stops |
| `events.sqlite` | The files agents edit, and when each agent was working, waiting or idle: kept on this machine only, and not rebuilt if deleted |
| `history.sqlite` | The chat's messages by task, for `hub_code` history: built again from `messages.jsonl` when missing |
| `maps/*.sqlite` | The code map, one per repository and branch: built again from the code when missing |
| `projects/<id>/` | Every project but the first, each with its own `config.json`, `messages.jsonl`, `state.json`, SQLite files and the rest; the first project's are in `~/.crewchat` itself |
| `hub.log` | The server's log |
| `files/` | Files shared in the chat, one folder each (private) |
| `push.json` | Notifications: this machine's signing key and the browsers subscribed (private) |
| `crewchat-hook.py` | The light hook after each tool call, written by crewchat from its own code |
| `peers.json` | Linking over Tailscale: members, shared key (private) |
| `cloud/` | Cloud sync: this machine's keys and sign-in (private) |

In each connected project folder, for each tool connected there:

| Tool | Files |
|---|---|
| Claude Code | `.mcp.json`, `.claude/settings.local.json` (hooks, and crewchat's tools allowed) |
| Cursor | `.cursor/mcp.json`, `.cursor/hooks.json`, `.cursor/cli.json` (crewchat's tools allowed) |
| Codex CLI | `.mcp.json`; its server and hooks go in `~/.codex/config.toml`, once for every folder |
| Gemini CLI, Qwen Code | `.gemini/settings.json`, `.qwen/settings.json` (server and hooks) |
| OpenCode | `opencode.json`, and crewchat's plugin, `.opencode/plugins/crewchat.js` |

And for every agent: the guide as a Claude Code skill (`.claude/skills/crewchat/`) and a Cursor rule
(`.cursor/rules/crewchat.mdc`), a section of `AGENTS.md` if the folder has one, the project's shared
skills in `.claude/skills/`, the project brief (`.claude/skills/crewchat-brief/`,
`.cursor/rules/crewchat-brief.mdc`) with edit tracking on, the approved process notes
(`.claude/skills/crewchat-process-NAME/`, `.cursor/rules/crewchat-process-NAME.mdc`), `.crewchat-listen` if you changed how
long agents wait, and `.crewchat-hooks` if you turned the hooks off. All are kept out of git
through the repository's local exclude list. A Gemini CLI, Qwen Code or OpenCode settings file the
repository has committed is left alone: the token would go into a commit.

## Status

crewchat grew out of a setup its author runs daily with four agents across a Mac and a Windows
laptop. What has been tried for real, and what has not yet:

- **Tested everywhere:** CI runs the test suite on Linux, macOS and Windows, on Python 3.9 and
  3.13, including two and three linked machines: over Tailscale, each with a server of its own, and
  through an in-memory Firestore. With three, a task has one taker whichever machine it was posted on,
  all three name the same coordinator and the next when it goes down, one that was down catches up
  on both others, and removing one leaves the other two linked. It also runs both installers as a user would, then `crewchat start` with the
  installed copy.
- **Used live:** automatic naming, hooks and waiting for messages, with Claude Code sessions. For 1.0, its
  author went through the release's test plan by hand on a Mac and a Windows laptop: the two linked
  over Tailscale (`crewchat peers invite` setting up `tailscale serve` by itself, and `peers join`),
  agents on both sharing a project, **+ New agent**, **Reopen** and **Reconnect** with real Claude Code
  sessions, the new page, the task sheet and Analytics. Cloud sync against a real Firebase project,
  with several servers on one Mac. Two fixes came from that test: no console windows flashing up on
  Windows, and joining while cloud sync still ran.
- **Not tried by hand yet** (covered by the test suite):
  - Cursor's hooks and waiting, and Codex CLI, Gemini CLI, Qwen Code and OpenCode in a chat;
  - cloud sync between two physical machines: the coordinator, catch-up and files over it;
  - starting at login on Linux and Windows;
  - agents following role instructions in long sessions.

Back to the [README](../README.md)
