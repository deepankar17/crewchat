# Contributing

Thanks for helping. crewchat is deliberately small: plain Python 3.9+, standard library only at
its core.

## Ground rules

- **No dependencies in the core.** `crewchat.py`, `crewchat_peers.py` and `crewchat_map.py` use the
  standard library only. A new dependency goes in an optional extra, imported lazily, the way
  `crewchat_cloud.py` is. `crewchat.py` is what a release ships and what someone copies alone: it
  must run with nothing beside it. `crewchat_peers.py`, `crewchat_map.py` and `crewchat_cloud.py`
  stay separate files. The extras are `cloud` for cloud sync (`crewchat_cloud.py`) and `map` for the
  code map's accurate readers (tree-sitter and its grammars, in `crewchat_map.py`).
- **The server binds to 127.0.0.1.** Changes that make it listen elsewhere will not be merged;
  remote access goes through a private network.
- **Storage follows [docs/storage.md](docs/storage.md).** `messages.jsonl` is the source of truth;
  SQLite only for rebuildable local data; Markdown only as generated output.
- **Tests come with the change.** The suite starts real servers; add a test that fails without
  your change.

## The code

| File | |
|---|---|
| `crewchat.py` | The server, the MCP tools, the chat page (and its analytics view, `ANALYTICS_JS`), hooks, and every command, in one file. It also writes the light hook script, `~/.crewchat/crewchat-hook.py`, from its own code (`hook_script`): edit the functions it copies, never the written file. Anything a hook-side function calls must be in `hook_script`'s lists |
| `crewchat_peers.py` | Linking machines over Tailscale |
| `crewchat_cloud.py` | Linking machines with cloud sync (Firestore), and the phone page from anywhere |
| `crewchat_map.py` | The code map: files, definitions and imports of the project's code (standard library) |
| `plugin/` | The Claude Code and Cursor plugin: the setup skill, and the Claude Code mod (`plugin/hooks/register.js`) that draws the band above the prompt and the chat pane |
| `install.sh`, `install.ps1` | The one-line installers: `install.sh` for macOS and Linux, `install.ps1` for Windows (PowerShell) |
| `tests/` | The unit tests, well over a thousand: `test_crewchat.py` (one machine, most of them), `test_peers.py` and `test_cloud.py` (several machines), `test_map.py` (the code map), `test_mcp_servers.py`, `test_crew.py` (a lead adding agents), `test_park.py` (parking tasks), `test_phone.py`, `test_conversation.py` (the advisor's excerpt), `test_desktop_mod.py` (the Claude Code mod, run in Node when it is installed), `test_durability.py`, `test_workflows.py` (GitHub Actions run only for main), and smaller ones |
| `e2e/` | End-to-end tests, about 200: real servers, agents' hooks, the chat page in Chrome, linked machines, the installer, security, load, and the benchmark (`bench.py`) |

`crewchat.py` reads top to bottom in parts, each opening with a comment that names it: the shared
constants and helpers; configuration (tokens, projects, the roster); what waits on you in Needs you;
the advisor's view of a conversation and the history behind Activity; process notes; routines;
Google Chat; a lead adding agents; the Jira ticket cycle; `Hub`, the messages, roster and tasks; the
MCP tools agents get; the HTTP server; host commands and agents started from the page; the agent
side (joining, connecting folders, the hooks); your own MCP servers; the browser and device proxies;
starting at login, updates and Web Push; the guide agents read; the chat page; and the command line.
The parts share one namespace.



## Running the tests

```bash
python3 -m unittest discover -s tests -v   # the unit tests
python3 -m unittest tests.test_peers -v    # or one file
```

The cloud-sync tests need `pip install cryptography`; they are skipped without it. The code map's
accurate readers are tested with `pip install tree-sitter tree-sitter-java tree-sitter-kotlin
tree-sitter-javascript tree-sitter-typescript`, and the regular-expression readers without them.

Run the end-to-end tests too when a change touches the hooks, the chat page or linked machines, and
before a release. They start real servers in a throwaway folder with the `crewchat` command, and drive
them as agents and the owner would:

```bash
python3 -m unittest discover -s e2e -v
```

The chat-page tests need Google Chrome and `pip install websocket-client`, and the installer tests
need `uv`; each is skipped without them. `CREWCHAT_BIN=/path/to/crewchat` tests an installed copy
instead of this checkout, `CREWCHAT_LOAD=80` sends more messages in the load test, and
`CREWCHAT_E2E_KEEP=1` keeps the throwaway folder for a look afterwards. Cloud sync needs a real
Firebase project, so it is covered by `tests/test_cloud.py` with a stand-in store; Windows is not
covered here. `tests/test_desktop_mod.py` runs the Claude Code mod in Node, and is skipped without it.

Text files check out with LF line endings everywhere, Windows included (`.gitattributes`): keep it
that way, as `crewchat.py` with CRLF is a few hundred KB bigger and once passed the code map's file limit.

## Measuring speed

A speed change comes with numbers from before and after it, from the benchmark: a real server with
5 agents and 5,000 messages, its hook calls, the chat page's poll and server startup, as medians.

```bash
python3 e2e/bench.py --json before.json      # on the code before the change
python3 e2e/bench.py --compare before.json   # after it: exit 1 if a median got over 20% worse
```

Compare on the same machine and Python. `CREWCHAT_BIN=/path/to/crewchat` measures an installed copy,
whose hooks import a cached `.pyc` instead of compiling `crewchat.py` on every call.

## Trying a change by hand

Use a throwaway home so you do not touch your real chat:

```bash
export CREWCHAT_HOME=/tmp/crewchat-dev
mkdir -p /tmp/project && cd /tmp/project
python3 ~/code/crewchat/crewchat.py start --no-service --port 8799
```

## Documentation

The guides are in `docs/`.

## Reporting bugs

Open an issue with what you ran, what you expected and what happened. For anything
security-sensitive, see [SECURITY.md](SECURITY.md) instead.
