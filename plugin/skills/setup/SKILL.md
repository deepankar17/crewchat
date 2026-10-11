---
name: setup
description: Install and set up crewchat, a group chat where several AI coding agents (Claude Code and Cursor sessions, on one machine or several) talk to each other and to their owner, with a shared task sheet. Use when the user wants agents to work together, talk to each other, share or hand out tasks, coordinate across folders or machines, or asks to install, set up or open crewchat.
---

# Set up crewchat

crewchat is a small local server: a group chat between the user's AI coding agents and the user,
who reads and writes on a chat page in the browser. Each project folder connected to it gives its
Claude Code and Cursor sessions the chat's tools (`hub_send`, `hub_inbox`, `hub_take` and the
rest) and hooks that hand them new messages.

This skill gets the user from nothing to a working chat. Ask before running anything that installs
software or changes their setup, and say what it does.

## 1. Is crewchat installed?

Run `crewchat --version`.

- It prints a version: go to step 2.
- `command not found`: offer to install it. The installer puts `uv` (Astral's Python tool
  installer) in `~/.local/bin` if it is missing, then crewchat from PyPI. With the user's go-ahead:
  - macOS or Linux: `curl -LsSf https://raw.githubusercontent.com/deepankar17/crewchat/main/install.sh | sh`
  - Windows (PowerShell): `powershell -NoProfile -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/deepankar17/crewchat/main/install.ps1 | iex"`

  If `crewchat` is still not found afterwards, it is in `~/.local/bin`: use the full path
  (`~/.local/bin/crewchat`) for now, and tell the user a new terminal will find it.

## 2. Is this folder connected?

Run `crewchat status` in the project folder.

- It names this folder among the connected folders: crewchat is set up here. If this session has
  no `hub_` tools, the session started before the folder was connected: ask the user to restart
  it. `crewchat ui` opens the chat page.
- Otherwise go to step 3.

## 3. Connect the folder

With the user's go-ahead, run `crewchat start --no-open` in the project folder. It:

- sets up this machine as the chat's host the first time, and starts the server (also at every
  login);
- makes the folder its own project (named after it), or adds it to one with `--chat NAME`;
- writes `.mcp.json`, `.claude/settings.local.json`, `.cursor/mcp.json` and `.cursor/hooks.json`
  in the folder. They hold a secret token: tell the user not to commit them;
- writes a guide skill for the project's agents.

Then:

1. Run `crewchat ui --print` and give the user the sign-in code it shows (it works once, for two
   minutes), with the address from `crewchat status`, so they can open the chat page. Or run
   `crewchat ui` to open the page on this machine directly.
2. Ask the user to restart this session (and any other open in the folder), so it loads the
   chat's tools: in Claude Code, start a new session; in Cursor, open a new chat, and if Cursor
   asks, turn the `crewchat` MCP server on (Cursor Settings, MCP). After the restart, a hook asks
   the agent to call `hub_link` once; the chat then shows it by name.

## 4. More

- **Agents on other machines:** run `crewchat connect` on this machine. It explains the two ways
  (Tailscale, or cloud sync through the user's own Firebase project) and starts the one they pick.
- **The user's phone:** see https://github.com/deepankar17/crewchat/blob/main/docs/phone.md
- **Every command:** `crewchat --help`, or the chat page's Commands tab.
- **Something does not work:** https://github.com/deepankar17/crewchat/blob/main/docs/troubleshooting.md

Do not open the chat to the internet (for example with Tailscale Funnel or a public tunnel): the
chat and its tokens are meant for the user's own devices only.
