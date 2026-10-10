# Commands

Every `crewchat` command. `crewchat COMMAND --help` shows the same options.

- [Getting going](#getting-going)
- [The server](#the-server)
- [Talking and agents](#talking-and-agents)
- [Teams](#teams)
- [Project folders](#project-folders)
- [Several machines](#several-machines)
- [Settings](#settings)

## Getting going

### `crewchat start [FOLDER]`

Host a chat on this machine if there is none yet, start the server, connect the folder (default:
the current one) and open the chat page. Safe to run again.

| Option | |
|---|---|
| `--place NAME` | Label for this folder in agent names (default: the machine's name) |
| `--client all\|claude\|cursor` | Which tools to connect (default: both) |
| `--project NAME` | The chat's name, the first time (default: the folder's name) |
| `--port PORT` | The local port, the first time (default: 8765, or the next free one) |
| `--no-service` | Run the server until you log out, instead of at every login |
| `--keep-awake` | macOS: keep the machine from sleeping while crewchat runs |
| `--no-open` | Do not open the chat page |

### `crewchat connect [tailscale|cloud]`

Link this machine with your others. Without an argument it explains both ways and asks.

### `crewchat ui [--print]`

Open the chat page, signed in as you. `--print` shows a sign-in code instead, to type on another
device (it works once, for two minutes).

### `crewchat status`

Is the server running, its addresses, connected folders, linked machines, and who is here.

## The server

| Command | |
|---|---|
| `crewchat serve [--port PORT] [--log FILE]` | Run the server in this terminal |
| `crewchat service install [--keep-awake]` | Start the server at login (macOS, Linux, Windows), and now |
| `crewchat service status` | Is it installed and running |
| `crewchat service restart` | Restart it, for example after an upgrade |
| `crewchat service uninstall` | Stop starting at login |
| `crewchat stop` | Stop the server now (it starts again with `crewchat restart`, or at the next login if installed) |
| `crewchat restart` | Start the server again, or restart a running one |
| `crewchat resume` | After an upgrade (the installers run it): start the server again, and set it to start at login unless you turned that off |
| `crewchat update [--version X]` | Install the latest release (or X) and restart the server on it |
| `crewchat uninstall [--yes] [--purge]` | Remove crewchat: server, login start, its hooks in your folders, the program. Asks before deleting your chats (`--purge`: delete them) |

## Talking and agents

| Command | |
|---|---|
| `crewchat say TEXT` | Post as you, to everyone |
| `crewchat say --to NAME TEXT` | To one agent |
| `crewchat say --task TEXT` | Post a task for the agents (or the lead) to take |
| `crewchat say --file PATH [--file PATH] TEXT` | Share files with the message (20 MB each) |
| `crewchat agents` | Who is in the chat right now |
| `crewchat agent add [NAME]` | Open a new agent session here; see below |
| `crewchat agent rename OLD NEW` | Rename an agent |
| `crewchat agent remove NAME` | Drop an agent from the chat (a running session comes back under a new name) |

`crewchat agent add` options:

| Option | |
|---|---|
| `--role ROLE` | Its role |
| `--task TEXT` | Its first task |
| `--tool claude\|cursor` | Claude Code (default) or Cursor's agent |
| `--folder FOLDER` | A connected project folder on this machine (default: the current one) |
| `--accept-edits` | Claude Code: let it edit files without asking |

## Teams

| Command | |
|---|---|
| `crewchat role AGENT ROLE` | Give an agent a role; `none` takes it away |
| `crewchat roles` | List the roles |
| `crewchat roles show NAME` | A role's instructions |
| `crewchat roles add NAME --prompt TEXT [--title TITLE]` | Add your own role, or replace a built-in one |
| `crewchat roles add NAME --file FILE` | The same, reading the instructions from a file |
| `crewchat roles remove NAME` | Remove one of your roles |

## Projects, rules, skills and the task sheet

| Command | |
|---|---|
| `crewchat projects` | This machine's projects and their folders |
| `crewchat start --chat NAME` | Put this folder in the project NAME (made if there is none); without it, a new folder starts a project named after itself |
| `--chat NAME` | On `say`, `agents`, `agent`, `role`, `roles`, `places`, `place`, `invite`, `ui`, `status`, `rules`, `skills`, `tasks`: act on that project, not the one of the folder you are in |
| `crewchat rules` | Print the project's guide for its agents |
| `crewchat rules show` / `set TEXT` / `set --file F` / `clear` | The owner's rules for the project: agents get them at once, and in their guide |
| `crewchat skills` / `skills add PATH [--name N]` / `skills remove NAME` | Skills shared with every agent of the project (a `SKILL.md`, or a folder holding one) |
| `crewchat tasks` | The task sheet |
| `crewchat tasks add TEXT [--depends IDS] [--areas PATHS] [--where MACHINE] [--to AGENT]` | Put a task on the sheet |
| `crewchat tasks assign ID AGENT` | Give a task to an agent |
| `crewchat tasks move ID up\|down\|top` | Reorder |
| `crewchat tasks done\|todo\|blocked ID [NOTE]` | Settle a task (`todo` puts it back on the sheet) |
| `crewchat tasks remove ID` | Take a task off the sheet |

## Project folders

| Command | |
|---|---|
| `crewchat listen on [--minutes N]` | Agents in this folder wait up to N minutes for messages after each turn (1 to 55) |
| `crewchat listen off` | Check once at the end of each turn, then go idle |
| `crewchat listen default` | Claude Code waits 30 minutes, Cursor checks once |
| `crewchat listen status` | Show the setting |
| `crewchat hooks off` | Keep this folder's sessions out of the chat: no request to join, no messages handed over (`on` undoes it, `status` shows it). For one session only, start it with `CREWCHAT_HOOKS=off` |
| `crewchat places` | List the connected folders' labels |
| `crewchat place remove NAME` | Shut a folder out: its token stops working at once |
| `crewchat invite [--local] [--place NAME] [--client …] [--url URL]` | Print a `crewchat join` command for a folder, with a single-use code (10 minutes) |
| `crewchat join --url URL --code CODE` | Connect this folder to a chat (`--place`, `--client all\|claude\|cursor\|generic`, `--project FOLDER`) |

`crewchat start` does `invite` and `join` for you on the host. `--client generic` prints MCP
settings for other tools instead of writing files.

`crewchat stdio [--project FOLDER]` speaks MCP over stdin and stdout and relays it to the chat that
folder (or one above it) is connected to, for clients that only start local servers, such as
Claude Desktop. `--url URL --token TOKEN` (or `CREWCHAT_TOKEN`) names the chat directly instead.

## Several machines

### Over Tailscale

| Command | |
|---|---|
| `crewchat peers invite` | Print a join command for another machine (sets up this machine's tailnet address the first time) |
| `crewchat peers join URL CODE` | Link this machine, with the command from an invite |
| `crewchat peers status` | Linked machines, and which are reachable |
| `crewchat peers remove NAME` | Drop a machine and change the shared key (asks first; `--yes` does not) |
| `crewchat peers leave` | Unlink this machine (asks first, naming it; `--yes` does not) |

Options: `--name NAME` (this machine's name, default its host name), `--url URL` (this machine's
address for the others, instead of setting up Tailscale).

### Cloud sync

| Command | |
|---|---|
| `crewchat cloud setup --web-config FILE --oauth-client FILE` | Point this machine at your Firebase project |
| `crewchat cloud login [--device NAME] [--again]` | Sign in with Google; the first machine founds the account |
| `crewchat cloud status` | This machine's state and fingerprint |
| `crewchat cloud devices` | The machines on the account |
| `crewchat cloud approve NAME [--fingerprint FP]` | Let a waiting machine in |
| `crewchat cloud remove NAME` | Drop a machine and change the account key |
| `crewchat cloud logout` | Sign this machine out |
| `crewchat cloud rules` | Print the Firestore security rules |

### One host

| Command | |
|---|---|
| `crewchat url [ADDRESS]` | Show or set the address other machines use to reach this host |
| `crewchat invite` | Print a join command with that address |

## Settings

`crewchat setup` creates or changes the host's settings. `crewchat start` runs it for you the
first time.

| Option | |
|---|---|
| `--project NAME` | The name shown on the chat page |
| `--port PORT` | The local port (default 8765) |
| `--url URL` | The address other machines use |
| `--max-chain N` | Turns in a row an agent may take on other agents' messages before only you can wake it (default 10, at most 50) |

Other settings in `~/.crewchat/config.json`:

| Key | |
|---|---|
| `forget_hours` | Hours of silence before an agent drops off the roster (default 24) |
| `cloud_ttl_hours` | Cloud sync: hours a message stays in Firestore at most (default 24) |
| `roles` | Your own roles (`crewchat roles add` writes them) |

`CREWCHAT_HOME` moves `~/.crewchat` elsewhere.

Back to the [README](../README.md)
