# Security

## What crewchat protects

- **Who can connect.** The server listens on `127.0.0.1` only. Other machines reach it through a
  private network you control (for example `tailscale serve`), never a public port.
- **Only under its own names.** A web page can point a name it owns at `127.0.0.1` and reach the
  server from your browser as if it were that site (DNS rebinding). The server answers only
  requests addressed to this machine (`localhost`, an address in numbers, a one-word or `.local`
  machine name, a `.ts.net` name), to the address set with `crewchat url`, to linked machines, or to
  a name listed in `CREWCHAT_HOSTS`; any other name gets HTTP 421.
- **Who is who.** Each joined folder has its own token, and the server names every session that
  connects with it. Over MCP and its hooks, an agent cannot choose who it posts as, and cannot
  post as the owner. Only the owner token can mint sign-in and join codes, post tasks, or rename
  and remove agents. That token is a file your user can read, though, so an agent with shell
  access can act as you: see [Agents run as you](#agents-run-as-you).
- **The chat page.** Sign-in uses a single-use code valid for two minutes. The session cookie is
  HttpOnly and SameSite=Strict, cross-site posts are refused, and the page ships a
  Content-Security-Policy that allows nothing from anywhere else (`default-src 'none'`; the page's
  own scripts and styles are inline, so `unsafe-inline` is on for them). The owner token never
  reaches a browser.
- **Guessing.** Ten wrong tokens or codes from one address lock it out for ten minutes. Behind
  `tailscale serve`, the address is the one the proxy adds, not one the client writes itself.
- **Tokens on disk.** Files that hold a token (`~/.crewchat/tokens/`, `state.json`, a project's
  `.mcp.json` and `.cursor/mcp.json`) are readable only by you from the moment they are written
  on macOS and Linux. Windows has no such file modes: there a file gets the permissions of the
  folder it is in, which for `~/.crewchat` is your user profile (yours and administrators'), and
  for a project's files is whatever the project folder allows. Project connection files are kept out of git through the repository's local exclude list.
  crewchat never writes a token into a settings file the repository has committed (`.mcp.json`,
  `.cursor/mcp.json`, `.gemini/settings.json`, `.qwen/settings.json`, `opencode.json`): it leaves
  that tool unconnected and says how to take the file out of git. It does not edit a committed
  `AGENTS.md` either.
  Folders join with a single-use code, so tokens are never copied by hand, and
  `crewchat place remove NAME` shuts a folder out at once.

## Shared files

Only the owner (a signed-in chat page, or the owner token) can upload files, and only the
signed-in chat page and connected agents can open them. Files are kept readable only by you, in
`~/.crewchat/files/`. Plain images (PNG, JPEG, GIF, WebP) are shown in the page; everything else,
SVG and HTML included, is only offered as a download, and every file is served with a policy that
stops it running anything. An agent can share any file it can read on its machine (as it could
already paste its contents into a message), so the usual rule applies: do not give agents in the
chat more access than you would give them alone. Over Tailscale, a machine only hands out files
that were shared on it, and only to members.

## Starting agents from the chat page

**+ New agent** (and `crewchat agent add`) lets the owner open a new agent session on the
machine that hosts the page. Only the owner can do it (a signed-in chat page, or the owner
token). It only starts one of the tools crewchat knows (Claude Code, Cursor's agent, Codex CLI,
Gemini CLI, Qwen Code, OpenCode), only in a folder connected to the chat on that machine, and the
only text passed to it is a fixed sentence with a one-time key: the name, role and task you type
reach the agent as chat messages, never through a shell. The agent runs with your normal
permissions for that tool, so it asks before acting unless you choose otherwise. Anyone who can
sign in to your chat page can start agents on that machine: keep sign-in codes to yourself.

New agent also reaches the host's folders and tools, for the owner only:

- `/api/folders` (a signed-in chat page, from the page itself, or the owner token) lists the
  folders in any folder your user can open (hidden ones and crewchat's own left out), makes a new
  folder (not a hidden one, not in crewchat's own folder), or connects a folder to the project,
  which runs `crewchat start` in it. Connecting refuses the disk's top, your home folder or any
  folder above it, and crewchat's own folder.
- `/api/tool-check` (a signed-in chat page) says whether a tool is installed, signed in, and what
  it asks once in one of the project's connected folders (any other folder is ignored). It only
  reads whether these are there, from the tool's own files and status command, and changes
  nothing.

## Permission prompts and notifications

**Allow** and **Deny** in Needs you answer a Claude Code agent's permission prompt. Only the owner
can (a signed-in chat page, or the owner token), and the prompt shows what the tool asks for in
full, as text. Anyone who can sign in to your chat page can allow what your agents ask: one more
reason to keep sign-in codes to yourself. `crewchat setup --permissions terminal` turns it off.

With notifications on, a browser that supports Web Push subscribes with its own push service
(Google's, Apple's, Mozilla's or Microsoft's). While no chat page is open, the server sends that
service an **empty** push, signed with a key kept in `~/.crewchat/push.json` (readable by you
only); the page's service worker then fetches what is new from your server, signed in as the page
is. Nothing from the chat goes through the push service, and the server posts only to those
services' addresses.

## Linking machines over Tailscale

- **Who can reach a server.** It still listens only on 127.0.0.1. `tailscale serve` makes it
  reachable from devices on your tailnet only; Funnel (the public internet) is never used.
  Traffic between machines is encrypted by Tailscale (WireGuard).
- **Who counts as a member.** Machines share a random key, handed to a new machine in exchange
  for a single-use code that expires after 10 minutes. Requests without the key are refused and
  count towards the lockout. The key is stored in `peers.json`, readable only by you.
- **Removing a machine** changes the key and tells the other members. The removed machine is
  refused at once, and the old key opens nothing for anyone: a member still catching up is asked to
  wait for the news. A member takes a new key only when the machine that sent it confirms it, so a
  removed machine cannot hand out a key of its own (but see the gap below). News that a member was removed is checked with
  that member first: one that answers with a newer key changed the key itself, so the news is
  refused, and a removed machine cannot cut off the machine that removed it. A member that missed
  more than one change is sent the keys in between, which only a member that made them knows. A member that was off
  during the change hears of it when it is back: the machine that removed someone keeps telling it,
  for up to a week, and asks again when it could not be confirmed. The removed machine keeps what it
  already received. One gap is left: a member that missed the real removal checks the news with the
  machine it names, and while that machine is off it cannot. Then a removed machine that reaches
  that member first can name the remover as removed and give the member a key of its own. It then
  reads that member's chat and can start agents there (as a linked machine can, with any permission
  mode), until the member joins again. The remover's own news is then refused, and **Machines** on the
  remover says so on that member's card, with how to link it again. So when you remove a lost or
  stolen machine, also remove it from your tailnet in Tailscale's admin console, so it cannot reach
  your other machines at all, and keep the machine you removed it from on until the others have heard:
  after **Remove**, the page lists any that were not reachable then. (Cloud sync: `crewchat cloud
  remove NAME` changes the account's key, and signing that Google account out of the lost machine
  (Google Account › Security › Your devices, below) stops it reading anything new.)
- **A member's address is checked before it changes.** Every member holds the key, so one could
  claim another's place. A new address is taken only when the machine no longer answers at the old
  one, and, when nothing answers there, only on the word of the machine itself, not another member's.
  A machine that answers there with a key this one lacks (it changed the key) has not moved. A key
  change is confirmed only at the address already known for its sender, never at one the request
  brings, and never at an address taken while the old one could not be checked. So neither a member
  nor a removed machine can point another member's entry at itself and confirm a key change it made up.
- **What one shared key leaves open.** Members share one key and name themselves in a header, so a
  member that pretends to be an asleep machine can still claim its new address for messages. These
  checks narrow what that can do; they do not make each machine prove who it is. Link only machines
  you control.
- **Any member can do anything a member can:** invite, remove, and send messages that show as
  coming from its agents or its owner. Link only machines you control.

## Cloud sync

- **Lock the project to your account.** A new Firebase project's rules let any Google account sign
  in. `crewchat cloud rules` prints rules that admit only your own account; publish them in the
  Firebase console (Firestore › Rules). Until you do, anyone who signs in to your project can fill
  it with data, though what is there stays sealed, as below.
- **Who can read.** Firestore's rules confine each Google account to its own data. Within an
  account, everything is encrypted on the machine before it is sent: messages, rosters and task
  claims are sealed with an AES-256-GCM account key, bound to the account, message, key and
  sending machine, so a stored item cannot be altered, moved or replayed as another. Whoever
  administers the Firebase project sees only ciphertext, timestamps and how many machines and
  messages there are.
- **Who can join.** All of an account's machines sign in as the same user, so the rules cannot
  tell them apart. Membership is therefore enforced by signatures: each machine has an RSA-3072
  key, the list of trusted machines is signed by a trusted machine, and the account key is only
  wrapped for machines on that list. A new machine is added only when a trusted one approves it
  after the person compares fingerprints on both screens.
- **The first approval is trust on first use.** A new machine accepts the list that its approver
  signed. `crewchat cloud status` on the new machine shows who signed it and their fingerprint;
  compare that with `crewchat cloud status` on the approving machine. Someone controlling the
  Firebase project could otherwise impersonate the approver to a brand-new machine.
- **Removing a machine** drops it from the list and switches the others to a new account key.
  It can still read what was sent before it was removed and is still in Firestore (at most 24
  hours), and, being signed in to the same Google account, it can delete or flood the account's
  data. Sign it out of your Google account too (Google Account → Security → Your devices).
- **No forward secrecy beyond deletion.** Someone who later obtains a machine's private key and
  the account key can decrypt what is still in Firestore. Deleting delivered messages keeps that
  small.
- **The OAuth client secret** in `oauth-client.json` identifies the crewchat app to Google; for
  desktop apps Google does not treat it as a strong secret, but keep it private anyway.

## Servers and routines from another machine

An MCP server runs a command, and a routine posts work and can start an agent. One the owner adds or changes on one
linked machine reaches the others, but runs nothing there until the owner approves it in Needs you on that machine's
own page, where its full command, address or task is shown. Every change to what it runs is asked again. That
approval is never offered to or taken from another machine, and a server's "runs without asking" is never carried
across: an agent that can post as you on one machine cannot get a command run on another without you there.

## Jira, Google Chat and the phone

These are extras, off until you turn them on in Project settings › Integrations or Settings ›
Machines & devices. [Privacy](docs/privacy.md) lists what each sends where.

- **Jira** signs in through Atlassian's own page (SSO); crewchat never asks for an API token. The
  sign-in is kept in the project's `config.json` on that machine and never reaches the chat page or
  an agent. Agents change Jira only through crewchat: `hub_jira`, one step at a time in the ticket cycle, and
  `hub_jira_write`, a comment or a new ticket, each of which waits for your Allow in Needs you. Text with a secret in
  it is refused before it is sent. What people write in a ticket reaches agents marked as their
  words, not your instructions, which limits but does not stop prompt injection through tickets.
- **GitHub**: the token you add on the GitHub card is this machine's, kept in `~/.crewchat/config.json`, never given
  to an agent or the page, and guarded like crewchat's own tokens. Agents only read through it (`hub_github_read`),
  not in a held project, and what pull requests say reaches them marked as other people's words. Use a fine-grained
  token with read access only.
- **Google Chat** keeps its webhook addresses and Google sign-in in `config.json`, never given to an
  agent. When a project is mirrored into a space, only your own messages there come back into the
  chat, as yours; everyone else's stay in Google Chat. Anyone who can post as you in that space can
  therefore post as you in the chat.
- **The phone** signs in like any browser, with a single-use code from `crewchat ui --print`, and
  keeps nothing from the chat. The page from anywhere (cloud sync) holds its own key, paired with a
  QR code that works once for 10 minutes; removing the phone in Machines & devices shuts it out like
  a machine. A lost phone that is still signed in can read and post as you until its sign-in ends
  (30 days) or you remove it.

## Agents run as you

Every agent in the chat runs as your user on its machine. One that can run shell commands can
read anything you can, including `~/.crewchat/tokens/Owner.token` and the browser bridge's token,
so it could post as you with `crewchat say`, answer its own permission prompts through the
server's owner API, or drive the browser around crewchat's checks. An agent may act as you only
when you allow it, and crewchat holds that line in four soft ways:

- **Owner commands ask you first inside an agent's session.** When `crewchat say`, `crewchat task`
  or any other command that runs as the owner starts in a process with an AI agent's session
  variables set (`CLAUDECODE`, `CURSOR_AGENT`, `GEMINI_CLI`, `QWEN_CODE`, `CODEX_SANDBOX`,
  `CODEX_SANDBOX_NETWORK_DISABLED`, `OPENCODE`, or crewchat's own `CREWCHAT_LINK_KEY`), it puts a
  card in Needs you with the whole command (and what it read from a file or stdin, such as
  `crewchat mcp add --json`), and runs only if you press Allow. Deny, no answer within ten minutes,
  or no server to ask stops it. There is no Always allow for these, and only this machine's page or
  your phone can answer them: a linked machine's page cannot.
- **Agents' hooks refuse reading the token files and the secrets beside them.** crewchat's
  before-hook on shell, edit and read calls (Claude Code, Codex, Gemini CLI, Qwen Code, OpenCode,
  and Cursor's shell and file reads) refuses a call whose command or path names
  `~/.crewchat/tokens/`, `Owner.token`, or, in any crewchat home, `peers.json` (the linked
  machines' key), `push.json` (the phone's push key), `cloud/` (cloud sync's keys), `config.json`
  (the Jira and Google Chat sign-ins) or `folders.json` (every connected folder's token) or `state.json` (the agents' link keys), or the
  crewchat folder itself (a search or an archive of it). The same goes for each workspace folder on
  this machine's list (a project kept outside `~/.crewchat`): its `tokens/`, the `.json` files above
  at its top, a wildcard there, and the folder itself; a code folder inside it is not refused. Editing
  `workspaces.json` or `held-workspaces.json` (which would clear a hold) is refused too. A file an
  agent shares in the chat is checked for all of these keys too.
- **A closed agent you gave Bypass or Auto permissions is yours to reopen.** A lead may reopen an agent that reports
  to it, in the background, but not one that runs with Bypass (`all`) or Auto permissions: once it closes, only your
  Reopen, or your own message to it, starts it again.
- **The guide** tells agents never to act as you unless you told them to in the chat.

These are soft blocks: an agent that unsets those variables, or reads the file through a command
the hook cannot recognise (a script, another spelling of the path), still gets past them. If you
need a hard line, run agents as a different OS user or in a container, and give them only their
folder's token.

## Held workspaces and Trust

A workspace folder holds a whole project: its chat, tasks and settings, and its place tokens. Anyone who can read the
folder can join the chat as one of its places, so keep it private. On macOS and Linux crewchat keeps its tokens
readable by your user only. On Windows, a workspace folder outside your user profile (`D:\ws\Bluebird`)
inherits that drive's permissions, which may let other users of the PC read it: keep workspaces under your profile,
or limit who can read the folder.

A workspace's own files prove nothing: anyone can write a marker, a `config.json` or routines. So a workspace this
machine did not have (made on another machine, copied, moved by hand, or located after it was not found) opens
**held**, and every place token in it is issued anew. While it is held:

- its routines start no agent;
- its MCP servers are not written into its folders;
- its approval rules allow nothing without asking;
- the guide written into its folders is crewchat's own part only, with none of its rules, skills, process notes,
  servers or spaces.

Its chat and tasks work. **Trust** is on the chat page only: the card first in Needs you, "Trust this workspace?",
lists what waits. No command and nothing in a workspace sets or clears the hold: the server takes Trust only from the
signed-in page, never with the owner token. So the hold stops an agent that does not have the owner token: its hooks
refuse the token files, and the commands that use the token (`crewchat ui` makes a sign-in code) wait for your Allow
card. An agent you let act as you is you to the server, and could sign a page in, so allow only what you asked for.
After Trust, its chat history is shown
to the agents that work in it, like any project's, so read it first if you do not know where the workspace came from.

## What it does not protect

- **Prompt injection between agents.** Agents in the chat can usually run commands. crewchat
  tells them that only the owner's messages are instructions and that no chat message overrides
  their project's safety rules, but that is guidance to a model, not enforcement. Treat every
  agent in the chat as able to influence the others.
- **A compromised host or agent machine.** Tokens are stored in plain files readable by your
  user (`~/.crewchat/tokens/` on the host, `.mcp.json` / `.cursor/mcp.json` in project folders).
  Anyone who can read a folder's token can join the chat as a new agent from that folder.
- **Messages at rest.** Chat history is stored unencrypted in `~/.crewchat/messages.jsonl`. Do
  not put secrets in messages.
- **Transport without a private network.** crewchat speaks plain HTTP on loopback and relies on
  the private network (Tailscale and similar) for encryption between machines.

## Good practice

- Only connect agents you run yourself.
- Do not use Tailscale Funnel or any public tunnel for the chat.
- Shut out a folder you no longer use (`crewchat place remove NAME`): its token stops working
  at once.
- If a folder's token may have leaked, remove that place and join the folder again.
- Sessions in the same folder share that folder's token, so they could read each other's
  messages through the hook endpoint if they guessed a link key. Keys are random; treat agents
  in one folder as trusting each other.

## Checking a download

Each GitHub release carries `SHA256SUMS`, the SHA-256 of its wheel and source package, made by the release workflow
from the files it builds and sends to PyPI. To check a file you downloaded from the release:

```bash
shasum -a 256 -c SHA256SUMS --ignore-missing     # macOS
sha256sum -c SHA256SUMS --ignore-missing         # Linux
```

On Windows, `Get-FileHash crewchat-X.Y.Z-py3-none-any.whl` prints the hash to compare. PyPI keeps its own SHA-256 of
each file too (on the release's "Download files" page), and `pip` checks it when it installs.

## Reporting a vulnerability

Please do not open a public issue. Use GitHub's "Report a vulnerability" on the repository's
Security tab, or email the maintainer listed on the GitHub profile. You should hear back within a
week.
