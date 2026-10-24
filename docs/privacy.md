# Privacy

crewchat has no service of its own and collects nothing. There is no account with crewchat, no
analytics and no telemetry. The chat, its tasks, files and tokens stay on your machines, in
crewchat's folder (`~/.crewchat`, or `CREWCHAT_HOME`).

What crewchat connects to, and only these:

- **Your own machines.** The chat server listens on this machine. Agents and the chat page reach
  it locally; other machines reach it only if you link them (Tailscale, or cloud sync below).
- **GitHub, for the update check.** About every 6 hours the server asks GitHub for the number of
  the latest crewchat release. Nothing about you, your machine or your chat is sent.
  `CREWCHAT_NO_UPDATE_CHECK=1` turns it off.
- **Your own Firebase project, if you turn on cloud sync.** Messages, tasks, agent lists and shared files (up to 5 MB) are
  encrypted on your machine before they are sent, with a key only your machines hold; Firestore
  stores the ciphertext under your Google account. You sign in with Google to your own project.
  Details: [cloud sync design](cloud-sync-design.md).
- **Tailscale, if you link machines with it.** Your machines talk to each other over your own
  tailnet.
- **Your browser's push service, if you turn on notifications.** With the bell on, the browser
  subscribes with its own push service (Google's, Apple's, Mozilla's or Microsoft's). When something
  needs you and no chat page is open, the server sends that service an **empty** push, signed with a
  key kept on your machine; the page then fetches what is new from your own server. Nothing from the
  chat goes through the push service.

- **Your own Chrome, if you turn the browser on for an agent.** The agent works in your browser with your logins,
  through Claude in Chrome or the Playwright MCP Bridge extension; what it sees goes to its own AI tool, as its other
  work does. crewchat only decides whether each browser call may go on: the sites you allowed and trusted are kept in
  the project's settings; the bridge token in `config.json`, the connected folders' MCP settings (kept out of git) and
  Codex's `~/.codex/config.toml`, each readable by you only; the log of what agents opened only in the server's memory.

What crewchat reads on your machine and keeps there:

- **Which files agents edit,** in a project whose team edits code (on by default for a software team,
  off for others; `crewchat edits on|off|auto`). The hook sends the path of each file an agent edits
  inside the project folder, never what it wrote and never a path outside the project, and it is kept
  on this machine in `events.sqlite`. Other machines only ever see what is worked out from it.
- **A map of the project's code,** while edits are kept: the server reads the source files of the
  project's folders (Python, Java, Kotlin, JavaScript and TypeScript, Angular templates) and keeps their
  paths, the names of the classes and functions in them (with their parameters: for Python, and for
  Java, Kotlin, JavaScript and TypeScript with the `map` extra) and which file imports which, in
  `maps/` in the project's crewchat folder. Never the code itself; it never leaves the machine
  (`crewchat map`).
- **When each agent was working, waiting for messages or idle,** for the timelines on the analytics
  page: only those states and when they changed, kept on this machine in `events.sqlite`. Not kept
  while `crewchat setup --activity off` keeps what agents do off the page.
- **Token counts.** At the end of each turn of a Claude Code agent, its hook reads how many tokens
  the turn used from the session's own transcript: the model and the numbers, never the
  conversation. They go to this machine's chat (or the one the folder is connected to), are added
  to the agent and the task it works on, and are shown to you as totals with a cost estimate.
  `crewchat usage off` stops it for a project; `crewchat setup --activity off` stops it everywhere.
- **An agent's conversation, for the advisor.** When an agent asks the chat's advisor (`hub_ask`), the
  server reads that agent's own session file on its machine and attaches the part since the agent took its
  task, at most its last 40 KB, to the question for the advisor and you only. Text that looks like a secret,
  and crewchat's own tokens, are replaced with `[secret left out]`; other text the agent saw is not.
  [Details](#what-the-advisor-is-shown).

## What the advisor is shown

When an agent asks the chat's advisor a question (`hub_ask`), crewchat reads that agent's own
session file on its machine (Claude Code's transcript, Codex's rollout, Gemini CLI's or Qwen Code's
session file, Cursor's transcript, or OpenCode's database) and attaches the part since the agent
took its task, at most its last 40 KB, to the question as `conversation-NAME.txt`.
[Teams](teams.md#the-advisor) says what it holds.

**Who can open it.** The advisor the question is for, and you. Another agent is refused, and the
file is not listed in its inbox or history. The question's own text is in the chat for everyone,
as always; the conversation is not.

**Where it goes.** To the advisor's machine, which may be another of your linked machines. Over
Tailscale that machine fetches it from the one it was made on. With cloud sync it travels as other
files do: encrypted on your machine before it is sent, and kept in the cloud for the retention
(24 hours, `cloud_ttl_hours`). A machine on an older crewchat that you approve into your account
during that time could read those encrypted pieces. While any linked machine runs an older crewchat,
or has not been heard from since this server started (one that has been off the whole time, say),
questions go without the conversation.

**What is left out.** The agent's thinking, most of each tool result (its first and last 10 lines
stay), and text that looks like a secret: private keys, `sk-...`, `ghp_...`, `github_pat_...`,
`AKIA...`, `xox...` tokens, `Bearer ...` headers, and crewchat's own tokens (an agent that printed
its `.mcp.json`). Each is replaced, where it stands, with `[secret left out]`.

**What is not.** Everything else the agent saw or wrote. A password typed in a command, a
customer's name in a file the agent read, an API key of a kind not listed: if it is in the
conversation, the advisor gets it. Choose your advisor knowing that. The file is kept in `files/`
like any shared file, on the machine it was made on and on each machine that opens it.

**How the server finds it.** The agent's hook sends only the file's path (or the session's id).
The server reads it only inside the tool's own folder and only when a question comes in, so an
agent cannot have another file of yours read and passed on. Where the file is stays in the
server's memory: it is not saved, synced or shown.

Installing crewchat downloads it from PyPI or GitHub (and `uv` from Astral, if missing); those
sites see the download as they see any other.

The Claude Code and Cursor plugin is a setup guide only: it contains no code that sends data
anywhere.

Questions: [open an issue](https://github.com/deepankar17/crewchat/issues).

## The website, crewchat.dev

The app collects nothing; the website counts its visits. Cloudflare Web Analytics counts page views on every page,
with no cookies. Google Analytics runs only after you click Accept on the site's banner (Cookie settings, at the
bottom of each page, changes your choice; Decline removes its cookies). When an AI agent fetches `/llms.txt` or a
guide as Markdown, the site counts the read in Google Analytics by page and kind of reader (from its user agent),
with no IP address. Every request passes through Cloudflare and is served by Firebase Hosting, which handle
requests like any web host; the function serving `/llms.txt` runs on Google Cloud, whose request logs keep each
request's IP address and user agent for about 30 days. The site's own page says it in full: https://crewchat.dev/privacy.html#this-site
