"""The shared fixture of the server tests: a throwaway home folder set before crewchat is imported, the
patches that keep tests off the real machine, and Base, a real server on a free port. Split out of
test_crewchat.py (review 14 Nov) so each area's tests have a file of their own."""
import atexit
import argparse
import ast
import base64
import contextlib
import gzip
import http.cookiejar
import io
import json
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def old_bridge(folder, client):
    """The bridge as crewchat wrote it before the browser proxy (R8): npx @playwright/mcp, gated by the hooks."""
    rel, servers = crewchat.BRIDGE_FILES[client]
    path = Path(folder) / rel
    data = json.loads(path.read_text())
    data[servers]["browser"] = {"command": "npx", "args": ["@playwright/mcp@latest", "--extension"],
                                "env": {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": "bridge-secret-123"}}
    path.write_text(json.dumps(data))
sys.path.insert(0, str(ROOT))

TMP = tempfile.mkdtemp(prefix="crewchat-test-")
os.environ["CREWCHAT_HOME"] = str(Path(TMP) / "home")
os.environ["CREWCHAT_NO_SHORTCUT"] = "1"  # no test, nor a child process of one, makes a real shortcut

import crewchat  # noqa: E402  (after CREWCHAT_HOME is set)

crewchat.Handler.log_message = lambda *args: None  # keep the request log out of the test output
crewchat.user_home = lambda: Path(TMP) / "user"  # never the real ~/.claude.json or ~/.cursor
crewchat.cursor_exe = lambda root=None: None  # never this machine's Cursor agent (tests that need one patch it)
REAL_WRITE_SHORTCUT = crewchat.write_shortcut
crewchat.write_shortcut = lambda *args, **kwargs: []  # `start` makes no real shortcut here (class Shortcut puts the real one back)
os.environ.pop("CLAUDE_CONFIG_DIR", None)
os.environ.pop("CODEX_HOME", None)  # Codex's settings: under user_home too
for _name in crewchat.AGENT_SESSION_ENV:  # run inside an agent's session (Claude Code's shell): owner commands would ask (O1)
    os.environ.pop(_name, None)


def committed_repo(files):
    """A new git repository in which these files (relative path -> text) are committed."""
    folder = Path(tempfile.mkdtemp(prefix="committed-", dir=TMP)).resolve()
    for rel, text in files.items():
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text(text)
    for args in (["init", "-q"], ["add", "-f", "."],  # -f: whatever the machine's own git ignore file says
                  ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "one"]):
        subprocess.run(["git", "-C", str(folder)] + args, check=True, capture_output=True)
    return folder


def connected(folder, *files):
    """crewchat's server in a folder's connection files, as `crewchat start` writes it (another chat's address)."""
    for rel in files:
        (Path(folder) / rel).parent.mkdir(parents=True, exist_ok=True)
        (Path(folder) / rel).write_text(json.dumps({"mcpServers": {"crewchat": {
            "type": "http", "url": "http://127.0.0.1:9/mcp", "headers": {"Authorization": "Bearer old-token"}}}}))


def cli(*argv):
    """Run a crewchat command in-process; returns what it printed."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        crewchat.main(list(argv))
    return out.getvalue()


def cli_in(home, *argv):
    """cli() with crewchat's home somewhere else."""
    with mock.patch.dict(os.environ, {"CREWCHAT_HOME": str(home)}), mock.patch.object(crewchat, "CURRENT_CHAT", None):
        return cli(*argv)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Session:
    """One agent session, as an MCP client sees the server: initialize, then tools with its id."""

    def __init__(self, base, token, client="claude-code"):
        self.mcp, self.token = base + "/mcp", token
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                           "clientInfo": {"name": client, "version": "1"}}}
        answer, headers = crewchat.post_json(self.mcp, token, body, want_headers=True)
        self.hello = answer["result"]
        self.sid = headers["Mcp-Session-Id"]

    def call(self, tool, **arguments):
        return crewchat.rpc(self.mcp, self.token, "tools/call", {"name": tool, "arguments": arguments},
                            session=self.sid)["result"]

    def text(self, tool, **arguments):
        return self.call(tool, **arguments)["content"][0]["text"]

    @property
    def me(self):
        """This session's name, as hub_agents reports it (creates the agent if it has none yet)."""
        line = next(l for l in self.text("hub_agents").splitlines() if " (you)" in l)
        return line.split(" (you)")[0]


def crewchat_sqlite():
    import sqlite3
    return sqlite3


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        shutil.rmtree(crewchat.home(), ignore_errors=True)
        cli("setup", "--project", "Demo", "--join", "anyone")  # its sessions link with their hooks' keys
        cls.server = crewchat.make_server(0)
        cls.hub = cls.server.RequestHandlerClass.hub
        port = cls.server.server_address[1]
        config = crewchat.load_config()
        config["port"] = port
        crewchat.save_config(config)
        cls.base = "http://127.0.0.1:%d" % port
        cls.mcp = cls.base + "/mcp"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.owner = crewchat.read_token("Owner")
        cls.place, cls.token = cls.join_place("mac")

    @classmethod
    def tearDownClass(cls):
        # What waits to be saved (Hub._save_soon) is written now, as a stopping server does: a timer left
        # behind would write this class's state into the next class's home.
        projects = cls.server.RequestHandlerClass.projects
        crewchat.flush_all(projects)
        cls.server.shutdown()
        cls.server.server_close()
        # Its SQLite files closed too, as a stopping server's process does: Windows cannot delete an open file, so the
        # next class's fresh home would keep this class's chat index (and search would find this class's messages).
        for project in projects.all() + ([projects.shared] if projects.shared is not None else []):
            project.hub.close_stores()

    @classmethod
    def join_place(cls, label):
        code = crewchat.post_json(cls.base + "/api/invite", cls.owner, {})["code"]
        answer = crewchat.post_json(cls.base + "/api/join", None, {"code": code, "place": label})
        return answer["place"], answer["token"]

    def agent(self, client="claude-code", token=None):
        """A session that has used a tool, so it is in the roster. Returns (session, name)."""
        session = Session(self.base, token or self.token, client)
        return session, session.me

    def raw(self, path, data=None, headers=None, opener=None, method=None):
        req = urllib.request.Request(self.base + path, data=data, headers=headers or {}, method=method)
        try:
            with (opener or urllib.request.build_opener(NoRedirect)).open(req, timeout=30) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def say(self, to, text, kind="msg"):
        return crewchat.post_json(self.base + "/api/send", self.owner, {"to": to, "text": text, "kind": kind})["id"]

    def names(self):
        return [row["agent"] for row in self.hub.rows()]

    def owner_browser(self):
        code = crewchat.post_json(self.base + "/api/login-code", self.owner, {})["code"]
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect)
        status, _, headers = self.raw("/login?code=" + code, opener=opener)
        self.assertEqual(status, 303)
        return opener, headers["Set-Cookie"]


PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
                    "0000000c4944415408d763f8cfc000000301010018dd8db00000000049454e44ae426082")


CLAUDE_LINKS = ("documentationUrl", "supportUrl", "privacyPolicyUrl")


def eventually(check, timeout=10.0, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError("timed out waiting for " + what)


# One home folder for every test module, removed once they have all run.
atexit.register(shutil.rmtree, TMP, ignore_errors=True)


CLIENT_JSON = json.dumps({"installed": {"client_id": "123-abc.apps.googleusercontent.com",
                                        "client_secret": "GOCSPX-" + "s" * 28,
                                        "redirect_uris": ["http://localhost"]}})
HOOK = ("https://chat.googleapis.com/v1/spaces/AAAAhook/messages?key=AIza" + "k" * 35
        + "&token=" + "t" * 43)


GIT_PEOPLE = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


class ProxyHarness:
    """A proxy (PROXY, crewchat's class) between a test and a fake MCP child (FAKE, whose server is called NAME), with a
    stand-in for the crewchat server it asks."""

    class Server:  # what the proxy asks, recorded; `deny` refuses calls, `down` is a server that is not running
        def __init__(self):
            self.calls, self.seen_tabs, self.deny, self.down, self.gate, self.broken = [], [], "", False, None, False
            self.about = []

        def call(self, tool, args, **about):
            self.about.append(about)
            if self.down:
                raise OSError("connection refused")
            if self.broken:
                raise KeyError("proxy")  # an answer it could not read
            self.calls.append((tool, args, len(self.seen_tabs)))
            if self.gate:
                self.gate.wait(5)
            return {"decision": "deny", "reason": self.deny} if self.deny else {"decision": "allow"}

        def seen(self, tabs):
            self.seen_tabs.append(tabs)

    def start(self, env=None, child=None):
        self.server = self.Server()
        self.log = Path(tempfile.mkdtemp(prefix="proxy-", dir=TMP)) / "calls"
        env = dict(os.environ, FAKE_LOG=str(self.log), **(env or {}))
        to_proxy_r, self.to_proxy = os.pipe()
        from_proxy, from_proxy_w = os.pipe()
        self.inp = os.fdopen(self.to_proxy, "w", buffering=1)
        self.out = os.fdopen(from_proxy, "r")
        proxy = getattr(crewchat, self.PROXY)("gemini", child or [sys.executable, self.FAKE], self.server,
                                              os.fdopen(to_proxy_r, "r"), os.fdopen(from_proxy_w, "w", buffering=1),
                                              env=env)
        self.thread = threading.Thread(target=proxy.run, daemon=True)
        self.thread.start()
        self.addCleanup(self.out.close)
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.inp.close)  # first: the proxy stops, and stops its child
        self.send({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
        hello = self.read()
        if child is None:
            self.assertEqual(hello["result"]["serverInfo"]["name"], self.NAME)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.n = 0

    def send(self, msg):
        self.inp.write(json.dumps(msg) + "\n")

    def read(self):
        line = self.out.readline()
        self.assertTrue(line, "the proxy closed its output")
        return json.loads(line)

    def call(self, name, **args):
        self.n += 1
        self.send({"jsonrpc": "2.0", "id": self.n, "method": "tools/call", "params": {"name": name, "arguments": args}})
        return self.n

    def result(self, name, **args):
        mid = self.call(name, **args)
        msg = self.read()
        self.assertEqual(msg["id"], mid)
        return msg["result"]

    def child_calls(self):
        return self.log.read_text().split() if self.log.exists() else []
