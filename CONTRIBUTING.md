# Contributing

Thanks for helping. crewchat is deliberately small: plain Python 3.9+, standard library only at
its core.

## Ground rules

- **No dependencies in the core.** `crewchat.py`, `crewchat_peers.py` and `crewchat_map.py` use the standard library
  only, and `crewchat.py` must keep working when copied on its own to another machine. Libraries
  go in an optional extra, imported only when used: `cloud` for cloud sync (`crewchat_cloud.py`),
  and `map` for the code map's accurate readers (tree-sitter and its grammars, in `crewchat_map.py`).
- **The server binds to 127.0.0.1.** Changes that make it listen elsewhere will not be merged;
  remote access goes through a private network.
- **Storage follows [docs/storage.md](docs/storage.md).** `messages.jsonl` is the source of truth;
  SQLite only for rebuildable local data; Markdown only as generated output.
- **Tests come with the change.** The suite starts real servers; add a test that fails without
  your change.

## The code

| File | |
|---|---|
| `crewchat.py` | The server, the MCP tools, the chat page (and its analytics view, `ANALYTICS_JS`), hooks, and every command. It also writes the light hook script, `~/.crewchat/crewchat-hook.py`, from its own code (`hook_script`): edit the functions it copies, never the written file |
| `crewchat_peers.py` | Linking machines over Tailscale |
| `crewchat_cloud.py` | Linking machines with cloud sync (Firestore) |
| `crewchat_map.py` | The code map: files, definitions and imports of the project's code (standard library) |
| `install.sh`, `install.ps1` | The one-line installers: `install.sh` for macOS and Linux, `install.ps1` for Windows (PowerShell) |
| `tests/` | `test_crewchat.py` (one machine), `test_peers.py`, `test_cloud.py` (several machines), `test_map.py` (the code map), `test_workflows.py` (GitHub Actions run only for main) |
| `e2e/` | End-to-end tests: real servers, agents' hooks, the chat page, linked machines, the installer |


## Running the tests

```bash
python3 -m unittest discover -s tests -v
```

The cloud-sync tests need `pip install cryptography`; they are skipped without it. The code map's
accurate readers are tested with `pip install tree-sitter tree-sitter-java tree-sitter-kotlin
tree-sitter-javascript tree-sitter-typescript`, and the regular-expression readers without them.

Before a release, run the end-to-end tests too. They start real servers in a throwaway folder
with the `crewchat` command, and drive them as agents and the owner would:

```bash
python3 -m unittest discover -s e2e -v
```

The chat-page tests need Google Chrome and `pip install websocket-client`, and the installer tests
need `uv`; each is skipped without them. `CREWCHAT_BIN=/path/to/crewchat` tests an installed copy
instead of this checkout, `CREWCHAT_LOAD=80` sends more messages in the load test, and
`CREWCHAT_E2E_KEEP=1` keeps the throwaway folder for a look afterwards. Cloud sync needs a real
Firebase project, so it is covered by `tests/test_cloud.py` with a stand-in store; Windows is not
covered here.

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
