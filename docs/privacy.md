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
- **Your own Firebase project, if you turn on cloud sync.** Messages, tasks and agent lists are
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

What crewchat reads on your machine and keeps there:

- **Which files agents edit,** in a project whose team edits code (on by default for a software team,
  off for others; `crewchat edits on|off|auto`). The hook sends the path of each file an agent edits
  inside the project folder, never what it wrote and never a path outside the project, and it is kept
  on this machine in `events.sqlite`. Other machines only ever see what is worked out from it.

- **When each agent was working, waiting for messages or idle,** for the timelines on the analytics
  page: only those states and when they changed, kept on this machine in `events.sqlite`. Not kept
  while `crewchat setup --activity off` keeps what agents do off the page.
- **Token counts.** At the end of each turn of a Claude Code agent, its hook reads how many tokens
  the turn used from the session's own transcript: the model and the numbers, never the
  conversation. They go to this machine's chat (or the one the folder is connected to), are added
  to the agent and the task it works on, and are shown to you as totals with a cost estimate.
  `crewchat usage off` stops it for a project; `crewchat setup --activity off` stops it everywhere.

Installing crewchat downloads it from PyPI or GitHub (and `uv` from Astral, if missing); those
sites see the download as they see any other.

The Claude Code and Cursor plugin is a setup guide only: it contains no code that sends data
anywhere.

Questions: [open an issue](https://github.com/deepankar17/crewchat/issues).
