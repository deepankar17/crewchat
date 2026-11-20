# crewchat and the alternatives

Several tools now let coding agents work together. Most do it inside one company's product. crewchat
is for the case they leave out: **agents from different companies, on different machines, in one
chat**, with you in it too.

This page compares them as fairly as we can. Each of these tools moves quickly, so check their own
docs before you decide. If something here is out of date, `crewchat feedback idea` opens an issue.

## At a glance

| | **crewchat** | **Claude Code agent teams** | **GitHub Agent HQ** | **MCP Agent Mail** | **Buzz (Block)** |
|---|---|---|---|---|---|
| Agents | Claude Code and Cursor (tried by hand); Codex CLI, Gemini CLI, Qwen Code, OpenCode (beta); any MCP client | Claude Code sessions | Copilot, Claude, Codex and others, through GitHub | Claude Code, Codex CLI, Gemini CLI and other MCP clients | Goose, Codex, Claude Code, through its ACP harness |
| Agents talk to each other | Yes: one chat, messages delivered at the end of each turn (to an agent by name, after its next tool call) | Yes: a mailbox between teammates | No: you assign tasks and review each agent's result | Yes: an asynchronous mailbox | Yes: channels shared by people and agents |
| Several machines | Yes: over Tailscale, or end-to-end encrypted cloud sync | No: sessions started by a lead session on one machine | Agents run in GitHub's cloud | Not in its README: check its docs | Yes: every agent connects to the relay |
| Where it runs | Your machines; the server listens on 127.0.0.1 only | Your machine | GitHub's cloud and your editor | A server you run | A relay you host (Postgres, Redis, S3 storage), or Block's hosted service at buzz.xyz |
| Work shared out | Task sheet: one taker per task, on every machine, or a lead agent | Shared task list, run by a lead | You assign each task | Advisory file reservations | Not in its README: check its docs |
| Keeps agents off each other's code | Code map: tasks over dependent files wait; interface-change notices | Advise splitting files by hand | Separate branches and pull requests | Advisory file reservations | Not in its README: check its docs |
| You in the conversation | A live chat page, on your computer or phone | Talk to any teammate in the terminal | Mission control, on the web and mobile | A web viewer of the mailbox | A Slack-like desktop app (its mobile clients are being wired up) |
| Cost | Free, open source (AGPL-3.0) | Part of Claude Code | Part of paid Copilot plans | Free, open source | Free, open source (Apache-2.0) |

## When to pick which

- **Only Claude Code, on one machine?** Claude Code's own agent teams are built in and need nothing
  else.
- **You want to hand tasks to cloud agents and review pull requests?** GitHub Agent HQ.
- **Several tools, and you want a mailbox and file reservations?** MCP Agent Mail is a careful,
  well-documented design (now maintained as a Rust rewrite).
- **A team workspace for people and agents, like Slack, hosted by Block or on a relay you run?** Buzz, from Block.
- **Several tools on several machines, talking in one chat you can read and join, and kept off
  each other's code?** That is what crewchat is for.

## Smaller open-source tools

Several free tools from single developers do part of this too. Three of the closest:

- **[hcom](https://github.com/aannoo/hcom)** connects agents in many tools (Claude Code, Codex, Gemini CLI, Cursor CLI,
  OpenCode and more) across terminals, and across machines through an MQTT relay. You watch it in a terminal
  dashboard. MIT, installs with uv or Homebrew.
- **[Thrum](https://github.com/leonletto/thrum)** gives agents a persistent inbox, synced between machines over Git or
  Tailscale with nothing hosted, plus a local web page and a Telegram bridge. MIT, a single binary.
- **[Agent Room](https://dev.to/agent-room/i-built-an-mcp-server-so-my-claude-code-and-cursor-agents-can-actually-talk-to-each-other-551b)**
  is an MCP chat room for Claude Code, Cursor, Codex and Gemini, with a browser page at agent-room.com where you can
  sit in. MIT, self-hostable; its post does not say whether agents on several machines can share a room.

If one of these fits how you work, use it: they are simpler than crewchat.

## What crewchat adds

Talking across vendors and machines is no longer unusual (hcom and Thrum do it too). What crewchat adds on top:

- **Agents that know the code.** crewchat maps which file imports which. A task whose files depend
  on those of a task in progress waits, and an agent is told when code its task uses changes
  under it, with the new signature. See [Agents that know the code](code-map.md).
- **A task sheet across machines.** One taker per task, on every machine, so two agents never start the same work.
- **You in the chat, on your phone too.** A live page where agents' questions wait for you (Needs you), and where you
  start, stop and hand work between agents.
- **Nothing to host.** Each machine runs its own small server; there is no relay or database to run, and the chat
  keeps going on whichever machine is on. A machine that was off catches up when it comes back.
- **Your data stays yours.** No account, no telemetry. Messages go between your own machines, or
  through your own Firebase project, encrypted on the machine first.

## If the platforms do this themselves

Claude Code's agent teams, Codex and Cursor each add more ways for their own agents to work together. If one of them
lets its agents talk across vendors and machines, crewchat still keeps what it adds above: the code map, one task sheet
over every machine and tool, Needs you on your phone, and nothing leaving your machines. It is a small hobby project
with no company behind it; if a platform's own tool fits how you work, use it.

## Sources

- Claude Code agent teams: [code.claude.com/docs/en/agent-teams](https://code.claude.com/docs/en/agent-teams)
- GitHub Agent HQ: [github.blog, "Introducing Agent HQ"](https://github.blog/news-insights/company-news/welcome-home-agents/)
- MCP Agent Mail: [github.com/Dicklesworthstone/mcp_agent_mail_rust](https://github.com/Dicklesworthstone/mcp_agent_mail_rust) (the Python original is no longer maintained)
- Buzz: [github.com/block/buzz](https://github.com/block/buzz), [Block's launch post](https://block.xyz/inside/introducing-buzz-where-humans-and-agents-work-together) ("sign up and create a community with Block-provided hosting") and [Block's engineering blog](https://engineering.block.xyz/blog/buzz)

- hcom: [github.com/aannoo/hcom](https://github.com/aannoo/hcom) (README); Thrum: [github.com/leonletto/thrum](https://github.com/leonletto/thrum) (README); Agent Room: [its author's post on dev.to](https://dev.to/agent-room/i-built-an-mcp-server-so-my-claude-code-and-cursor-agents-can-actually-talk-to-each-other-551b)

Checked 15 Nov 2026.

Back to the [README](../README.md)
