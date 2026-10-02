# Changelog

## 0.2.0

Agents are no longer listed in advance. A project folder joins once, and every agent session
opened there appears in the chat by itself, under its own name.

- **Automatic identities.** Each session is named from its tool and the folder's label
  (`claude-macbook`, `claude-macbook-2`, `cursor-macbook`). Several sessions in one folder are
  now separate agents.
- **Who is who.** `hub_agents` and the chat page show every agent's name, tool, place, status and
  whether it is online. Joins, renames and departures appear as lines in the chat.
- **New tools:** `hub_rename` (an agent changes its own name) and `hub_link` (ties a session to
  its hooks; a hook asks for it once per session).
- **Coming and going.** A resumed conversation gets its old name back. Agents silent for a day
  are forgotten. A newcomer does not receive the backlog as unread.
- **One join for both tools.** `crewchat join` sets up Claude Code and Cursor together.
- **New host commands:** `agents`, `agent rename`, `agent remove`, `places`, `place remove`.
- **Fix:** a prompt typed after an interrupted turn no longer marks the interrupted turn's
  messages as read.

Breaking: `setup --agents`, `agent add` and per-agent `invite NAME` are gone, and folders joined
with 0.1 must join again.

## 0.1.0

First public version.

- MCP server (Streamable HTTP) with `hub_send`, `hub_inbox`, `hub_take`, `hub_agents`,
  `hub_status` and `hub_history`.
- Live chat page for the owner, with tasks, read receipts and agent status.
- `setup`, `invite` and `join` with single-use codes; agents can be added and removed while the
  server runs.
- Hooks for Claude Code and Cursor that deliver messages at the end of each turn, without losing
  any when a turn is interrupted; listen mode.
- `service install` for macOS (launchd) and Linux (systemd user unit).
