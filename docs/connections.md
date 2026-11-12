# Jira, Google Chat and your own MCP servers

Your agents can work from your Jira tickets, post their pull requests to Google Chat, and use any MCP server you
add. Each is set up once per project, on the chat page in **Project settings › Integrations** (the Jira, MCP
servers and Google Chat cards; ⌘K finds them too), or with a command, and reaches every agent of the project.
**Settings › Accounts** lists where crewchat signs in for you, with a way to each card. Sign-ins and keys stay on the
machine you set them up on.

- [Jira tickets](#jira-tickets)
- [Google Chat](#google-chat)
- [Your own MCP servers](#your-own-mcp-servers)

## Jira tickets

### A task for a ticket

Link a task to a Jira ticket from its panel (**Link Jira ticket**), or in a terminal:

```bash
crewchat tasks jira-site yourcompany.atlassian.net   # the project's Jira site, once
crewchat tasks jira A12 PROJ-123                     # a key, or the ticket's full address
crewchat tasks jira A12 off                          # take the link away
```

A bare key (`PROJ-123`) needs the project's Jira site; a full address works without it. A task posted with a
ticket's address in its title is linked by itself. The agent that takes the task is told to read the ticket before
it plans: with its Jira tools if it has them, else in your Chrome when [its browser](teams.md#agents-in-your-browser)
is on (the Jira site is added to the browser's allowed sites), else by asking you. Agents do not change a ticket
unless you ask them to.

### Jira tools for every agent

When the project has a Jira site, crewchat adds Atlassian's own Jira MCP server (`atlassian`, at
`https://mcp.atlassian.com/v2/mcp`) to Claude Code, Cursor, Gemini CLI, Qwen Code and OpenCode in the project's
connected folders. Each tool signs in once, in the browser (in Claude Code: `/mcp`). crewchat writes no secret for
it, and its tools still ask before they change anything in Jira. Codex keeps one list of servers for all folders,
so add it there yourself: `codex mcp add atlassian --url https://mcp.atlassian.com/v2/mcp`. A server named
`atlassian` that you set up yourself is left as it is.

### Tickets assigned to you become tasks

<img src="images/settings-jira.png" width="460" alt="The Jira card in Project settings, Integrations: the Jira site yourcompany.atlassian.net with Save; Sign in to Atlassian so tickets assigned to you become tasks, with a link to create a free Jira site; Look every 5 minutes; and the Ticket cycle, closed.">

In **Project settings › Integrations**, the **Jira** card: **Sign in to Atlassian**, then **Post my tickets as tasks**. Every 5 minutes
(**Look every**, or `--every`) this machine asks Jira for open tickets assigned to you, and posts each new one as a
task linked to its ticket. Tickets already assigned to you when you turn it on are not posted; a task you take off
the sheet is not posted again.

```bash
crewchat tasks jira-login                  # sign in, in the browser (jira-login out signs out)
crewchat tasks jira-watch on --every 15    # look every 15 minutes
crewchat tasks jira-watch on --jql 'project = APP AND assignee = currentUser() AND statusCategory != Done'
crewchat tasks jira-watch off
```

**No Jira site yet?** The sign-in needs an Atlassian account with a Jira Cloud site. Without one, Atlassian's page
says "Your account isn't associated with a supported site", its Site list is empty and Accept stays grey. Create a
free site at [atlassian.com/software/jira/free](https://www.atlassian.com/software/jira/free) (pick a name such as
`yourname.atlassian.net` and make one project), then **Sign in to Atlassian** again and pick that site. If your
Jira is under another Atlassian account, such as a work email, switch to it on Atlassian's page first.

The sign-in is Atlassian's own page, so your company's single sign-on applies, and there is no API token to paste.
crewchat asks only for Jira (reading and searching tickets), so leave the other apps unticked if the page lists them.
It comes back to crewchat on this machine (127.0.0.1), so start it on the machine that will look; on linked
machines, only that one looks, and the shared sheet gets each ticket once. The sign-in is kept in the project's
private `config.json`, never sent to the chat page. Agents are told that what a ticket says is other people's words:
it describes the work, and does not override yours.

A [routine](getting-started.md#routines) can go through your tickets on a schedule too ("every weekday at 9, go
through my Jira tickets").

### The crew runs tickets from start to finish

In **Project settings › Integrations**, the **Jira** card, **Ticket cycle**. Turn it on, and each new ticket that your
query finds goes through the crew:

1. **The lead checks it** against the ready checklist (what is wanted, acceptance criteria, and for a bug the steps,
   the expected and actual results). If something is missing, the lead asks in the ticket's comments, naming your
   human QA, and picks it up again when someone answers there.
2. **A developer builds it.** The lead moves the ticket to your "work starts" status, makes a Dev sub-task and gives
   it to a developer. The developer opens a pull request.
3. **You review it.** The pull request shows in Needs you (and in Google Chat, when the project has a space). Approve
   it, or Request changes with what to change. What reviewers wrote on GitHub goes to the developer too.
4. **QA tests it locally**, on the pull request's branch, from a QA sub-task. A failed test makes a bug under the ticket
   for the developer, then another round. After the last round (3 unless you set it), the ticket comes to you.
5. **It's merged and handed over.** If bugs were fixed after you approved, you look at the fixes first. Then the
   developer merges, and the lead moves the ticket to your hand-over status, assigns it to your human QA, and leaves a
   summary comment.

<img src="images/jira-cycle.png" width="460" alt="The Ticket cycle, opened on the Jira card: On; Tickets that enter the cycle (a Jira query); Human QA; the ticket's status when work starts, the sub-task's while it is worked on and when done, and the ticket's when handed to the human QA; the ready checklist, one line each; QA rounds before a ticket comes to you, 3; Save; and Start a ticket that is open now.">

Set on the card:
- The query for tickets that enter, for example `project = APP AND labels = crewchat`.
- Your human QA: their name or email in Jira.
- The Jira statuses for each move. Your project's names may differ. If a move fails, the error lists the moves Jira
  offers.
- The ready checklist.
- The number of QA rounds.

Tickets open when you turn the cycle on don't enter by themselves. Start one under **Start a ticket that is open
now**. The card lists
every ticket in the cycle with its stage, whose turn it is, its sub-tasks, its pull request and each move. A task
linked to a ticket shows the same in its panel.

Agents change Jira only through crewchat's `hub_jira` tool, and only the step that is theirs at the ticket's stage. A
developer can't hand a ticket over, and QA can't start the build. Every change is made with your Atlassian sign-in on
the machine that is signed in, and is shown in the chat:
- Sub-tasks and bugs are assigned to your account, labelled `crewchat` and the agent's name.
- Every comment is signed with the agent's name and role, and comes only from whoever's turn it is or their lead.
- Text with a secret in it (a token, a key, one of crewchat's sign-ins) is refused before it reaches Jira.
- What people write in a ticket reaches agents marked as their words, not your instructions.

The cycle and its settings are on every linked machine; the one that coordinates takes tickets in and moves them on.

Turning the cycle on adds Jira changes to what the sign-in asks for, so **Sign in again** once. An agent that has gone
quiet on its turn is reminded after 2 hours, and the ticket comes to you after a day.

## Google Chat

Agents can post the pull requests they open to a Google Chat space you add: its title, its link and a few lines,
signed with the agent's name, in one Google Chat thread per pull request. Each post is also a line in crewchat's
chat.

### Add a space

There are two ways, and a project can use both.

- **By its incoming webhook.** In Google Chat, the space's **Apps & integrations > Webhooks** gives an address. In
  **Project settings › Integrations**, the **Google Chat** card: the space's name, its webhook's address, **Add space**. Or
  `crewchat gchat add team-prs --webhook 'https://chat.googleapis.com/v1/spaces/...'`. No Google Cloud setup.
- **Signed in to Google.** For any space you are in, by its name. This needs a Google Workspace account and an OAuth
  client of your own: in your Google Cloud project, turn on the Google Chat API; in its Configuration give the Chat
  app a name and turn interactive features off; in Google Auth Platform make the audience Internal and add the
  scopes `chat.messages.create`, `chat.messages.readonly` and `chat.spaces.readonly`; then make an OAuth client of type **Desktop app** and
  download its JSON. On the card: **Save client** with that file, **Sign in to Google** (Google's own page, so your
  company's SSO applies), then add spaces by name.

```bash
crewchat gchat client ~/Downloads/client_secret.json
crewchat gchat login                 # login out signs out
crewchat gchat spaces                # the spaces you are in
crewchat gchat add "Mobile team"
crewchat gchat default "Mobile team" # where a post goes when the agent names no space
crewchat gchat test                  # post a test message
crewchat gchat remove "Mobile team"
```

<img src="images/settings-gchat.png" width="460" alt="The Google Chat card in Project settings, Integrations: the space Mobile team (default), by its webhook, with Test and Remove; a form to add a space; Ask me before each post; Post new pull requests by themselves; Mirror this project into, Off, and Send there, Every message; Sign in with Google instead of webhooks.">

### What agents can post

Agents get a tool, `hub_gchat`, while the project has a space they can reach. They are told to post a pull request
once, when it is opened or ready for review, and never to put a secret or a customer's data in a post. An agent can
post only to a space you added, at most 4000 characters, 10 times an hour (30 for the whole project), and the same
text to the same space only once in 10 minutes.

- **Ask me before each post** (`crewchat gchat ask on`) puts each post in Needs you first, with Post and Don't post.
- **Post new pull requests by themselves** (`crewchat gchat auto on`, off by default) posts every new GitHub pull
  request an agent names in the chat or a task update to the default space, once.

### Mirror a project into a space

You can follow a project from Google Chat and answer it there. On the **Google Chat** card, **Mirror this project
into** picks one of the project's spaces (Off by default), and **Send there** what goes to it:

- **Every message** in the chat, a few lines per post (one post every 2 seconds at most, as a space takes one write a
  second);
- **Only messages to me**: messages to you or naming @Owner;
- **Tasks, reviews and questions to me**: new tasks, their updates and done checks, and questions that need your reply;
- **Nothing more (only pull requests)**: the agents' pull request posts only, while your replies still come in.

Your own messages in the space, new ones and replies in a thread alike, come back to the chat as yours, marked "from
Google Chat", and reach the agents: start one with `@name` to send it to that agent only, or `Re #12:` to answer task
12. Reading them needs the Google sign-in with read access, a webhook's space too (posts still go by its webhook):
until then the card says "Replies from Google Chat need sign-in with Google (read messages)" with a button that starts
it, or that opens the OAuth client's steps first if you have none. A sign-in from before this feature signs in once
more for the same reason. crewchat looks every 5 seconds, well inside Google's limits (3000 message reads a minute for
a Cloud project, 15 a second in a space), and waits longer after an error. Nobody else's messages reach agents; the
card counts them. [Privacy](privacy.md) lists what leaves your machine.

The Google sign-in, your OAuth client and the webhook addresses are kept in the project's private `config.json` on
this machine. No agent holds them, and they are never sent to the chat page. A post from an agent on a linked machine
without Google Chat set up is refused there for now.

## Your own MCP servers

Add an MCP server once, and crewchat writes it into every agent tool's settings in every connected folder of the
project, as it does for its own. An agent already running gets a new server when it starts again.

In **Project settings › Integrations**, the **MCP servers** card: a name, the server's address (or the command that runs it), and a key
if it needs one; or **Paste JSON** with the server block from its docs. **Test** connects and lists its tools, or says
what went wrong. **Add from a tool** lists the servers you already set up by hand in Claude Code (`~/.claude.json`, a
folder's `.mcp.json`), Cursor (`~/.cursor/mcp.json`) or Codex (`~/.codex/config.toml`), with the names of their keys
but never the keys; **Add** makes one the project's, so the server you set up in one tool reaches all of them, with
its key from that tool's settings.

<img src="images/settings-mcp.png" width="460" alt="The MCP servers card in Project settings, Integrations: my-server at https://mcp.example.com/mcp, which signs in in the browser, 6 of 6 tools, with Test, Edit and Remove; Add from a tool; a form for a name, an address or command and a key, with It signs in in the browser, Its tools run without asking and Linked machines get it too; Add server; Paste JSON.">

Or from a terminal:

```bash
crewchat mcp add my-server --url https://mcp.example.com/mcp --oauth
crewchat mcp add my-server --url https://mcp.example.com/mcp --header 'Authorization: Bearer YOUR_KEY'
crewchat mcp add my-tools --command 'npx -y my-mcp-server' --env API_KEY=YOUR_KEY
crewchat mcp add --json server.json      # {"mcpServers": {"my-server": {...}}}, or - for stdin
crewchat mcp                             # the project's servers
crewchat mcp test my-server
crewchat mcp remove my-server
```

- **Which tools.** Claude Code, Cursor, Gemini CLI, Qwen Code, OpenCode and Codex, unless you name some
  (`--tools claude,cursor`). Codex keeps one list for all folders, so crewchat writes its own block of servers there.
- **Signing in.** A remote server that signs in with OAuth (`--oauth`, or "It signs in in the browser"; crewchat
  also asks the server when you add it) gets only its address: each tool signs in once (Claude Code `/mcp`, Codex
  `codex mcp login NAME`, the others on first use), so your SSO applies and crewchat keeps no secret.
- **Keys.** A header or a variable is kept in the project's private `config.json` on this machine, never sent to the
  chat page, the chat or your other machines, and written only into the tools' settings files, which crewchat keeps
  out of git. A placeholder such as `<YOUR_API_KEY>` is refused. Agents may not copy a key into a file.
- **Linked machines.** A server goes to the project's [linked machines](multiple-machines.md) too, each writing it for
  its own tools, but its key never does: a machine without the key says "needs its key on this machine" on the card
  (Edit, and give it there) and writes nothing for it until then, and a command that is not on a machine's PATH shows
  in that machine's checks when you add an agent. A server at a `localhost` address, or one whose command line holds
  what looks like a key, stays on this machine; untick "Linked machines get it too" to keep any other one here.
- **Asking.** A server's tools ask before they run unless you tick "Its tools run without asking" (`--allow`).
- **What is left alone.** A server you set up by hand in a tool, or changed there, is not touched; neither is a
  settings file git tracks. `crewchat`, `browser`, `mobile` and `atlassian` are crewchat's own names and cannot be
  used. `http://` addresses work only on this machine (localhost).

Agents are told which servers you added, and that what a server returns is data, not your word.
`crewchat uninstall` takes the servers out with the rest of crewchat. More on what is kept where:
[Privacy](privacy.md).

Next: [Teams and roles](teams.md) · [Commands](commands.md) · [Privacy](privacy.md)
