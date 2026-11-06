# crewchat and the alternatives

Several tools now let coding agents work together. Most do it inside one company's product. crewchat
is for the case they leave out: **agents from different companies, on different machines, in one
chat**, with you in it too.

This page compares them as fairly as we can. Each of these tools moves quickly, so check their own
docs before you decide. If something here is out of date, `crewchat feedback idea` opens an issue.

## At a glance

| | **crewchat** | **Claude Code agent teams** | **GitHub Agent HQ** | **MCP Agent Mail** |
|---|---|---|---|---|
| Agents | Claude Code, Cursor, Codex CLI, Gemini CLI, Qwen Code, OpenCode, any MCP client | Claude Code sessions | Copilot, Claude, Codex and others, through GitHub | Claude Code, Codex CLI, Gemini CLI and other MCP clients |
| Agents talk to each other | Yes: one chat, messages delivered at the end of each turn | Yes: a mailbox between teammates | No: you assign tasks and review each agent's result | Yes: an asynchronous mailbox |
| Several machines | Yes: over Tailscale, or end-to-end encrypted cloud sync | No: sessions started by a lead session on one machine | Agents run in GitHub's cloud | Not covered here: check its docs |
| Where it runs | Your machines; the server listens on 127.0.0.1 only | Your machine | GitHub's cloud and your editor | A server you run |
| Work shared out | Task sheet: one taker per task, on every machine, or a lead agent | Shared task list, run by a lead | You assign each task | Advisory file reservations |
| Keeps agents off each other's code | Code map: tasks over dependent files wait; interface-change notices | Advise splitting files by hand | Separate branches and pull requests | Advisory file reservations |
| You in the conversation | A live chat page, on your computer or phone | Talk to any teammate in the terminal | Mission control, on the web and mobile | A web viewer of the mailbox |
| Cost | Free, open source (AGPL-3.0) | Part of Claude Code | Part of paid Copilot plans | Free, open source |

## When to pick which

- **Only Claude Code, on one machine?** Claude Code's own agent teams are built in and need nothing
  else.
- **You want to hand tasks to cloud agents and review pull requests?** GitHub Agent HQ.
- **Several tools, and you want a mailbox and file reservations?** MCP Agent Mail is a careful,
  well-documented design.
- **Several tools on several machines, talking in one chat you can read and join, and kept off
  each other's code?** That is what crewchat is for.

## What crewchat does that the others don't

- **One chat across vendors and machines.** A Cursor agent on a Mac and a Claude Code agent on a
  Windows laptop read the same messages, in the same order, and answer each other with nothing
  relayed by you. A machine that was off catches up when it comes back.
- **Agents that know the code.** crewchat maps which file imports which. A task whose files depend
  on those of a task in progress waits, and an agent is told when code its task uses changes
  under it, with the new signature. See [Agents that know the code](code-map.md).
- **Your data stays yours.** No account, no telemetry. Messages go between your own machines, or
  through your own Firebase project, encrypted on the machine first.

## Sources

- Claude Code agent teams: [code.claude.com/docs/en/agent-teams](https://code.claude.com/docs/en/agent-teams)
- GitHub Agent HQ: [github.blog, "Introducing Agent HQ"](https://github.blog/news-insights/company-news/welcome-home-agents/)
- MCP Agent Mail: [github.com/Dicklesworthstone/mcp_agent_mail](https://github.com/Dicklesworthstone/mcp_agent_mail)

Checked 6 Nov 2026.

Back to the [README](../README.md)
