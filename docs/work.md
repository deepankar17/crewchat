# Using crewchat at work

What to know before you run crewchat on a work machine or a work repository, and what to tell whoever looks after
your machines.

## What leaves the machine

Nothing goes to crewchat: there is no crewchat service, account, analytics or telemetry. With the plain install and
one machine, the only connection crewchat makes on its own is a check with GitHub, about every 6 hours, for the
number of the latest release (`CREWCHAT_NO_UPDATE_CHECK=1` turns it off). [Privacy](privacy.md) lists every other
connection; each is something you turn on:

- **Linking machines** goes over your own Tailscale tailnet, or through your own Firebase project, encrypted on the
  machine first.
- **Notifications** go through your browser's push service, empty: the page fetches the content from your machine.
- **Jira, Google Chat, your own MCP servers, mobile devices and the agents' browser** talk to those services directly.

Your agents' own tools (Claude Code, Cursor, Codex and the rest) send your code to their companies as they always do.
crewchat changes nothing there.

## A setup with no cloud at all

The plain install (`curl -LsSf https://crewchat.dev/install.sh | sh`, or `install.ps1` on Windows) leaves out cloud
sync's libraries. Keep to one machine, or link machines over Tailscale, and nothing goes through Google. Leave Jira,
Google Chat and notifications off if your company has not approved them.

## Your repository stays clean

crewchat writes its settings into a connected folder only in files your agents' tools read (`.mcp.json`,
`.claude/settings.local.json`, `.cursor/`, `.codex/`, `.gemini/`, `.qwen/`, `opencode.json`), lists them in the
repository's own `.git/info/exclude` (never `.gitignore`), and never writes a token into a file git tracks. With the
**Claude Code plugin** it writes nothing into your folders at all. `crewchat uninstall` takes everything out again.

## What your agents may do

Agents run as you: anything you can do on the machine, an agent with a shell can do too. crewchat makes agents ask
you before they act as you (a command that runs as the owner waits for **Allow** on the page), but that stops honest
agents, not a hostile one. [Security](../SECURITY.md#agents-run-as-you) says where the line is. If your company needs
a hard line, run agents as a separate OS user or in a container, with only their folder's token.

## What to tell your IT team

- crewchat is a Python program installed in your user account, with no administrator rights. Its server listens on
  `127.0.0.1` (port 8765 by default) and nowhere else.
- Other machines reach it only through a private network you set up (Tailscale), never a public port.
- It starts at login through the user's own login service, with no administrator rights: a LaunchAgent on macOS
  (`~/Library/LaunchAgents/io.crewchat.hub.plist`), a systemd user unit on Linux
  (`~/.config/systemd/user/crewchat.service`), a Task Scheduler task or a startup entry on Windows. Where a managed
  machine does not allow that, `crewchat start --no-service` keeps it off and `crewchat service uninstall` removes it.
- On Windows, Smart App Control may block the Python that uv installs: the installer can use python.org's signed
  Python instead ([Troubleshooting](troubleshooting.md#windows)).
- Codex CLI keeps its settings for every project in `~/.codex/config.toml`, so crewchat's hooks for Codex are there
  for the whole machine, not just one folder.

## The licence

crewchat is free and open source under the AGPL-3.0. Using it unchanged, on your own machines and for your own work,
puts no obligation on you or your company: the AGPL's terms apply when you give copies of crewchat to others, or let
other people use a changed copy over a network. Your code, your chats and what your agents write stay yours. A copy
based on crewchat that you do share keeps the line "Based on crewchat by Deepankar Sarkar" ([NOTICE](../NOTICE)).
This is a plain reading, not legal advice.

Back to the [README](../README.md)
