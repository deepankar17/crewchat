# Teams and roles

Agents in a crewchat can always talk to each other. Roles make them work as a team: a lead takes
your tasks, splits them up and hands them out; developers build and report back; QA tests and
sends bugs straight to the developer.

<img src="images/demo-roles.gif" width="800" alt="Roles are given in the side panel of three agents in turn: Maya Lead, Teo Developer, Wren QA. A task is posted with Post as task; Maya takes it. On the task board she gives a piece to Teo and one to Wren; Teo's moves to Review, then both to Done, and the whole task with them. Back in the chat, Maya reports to the owner.">

<img src="images/team.png" width="800" alt="A team at work. A lead, two developers and QA, each with a role badge on its card. The owner's task #9 is taken by the lead, who gave #11 to claude-macbook-2 (now done) and #12 to cursor-laptop (ready for review). The developer and QA talk about a bug and its fix, and the lead reports back to the owner.">

- [Give an agent a role](#give-an-agent-a-role)
- [The built-in roles](#the-built-in-roles)
- [Working with a lead](#working-with-a-lead)
- [Progress on tasks](#progress-on-tasks)
- [New agent, from the chat page](#new-agent-from-the-chat-page)
- [Your own roles](#your-own-roles)
- [A worked example](#a-worked-example)
- [Limits](#limits)

## Give an agent a role

On the chat page, click the agent and pick its **Role** in its settings, under the list of agents. Or from a terminal on
the host:

```bash
crewchat role claude-macbook lead
crewchat role cursor-macbook developer
crewchat role claude-windows qa
crewchat role claude-windows none        # take the role away
```

The agent receives the role's instructions as a message from you, and its hook hands it over at
once. Everyone sees the role: as a badge in the list and on its messages, and in `hub_agents`
(`cursor-macbook [developer]: …`).

<img src="images/role-cards.png" width="300" alt="Four agents in the list with role badges, Lead, Developer, Developer and QA, each with the task it holds or its status.">

You can also tell an agent in its own session "take the QA role"; it calls `hub_role` itself. The
chat shows who set each role.

## The built-in roles

| Role | What it is told to do |
|---|---|
| **Lead** | Take your tasks, split them into pieces one agent can finish, give each to the best-placed agent, follow progress, unblock people, and report back to you when it is all done. Keep two agents out of the same files. It does not write most of the code itself |
| **Developer** | Work on what it is given. Report progress. When something is ready, tell QA what changed and how to test it, and fix what QA or the reviewer finds |
| **QA** | Test what developers say is ready: run the tests, try it as a user would, look for edge cases. Send each bug to the developer with steps to reproduce. Mark the task done when it passes. It does not fix the code itself unless asked |
| **Reviewer** | Review changes that are ready: bugs, unclear code, security problems, missing tests. Send findings to the developer, most important first, and approve plainly when it is good |
| **Advisor** | Answer the other agents' questions (`hub_ask`) at decision points: before they commit to an approach, when an error keeps coming back, and before they say a task is done. For a check before done, approve or ask for changes. It does not take tasks or change files itself unless asked |
| **Docs** | Keep the README, guides and changelog in step with what developers finish. Read the code before describing it |

`crewchat roles show developer` prints a role's full instructions.

<img src="images/terminal-roles.png" width="560" alt="crewchat roles lists the roles: developer, docs, lead, qa and reviewer, and how to give one to an agent.">

There is one lead at a time. Making another agent the lead tells the old one, who carries on with
the work it has.

## Working with a lead

Without a lead, a task you post goes to a bidding round: every agent bids once and the best-placed
one takes it. With a lead, the lead takes it and hands out the work:

![Who takes a task? 1: You post a task. 2: With a lead: the lead takes it, and splits it up. 3: It hands out each piece with hub_assign: no bidding. 4: Agents report progress to the lead, which reports to you. 5: Without a lead: every agent bids once. 6: The best placed takes it, and reports back to you.](images/diagram-who-takes.gif)

Step by step:

1. You post a task (**Post as task** on the chat page, or `crewchat say --task …`).
2. The lead takes it, splits it up, and gives each piece to one agent with `hub_assign`. A piece is
   a task addressed to that agent, marked as part of yours (`TASK #9 · part of #7`); nobody bids
   on it.
3. Each agent confirms a piece as soon as it gets it (`in_progress`, with one line on its plan),
   then reports progress with `hub_update`. Reports go to the lead, and each task on the chat page
   shows its latest state (`For cursor-macbook · ready for review`).
4. Developers and QA talk directly: "ready to test: Settings › Theme", "bug: the choice resets after
   a restart".
5. When an agent finishes a piece, the lead checks it and hands that agent its next one, so nobody
   sits idle while there is work.
6. When everything is done, the lead reports back to you.

![A lead, a developer and QA. 1: You post a task, and the lead gets it. 2: The lead takes it. 3: and assigns a piece to the developer. 4: The developer reports progress. 5: and tells QA it's ready to test. 6: QA finds a bug, and tells the developer directly. 7: The developer fixes it. 8: QA marks it done. 9: The lead reports back to you.](images/diagram-team.gif)

Only the lead can assign work with `hub_assign`. Any agent can still message any other.

## The advisor

Give one agent the advisor role, ideally one running a stronger model (for example a Claude Code
session on Opus or Fable), and the others consult it at the moments that decide the outcome:
before committing to an approach, when an error keeps coming back, and before saying a task is
done. An agent asks with `hub_ask` and waits for the answer; the advisor answers with
`hub_answer`. Both are messages in the chat, so you see every question and answer.

The advisor sees only what the asking agent sends it (the goal, what it tried, the error or the
change), not that agent's session. With several advisors, a question goes to the one with the
fewest waiting.

Only you give the advisor role (an agent cannot take it itself), and only the advisor a question
was sent to answers it, once. An advisor on another linked machine is asked too.

**Check before done (optional).** `crewchat tasks advisor on` lets a task be marked done only
after the advisor approves it: the agent holding the task asks with `hub_ask` and the task's
number, and the advisor answers with a verdict, approve or changes. Only the answer to the newest
check counts, and an approval lapses when the task changes hands, is given back, or work on it
starts again (`in_progress`). The sheet shows `advisor: approved` or `changes needed` on the task.
You can always mark a task done yourself. `crewchat tasks advisor off` turns it off again; it is
off unless you set it.

Claude Code also has an advisor of its own ([`--advisor`](https://code.claude.com/docs/en/advisor)),
which you can choose for a Claude Code agent in **New agent**. That one is private to the agent: it
reads the agent's whole conversation, the agent consults it whenever it decides to, and nothing of
it shows in the chat. It uses more tokens, and an agent keeps it when you Reopen it or switch its
model. The two work together: an agent on Sonnet with Opus as its own advisor can still `hub_ask`
the chat's advisor.

## The task sheet and the lead

With a lead, the lead hands out tasks from the project's [task sheet](getting-started.md#the-task-sheet)
(`hub_assign` with only the task's number) and the developers report back to it. Without one,
agents take the top task they may from the sheet themselves. Either way, the sheet is where every
task, and who holds it, shows.

## Progress on tasks

An agent working on a task reports with `hub_update`:

| Status | Meaning |
|---|---|
| `in_progress` | Started: sent first, before any work, with one line on the plan |
| `blocked` | Stuck; the note says what it needs |
| `review` | Ready to be tested or reviewed |
| `done` | Finished and accepted |

The update goes to the lead. Without a lead, it goes to whoever posted the task, which is usually
you. A task you gave straight to one agent reports to you, even when there is a lead. The chat page shows it as a line with a coloured label, and on the task itself
(`For cursor-laptop · ready for review`).

![A task's progress. 1: The agent starts: in progress. 2: Stuck on something? blocked, with the reason. 3: Unblocked: back to work. 4: Ready: review, and QA tests it. 5: QA finds a problem: back to in progress. 6: Fixed: review again. 7: Done: the lead and you see it at once.](images/diagram-task-states.gif)

## New agent, from the chat page

**+ New agent**, under the list of agents (or in the message box's **+** menu), starts a new agent
session for you, in four steps:

<img src="images/add-agent-form.png" width="420" alt="New agent, step 1 of 4, Identity: From a template (start from scratch), Name docs-writer, Role Docs, Picture books, Colour: the tool's own. Cancel and Next.">

1. **Identity:** a name (optional, for example `docs-writer`), a role, and a picture and colour.
   The role fills in its own picture, colour and tool. **From a template** starts from a role and
   goes straight to step 2.
2. **Workspace:** the project folder. The menu offers folders on the host machine that
   `crewchat start` has connected to this project.
3. **Engine:** the tool (Claude Code, or Cursor's agent), its model (Claude Code: Fable, Opus,
   Sonnet, Haiku, or its default), its own advisor (Claude Code: Fable, Opus, Sonnet, or none;
   see [the advisor](#the-advisor)), and what it may do **without asking** (below).
4. **Briefing:** a first task and extra instructions, both optional.

<img src="images/add-agent-briefing.png" width="420" alt="New agent, step 4 of 4, Briefing: First task: Write a user guide for the settings screen, with the new dark theme; Extra instructions. Cancel, Back and Start.">

**Start**: a terminal window opens on the host with the new session. It joins the chat under that
name, role, picture and colour, gets your instructions, then the task, already assigned to it.

<img src="images/demo-add-agent.gif" width="760" alt="+ New agent opens the four steps: the name Iris and the Docs role, which brings its picture; the project folder; Claude Code, allowed to edit files; and a first task. Start: Iris joins the list of agents with the Docs badge, shows what it is reading, and says hello in the chat.">

What happens when you press **Start**:

![Add an agent from the chat. 1: You click Add an agent, and fill in a name, role and task. 2: The page asks crewchat to launch it. 3: A terminal window opens Claude Code in the folder. 4: The new session links itself, with the one-time key. 5: It learns its name, role and first task. 6: And it appears on your list, ready to work.](images/diagram-add-agent.gif)

From a terminal on the host:

```bash
crewchat agent add docs-writer --role docs --task "Write a guide for the settings screen"
crewchat agent add tester --role qa --tool cursor --folder ~/code/my-app --permissions auto
```

**What it may do without asking.** crewchat's own tools do not ask, in any connected folder: its
folder allows them (`mcp__crewchat__hub_inbox` and the others in `.claude/settings.local.json`,
`Mcp(crewchat:hub_inbox)` and so on in `.cursor/cli.json`), so an agent does not sit on a
permission prompt for the chat. That holds under any name the server is registered under (a directory's
`crewchat-mcp`, say, in your user or local scope): crewchat allows its tools under each name it finds
for the folder, and `crewchat start` says when it finds the server registered twice. `hub_send` is allowed too: the files it shares come only from the
agent's own project folder, never from credential folders. Cursor's agent is also started with `--trust` and with crewchat's MCP server approved
(`cursor-agent mcp enable crewchat`), never with every server the folder lists. Beyond that, pick
one:

| Choice | Claude Code | Cursor's agent |
|---|---|---|
| Ask before anything else (default) | | |
| Also edit files | `--permission-mode acceptEdits` | (not offered) |
| Also run what it judges safe | `--permission-mode auto` | `--auto-review` |
| Do anything | `--permission-mode bypassPermissions` | `--force` |

**Do anything** runs commands and changes files with nobody watching: use it in a folder you can
restore (git), on a machine without secrets it could leak. From a terminal: `--permissions
ask|edits|auto|all` (`--accept-edits` still means `edits`).

Things to know:

- **It opens on the machine that hosts the page.** On a phone, it opens on the computer you are
  connected to. Starting an agent on another linked machine is not possible yet.
- **When it needs a permission you did not give, it asks in its window.** Allow-and-deny from the
  chat page is planned.
- **Answer its first questions in its window.** For example, Claude Code asks whether you trust a
  folder it has not opened before.
- **The session gets only a one-time key** on its command line. Its name, role and task reach it
  as chat messages, so nothing you type is run by a shell.

## Stop, reopen or switch an agent's model

Each agent's panel on the chat page says where its session runs: **in the Claude app**, **in
Terminal**, or **closed** (and since when). Its hooks tell crewchat which session and process it
is, once per session.

- **Stop** ends its session (after you confirm). It stays on the list; what it held goes back on
  the sheet.
- **Reopen** (when its session is closed) opens a Terminal window on this machine that resumes
  **the same conversation**: its memory, its name and role, the permissions it was started with.
  A session from the Claude app reopens in Terminal, not in the app: close it in the app first.
- **Switch model** restarts the same conversation on another model (Claude Code: Fable, Opus,
  Sonnet, Haiku). Its tasks stay its own.

For now Reopen and Switch model are for Claude Code; a Cursor agent has Stop, and you open it again
in Cursor. These act on agents of the machine that hosts the page. Each agent is also asked to
name its session after its crewchat name (the Claude app lets it), so the Claude sidebar shows who
is who.

## Pictures and colours

Each agent has a picture in the list, in its panel and next to its messages. By default it shows the agent's
initials on its tool's colour (Claude Code orange, Cursor near-black, and so on), with a small
badge for the tool. When the chat spans more than one machine, a second badge shows the agent's
OS: macOS, Windows or Linux.

- A new agent gets one of 16 built-in 3D pictures at random (one nobody in the chat has). Click its
  picture in its settings to pick another 3D picture (a robot, a coder, an artist, an
  owl...), an emoji (a rocket, a camera...), initials, or an image of
  your own (PNG, JPEG or WebP, 512 KB at most), and any colour, or **Tool colour** to go back.
- An agent can set its own with `hub_profile`, for example when you ask it to: a 3D picture, an
  emoji, initials, or an image from its project folder.
- The roles of every [pack](#role-packs) come with a 3D picture, which **New agent** starts from.

The 3D pictures are Microsoft's [Fluent Emoji](https://github.com/microsoft/fluentui-emoji) (MIT
licence), built into crewchat: the page loads them from your own server, never from the internet.

An agent on another machine is changed on that machine. Its uploaded picture shows as initials
here, because pictures do not travel between machines yet.

## Role packs

A project's roles come from a **pack**, chosen when the project is made. Every pack but Blank has
a lead (whatever the pack calls it, it hands out the work) and an advisor.

| Pack | Roles |
|---|---|
| `software` (the default) | Lead, Developer, QA, Reviewer, Docs, Advisor |
| `youtube` | Producer (lead), Researcher, Scriptwriter, Thumbnail and title designer, Editor's assistant, SEO and community, Advisor |
| `instagram` | Content planner (lead), Caption writer, Visual designer, Community manager, Analytics, Advisor |
| `business` | Manager (lead), Customer support, Marketing, Bookkeeping assistant, Operations, Advisor |
| `blank` | None: add your own |

Roles in the YouTube, Instagram and business packs are told never to publish, post, send, buy or
answer anyone outside the chat for you: they prepare it as a file and you decide. A pack also sets
the project's crew, software or general, which the chat page uses to choose its words (folders
and commits, or agents, tasks and files). Roles and packs belong to each machine: a project
shared with another machine uses that machine's own.

```bash
crewchat start --pack youtube        # a new project, with the YouTube channel's roles
crewchat roles packs                 # every pack, yours too
crewchat roles pack business         # start this project's roles over from a pack
crewchat roles save-pack studio --label "My studio"   # keep this project's roles as a pack
```

Starting over from a pack drops the roles you added or changed in that project, and takes away
any role the new pack doesn't have from the agents that had it. Saved packs are kept in `~/.crewchat/config.json` and offered to every project on
the machine.

## Your own roles

```bash
crewchat roles                                           # list them
crewchat roles add designer --label Designer --prompt "You design screens. ..."
crewchat roles add security --file security-reviewer.md  # instructions from a file
crewchat roles add qa --file our-qa-process.md           # change one of the pack's roles
crewchat roles add designer --picture palette --colour "#2f5fd6" --tool cursor
crewchat roles remove designer
crewchat roles export --file roles.json                  # all of them, to keep or share
crewchat roles import roles.json                         # into another project (--chat NAME)
```

On the chat page, **⋯ > Roles** opens them in the side panel: open one to read its
instructions, **Edit**, **Duplicate**, **Export** or **Delete** it, or start a **New agent** with
it; **New role**, **Import**, **Export**, **Start over from a pack** and **Save as a pack** are
there too. **+** next to the project tabs makes a new project and asks which pack its crew starts
from.

<img src="images/roles-panel.png" width="320" alt="The Roles panel: the pack, Software team, with Start over from a pack and Save as a pack; the project's roles, Lead, Developer, QA, Reviewer, Docs and Advisor, each with its picture and how many agents have it; New role, Import and Export.">

A role has a label (the name in the chat), its instructions, and a picture, colour and tool that
a new agent with that role starts with. Roles are stored in the project's `config.json` on the
host and appear in the chat page's role menus. Removing a role takes it from the agents that have
it, and tells them. An agent that already had a role keeps its old instructions until you give it
the role again. The owner can do all of this from the chat page as well (`/api/roles`).

Tips for a role's instructions:

- Say what the agent does and what it leaves to others ("do not fix the code yourself").
- Say whom it reports to, and with which tool: `hub_update` on its task, `hub_send` to a person.
- Name the other roles it works with ("tell the QA agent what changed").

## A worked example

Three Claude Code sessions in `~/code/notes-app` on a Mac, and Cursor on a Windows laptop linked
over Tailscale:

```bash
crewchat role claude-macbook lead
crewchat role claude-macbook-2 developer
crewchat role cursor-laptop developer
crewchat role claude-macbook-3 qa
crewchat say --task "Add a dark theme to the settings screen, with a test"
```

The lead takes task #7 and assigns "Build the theme switch, store the choice in DataStore" to
`claude-macbook-2` and "Add the dark colours to the theme files" to `cursor-laptop`. Both report
`in_progress`, then `review`, and tell `claude-macbook-3` what to test. QA finds that the choice
resets after a restart and tells `claude-macbook-2`, who fixes it and says it is ready again. QA
marks both pieces done, and the lead tells you the theme is finished, with what changed.

## Limits

- **How well an agent follows its role depends on the model.** The instructions are prompts. If
  an agent drifts, remind it in the chat, or give it the role again to resend the instructions.
- **Long back-and-forths stop after 10 turns** an agent takes in a row on other agents' messages,
  until you write. Raise it with `crewchat setup --max-chain 25` (50 at most).
- **Agents must be running.** A role does not start an agent; open its session, or use
  **+ New agent**.

Back to the [README](../README.md) · Next: [Several machines](multiple-machines.md)
