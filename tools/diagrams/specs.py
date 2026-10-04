"""The diagrams: what each shows and the steps it plays. Coordinates are on a 1280x720 canvas;
the title sits top left, the caption along the bottom (y 650 to 700)."""


def card(id, label, x, y, w=220, h=72, icon=None, color="blue", sub="", shape="card"):
    return {"id": id, "label": label, "sub": sub, "x": x, "y": y, "w": w, "h": h, "icon": icon, "color": color,
            "shape": shape}


def edge(a, b, label="", **kw):
    return dict({"from": a, "to": b, "label": label}, **kw)


def go(*edges, caption, **kw):
    """A step: packets along these edges ("a-b", or {"id": "a-b", "reverse": True})."""
    return dict({"edges": [e if isinstance(e, dict) else {"id": e} for e in edges], "caption": caption}, **kw)


def back(id, **kw):
    return dict({"id": id, "reverse": True}, **kw)


def msg(a, b, label, caption=None, reply=False, **kw):
    return dict({"from": a, "to": b, "label": label, "caption": caption or label, "reply": reply}, **kw)


def note(over, text, caption=None, **kw):
    return dict({"note": text, "over": over, "caption": caption or text}, **kw)


AGENT = "orange"   # Claude Code
CURSOR = "slate"
SERVER = "blue"

SPECS = {}

# ---------------------------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------------------------
SPECS["architecture"] = {
    "title": "One chat for every agent",
    "subtitle": "Each machine runs crewchat; the machines link up, and you see it all",
    "video": True,
    "groups": [
        {"label": "Mac", "icon": "laptop", "color": "blue", "x": 60, "y": 262, "w": 540, "h": 362},
        {"label": "Windows laptop", "icon": "monitor", "color": "purple", "x": 680, "y": 262, "w": 540, "h": 362},
    ],
    "nodes": [
        card("you", "You", 500, 118, 280, 74, "user", "teal", "the chat page, on any device"),
        card("c1", "Claude Code", 86, 300, 236, 72, "bot", AGENT, "session 1"),
        card("c2", "Claude Code", 340, 300, 236, 72, "bot", AGENT, "session 2"),
        card("s1", "crewchat", 190, 506, 280, 84, "crewchat", SERVER, "server on 127.0.0.1"),
        card("c3", "Cursor", 706, 300, 236, 72, "bot", CURSOR, "session 1"),
        card("c4", "Claude Code", 960, 300, 236, 72, "bot", AGENT, "session 2"),
        card("s2", "crewchat", 810, 506, 280, 84, "crewchat", "purple", "server on 127.0.0.1"),
    ],
    "edges": [
        edge("c1", "s1", "MCP"), edge("c2", "s1", "MCP"), edge("c3", "s2", "MCP"), edge("c4", "s2", "MCP"),
        edge("you", "s1", "", sides="lt", via=[[130, 155], [130, 230], [330, 230]]),
        edge("you", "s2", "", sides="rt", via=[[1150, 155], [1150, 230], [950, 230]]),
        edge("s1", "s2", "Tailscale or cloud sync", sides="rl", both=True, dashed=True),
    ],
    "steps": [
        go("you-s1", caption="You write a message on the chat page", icon="chat"),
        go("s1-s2", caption="crewchat syncs it to your other machine, privately", icon="lock"),
        go(back("c3-s2"), caption="Cursor's hook hands it over as soon as its turn ends", icon="mail"),
        go("c3-s2", caption="Cursor answers, with the hub_send tool", packet="answer"),
        go(back("s1-s2"), caption="The answer syncs back", icon="lock"),
        go(back("you-s1"), caption="It appears on your page, next to every other agent's messages", icon="chat"),
        go(back("c1-s1"), back("c2-s1"), back("c4-s2"), caption="Every agent sees it too, and can join in", icon="eye"),
    ],
    "summary": "One conversation, whichever machine each agent runs on",
}

# ---------------------------------------------------------------------------------------------
# Getting started
# ---------------------------------------------------------------------------------------------
SPECS["talk"] = {
    "type": "sequence", "video": True,
    "title": "How a message reaches an agent",
    "subtitle": "No relaying: the agent's hook delivers it the moment its turn ends",
    "participants": [
        {"id": "you", "label": "You", "icon": "user", "color": "teal"},
        {"id": "page", "label": "Chat page", "icon": "page", "color": "blue"},
        {"id": "srv", "label": "crewchat", "icon": "crewchat", "color": SERVER},
        {"id": "hook", "label": "Stop hook", "icon": "hook", "color": "purple"},
        {"id": "agent", "label": "Agent", "icon": "bot", "color": AGENT},
    ],
    "stepDur": 2.1,
    "steps": [
        note(["hook", "agent"], "turn finished: the hook waits", "The agent finished its turn; its hook waits for messages"),
        msg("you", "page", "type a message", "You type a message on the chat page"),
        msg("page", "srv", "send", "The page sends it to crewchat"),
        msg("srv", "hook", "a new message", "crewchat answers the waiting hook at once", reply=True),
        msg("hook", "agent", "here it is: carry on", "The hook gives the message to the agent", reply=True),
        msg("agent", "srv", "hub_send to Owner", "The agent answers with the hub_send tool", packet="answer"),
        msg("srv", "page", "the answer", "The answer appears on your page", reply=True),
    ],
    "summary": "You never copy anything between sessions",
}


SPECS["start"] = {
    "title": "What crewchat start does",
    "subtitle": "Safe to run again: each check skips what is already done",
    "nodes": [
        card("run", "crewchat start", 24, 334, 164, 64, None, "teal", shape="pill"),
        card("d1", "Set up as a host?", 228, 322, 236, 88, "question", "amber"),
        card("a1", "Set up ~/.crewchat", 228, 506, 236, 88, "folder", "green", "settings and your token"),
        card("d2", "Server running?", 512, 322, 236, 88, "question", "amber"),
        card("a2", "Start the server", 512, 506, 236, 88, "server", "green", "and at every login"),
        card("d3", "Folder connected?", 796, 322, 236, 88, "question", "amber"),
        card("a3", "Connect the folder", 796, 506, 236, 88, "plug", "green", "MCP config and hooks"),
        card("a4", "Refresh the hooks", 796, 140, 236, 88, "refresh", "purple"),
        card("end", "Open the chat", 1072, 326, 190, 80, "chat", "blue", "signed in as you"),
    ],
    "edges": [
        edge("run", "d1"), edge("d1", "a1", "no"), edge("d1", "d2"),
        edge("a1", "d2", "", sides="rl"), edge("d2", "a2", "no"), edge("d2", "d3"),
        edge("a2", "d3", "", sides="rl"), edge("d3", "a3", "no"), edge("d3", "a4", "yes"),
        edge("a3", "end", "", sides="rl"), edge("a4", "end", "", sides="rt"),
    ],
    "steps": [
        go("run-d1", caption="You run crewchat start in your project folder", icon="terminal"),
        go("d1-a1", caption="The first time, it sets this machine up as the chat's host", icon="folder"),
        go("a1-d2", "d2-a2", caption="It starts the server, and has it start at every login", icon="server",
           ) | {"edges": [{"id": "a1-d2", "span": [.1, .45]}, {"id": "d2-a2", "span": [.5, .85]}]},
        go("a2-d3", "d3-a3", caption="It connects the folder: MCP config and hooks for Claude Code and Cursor",
           icon="plug") | {"edges": [{"id": "a2-d3", "span": [.1, .45]}, {"id": "d3-a3", "span": [.5, .85]}]},
        go("a3-end", caption="Then it opens the chat page, signed in as you", icon="chat"),
        go("run-d1", caption="Next time, every check passes: it only refreshes the hooks", icon="refresh", dur=3.4) | {
            "edges": [{"id": "run-d1", "span": [.04, .2]}, {"id": "d1-d2", "span": [.22, .38]},
                      {"id": "d2-d3", "span": [.4, .56]}, {"id": "d3-a4", "span": [.58, .74]},
                      {"id": "a4-end", "span": [.76, .92]}]},
    ],
}

SPECS["link"] = {
    "type": "sequence",
    "title": "How a new session joins",
    "subtitle": "Each session links its name to its hook once, by itself",
    "left": 230, "right": 1050,
    "participants": [
        {"id": "agent", "label": "Agent session", "icon": "bot", "color": AGENT},
        {"id": "hook", "label": "Its hook", "icon": "hook", "color": "purple"},
        {"id": "srv", "label": "crewchat", "icon": "crewchat", "color": SERVER},
    ],
    "steps": [
        msg("agent", "srv", "first chat tool call (MCP)", "The new session calls a chat tool for the first time"),
        msg("srv", "agent", "you are claude-macbook", "crewchat gives it a name: claude-macbook", reply=True),
        msg("hook", "srv", "messages for key K?", "Its hook asks for messages, with the session's key"),
        msg("srv", "hook", "link first", "crewchat doesn't know that key yet", reply=True),
        msg("hook", "agent", "call hub_link with key K", "The hook tells the agent to link", reply=True),
        msg("agent", "srv", "hub_link K", "The agent calls hub_link with the key"),
        msg("srv", "agent", "linked: here is who else is here", "Linked: from now on, its hook gets its messages",
            reply=True),
    ],
}

SPECS["bids"] = {
    "title": "Post a task: the agents settle who takes it",
    "subtitle": "Every agent bids once; exactly one takes it",
    "nodes": [
        card("you", "You", 60, 330, 190, 80, "user", "teal", "post a task"),
        card("srv", "crewchat", 360, 324, 240, 92, "crewchat", SERVER, "the chat"),
        card("a1", "claude-macbook", 820, 140, 320, 84, "bot", AGENT, "wrote these tests"),
        card("a2", "cursor-laptop", 820, 328, 320, 84, "bot", CURSOR, "busy with the UI"),
        card("a3", "claude-desktop", 820, 516, 320, 84, "bot", AGENT, "new to this repo"),
    ],
    "edges": [
        edge("you", "srv"), edge("srv", "a1", bend=710), edge("srv", "a2"), edge("srv", "a3", bend=710),
    ],
    "steps": [
        go("you-srv", caption="You post a task: the test that fails now and then", packet="task 9"),
        go("srv-a1", "srv-a2", "srv-a3", caption="Every agent gets it", packet="task 9"),
        go(back("srv-a1", packet="yes: I wrote it"), back("srv-a2", packet="no: busy"),
           back("srv-a3", packet="no"), caption="Each bids once: yes or no, and why", dur=2.8),
        go(back("srv-a1", packet="hub_take 9"), caption="The best placed calls hub_take: only the first caller gets it"),
        go("srv-a2", "srv-a3", caption="Everyone else is told it's taken", packet="taken"),
        go(back("srv-a1", span=[.1, .45], packet="found it"), back("you-srv", span=[.5, .85], packet="found it"),
           caption="It reports back to you as it works"),
    ],
    "summary": "No double work: exactly one agent owns each task",
}

SPECS["listening"] = {
    "title": "When an agent is listening",
    "subtitle": "Its card on the chat page shows which of these it is in",
    "nodes": [
        card("work", "Working", 120, 220, 290, 100, "zap", "green", "busy with its turn"),
        card("wait", "Waiting for messages", 870, 220, 300, 100, "clock", "purple", "its hook holds the line"),
        card("idle", "Idle", 495, 470, 290, 100, "moon", "slate", "until its user types"),
    ],
    "edges": [
        edge("work", "wait", "its turn ends", sides="rl", offset=-24, labelAt=.5),
        edge("wait", "work", "a message arrives", sides="lr", offset=24, labelAt=.5),
        edge("wait", "idle", "nothing for 30 minutes", sides="br"),
        edge("idle", "work", "its user types", sides="lb"),
    ],
    "steps": [
        go("work-wait", caption="Its turn ends: the hook waits for messages, up to 30 minutes", icon="clock"),
        go("wait-work", caption="A message arrives: it gets to work at once", icon="mail"),
        go("work-wait", caption="Done: back to waiting", icon="clock"),
        go("wait-idle", caption="Nothing for 30 minutes: it goes idle, and costs nothing", icon="moon"),
        go("idle-work", caption="When its user types, it's back at work", icon="zap"),
    ],
}

# ---------------------------------------------------------------------------------------------
# How it works
# ---------------------------------------------------------------------------------------------
SPECS["pieces"] = {
    "title": "The pieces",
    "subtitle": "One small server per machine, listening on 127.0.0.1 only",
    "groups": [
        {"label": "A connected folder", "icon": "folder", "color": "amber", "x": 44, "y": 140, "w": 340, "h": 486},
        {"label": "crewchat, on 127.0.0.1", "icon": "server", "color": "blue", "x": 468, "y": 140, "w": 410, "h": 486},
    ],
    "nodes": [
        card("cc", "Claude Code", 74, 186, 280, 76, "bot", AGENT),
        card("cu", "Cursor", 74, 300, 280, 76, "bot", CURSOR),
        card("hk", "Their hooks", 74, 414, 280, 76, "hook", "purple", "crewchat hook"),
        card("mcp", "/mcp", 498, 186, 350, 76, "plug", SERVER, "the tools agents use"),
        card("api", "/api/hook", 498, 300, 350, 76, "hook", SERVER, "new messages for a hook"),
        card("pg", "/", 498, 414, 350, 76, "page", SERVER, "the chat page"),
        card("st", "Plain files", 498, 528, 350, 76, "db", "slate", "messages.jsonl, state.json, files"),
        card("you", "You", 970, 414, 260, 76, "user", "teal", "signed in"),
        card("other", "Your other machines", 970, 186, 260, 76, "mesh", "purple", "Tailscale or cloud sync"),
    ],
    "edges": [
        edge("cc", "mcp", "MCP", labelAt=.32), edge("cu", "mcp", "", sides="rl", bend=446),
        edge("hk", "api"), edge("you", "pg"),
        edge("mcp", "other", "", both=True, dashed=True),
    ],
    "steps": [
        go("cc-mcp", "cu-mcp", caption="Agents use crewchat's tools over MCP, with the folder's own token", icon="plug"),
        go("hk-api", caption="Their hooks ask /api/hook for new messages", icon="hook"),
        go("you-pg", caption="You read and write on the chat page, once signed in", icon="chat"),
        {"glow": ["st"], "caption": "Everything is kept in plain files, on this machine", "badgeAt": [840, 534]},
        go("mcp-other", caption="Optionally, it syncs with crewchat on your other machines", icon="lock"),
    ],
}

SPECS["hooks"] = {
    "type": "sequence",
    "title": "Hooks: messages without relaying",
    "subtitle": "A message counts as read only after the agent has handled it",
    "left": 230, "right": 1050,
    "participants": [
        {"id": "agent", "label": "Agent", "icon": "bot", "color": AGENT},
        {"id": "hook", "label": "Stop hook", "icon": "hook", "color": "purple"},
        {"id": "srv", "label": "crewchat", "icon": "crewchat", "color": SERVER},
    ],
    "stepDur": 2.1,
    "steps": [
        msg("agent", "hook", "turn finished", "The agent finishes its turn, and its stop hook runs"),
        msg("hook", "srv", "anything for me? (up to 30 min)", "The hook asks crewchat, and waits up to 30 minutes"),
        note(["srv"], "you send message 12", "You send message 12"),
        msg("srv", "hook", "message 12, still unread", "crewchat answers the waiting hook at once", reply=True),
        msg("hook", "agent", "here is 12: carry on", "The hook hands it to the agent, which carries on", reply=True),
        msg("agent", "srv", "hub_send: the answer", "The agent answers", packet="answer"),
        msg("agent", "hook", "turn finished", "Its turn ends again"),
        msg("hook", "srv", "12 handled: anything more?", "Only now is message 12 marked read"),
    ],
}

SPECS["take"] = {
    "type": "sequence",
    "title": "Two agents, one task",
    "subtitle": "hub_take is first come, first served",
    "left": 230, "right": 1050,
    "participants": [
        {"id": "a", "label": "claude-macbook", "icon": "bot", "color": AGENT},
        {"id": "srv", "label": "crewchat", "icon": "crewchat", "color": SERVER},
        {"id": "b", "label": "cursor-laptop", "icon": "bot", "color": CURSOR},
    ],
    "cardW": 200,
    "steps": [
        note(["a", "srv", "b"], "both bid yes on task 9", "Both agents bid yes on task 9"),
        msg("a", "srv", "hub_take 9", "claude-macbook calls hub_take"),
        msg("b", "srv", "hub_take 9", "A moment later, so does cursor-laptop"),
        msg("srv", "a", "task 9 is yours", "The first call wins, and everyone is told", reply=True),
        msg("srv", "b", "already taken by claude-macbook", "The second gets a clear no, and moves on", reply=True),
    ],
}

SPECS["files"] = {
    "type": "sequence",
    "title": "Sharing a screenshot",
    "subtitle": "Files stay on your machines, and travel only when an agent asks",
    "participants": [
        {"id": "you", "label": "You", "icon": "user", "color": "teal"},
        {"id": "m", "label": "crewchat", "sub": "on the macbook", "icon": "crewchat", "color": SERVER},
        {"id": "l", "label": "crewchat", "sub": "on the laptop", "icon": "crewchat", "color": "purple"},
        {"id": "agent", "label": "Agent", "sub": "on the laptop", "icon": "bot", "color": AGENT},
    ],
    "cardW": 200,
    "steps": [
        msg("you", "m", "paste a screenshot, send", "You paste a screenshot and send it", icon="image"),
        msg("m", "l", "the message, with the file's id", "Only the message and the file's id sync to the laptop"),
        msg("agent", "l", "hub_file with the id", "The agent asks for the file"),
        msg("l", "m", "fetch the file, once", "The laptop fetches it from the macbook, once"),
        msg("m", "l", "the file", "The file comes over", reply=True, icon="image"),
        msg("l", "agent", "the image, to look at", "The agent sees the image itself", reply=True, icon="image"),
    ],
}

# ---------------------------------------------------------------------------------------------
# Several machines
# ---------------------------------------------------------------------------------------------
SPECS["choose"] = {
    "title": "Which way to link your machines?",
    "subtitle": "Pick once; you can switch later",
    "nodes": [
        card("q1", "Can every machine\nrun Tailscale?", 50, 300, 290, 96, "question", "amber"),
        card("ts", "Tailscale", 470, 150, 330, 92, "mesh", "green", "peers invite, then join"),
        card("q2", "Happy to set up\nFirebase once?", 470, 440, 330, 96, "question", "amber"),
        card("cs", "Cloud sync", 920, 330, 320, 92, "flame", "orange", "cloud setup, then login"),
        card("oh", "One host", 920, 530, 320, 92, "server", "slate", "the others connect to it"),
    ],
    "edges": [
        edge("q1", "ts", "yes", sides="rl"), edge("q1", "q2", "no", sides="rl"),
        edge("q2", "cs", "yes", sides="rl"), edge("q2", "oh", "no", sides="rl"),
    ],
    "steps": [
        go("q1-ts", caption="Every machine can run Tailscale? Link them directly: nothing leaves your devices", icon="mesh"),
        go("q1-q2", caption="If not: are you happy to set up a Firebase project once?", icon="question"),
        go("q2-cs", caption="Yes: cloud sync, end to end encrypted, from anywhere", icon="lock"),
        go("q2-oh", caption="No: run one host, and connect the other machines' agents to it", icon="server"),
    ],
}

SPECS["tailnet"] = {
    "title": "Linked over Tailscale",
    "subtitle": "Each crewchat talks to the others directly, inside your tailnet",
    "groups": [{"label": "Your tailnet: only your devices", "icon": "mesh", "color": "green",
                "x": 330, "y": 138, "w": 620, "h": 490}],
    "nodes": [
        card("A", "macbook", 520, 180, 240, 84, "crewchat", SERVER, "crewchat"),
        card("B", "laptop", 370, 500, 240, 84, "crewchat", "purple", "crewchat"),
        card("C", "desktop", 670, 500, 240, 84, "crewchat", "teal", "crewchat"),
        card("a1", "Claude Code", 50, 186, 220, 72, "bot", AGENT),
        card("b1", "Cursor", 50, 506, 220, 72, "bot", CURSOR),
        card("c1", "Claude Code", 1010, 506, 220, 72, "bot", AGENT),
    ],
    "edges": [
        edge("A", "B", "", sides="lt", both=True), edge("A", "C", "", sides="rt", both=True),
        edge("B", "C", "", sides="rl", both=True),
        edge("a1", "A", "MCP"), edge("b1", "B", "MCP"), edge("c1", "C", "MCP"),
    ],
    "steps": [
        go("a1-A", caption="An agent on the macbook sends a message", icon="chat"),
        go("A-B", "A-C", caption="crewchat sends it straight to the other machines", icon="lock"),
        go(back("b1-B"), caption="Cursor on the laptop gets it", icon="mail"),
        go("b1-B", caption="And answers", packet="answer"),
        go(back("A-B"), "B-C", caption="The answer reaches every machine", icon="lock"),
    ],
    "summary": "No server in the middle: a machine that's off only takes its own agents out",
}

SPECS["peers"] = {
    "type": "sequence",
    "title": "Linking two machines",
    "subtitle": "One code, used once; then each machine keeps a line open to the other",
    "left": 330, "right": 950,
    "participants": [
        {"id": "m", "label": "macbook", "icon": "laptop", "color": SERVER},
        {"id": "l", "label": "laptop", "icon": "laptop", "color": "purple"},
    ],
    "boxes": [{"from": 3, "to": 4, "label": "from then on"}],
    "steps": [
        msg("m", "m", "crewchat peers invite", "On the macbook: crewchat peers invite makes a code, for 10 minutes",
            icon="key"),
        msg("l", "m", "crewchat peers join (address, code)", "On the laptop: crewchat peers join, with the address and code"),
        msg("m", "l", "the shared key, the members, tag B", "The macbook sends back the shared key and the members",
            reply=True, icon="key"),
        msg("l", "m", "anything new? (waits until there is)", "From then on, each asks the other for anything new"),
        msg("m", "l", "anything new? (waits until there is)", "and waits until there is: messages arrive at once"),
    ],
}

SPECS["cloud"] = {
    "title": "Linked with cloud sync",
    "subtitle": "Your own Firestore relays the messages; only your machines can read them",
    "groups": [{"label": "Your Google account", "icon": "flame", "color": "orange", "x": 420, "y": 136, "w": 440, "h": 210}],
    "nodes": [
        card("fs", "Your Firestore", 470, 186, 340, 100, "db", "orange", "stores encrypted data only"),
        card("m1", "macbook", 60, 470, 290, 92, "crewchat", SERVER, "crewchat"),
        card("m2", "laptop", 930, 470, 290, 92, "crewchat", "purple", "crewchat"),
    ],
    "edges": [
        edge("m1", "fs", "encrypted", sides="tl"),
        edge("fs", "m2", "pushed at once", sides="rt"),
    ],
    "steps": [
        {"glow": ["m1"], "caption": "On the macbook, crewchat encrypts each message with your account key",
         "badgeAt": [346, 474]},
        go("m1-fs", caption="It writes the encrypted message to your Firestore", packet="a8f3 9c1e"),
        go("fs-m2", caption="Firestore pushes it to your other machines at once", packet="a8f3 9c1e"),
        {"glow": ["m2"], "caption": "The laptop decrypts it: Google only ever stored ciphertext", "badgeAt": [1216, 474]},
        go(back("fs-m2", span=[.1, .45], packet="7d02 b6aa"), back("m1-fs", span=[.5, .85], packet="7d02 b6aa"),
           caption="Answers travel back the same way"),
    ],
}

SPECS["one-host"] = {
    "title": "One host for every machine",
    "subtitle": "The other machines' agents connect to one crewchat over Tailscale",
    "groups": [{"label": "Host machine", "icon": "monitor", "color": "blue", "x": 390, "y": 150, "w": 500, "h": 330}],
    "nodes": [
        card("h1", "Claude Code", 500, 196, 280, 76, "bot", AGENT, "on the host"),
        card("s", "crewchat", 500, 364, 280, 86, "crewchat", SERVER),
        card("l1", "Cursor", 40, 370, 260, 80, "bot", CURSOR, "on the laptop"),
        card("d1", "Claude Code", 980, 370, 260, 80, "bot", AGENT, "on the desktop"),
    ],
    "edges": [
        edge("h1", "s", "MCP"), edge("l1", "s", "over Tailscale", dashed=True), edge("d1", "s", "over Tailscale", dashed=True),
    ],
    "steps": [
        go("h1-s", caption="Agents on the host connect as usual"),
        go("l1-s", caption="Agents on other machines connect to it over Tailscale", icon="mesh"),
        go("d1-s", caption="Each with its own folder's token", icon="key"),
        {"glow": ["s"], "caption": "The catch: when the host is off, everyone loses the chat", "badgeAt": [776, 368]},
    ],
}

SPECS["phone"] = {
    "title": "Your phone",
    "subtitle": "The chat page over your tailnet; nothing is open to the internet",
    "groups": [{"label": "Your computer", "icon": "monitor", "color": "green", "x": 470, "y": 140, "w": 770, "h": 470}],
    "nodes": [
        card("p", "Phone", 40, 290, 270, 100, "phone", "teal", "Tailscale + the chat page"),
        card("t", "tailscale serve", 510, 290, 260, 100, "mesh", "green", "your tailnet only"),
        card("c", "crewchat", 930, 290, 270, 100, "crewchat", SERVER, "on 127.0.0.1"),
        card("a", "Claude Code", 930, 470, 270, 90, "bot", AGENT, "working in your project"),
        card("net", "The internet", 40, 500, 270, 90, "ban", "red", "can't reach any of it"),
    ],
    "edges": [edge("p", "t", "encrypted", dashed=True), edge("t", "c", "local"), edge("a", "c", "MCP")],
    "steps": [
        go("p-t", caption="You open the chat on your phone: it goes over your tailnet, encrypted", icon="lock"),
        go("t-c", caption="tailscale serve hands it to crewchat on the computer", icon="chat"),
        go(back("a-c"), caption="Your agent gets the message", icon="mail"),
        go("a-c", "t-c", "p-t", caption="Its answer comes back to your phone, live", packet="answer") | {
            "edges": [{"id": "a-c", "span": [.05, .3]}, {"id": "t-c", "reverse": True, "span": [.33, .58]},
                      {"id": "p-t", "reverse": True, "span": [.61, .86]}]},
        {"glow": ["net"], "caption": "Nothing is open to the internet: only your own devices get in",
         "badgeAt": [306, 504]},
    ],
    "summary": "Add it to your home screen, and answer your agents from anywhere",
}

# ---------------------------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------------------------
SPECS["who-takes"] = {
    "title": "Who takes a task?",
    "subtitle": "With a lead, the lead splits it up; without one, the agents bid",
    "nodes": [
        card("task", "You post a task", 24, 334, 170, 64, None, "teal", shape="pill"),
        card("q", "Is there a lead?", 236, 320, 220, 92, "question", "amber"),
        card("L", "The lead takes it", 520, 150, 250, 86, "crown", "purple", "and splits it up"),
        card("A", "hub_assign", 830, 150, 210, 86, "task", "purple", "a piece to each agent"),
        card("R", "Reports to you", 1090, 157, 170, 72, None, "green", shape="pill"),
        card("B", "Every agent bids", 520, 500, 250, 86, "users", "amber", "once: yes or no"),
        card("K", "hub_take", 830, 500, 210, 86, "check", "amber", "one takes it"),
        card("R2", "Reports to you", 1090, 507, 170, 72, None, "green", shape="pill"),
    ],
    "edges": [
        edge("task", "q"), edge("q", "L", "yes", sides="tl"), edge("L", "A"), edge("A", "R"),
        edge("q", "B", "no", sides="bl"), edge("B", "K"), edge("K", "R2"),
    ],
    "steps": [
        go("task-q", caption="You post a task", packet="task 9"),
        go("q-L", caption="With a lead: the lead takes it, and splits it up", icon="crown"),
        go("L-A", caption="It hands out each piece with hub_assign: no bidding", icon="task"),
        go("A-R", caption="Agents report progress to the lead, which reports to you", icon="chat"),
        go("q-B", caption="Without a lead: every agent bids once", icon="users"),
        go("B-K", "K-R2", caption="The best placed takes it, and reports back to you", icon="check") | {
            "edges": [{"id": "B-K", "span": [.1, .45]}, {"id": "K-R2", "span": [.5, .85]}]},
    ],
}

SPECS["team"] = {
    "type": "sequence",
    "title": "A lead, a developer and QA",
    "subtitle": "The lead splits the work; the others talk to each other directly",
    "participants": [
        {"id": "you", "label": "You", "icon": "user", "color": "teal"},
        {"id": "lead", "label": "Lead", "icon": "crown", "color": "purple"},
        {"id": "dev", "label": "Developer", "icon": "code", "color": AGENT},
        {"id": "qa", "label": "QA", "icon": "bug", "color": "green"},
    ],
    "cardW": 190, "stepDur": 2.1,
    "steps": [
        msg("you", "lead", "task 9: add a dark theme", "You post a task, and the lead gets it", packet="task 9"),
        msg("lead", "lead", "hub_take 9", "The lead takes it"),
        msg("lead", "dev", "hub_assign 11, part of 9", "and assigns a piece to the developer"),
        msg("dev", "lead", "hub_update 11: in progress", "The developer reports progress"),
        msg("dev", "qa", "ready to test: Settings, Theme", "and tells QA it's ready to test"),
        msg("qa", "dev", "bug: it resets after a restart", "QA finds a bug, and tells the developer directly", icon="bug"),
        msg("dev", "qa", "fixed: ready again", "The developer fixes it"),
        msg("qa", "lead", "hub_update 11: done", "QA marks it done"),
        msg("lead", "you", "dark theme done, one question", "The lead reports back to you"),
    ],
}

SPECS["task-states"] = {
    "title": "A task's progress",
    "subtitle": "hub_update moves it along; the lead and you see every change",
    "nodes": [
        card("as", "assigned", 30, 230, 200, 88, "task", "blue"),
        card("ip", "in progress", 330, 230, 220, 88, "zap", "green"),
        card("bl", "blocked", 330, 480, 220, 88, "pause", "red", "with the reason"),
        card("rv", "review", 720, 230, 220, 88, "eye", "purple", "QA tests it"),
        card("dn", "done", 1040, 230, 200, 88, "check", "green"),
    ],
    "edges": [
        edge("as", "ip"),
        edge("ip", "bl", "", sides="bt", offset=-40), edge("bl", "ip", "", sides="tb", offset=40),
        edge("ip", "rv", "", sides="rl", offset=-20), edge("rv", "ip", "a problem", sides="lr", offset=20, labelAt=.5),
        edge("rv", "dn"),
    ],
    "steps": [
        go("as-ip", caption="The agent starts: in progress", icon="zap"),
        go("ip-bl", caption="Stuck on something? blocked, with the reason", icon="pause"),
        go("bl-ip", caption="Unblocked: back to work", icon="zap"),
        go("ip-rv", caption="Ready: review, and QA tests it", icon="eye"),
        go("rv-ip", caption="QA finds a problem: back to in progress", icon="bug"),
        go("ip-rv", caption="Fixed: review again", icon="eye"),
        go("rv-dn", caption="Done: the lead and you see it at once", icon="check"),
    ],
}

SPECS["add-agent"] = {
    "type": "sequence",
    "title": "Add an agent from the chat",
    "subtitle": "A new session opens in a terminal window, already linked and briefed",
    "participants": [
        {"id": "you", "label": "You", "icon": "user", "color": "teal"},
        {"id": "page", "label": "Chat page", "icon": "page", "color": "blue"},
        {"id": "srv", "label": "crewchat", "icon": "crewchat", "color": SERVER},
        {"id": "term", "label": "New terminal", "icon": "terminal", "color": AGENT},
    ],
    "cardW": 190,
    "steps": [
        msg("you", "page", "Add an agent: name, role, task", "You click Add an agent, and fill in a name, role and task"),
        msg("page", "srv", "launch", "The page asks crewchat to launch it"),
        msg("srv", "term", "open Claude Code, one-time key", "A terminal window opens Claude Code in the folder", icon="terminal"),
        msg("term", "srv", "hub_link with the key", "The new session links itself, with the one-time key"),
        msg("srv", "term", "you are docs-writer: Docs, task 12", "It learns its name, role and first task",
            reply=True),
        msg("srv", "page", "docs-writer joins the list", "And it appears on your list, ready to work", reply=True),
    ],
}

# ---------------------------------------------------------------------------------------------
# Troubleshooting
# ---------------------------------------------------------------------------------------------
SPECS["no-answer"] = {
    "title": "An agent doesn't answer?",
    "subtitle": "Its card on the chat page tells you why",
    "nodes": [
        card("q", "Is it on the list?", 36, 300, 240, 86, "question", "amber"),
        card("n", "Restart its session", 36, 510, 240, 86, "refresh", "red", "in the connected folder"),
        card("c", "What does\nits card say?", 330, 300, 230, 86, "question", "amber"),
        card("idle", "Idle", 640, 126, 280, 86, "moon", "slate", "turn on: crewchat listen on"),
        card("wait", "Waiting", 640, 300, 280, 86, "clock", "purple", "for messages, but silent"),
        card("work", "Working", 640, 474, 280, 86, "zap", "green", "answers when its turn ends"),
        card("loop", "Loop limit", 990, 220, 250, 86, "ban", "orange", "write to it yourself"),
        card("to", "Check the To menu", 990, 400, 250, 86, "mail", "blue", "to it, or to everyone?"),
    ],
    "edges": [
        edge("q", "n", "no"), edge("q", "c", "yes"),
        edge("c", "idle", "", sides="rl", bend=600), edge("c", "wait"), edge("c", "work", "", sides="rl", bend=600),
        edge("wait", "loop", "busy?", sides="rl", bend=955), edge("wait", "to", "else", sides="rl", bend=955),
    ],
    "steps": [
        go("q-n", caption="Not on the list? Restart its session in the connected folder", icon="refresh"),
        go("q-c", caption="On the list: look at its card", icon="eye"),
        go("c-idle", caption="Idle: it reads messages when its user next types; turn listening on", icon="moon"),
        go("c-work", caption="Working: it answers when its current turn ends", icon="zap"),
        go("c-wait", caption="Waiting, but no answer?", icon="clock"),
        go("wait-loop", caption="Agents just talked a lot? The loop limit holds them: write to it yourself", icon="ban"),
        go("wait-to", caption="Otherwise, check the message went to it: the To menu", icon="mail"),
    ],
}

# ---------------------------------------------------------------------------------------------
# Videos only
# ---------------------------------------------------------------------------------------------
SPECS["link-options"] = {
    "gif": False, "video": True,
    "title": "Several machines, one chat",
    "subtitle": "Link them directly, or through your own Firebase project",
    "stepDur": 2.9,
    "groups": [
        {"label": "Tailscale: directly", "icon": "mesh", "color": "green", "x": 44, "y": 140, "w": 570, "h": 486},
        {"label": "Firebase: through your project", "icon": "flame", "color": "orange", "x": 666, "y": 140, "w": 570, "h": 486},
    ],
    "nodes": [
        card("la", "macbook", 209, 190, 240, 84, "crewchat", SERVER, "crewchat"),
        card("lb", "laptop", 74, 500, 240, 84, "crewchat", "purple", "crewchat"),
        card("lc", "desktop", 344, 500, 240, 84, "crewchat", "teal", "crewchat"),
        card("fs", "Your Firestore", 820, 186, 260, 96, "db", "orange", "encrypted data only"),
        card("ra", "macbook", 696, 500, 240, 84, "crewchat", SERVER, "crewchat"),
        card("rb", "laptop", 966, 500, 240, 84, "crewchat", "purple", "crewchat"),
    ],
    "edges": [
        edge("la", "lb", "", sides="lt", both=True), edge("la", "lc", "", sides="rt", both=True),
        edge("lb", "lc", "", sides="rl", both=True),
        edge("ra", "fs", "encrypted", sides="tl"), edge("fs", "rb", "encrypted", sides="rt"),
    ],
    "steps": [
        go("la-lb", "la-lc", caption="Over Tailscale, machines talk directly: nothing leaves your devices", icon="lock"),
        go("ra-fs", caption="Or through your own Firebase project, from anywhere", packet="a8f3 9c1e"),
        go("fs-rb", caption="End to end encrypted: Google stores it, but can't read it", packet="a8f3 9c1e"),
        {"glow": ["lb", "rb"], "caption": "Either way, a machine that's off only takes its own agents out",
         "badge": False},
    ],
}
