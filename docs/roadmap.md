# Roadmap

The plan for crewchat's next releases: speed fixes, storage rules, code-aware coordination,
multi-machine sync gaps and an analytics page. Each task has a goal, where it lives in the code,
the approach, what counts as done, risks and dependencies. Decisions the owner has made are marked.

Line references were taken at commit d3c38ea (v0.14.0); `crewchat.py`, `crewchat_peers.py` and
`crewchat_cloud.py` are unchanged since, so they hold on main. Re-check them if those files change.

Priority: P1 is highest. Effort: S is a day or less, M a few days, L a week or more.

## Ground rules for every task

- `crewchat.py` and `crewchat_peers.py` stay standard library only. New dependencies go in an optional extra, loaded lazily, like cloud sync.
- The server listens on 127.0.0.1 only. No telemetry. Code and data stay on the owner's machines.
- Measure with the benchmark (P0) before and after every speed change.
- Update this file's status line for a task in the same pull request that changes it.

## Suggested order

1. P0 benchmark, then P1 hook script and P2 tool trim (they cost every agent on every turn).
2. S1 storage doc, P3 to P7.
3. C0 edit events, then C2 areas and C3 interface-change notices; C1 code map alongside.
4. X4 token tracking and X1 endpoint, then X2 analytics page and X3 public demo.
5. Y1 coordinator design, then Y2 to Y4.
6. C4 to C9 as time allows.

## All tasks

| ID | Task | Area | Priority | Effort | Status | Depends on |
|---|---|---|---|---|---|---|
| [P0](#p0) | Benchmark harness | Speed and efficiency | P1 | S | Done | none |
| [P1](#p1) | Lightweight hook script | Speed and efficiency | P1 | M | Done | P0 |
| [P2](#p2) | Trim agent tool schemas and outputs | Speed and efficiency | P1 | M | Done | P0 |
| [P3](#p3) | Fast startup from the message log | Speed and efficiency | P2 | S | Done | P0 |
| [P4](#p4) | Split fixed data out of /api/poll | Speed and efficiency | P2 | S | Done | P0 |
| [P5](#p5) | Coalesce state.json writes | Speed and efficiency | P3 | S | Done | P0 |
| [P6](#p6) | Index messages by id | Speed and efficiency | P3 | S | Done | none |
| [P7](#p7) | Compress and cache the chat page | Speed and efficiency | P3 | S | Done | none |
| [S1](#s1) | Storage decision record | Storage rules | P1 | S | Done | none |
| [C0](#c0) | Opt-in full-path edit events | Code-aware coordination | P1 | S | Done | P1 |
| [C1](#c1) | Built-in code map (stdlib) | Code-aware coordination | P2 | L | Done | S1 |
| [C9](#c9) | Accurate parsers as an optional extra | Code-aware coordination | P3 | M | To do | C1 |
| [C2](#c2) | Auto-suggest and check task areas | Code-aware coordination | P1 | M | Done | C0 |
| [C3](#c3) | Interface-change notices | Code-aware coordination | P1 | M | Done | C0, C1 |
| [C4](#c4) | Blast-radius warnings | Code-aware coordination | P2 | S | Done | C1, C2 |
| [C5](#c5) | Collaboration history links | Code-aware coordination | P2 | M | To do | C0, S1 |
| [C6](#c6) | Project brief for joining agents | Code-aware coordination | P2 | S | To do | C1 |
| [C7](#c7) | Lead split suggestions | Code-aware coordination | P3 | M | To do | C1 |
| [C8](#c8) | Code map view on the chat page | Code-aware coordination | P3 | M | Done | C1, X1 |
| [Y1](#y1) | Automatic coordinator (no fixed machine) | Multi-machine sync gaps | P2 | S | In progress | none |
| [Y2](#y2) | Claims when the posting machine is off | Multi-machine sync gaps | P2 | M | To do | Y1 |
| [Y3](#y3) | Catch-up beyond 24 hours | Multi-machine sync gaps | P2 | M | Done | Y1 |
| [Y4](#y4) | Shared files over cloud sync | Multi-machine sync gaps | P2 | M | To do | Y1 |
| [X1](#x1) | Analytics data endpoint | Analytics page | P1 | M | Done | P4, X4 |
| [X4](#x4) | Token and cost tracking | Analytics page | P1 | M | Done | P1 |
| [X2](#x2) | Analytics show-off page | Analytics page | P1 | L | Done | X1 |
| [X3](#x3) | Demo mode with sample data | Analytics page | P2 | S | Done | X2 |

## Speed and efficiency

Make every agent turn cheaper and the server lighter. Benchmark first.

### P0

**Benchmark harness** · P1 · effort S · Done

**Goal.** Measure before changing anything, so every speed-up below can be proven and regressions caught.

**Where in the code**

- e2e/ (new bench script)
- crewchat.py Hub, /api/hook, /api/poll

**Approach**

- Script starts a server on a temp home, simulates 5 agents and 5,000 messages.
- Times: one hook call end to end, server startup with a large messages.jsonl, one /api/poll round trip and its payload size.
- Prints a small table; optionally fails CI if a number regresses by more than 20%.

**Done when**

- Runs with one command, stdlib only.
- Baseline numbers recorded in the PR that adds it.

**Risks**

- Timings are noisy on CI runners; compare medians of several runs.

**Depends on:** nothing

**Status.** Done: `python3 e2e/bench.py` (`--json FILE` saves the medians, `--compare FILE` exits 1 on one
over 20% worse; not run in CI). It prints the Python and the crewchat command it ran, because the numbers
depend on both: from a checkout the hooks run `python crewchat.py`, which compiles the file on every call,
while an installed crewchat (`CREWCHAT_BIN=...`) imports a cached `.pyc`. Baseline in the pull request.

### P1

**Lightweight hook script** · P1 · effort M · Done

**Goal.** Stop paying a full crewchat.py import (about 150 ms measured) on every agent tool call.

**Where in the code**

- crewchat.py:5993 hook_command
- crewchat.py:6168 set_claude_hooks
- crewchat.py:6648 run_hook
- Cursor hooks ~6211

**Approach**

- New tiny module (e.g. crewchat_hook.py) importing only json, urllib, sys, os.
- It reads the hook payload, posts to /api/hook and prints the answer; same contract as run_hook today.
- hook_command points at it; installer copies it next to crewchat.py.
- Throttle the tool event client-side: skip if the last one for this session was under ALIVE_EVERY seconds ago (a stamp file in the temp dir).

**Done when**

- Hook call under 50 ms on Linux in the benchmark.
- Rejoining a folder replaces old hook commands with the new ones.
- Stop-hook long-poll and wake-ups behave exactly as before (e2e passes).

**Risks**

- Windows launcher paths and quoting; test on PowerShell and cmd.
- Older installs must keep working until rejoined.

**Depends on:** [P0](#p0)

**Status.** Done, with the owner's choices (17 Oct): only the tool-call hook moved (prompt, stop and
permission hooks fire once a turn and keep running crewchat); crewchat.py writes the script,
`~/.crewchat/crewchat-hook.py`, from its own code (`hook_script`), so there is one copy of the logic and a
crewchat.py copied alone still works; it posts to this machine's server over a plain socket (urllib only
for a chat on another machine); all six tools use it (Codex and Gemini CLI ask once more to review the
changed hook). Measured on macOS, not Linux: an installed crewchat's tool-call hook from about 78 ms to
about 20 ms; numbers in the pull request.

### P2

**Trim agent tool schemas and outputs** · P1 · effort M · Done

> **Decided:** Breaking change, no aliases (owner, 17 Oct).

**Goal.** Cut the ~3,300 tokens of hub_* tool definitions every agent loads, and keep tool answers short.

**Where in the code**

- crewchat.py:3203-3476 TOOLS
- crewchat.py:3487 call_tool
- crewchat.py:2293 inbox, 2327 history
- generated guides via write_guides 7791

**Approach**

- Merge hub_task_view/add/take/assign/update into one hub_task with an action field.
- Move usage prose from descriptions into the generated skill/guide, which agents read on demand.
- Compact one-line-per-message format for hub_inbox and hub_history, with a character cap and a 'more' hint.
- Ship as a breaking change: old task tool names are removed, not aliased.
- On upgrade, the server rewrites the guides and skills in every connected folder, so agents pick up the new tool on their next session.
- Release note in CHANGELOG.md lists the old and new names.

**Done when**

- Tool definitions JSON under 7,000 characters.
- All e2e flows still pass with the merged tool.
- Inbox output of 50 messages fits under a fixed cap.

**Risks**

- Agents in sessions started before the upgrade call tools that no longer exist until they restart; the error message should say to restart the session.

**Depends on:** [P0](#p0)

**Owner's note:** no need to maintain the backward compatibility. we can do the change

**Status.** Done. The tools merged were `hub_tasks`, `hub_task_add`, `hub_take`, `hub_assign` and
`hub_update` (the spec's "hub_task_view/add/take/assign/update"); `hub_ask` and `hub_answer` stay. The tool
definitions went from 13,074 to 6,948 characters, with a description for every parameter and `readOnlyHint`
on every tool (Glama's rating asks for both). Owner's choices (17 Oct): `hub_inbox` and `hub_history` give
whole messages up to 6,000 characters, the rest unread with a "more" hint, never cutting one; the usage text
went to the guide, and a few task lines to the MCP instructions (PROTOCOL, still under the 2,000 characters
Claude Code keeps), so Gemini CLI, Qwen Code and Codex without an AGENTS.md keep them; the benchmark reports
the tool definitions and a 50-message inbox.

### P3

**Fast startup from the message log** · P2 · effort S · Done

**Goal.** Startup time and memory stay flat however long messages.jsonl grows.

**Where in the code**

- crewchat.py:1099 _load
- crewchat.py:2040 log rewrite after adopt_open_tasks

**Approach**

- Read the file backwards in blocks until KEEP_MESSAGES full lines are found.
- Optionally move entries older than N months into messages-YYYY-MM.jsonl archives (feeds the search index later).

**Done when**

- Startup with a 500 MB log is as fast as with a 1 MB log (benchmark).
- Same in-memory messages and next_seq as the current loader on the test fixtures.

**Risks**

- A partly written last line must be skipped, as now.
- next_seq must still come from the true last message.

**Depends on:** [P0](#p0)

**Status.** Done: `_load` reads messages.jsonl from its end (`read_back`) until it has KEEP_MESSAGES, and
`_rewrite_log` streams the file. Archives were left out (optional, and no N was given). Lines now end at `\n`
only (a trailing `\r` dropped): the old `splitlines()` also split on U+2028 and the like, losing a message
whose text held one. `bench.py --log-mb 500` times startup with a grown log; numbers in the pull request.

### P4

**Split fixed data out of /api/poll** · P2 · effort S · Done

**Goal.** The chat page's long-poll carries only what changed, not every constant on every change.

**Where in the code**

- crewchat.py:2687 Hub.poll
- crewchat.py:4418 /api/poll route
- CHAT_PAGE fetch calls ~9112, 11755

**Approach**

- New /api/boot returning roles, tools, presets, art, pictures, models, advisors, names, images, max_upload, releases.
- Page calls it once at load and on server version change.
- In poll, find the first new message with bisect on seq instead of scanning all 2,000.

**Done when**

- Poll payload shrinks measurably (benchmark).
- Classic page keeps working, or is updated in the same change.

**Risks**

- Both CHAT_PAGE and CLASSIC_PAGE read these fields.

**Depends on:** [P0](#p0)

**Status.** Done. The classic page was gone already. Owner's choices (17 Oct): roles move to /api/boot as
well, and every poll carries `boot`, a tag of what /api/boot returns, which changes with the server version
or the roles, so the page fetches it again then; `statuses` moved too. The poll finds new messages by
halving (no `bisect` key on Python 3.9). The answer for one new message went from 5,659 to 2,333 bytes.

### P5

**Coalesce state.json writes** · P3 · effort S · Done

**Goal.** Fewer full rewrites of state.json as the task sheet grows.

**Where in the code**

- crewchat.py:1170 _save (42 call sites)
- write_json 5956

**Approach**

- _save marks state dirty; a background thread writes at most once a second.
- Keep an immediate-write path for task claims, links and anything that must survive a crash.
- Flush on shutdown.

**Done when**

- No lost claims in a kill -9 test.
- Write count per minute drops under load (benchmark).

**Risks**

- Up to one second of roster changes lost on a crash; acceptable for presence data only.

**Depends on:** [P0](#p0)

**Status.** Done. A one-shot timer (`_save_soon`, `SAVE_DELAY` 1 s) rather than a thread of its own. Owner's
choices (17 Oct): read positions, status lines, sessions and presence wait; claims, links, the sheet and
progress, renames, removals and owner settings are written at once (an immediate write takes what waits
with it). SIGTERM and `crewchat stop` flush first. The benchmark counts state.json writes for a fixed busy
load in its own process: 179 before, 60 after.

### P6

**Index messages by id** · P3 · effort S · Done

**Goal.** Remove linear scans when looking up a message by id.

**Where in the code**

- Hub.ids (id -> seq)
- claim_task crewchat_peers.py:316
- stale rename scan ~1160

**Approach**

- Keep a second dict id -> message, maintained in _store and trimming.
- Replace next(m for m in messages if ...) lookups with it.

**Done when**

- Unit tests pass; no behaviour change.

**Risks**

- Forgetting to update the dict when messages are renamed or trimmed.

**Depends on:** nothing

**Status.** Done: `Hub.by_id`, kept by `_store` (and its trimming) and rebuilt with `ids` by `_index` after a
load, `_apply_renames` and `adopt_open_tasks`; used by `_task`, the advisor's answers, `adopt_open_tasks` and
crewchat_peers' `claim_task`. `_apply_renames` stays a scan (it also follows `task` references) and runs only
at load when there are renames.

### P7

**Compress and cache the chat page** · P3 · effort S · Done

**Goal.** Fast reloads of the 156 KB page, especially on phones.

**Where in the code**

- crewchat.py:4181 Handler, route for /

**Approach**

- Check current behaviour first.
- gzip once at startup (stdlib gzip), serve with Content-Encoding when accepted.
- ETag from the page hash; answer 304 on If-None-Match.

**Done when**

- Second load transfers under 1 KB.
- CSP headers unchanged.

**Risks**

- None significant.

**Depends on:** nothing

**Status.** Done. The page was 170 KB and sent with `Cache-Control: no-store`, so the browser kept nothing and
never asked again with an ETag. Owner's choice (18 Oct): the page's HTML (the same for everyone, no chat data)
is `private, no-cache` with an ETag, so a reload is a 304 of 491 bytes; the first load is gzipped (47 KB). The
API stays `no-store`, the sign-in check comes before any 304, and the CSP is unchanged.

## Storage rules

`messages.jsonl` stays the source of truth; SQLite for rebuildable local data; Markdown only as output.

### S1

**Storage decision record** · P1 · effort S · Done

> **Decided:** docs/storage.md written in the roadmap pull request; done when it merges.

**Goal.** Write down the storage rules so future work follows one model.

**Where in the code**

- docs/ (new docs/storage.md or a section in how-it-works.md)
- CONTRIBUTING.md

**Approach**

- messages.jsonl stays the source of truth and the only thing that syncs.
- state.json and config.json stay for small machine-owned state.
- SQLite (stdlib sqlite3) for local derived data: code map, collaboration links, search index. Always rebuildable, never synced, kept under ~/.crewchat/projects/<id>/, written only by the server process.
- Markdown only as generated output for agents and people (briefs, guides), never as storage.

**Done when**

- Doc merged; referenced from CONTRIBUTING.md.

**Status.** Done: docs/storage.md merged in #60, linked from CONTRIBUTING.md.

**Depends on:** nothing

## Code-aware coordination

Ideas borrowed from Graphify, built stdlib-only inside crewchat. Software projects only.

### C0

**Opt-in full-path edit events** · P1 · effort S · Done

> **Decided:** On by default for the Software team pack, off for every other pack (owner, 17 Oct).

**Goal.** Let the server know which files each agent edits in coding projects, which C2, C3, C5 and the analytics page need.

**Where in the code**

- crewchat.py:6600 tool_summary (basename only today)
- PostToolUse / afterFileEdit hooks 6168, 6211
- activity off setting
- docs/privacy.md
- ROLE_PACKS crewchat.py:224, project_pack ~576

**Approach**

- New per-project setting edit_tracking: on, off, or auto.
- auto (the default) means on when the project's role pack is software (DEFAULT_PACK, crewchat.py:179) and off for youtube, instagram, business and custom packs.
- When on, the hook sends the project-relative path of edited files (never contents, never arguments).
- Always off when activity is off, whatever the pack.
- The owner can override it either way on the chat page.
- Server keeps the events locally; they travel to other machines only as derived areas or notices.

**Done when**

- A new software project tracks edits; a new YouTube project doesn't.
- Switching a project's pack updates the auto setting.
- Setting visible on the chat page and in commands.
- privacy.md updated in the same change.
- Paths outside the project folder are never sent.

**Risks**

- Changes the current basename-only privacy promise; needs a clear doc.

**Depends on:** [P1](#p1)

**Owner's note:** we can on it of coding projects and keep it off for other project types

**Status.** Done. Owner's choice (18 Oct): the events go in a local SQLite file, `events.sqlite` per project,
written by the server only and never synced (docs/storage.md now says so). The light tool-call hook works out
the edited file's path relative to the project (`edited_path`: Claude Code's Edit, MultiEdit, Write and
NotebookEdit, Cursor's afterFileEdit, the other tools' own names; nothing outside the folder) and sends it only
while the server's last answer said edits are kept; the server checks it again (`clean_edit_path`) and keeps
it with the agent and the task it works on. `edit_tracking` per project: auto (on for the software pack),
on, off; always off with activity off. Shown and set in the project panel ("Files agents edit"), by
`crewchat edits` and `/api/edits`. Codex's apply_patch names no single file, so its edits are not seen yet.

### C1

**Built-in code map (stdlib)** · P2 · effort L · Done

> **Decided:** After Python: Java, React, Angular and Kotlin (owner, 17 Oct). One map per repository and
> branch name, not per project (owner, 18 Oct): `projects/<id>/maps/<key>.sqlite`, the key a hash of the
> repository (its origin remote, else its top folder) and the branch, so checkouts of one branch share a map.

**Goal.** A local map of files, definitions and imports that CrewChat can query, with no dependencies.

**Where in the code**

- new crewchat_map.py, lazily imported like cloud sync in cmd_serve ~5561
- ~/.crewchat/projects/<id>/map.sqlite

**Approach**

- Tables nodes(id, kind, path, name, line, lang) and edges(src, dst, kind, certain).
- Python via the stdlib ast module: modules, classes, functions, signatures, imports, calls where resolvable.
- Java and Kotlin via regex: package, import (including wildcard and static), class, interface, object, fun and method declarations. Imports resolve to files by package path under src/main/java and src/main/kotlin, so those links count as certain.
- React (JS, JSX, TS, TSX) via regex: import and export from relative paths, resolved with .ts/.tsx/.js/.jsx and index files, plus tsconfig paths aliases. Components are exported functions or classes whose names start with a capital letter.
- Angular: everything React gets, plus @Component, @NgModule, @Injectable and the selector each component declares, so a template that uses <app-cart> links to CartComponent. Constructor injection links a component to its services.
- Anything regex can't resolve is stored with certain = 0 and only ever used as a soft hint.
- Rebuild changed files on edit events, and on git HEAD change polled by a thread like reclaim_checks 7278.
- Every answer carries the commit hash it was built from.
- Only built for projects where edit tracking is on (software pack by default).

**Done when**

- Python, Java, Kotlin, React and Angular sample repos each map imports to the right files, checked against a hand-made list.
- Builds the crewchat repo itself in under 2 seconds.
- Incremental update of one file under 100 ms.
- Deleting the file and restarting rebuilds it.

**Risks**

- Regex parsing misses edge cases: generics, annotations spanning lines, barrel files that re-export everything. C9 covers the accurate route.
- Gradle multi-module and monorepo layouts: cap file count and skip build/, node_modules/ and other generated folders.

**Depends on:** [S1](#s1)

**Owner's note:** java, react, angular, kotlin

### C9

**Accurate parsers as an optional extra** · P3 · effort M · To do

**Goal.** Exact parsing for Java, Kotlin, TS/JS and Angular for users who opt in, without adding dependencies to the core.

**Where in the code**

- crewchat_map.py
- pyproject.toml extras, next to crewchat[cloud]

**Approach**

- New extra: pip install "crewchat[map]" brings tree-sitter and the grammars for Java, Kotlin, TypeScript, TSX and JavaScript.
- crewchat_map.py uses tree-sitter when it can be imported, else falls back to the C1 regex parser.
- With tree-sitter, links between calls and signatures become certain, which makes interface-change notices (C3) work for these languages, not only Python.

**Done when**

- Same map tables either way; the page and tools can't tell which parser built them, except for more certain links.
- Core install still has zero dependencies.

**Risks**

- Grammar wheels per platform; test Windows and macOS ARM.

**Depends on:** [C1](#c1)

### C2

**Auto-suggest and check task areas** · P1 · effort M · Done

> **Decided:** Suggest when a task goes to review or done: the folders edited while it was held, grouped,
> at most five. The owner accepts with Accept files in the task panel (or `crewchat tasks areas ID accept`),
> the lead with a new hub_task action `areas` (owner, 18 Oct).

**Goal.** Fill in a task's areas from what the agent actually edits, and catch overlaps the prefix check misses.

**Where in the code**

- crewchat.py:943 areas_overlap
- crewchat.py:2440 _not_yet
- hub_task_add 3364-3375
- areas in EXTRA_FIELDS 110

**Approach**

- Record files edited while an agent holds a task (from C0).
- Post a suggested areas update for the task; owner or lead can accept.
- Widen the overlap check: same prefix OR a direct import link between the two areas (from C1, when present).
- Derived areas travel as normal message fields, so other machines get them with no sync work.

**Done when**

- A test where two tasks share no prefix but one imports the other is flagged as overlapping.
- Works with C0 alone (no map), just less precisely.

**Risks**

- Noisy suggestions if an agent touches many files; cap and group by folder.

**Depends on:** [C0](#c0)

### C3

**Interface-change notices** · P1 · effort M · Done

**Goal.** When an agent changes a function or class signature, agents whose tasks depend on it hear about it at once.

**Where in the code**

- edit events from C0
- stop hook delivery run_hook 6648
- new message kind or field for notices

**Approach**

- Keep the last parsed signatures per Python file (ast).
- On an edit, re-parse and diff: added, removed or changed parameters, renamed or removed public names.
- Post a short notice (about 130 tokens) to holders of tasks whose areas import that file, delivered through the stop hook.
- Rate-limit per file to avoid bursts while someone is mid-edit.

**Done when**

- Renaming a parameter in a function imported by another agent's area sends exactly one notice.
- No notice for body-only changes.

**Risks**

- Python only at first.
- Too many notices make agents ignore them; tune thresholds with real use.

**Depends on:** [C0](#c0), [C1](#c1)

### C4

**Blast-radius warnings** · P2 · effort S · Done

> Built after C2, which already keeps two tasks over certainly linked code from running at once: so a firm
> note mostly comes with a task given by the lead or the owner (which C2 does not stop), and taking one
> brings the hints on guessed links.

**Goal.** Warn, not block, when a task is taken that touches code another agent's task depends on.

**Where in the code**

- crewchat.py:2440 _not_yet
- hub_task_take

**Approach**

- Use C1's links, direct dependencies only at first.
- Hard warning only on certain links; soft hint on guessed ones.

**Done when**

- Warning text names the other task and agent.
- Never prevents taking the task.

**Risks**

- Overlap with C2; may merge into it.

**Depends on:** [C1](#c1), [C2](#c2)

### C5

**Collaboration history links** · P2 · effort M · To do

**Goal.** Answer 'who changed this file, for which task, and what was said about it'.

**Where in the code**

- messages.jsonl (source)
- map.sqlite (derived tables)
- new hub tool or an argument on hub_history

**Approach**

- Build tables task-file, agent-file, message-task from the log and edit events.
- Rebuild from scratch on startup if missing; append as events arrive.
- Expose a small query for agents and for the analytics page.

**Done when**

- Asking about a file returns its tasks, agents and the key messages, under a size cap.

**Risks**

- History from before C0 has no file links.

**Depends on:** [C0](#c0), [S1](#s1)

### C6

**Project brief for joining agents** · P2 · effort S · To do

**Goal.** A new agent starts with a short map of the project instead of exploring from scratch.

**Where in the code**

- crewchat.py:7791 write_guides
- generated skill / AGENTS.md block

**Approach**

- Generate a Markdown brief from C1: entry points, most-imported files, main modules, current tasks and who holds them.
- Keep it under a fixed size; refresh on HEAD change.

**Done when**

- Brief stays under about 1,500 tokens on the crewchat repo itself.

**Risks**

- A stale brief misleads; stamp it with the commit hash.

**Depends on:** [C1](#c1)

### C7

**Lead split suggestions** · P3 · effort M · To do

**Goal.** Help the lead hand each agent a module that hangs together.

**Where in the code**

- lead role text in ROLES ~114
- new hub tool or hub_task action

**Approach**

- Group files by connected imports plus folders (no library needed).
- Lead asks for groups touching a task's areas and assigns one per agent.

**Done when**

- On the crewchat repo, groups are sensible on manual review.

**Risks**

- Single-file repo like crewchat.py itself gives one group; fall back to functions or sections.

**Depends on:** [C1](#c1)

### C8

**Code map view on the chat page** · P3 · effort M · Done

**Goal.** See files and modules coloured by which agent holds them.

**Where in the code**

- CHAT_PAGE views (showView ~8408)
- new owner-only /api/map

**Approach**

- Inline SVG or canvas only (CSP allows no CDN).
- Fold into the analytics page (X2) rather than a separate view.

**Done when**

- Readable at 500 files; collapses to modules beyond that.

**Risks**

- Large graphs get unreadable; show modules, expand on click.

**Depends on:** [C1](#c1), [X1](#x1)

## Multi-machine sync gaps

No fixed primary. Whichever machine is online coordinates: offline claims, catch-up and files.

### Y1

**Automatic coordinator (no fixed machine)** · P2 · effort S · In progress

> **Decided:** No always-on machine assumed. Whichever machine is online takes over as coordinator for the session (owner, 17 Oct).

**Goal.** Any online machine can step in as coordinator, so offline claims, catch-up and file serving keep working whichever machines happen to be on.

**Where in the code**

- docs/cloud-sync-design.md (new section)
- crewchat_peers.py Mesh, PeerSync
- crewchat_cloud.py CloudSync, meta/ collection
- chat page machines strip (with X2)

**Approach**

- The coordinator has three jobs only: settle claims for tasks whose posting machine is off (Y2), serve catch-up history (Y3) and serve files (Y4).
- It never orders messages. Messages stay leaderless, so a hand-over never loses or reorders the chat.
- Cloud mode: a lease document meta/coordinator {device, term, expires}. The holder renews about every 30 s with a 90 s lease; any online machine takes over an expired lease in a Firestore transaction and increments term.
- Tailscale mode has no referee, so every machine applies the same rule to the members it can reach: the owner's preferred machine if online, else the one with the lowest tag. Term goes up on every change.
- The owner can mark a preferred machine; it takes the role back whenever it comes online.
- Every decision the coordinator makes carries its term and device, so later conflicts can be settled.
- The chat page shows which machine is coordinating now.

**Done when**

- Switch off the coordinator: another machine takes over within 2 minutes and no claim stays stuck.
- Bring the preferred machine back: it takes the role back without disrupting work.
- Network split in Tailscale mode: after it heals, each task has one holder and the other agent is told to give it back.

**Risks**

- Tailscale mode can have two coordinators during a network split, so two agents may briefly hold one task. Resolution: the earlier claim wins (Lamport counter plus machine tag, not wall clocks), and the loser gets a message.
- Machines that are all off for more than 24 hours still lose cloud messages from that gap; see Y3.

**Depends on:** nothing

**Status.** In progress: the coordinator itself is done (docs/cloud-sync-design.md section 8a). Cloud sync:
the lease `meta/coordinator`, renewed every 30 s for 90 s, taken with the conditional write `transact` (a
client transaction needs permissions a signed-in user lacks), handed on at a clean stop, cleared with the
account. Tailscale: the preferred machine if reachable, else the lowest reachable tag; the preference
travels in pull answers (newest wins). `crewchat coordinator [NAME|none]`, `/api/coordinator`, and the
page's connection line. Tested: takeover when the coordinator goes off (Tailscale within a minute in the
test; cloud at the next renewal after the lease runs out, so under 2 minutes), the preferred machine taking
the role back. The two "Done when" checks about claims (none stuck; one holder after a split) need Y2,
where the coordinator settles them.

**Owner's note:** no, users may not have an always on machine. it is possible that they will try to keep a machine always on, but it is possible that his one of the devices will be off due to some personal reasons. in that case whichever device is on, we need to make that as primary device for that session

### Y2

**Claims when the posting machine is off** · P2 · effort M · To do

**Goal.** Over Tailscale, tasks posted on a machine that is off can still be taken.

**Where in the code**

- crewchat_peers.py:316 claim_task
- crewchat_peers.py:197 Mesh.claim

**Approach**

- If the posting machine is unreachable, send the claim to the current coordinator (Y1).
- The coordinator settles it under its term; the posting machine adopts the decision when it comes back.
- If both decided during a split, the earlier claim wins and the other agent is told to give the task back.

**Done when**

- Posting machine offline: exactly one taker across three machines.
- No 'cannot reach X, try again' errors while any machine is coordinating.

**Risks**

- Split decisions during network partitions.

**Depends on:** [Y1](#y1)

### Y3

**Catch-up beyond 24 hours** · P2 · effort M · Done

> **Decided:** Over Tailscale, the messages of a machine that is off by then come through the coordinator
> (owner, 18 Oct).

**Goal.** A machine that was off for days catches up on every message, from whichever machine is online.

**Where in the code**

- crewchat_cloud.py TTL_HOURS, marks/synced_to

**Approach**

- On reconnect, if the gap is longer than the cloud TTL, ask the current coordinator for the missing messages.
- Over Tailscale: a direct pull, paged.
- Cloud only: the coordinator writes a short-lived encrypted catch-up batch to Firestore for that machine, deleted once read.
- If every machine was off for more than the TTL, those messages are gone; make TTL configurable (cloud_ttl_hours already exists) and say so in the docs.

**Done when**

- Machine offline 3 days gets every message after reconnect, in order, provided some machine was online during that time.

**Risks**

- Quota use for large catch-ups; page it.

**Depends on:** [Y1](#y1)

### Y4

**Shared files over cloud sync** · P2 · effort M · To do

**Goal.** Files shared on one machine open on another when using cloud sync.

**Where in the code**

- crewchat_cloud.py (files not carried today)
- files/<id>/ storage
- fetch_file in crewchat_peers.py:335

**Approach**

- Small files (cap, e.g. 5 MB): encrypt and chunk into Firestore documents (1 MB limit each), deleted after delivery like messages.
- Larger files: fetch from the machine that shared them; if it's off, from the coordinator if it already holds a copy.
- Otherwise show 'on machine X, offline' with no error.

**Done when**

- A 5 MB screenshot opens on another cloud-synced machine.
- Ciphertext only in Firestore.

**Risks**

- Firestore quota and cost for big files; set a size cap.

**Depends on:** [Y1](#y1)

## Analytics page

A live view of the crew at work, computed on the owner's machine, plus a public demo.

### X1

**Analytics data endpoint** · P1 · effort M · Done

> **Decided:** Yes, show token and cost numbers per agent (owner, 17 Oct). Collection is specced in X4.

**Goal.** One owner-only endpoint that computes everything the analytics page shows, locally.

**Where in the code**

- new /api/analytics behind the owner_actions auth (~4805)
- sources: messages.jsonl, state.json (agents, sheet, progress, claims, reclaims, verdicts), activity, edit events (C0)

**Approach**

- Aggregate by time bucket: messages per agent, per kind, per machine.
- Who-talks-to-whom matrix (from, to).
- Task funnel and timings: posted, taken, in progress, review, done; cycle time and time blocked.
- Agent timelines: working, listening, idle, offline, from activity and seen times.
- Advisor verdicts, reclaims, files shared.
- Cache results; recompute incrementally from new messages.
- Token totals per agent, per task and per model from X4: input, output, cache read, cache write.
- Estimated cost from a model price table, labelled as an estimate.

**Done when**

- Responds in under 200 ms on 50,000 messages.
- Nothing leaves the machine; no new network calls.
- Works with zero edit events (C0 off) and shows less.

**Risks**

- Activity history is in memory today; may need a small persisted timeline to show past days.

**Depends on:** [P4](#p4), [X4](#x4)

**Status.** Done. Owner-only `POST /api/analytics {days}`: `Analytics` keeps running totals built once
from the whole messages.jsonl in a background thread at start (counting the log up to the number it held then;
newer messages come through `_store`, so nothing counts twice and no id set is kept), then fed every new
message. Messages per hour and per day (by agent and machine), by kind, who-to-whom pairs, the task funnel
(posted, taken, done, in review, blocked now; median time to take, cycle time, time blocked; review pass rate;
done today), advisor verdicts, reclaims, files shared, each agent's message counts and first and last message
with its activity and online state now, and X4's tokens and estimated cost. 50,000 messages: built in about
140 ms, answered in under 1 ms. Agent timelines (owner, 18 Oct: kept in the local events.sqlite, with C0's
edits): each change of an agent's state (working, listening, idle) is recorded as it happens, an idle one when
a listening hook does not ask again within its grace, and `timelines` gives [start, end, state] pieces per agent
for the days asked; nothing is recorded while activity is off.

**Owner's note:** yes i want

### X4

**Token and cost tracking** · P1 · effort M · Done

**Goal.** Know how many tokens each agent, task and model uses, and roughly what that costs, without reading any conversation content.

**Where in the code**

- light hook script from P1
- Claude Code Stop hook input (transcript_path)
- new per-agent usage table in map.sqlite or state
- /api/analytics (X1)

**Approach**

- Claude Code: the Stop hook already receives the session's transcript path. The hook reads only the new lines since its last offset and keeps just the usage fields (model, input, output, cache read, cache write tokens), never the text.
- It sends the totals since the last turn with its normal /api/hook call; the server adds them to the agent and to the task it holds.
- Cursor: check what its hooks expose. If there's no usage data, show 'not available for Cursor' rather than a guess.
- Cost: multiply by a price table per model kept in crewchat.py, updated with each release. Label it 'estimated, at API prices', because people on a Claude subscription don't pay per token.
- Respect privacy: off when activity is off, a per-project toggle, and usage numbers stay on the machine except as totals shown on the owner's page.
- Usage numbers can travel to other machines as small message fields so the analytics page shows the whole crew.

**Done when**

- Token totals for a test session match the transcript's own usage fields.
- No message text is ever read into the server.
- Cost shows per agent, per task and per day, marked as an estimate.

**Risks**

- The transcript format isn't a public, stable API; verify it against the current Claude Code version and test on each release.
- Price table goes stale; show the date it was last updated.

**Depends on:** [P1](#p1)

**Status.** Done. The stop hook (full crewchat, not the light script: only the tool-call hook moved in P1)
reads the transcript from its last offset and sends `{model: [input, output, cache read, cache write, cache
write 1h]}` as its own `/api/hook` call (`event: usage`), counting each assistant message once (its lines
repeat the usage). Kept in state.json (`usage`: day, agent, model; and on the sheet row of the task the agent
last reported on). Cursor: its hooks give a transcript path but no token counts, and its transcript holds
none, so it shows "not available". Prices from Anthropic's list of 2026-10-06 (`PRICES`, `PRICES_DATE`).
Shown by `crewchat usage`, `/api/usage` and a line in the agent panel. Not done yet: carrying the totals to
other machines (the spec's "can"), left for X1.

### X2

**Analytics show-off page** · P1 · effort L · Done

**Goal.** A page that makes people say 'this is awesome': the crew working, live, at a glance.

**Where in the code**

- CHAT_PAGE new view next to Chat / Task sheet / Commands (view buttons ~8278)
- inline SVG and canvas only (CSP)

**Approach**

- Hero: live crew constellation. Agents as nodes (picture, role colour, machine), messages animate along edges as they happen, edge width = message volume.
- Task river: tasks flowing from posted to done, with counts and median cycle time.
- Agent swimlanes: a day timeline per agent showing working, listening, idle.
- Machines strip: each machine, its agents, online state, sync mode.
- Headline numbers: tasks done today, messages, active agents, median time to take a task, review pass rate.
- Replay: scrub through the day and watch the constellation and river change.
- Code heat map once C0/C1 exist: files coloured by edits and owner.
- Presentation mode: full screen, large type, auto-rotating panels.
- Token and cost panel: spend per agent and per task, tokens per completed task, and a cost-over-time line (from X4).

**Done when**

- Looks right in light and dark, desktop and phone.
- Live updates through the existing poll, no extra polling loop.
- 60 fps animation with 20 agents; respects reduced motion.

**Risks**

- Must stay a single inline page inside crewchat.py, which already holds 156 KB of page; consider serving it as a separate string loaded only when opened.

**Depends on:** [X1](#x1)

### X3

**Demo mode with sample data** · P2 · effort S · Done

> **Decided:** Public demo on the website, linked from GitHub (owner, 17 Oct).

**Goal.** Show the analytics page to people (website, talks, screenshots) without exposing a real project.

**Where in the code**

- X2 page
- site/ (project website)
- docs/images for README screenshots

**Approach**

- A generated sample crew and a day of activity, clearly labelled as a demo.
- Static build of the analytics page that runs from a bundled JSON file, no crewchat server.
- Published under site/ on crewchat.dev, e.g. /demo.
- README gets a 'See the live demo' link near the top, plus refreshed screenshots and a short GIF made from the demo.
- The same sample data powers a demo toggle inside the app.

**Done when**

- Demo page loads on crewchat.dev with no server and no sign-in.
- README links to it.
- Demo data never mixes with real data; the page says it's a demo.

**Risks**

- Sample data must look realistic without inventing a real customer.

**Depends on:** [X2](#x2)

**Owner's note:** yes we can publish demo on the website and also put a link on github
