"""End-to-end harness. A Machine is a separate crewchat install as a person would have it: its own
home folder, port, project folder and server process, set up and run with the crewchat command.
Agents use exactly what `crewchat start` wrote into the project (the MCP address and token in
.mcp.json, and the hook commands in .claude/settings.local.json or .cursor/hooks.json). The
owner signs in to the chat page with a code from `crewchat ui --print` and uses the page's API,
the way the page's script does. Nothing here imports crewchat or reaches into a server.

    CREWCHAT_BIN=/path/to/crewchat   run an installed crewchat instead of this checkout's
"""
import atexit
import base64
import hashlib
import http.cookiejar
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="crewchat-e2e-")).resolve()
HOOK_STATE = TMP / "hook-state"  # the hooks keep per-session state in the temp folder
HOOK_STATE.mkdir()
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def crewchat_command():
    if os.environ.get("CREWCHAT_BIN"):
        return [os.environ["CREWCHAT_BIN"]]
    return [sys.executable, str(ROOT / "crewchat.py")]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def eventually(check, timeout=20.0, what="condition", every=0.1):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = check()
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            last = None
        if last:
            return last
        time.sleep(every)
    raise AssertionError("timed out waiting for " + what)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request(url, data=None, headers=None, opener=None, method=None, timeout=60):
    """(status, body bytes, headers); never raises for an HTTP status."""
    if isinstance(data, (dict, list)):
        data = json.dumps(data).encode()
        headers = dict({"Content-Type": "application/json"}, **(headers or {}))
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with (opener or urllib.request.build_opener(NoRedirect)).open(req, timeout=timeout) as answer:
            return answer.status, answer.read(), answer.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


class Machine:
    count = 0
    every = []  # all machines made, so none is left running

    def __init__(self, name, project="Demo"):
        Machine.count += 1
        Machine.every.append(self)
        self.name = name
        self.project_name = project
        self.dir = TMP / ("%02d-%s" % (Machine.count, name))
        self.home = self.dir / "crewchat-home"
        self.folder = self.dir / "notes-app"
        self.folder.mkdir(parents=True)
        (self.dir / "user-home").mkdir()
        subprocess.run(["git", "init", "-q", str(self.folder)], check=True)
        self.port = free_port()
        self.env = dict(os.environ, CREWCHAT_HOME=str(self.home), TMPDIR=str(HOOK_STATE),
                        CREWCHAT_NO_UPDATE_CHECK="1",  # dozens of servers: GitHub's limit; see test_lifecycle
                        HOME=str(self.dir / "user-home"),  # never the real login service or settings
                        TEMP=str(HOOK_STATE), TMP=str(HOOK_STATE))
        for var in ("CLAUDE_PROJECT_DIR", "CREWCHAT_PROJECT", "CREWCHAT_LISTEN"):
            self.env.pop(var, None)
        self._owner = None

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.port

    def cli(self, *args, cwd=None, check=True, timeout=120, stdin=None):
        out = subprocess.run(crewchat_command() + [str(a) for a in args], cwd=str(cwd or self.folder),
                             env=self.env, capture_output=True, text=True, timeout=timeout, input=stdin)
        if check and out.returncode != 0:
            raise AssertionError("crewchat %s failed (%d):\n%s\n%s" % (" ".join(map(str, args)), out.returncode,
                                                                     out.stdout, out.stderr))
        return out.stdout + out.stderr

    def start(self, folder=None, place=None, client="all"):
        folder = Path(folder or self.folder)
        out = self.cli("start", "--no-service", "--no-open", "--port", self.port, "--place", place or self.name,
                       "--project", self.project_name, "--client", client, cwd=folder)
        self.wait_up()
        return out

    def up(self):
        try:
            return request(self.url + "/health", timeout=3)[0] == 200
        except OSError:
            return False

    def wait_up(self):
        eventually(self.up, what="%s's server to answer" % self.name)

    def server_pids(self):
        out = subprocess.run(["pgrep", "-f", "serve --log %s" % (self.home / "hub.log")], capture_output=True, text=True)
        return [int(p) for p in out.stdout.split()]

    def kill(self, hard=True):
        """Stop this machine's server as a crash (hard) or a normal quit would."""
        for pid in self.server_pids():
            try:
                os.kill(pid, 9 if hard else 15)
            except ProcessLookupError:
                pass
        eventually(lambda: not self.up() and not self.server_pids(), what="%s's server to stop" % self.name)
        self._owner = None

    def restart(self):
        self.kill(hard=False)
        out = self.cli("start", "--no-service", "--no-open", cwd=self.folder)
        self.wait_up()
        return out

    def log(self):
        try:
            return (self.home / "hub.log").read_text(errors="replace")
        except OSError:
            return ""

    def agent(self, session=None, client="claude", folder=None):
        return Agent(self, Path(folder or self.folder), session, client)

    @property
    def owner(self):
        if self._owner is None:
            self._owner = Owner(self)
        return self._owner

    def link_to(self, other):
        """Join `other`'s group of linked machines over the network, with the peers commands."""
        invite = other.cli("peers", "invite", "--url", other.url, "--name", other.name)
        line = next(l for l in invite.splitlines() if "crewchat peers join" in l).split()
        target, code = line[line.index("join") + 1], line[line.index("join") + 2]
        return self.cli("peers", "join", target, code, "--url", self.url, "--name", self.name)

    def join_host(self, host, place=None, client="all"):
        """Connect this machine's project folder to `host`'s chat (one host, no server here)."""
        invite = host.cli("invite", "--url", host.url)
        line = next(l for l in invite.splitlines() if "crewchat join" in l).split()
        args = ["join", "--url", line[line.index("--url") + 1], "--code", line[line.index("--code") + 1], "--client", client]
        if place:
            args += ["--place", place]
        return self.cli(*args)

    def peers_status(self):
        return self.cli("peers", "status")

    def stop(self):
        try:
            self.kill(hard=True)
        except AssertionError:
            pass


class Agent:
    """One Claude Code or Cursor session in a project folder: MCP calls with the address and token
    `crewchat start` wrote, and hooks run exactly as the client would run them."""

    count = 0

    def __init__(self, machine, folder, session=None, client="claude"):
        Agent.count += 1
        self.machine, self.folder, self.client = machine, folder, client
        self.session = session or "%s-%s-%d-%d" % (machine.name, client, Agent.count, os.getpid())
        rel = ".mcp.json" if client == "claude" else ".cursor/mcp.json"
        entry = json.loads((folder / rel).read_text())["mcpServers"]["crewchat"]
        self.mcp = entry["url"]
        self.token = entry["headers"]["Authorization"][7:]
        self.ids = 0
        self.sid = None
        self.hello = self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                              "clientInfo": {"name": "claude-code" if client == "claude" else client,
                                                             "version": "1"}})
        self._rpc("notifications/initialized", None, notify=True)
        self.name = None

    def _rpc(self, method, params, notify=False):
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            self.ids += 1
            body["id"] = self.ids
        headers = {"Authorization": "Bearer " + self.token, "Accept": "application/json, text/event-stream"}
        if self.sid:
            headers["Mcp-Session-Id"] = self.sid
        status, raw, answer_headers = request(self.mcp, body, headers)
        if answer_headers.get("Mcp-Session-Id"):
            self.sid = answer_headers["Mcp-Session-Id"]
        if notify:
            return None
        if status != 200:
            raise AssertionError("MCP %s: HTTP %d %s" % (method, status, raw[:300]))
        data = json.loads(raw)
        if "error" in data:
            raise AssertionError("MCP %s: %s" % (method, data["error"]))
        return data["result"]

    def call(self, tool, **arguments):
        """The tool's text; raises if the tool reported an error."""
        result = self._rpc("tools/call", {"name": tool, "arguments": arguments})
        text = result["content"][0].get("text", "")
        if result.get("isError"):
            raise ToolError(text)
        return text

    def call_result(self, tool, **arguments):
        return self._rpc("tools/call", {"name": tool, "arguments": arguments})

    def whoami(self):
        line = next(l for l in self.call("hub_agents").splitlines() if " (you)" in l)
        self.name = line.split(" (you)")[0].strip()
        return self.name

    # Hooks ------------------------------------------------------------------------------------
    def _hook_command(self, event):
        if self.client == "claude":
            settings = json.loads((self.folder / ".claude" / "settings.local.json").read_text())
            key = {"prompt": "UserPromptSubmit", "stop": "Stop"}[event]
            return settings["hooks"][key][0]["hooks"][0]["command"]
        hooks = json.loads((self.folder / ".cursor" / "hooks.json").read_text())
        return hooks["hooks"]["stop"][0]["command"]

    def hook(self, event, listen=0, extra=None, timeout=120):
        """Run this session's hook as the client would. Returns its stdout."""
        payload = {"session_id": self.session, "hook_event_name": event}
        if self.client == "cursor":
            payload = {"conversation_id": self.session, "status": "completed", "loop_count": 0,
                       "workspace_roots": [str(self.folder)]}
        payload.update(extra or {})
        env = dict(self.machine.env)
        if listen is not None:  # None: the project's own setting (crewchat listen)
            env["CREWCHAT_LISTEN"] = str(listen)
        if self.client == "claude":
            env["CLAUDE_PROJECT_DIR"] = str(self.folder)
        out = subprocess.run(shlex.split(self._hook_command(event)), input=json.dumps(payload), cwd=str(self.folder),
                             env=env, capture_output=True, text=True, timeout=timeout)
        if out.returncode != 0:
            raise AssertionError("hook %s failed (%d): %s" % (event, out.returncode, out.stderr))
        return out.stdout

    def stop_hook(self, listen=0, **kw):
        """What the stop hook hands the agent at the end of a turn ('' for nothing)."""
        out = self.hook("stop", listen, **kw).strip()
        if not out:
            return ""
        data = json.loads(out)
        return data.get("reason") or data.get("followup_message") or ""

    @property
    def key(self):
        """The link key this session's hooks keep."""
        state = HOOK_STATE / "crewchat-hooks" / ("%s-%s" % (self.client, hashlib.sha256(self.session.encode()).hexdigest()[:16]))
        return json.loads(state.read_text())["key"]

    def link(self):
        """Do what a newly opened session does: its first hook asks it to link, and it does."""
        if self.client == "claude":
            asked = self.hook("prompt")
        else:
            asked = self.stop_hook()
        key = re.search(r'key "([^"]+)"', asked).group(1)
        self.call("hub_link", key=key)
        return self.whoami()


class ToolError(AssertionError):
    pass


class Owner:
    """The owner on the chat page: signed in with a one-time code, using the page's API."""

    def __init__(self, machine):
        self.machine = machine
        out = machine.cli("ui", "--print")
        code = re.search(r"([A-Z2-9]{4}-[A-Z2-9]{4})", out).group(1)
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), NoRedirect)
        status, _, _ = request(machine.url + "/login?code=" + code.replace("-", ""), opener=self.opener)
        if status != 303:
            raise AssertionError("sign-in failed: HTTP %d" % status)
        self.code = code

    def post(self, path, body, origin=True):
        headers = {"Origin": self.machine.url} if origin else {}
        return request(self.machine.url + path, body, headers, self.opener)

    def send(self, to, text, kind="msg", files=None):
        body = {"to": to, "text": text, "kind": kind}
        if files:
            body["files"] = files
        status, raw, _ = self.post("/api/send", body)
        if status != 200:
            raise AssertionError("send failed: HTTP %d %s" % (status, raw[:200]))
        return json.loads(raw)["id"]

    def task(self, to, text):
        return self.send(to, text, "task")

    def role(self, agent, role):
        status, raw, _ = self.post("/api/role", {"agent": agent, "role": role})
        if status != 200:
            raise AssertionError("role failed: HTTP %d %s" % (status, raw[:200]))

    def upload(self, name, data, ctype):
        status, raw, _ = request(self.machine.url + "/api/upload", data, {
            "Origin": self.machine.url, "Content-Type": ctype, "X-File-Name": urllib.parse.quote(name)}, self.opener)
        if status != 200:
            raise AssertionError("upload failed: HTTP %d %s" % (status, raw[:200]))
        return json.loads(raw)

    chat = ""  # the project this owner looks at ("": the first)

    def poll(self, after=-1):
        url = self.machine.url + "/api/poll?after=%d&v=-1&wait=0" % after + ("&chat=" + self.chat if self.chat else "")
        status, raw, _ = request(url, opener=self.opener)
        if status != 200:
            raise AssertionError("poll failed: HTTP %d" % status)
        return json.loads(raw)

    def messages(self):
        """The whole chat, read the way a page open all along would have it."""
        out, after = [], 0
        while True:
            data = self.poll(after)
            out += data["messages"]
            if not data.get("more"):
                return out
            after = out[-1]["seq"]

    def agents(self):
        return self.poll()["agents"]

    def row(self, name):
        return next((a for a in self.agents() if a["agent"] == name), None)

    def find(self, text=None, **match):
        for m in self.messages():
            if text is not None and text not in m.get("text", ""):
                continue
            if all(m.get(k) == v for k, v in match.items()):
                return m
        return None

    def wait_for(self, text=None, timeout=20, **match):
        return eventually(lambda: self.find(text, **match), timeout, "the page to show %r %s" % (text, match or ""))

    def sees(self, agent, online=True):
        row = self.row(agent)
        return row is not None and (not online or row["online"])


@atexit.register
def cleanup():
    """Stop every server this run started and remove its files (CREWCHAT_E2E_KEEP=1 keeps them)."""
    for machine in Machine.every:
        if machine.server_pids():
            machine.stop()
    if not os.environ.get("CREWCHAT_E2E_KEEP"):
        shutil.rmtree(TMP, ignore_errors=True)
