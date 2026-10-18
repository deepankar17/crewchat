# Where crewchat keeps its data

A short record of which format holds what, so new features follow one model. Decided in October
2026 while planning the [roadmap](roadmap.md) (task S1).

| Data | Format | Why |
|---|---|---|
| Messages, tasks, claims, roles, progress: anything that syncs | `messages.jsonl`, append-only | The sync model is built on it. Machines merge messages by id over Tailscale and cloud sync alike. It survives crashes and can be read with `tail`. |
| Agents, sessions, the task sheet, settings: small state owned by one machine | `state.json`, `config.json` | Small, rewritten whole, written in one step (temporary file, then rename). |
| Derived local data: the code map, links between tasks, agents and files, search indexes | SQLite (`sqlite3`, standard library), one file per project; the code map one per repository and branch (`maps/<key>.sqlite`, owner, 18 October 2026) | Needs queries that follow links (what depends on this file?), changes a little at a time, and can always be rebuilt. |
| Derived history: the chat's messages by task and words, for "who changed this file and why" (C5) | SQLite, `history.sqlite`, one file per project | Read from `messages.jsonl` from where it got to, only when asked; deleted, it is built again. |
| Local event records: the files agents edit, the history of what agents were doing | SQLite, `events.sqlite`, one file per project | What happened on this machine that no message records, so it cannot be rebuilt; it grows a row at a time and is read by time and by file. Decided by the owner on 18 October 2026. |
| What agents and people read: project briefs, guides, rules | Markdown, generated | Language models read it well. It is output only, never a place data is kept. |

## The rules

1. **`messages.jsonl` is the source of truth.** Everything in SQLite and every generated Markdown
   file is built from it plus the project's code, except the local event records. Deleting a SQLite
   file or a brief loses nothing: the server rebuilds it. Deleting `events.sqlite` loses only that
   history (which files were edited, what agents were doing), never the chat.
2. **Derived data never syncs.** Each machine builds its own from its own checkout. When another
   machine needs a result (a task's suggested areas, an interface-change notice), it travels as an
   ordinary message field in `EXTRA_FIELDS`, so Tailscale and cloud sync carry it with no extra
   work.
3. **One writer.** Only the server process writes to a SQLite file. Hooks and tools go through the
   server's API, as they do today.
4. **Keep it local.** SQLite files live under the project's crewchat folder
   (`~/.crewchat/projects/<id>/`, or `~/.crewchat/` for the first project), never in the project
   repository and never on a network drive, where SQLite's locking is unreliable.
5. **No new dependencies.** `sqlite3` ships with Python, so the core stays standard library only.

## What this rules out

- Moving messages into SQLite. The log works and syncs; a search index can be built from it instead.
- A graph database server such as Neo4j: a separate process and a dependency, for data that fits in
  memory.
- Markdown or JSON files as a store that code queries or several writers update.
