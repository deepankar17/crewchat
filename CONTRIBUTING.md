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
| `install.sh`, `install.ps1` | The installers: macOS and Linux's one line; on Windows the README uses uv and PyPI, and `install.ps1` serves older notes |
| `tests/` | `test_crewchat.py` (one machine), `test_peers.py`, `test_cloud.py` (several machines) |
| `e2e/` | End-to-end tests: real servers, agents' hooks, the chat page, linked machines, the installer |
| `tools/docs_screenshots.py` | Makes the pictures in `docs/images` |

## Running the tests

```bash
python3 -m unittest discover -s tests -v
```

The cloud-sync tests need `pip install cryptography`; they are skipped without it.

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

## Trying a change by hand

Use a throwaway home so you do not touch your real chat:

```bash
export CREWCHAT_HOME=/tmp/crewchat-dev
mkdir -p /tmp/project && cd /tmp/project
python3 ~/code/crewchat/crewchat.py start --no-service --port 8799
```

## Documentation

The guides are in `docs/`. Their diagrams are animated GIFs, made from the specs in
`tools/diagrams/specs.py`: cards and arrows placed on a 1280x720 canvas (or the columns of a
sequence), and the numbered steps that play on them. Change a spec, then remake its GIF:

```bash
pip install websocket-client pillow
python3 tools/diagrams/diagrams.py talk team     # or no names, for every diagram
```

Each image's alt text lists its steps, so keep it in step with the spec.

The screenshots and terminal pictures in `docs/images` are made by a script, from real crewchat
servers with demo data, so they can be remade after a change to the page:

```bash
pip install websocket-client
python3 tools/docs_screenshots.py              # every picture
python3 tools/docs_screenshots.py team files   # only some scenes
```

It needs Google Chrome, and runs in a throwaway folder.

The animated GIFs (`docs/images/demo-*.gif`) are recordings of the real chat page, driven like a
person would: a pointer moves and clicks, text is typed, Enter sends, and scripted agents answer.
The same run writes 1080p MP4 clips to `media/clips/` for videos:

```bash
pip install websocket-client pillow
python3 tools/demo_recordings.py               # every recording
python3 tools/demo_recordings.py talk task     # only some
```

The tutorial video is made the same way, from `tools/tutorial/script.json` (the narration and
slides) and those pictures, with macOS's own voices and video framework:

```bash
python3 tools/tutorial/make_video.py    # macOS only; writes media/ (not in git)
```

The published tutorial is edited in Google Vids, with Vids' AI voiceover. Its script is
`tools/tutorial/scenes.json`. Give each scene its voiceover in Vids first, put the lengths Vids
shows into `durations`, then render one video per scene, exactly that long, so picture and voice
stay together:

```bash
python3 tools/tutorial/vids_scenes.py   # macOS only; writes media/vids/, one MP4 per scene
python3 tools/tutorial/vids_scenes.py --script firebase.json   # the Firebase video
```

The Firebase video's console steps come from screenshots of a throwaway Firebase project, kept
out of git in `media/console/` with a `steps.json` of where each click lands.
`tools/tutorial/console_clips.py` plays them back with a pointer, typing, and private values
blurred, into `media/clips/fb-*.mp4`.

## Reporting bugs

Open an issue with what you ran, what you expected and what happened. For anything
security-sensitive, see [SECURITY.md](SECURITY.md) instead.
