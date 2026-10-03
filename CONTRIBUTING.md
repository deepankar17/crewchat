# Contributing

Thanks for helping. crewchat is deliberately small: plain Python 3.9+, standard library only at
its core.

## Ground rules

- **No dependencies in the core.** `crewchat.py` and `crewchat_peers.py` use the standard library
  only, and `crewchat.py` must keep working when copied on its own to another machine. Cloud sync
  (`crewchat_cloud.py`) is the one optional part with libraries.
- **The server binds to 127.0.0.1.** Changes that make it listen elsewhere will not be merged;
  remote access goes through a private network.
- **Tests come with the change.** The suite starts real servers; add a test that fails without
  your change.

## The code

| File | |
|---|---|
| `crewchat.py` | The server, the MCP tools, the chat page, hooks, and every command |
| `crewchat_peers.py` | Linking machines over Tailscale |
| `crewchat_cloud.py` | Linking machines with cloud sync (Firestore) |
| `install.sh`, `install.ps1` | The one-line installers |
| `tests/` | `test_crewchat.py` (one machine), `test_peers.py`, `test_cloud.py` (several machines) |
| `tools/docs_screenshots.py` | Makes the pictures in `docs/images` |

## Running the tests

```bash
python3 -m unittest discover -s tests -v
```

The cloud-sync tests need `pip install cryptography`; they are skipped without it.

## Trying a change by hand

Use a throwaway home so you do not touch your real chat:

```bash
export CREWCHAT_HOME=/tmp/crewchat-dev
mkdir -p /tmp/project && cd /tmp/project
python3 ~/code/crewchat/crewchat.py start --no-service --port 8799
```

## Documentation

The guides are in `docs/`. Diagrams are [Mermaid](https://mermaid.js.org) blocks, which GitHub
draws. In sequence diagrams, avoid `;` (it ends a line) and `#` (it starts a character code) in
message text.

The screenshots and terminal pictures in `docs/images` are made by a script, from real crewchat
servers with demo data, so they can be remade after a change to the page:

```bash
pip install websocket-client
python3 tools/docs_screenshots.py              # every picture
python3 tools/docs_screenshots.py team files   # only some scenes
```

It needs Google Chrome, and runs in a throwaway folder.

## Reporting bugs

Open an issue with what you ran, what you expected and what happened. For anything
security-sensitive, see [SECURITY.md](SECURITY.md) instead.
