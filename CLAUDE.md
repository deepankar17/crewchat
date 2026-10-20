# Working on crewchat

crewchat is a group chat over MCP for several AI coding agents and their owner. Read
[CONTRIBUTING.md](CONTRIBUTING.md) for the code layout and tests.

## Hard rules

- `crewchat.py`, `crewchat_peers.py` and `crewchat_map.py` use the Python standard library only (3.9+), and
  `crewchat.py` must keep working when copied alone to another machine. A new dependency goes in an
  optional extra, imported lazily, the way `crewchat_cloud.py` is.
- The server listens on 127.0.0.1 only. No telemetry. Code and data stay on the owner's machines.
- Storage follows [docs/storage.md](docs/storage.md): `messages.jsonl` is the source of truth, SQLite
  only for rebuildable local data, Markdown only as generated output.
- Every change comes with a test that fails without it.

## What to work on

The plan is [docs/roadmap.md](docs/roadmap.md): every task with its goal, code locations, approach,
acceptance criteria and dependencies, and the owner's decisions. Follow its suggested order unless
the owner says otherwise. Background research is in
[docs/research/graphify-ideas.md](docs/research/graphify-ideas.md).

For each task:

1. Check its "Depends on" tasks are done.
2. Work on a branch named after the task, e.g. `p0-benchmark`.
3. For speed tasks, run the P0 benchmark before and after, and put both numbers in the pull request.
4. Run `python3 -m unittest discover -s tests -v`; run `e2e/` too when hooks, the chat page or
   linked machines change.
5. Update the task's status in `docs/roadmap.md` in the same pull request.
6. If a spec turns out wrong or a decision is needed, stop and ask the owner rather than guessing.
