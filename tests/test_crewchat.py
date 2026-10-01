"""End-to-end tests: a real server on a free port, a throwaway home folder, real HTTP."""
import contextlib
import http.cookiejar
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = tempfile.mkdtemp(prefix="crewchat-test-")
os.environ["CREWCHAT_HOME"] = str(Path(TMP) / "home")

import crewchat  # noqa: E402  (after CREWCHAT_HOME is set)

AGENTS = ["claude-a", "cursor-a", "claude-b", "cursor-b"]


def cli(*argv):
    """Run a crewchat command in-process; returns what it printed."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        crewchat.main(list(argv))
    return out.getvalue()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cli("setup", "--agents", ",".join(AGENTS), "--project", "Demo")
        cls.server = crewchat.make_server(0)
        port = cls.server.server_address[1]
        config = crewchat.load_config()
        config["port"] = port
        crewchat.save_config(config)
        cls.base = "http://127.0.0.1:%d" % port
        cls.mcp = cls.base + "/mcp"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.tok = {a: crewchat.read_token(a) for a in AGENTS + ["Owner"]}

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        for name in ("messages.jsonl", "state.json"):
            with contextlib.suppress(OSError):
                (crewchat.home() / name).unlink()

    def call(self, agent, name, **arguments):
        return crewchat.rpc(self.mcp, self.tok[agent], "tools/call", {"name": name, "arguments": arguments})["result"]

    def text(self, agent, name, **arguments):
        return self.call(agent, name, **arguments)["content"][0]["text"]

    def drain(self):
        for agent in AGENTS:
            self.call(agent, "hub_inbox")

    def raw(self, path, data=None, headers=None, opener=None):
        req = urllib.request.Request(self.base + path, data=data, headers=headers or {})
        try:
            with (opener or urllib.request.build_opener(NoRedirect)).open(req, timeout=30) as resp:
                return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def owner_browser(self):
        code = crewchat.post_json(self.base + "/api/login-code", self.tok["Owner"], {})["code"]
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect)
        status, _, headers = self.raw("/login?code=" + code, opener=opener)
        self.assertEqual(status, 303)
        return opener, headers["Set-Cookie"]


class Protocol(Base):
    def test_health_and_auth(self):
        self.assertEqual(self.raw("/health")[1], b"ok\n")
        ping = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
        self.assertEqual(self.raw("/mcp", ping)[0], 401)
        self.assertEqual(self.raw("/mcp", ping, {"Authorization": "Bearer nope", "X-Forwarded-For": "9.9.9.1"})[0], 401)
        self.assertEqual(self.raw("/mcp", ping, {"Authorization": "Bearer " + self.tok["claude-a"]})[0], 200)
        self.assertEqual(self.raw("/mcp")[0], 405)

    def test_initialize_tells_the_agent_who_it_is(self):
        result = crewchat.rpc(self.mcp, self.tok["cursor-b"], "initialize",
                              {"protocolVersion": "2025-03-26", "capabilities": {}})["result"]
        self.assertEqual(result["protocolVersion"], "2025-03-26")
        self.assertEqual(result["capabilities"], {"tools": {}})
        self.assertIn("You are cursor-b on the crewchat for Demo", result["instructions"])
        self.assertIn("hub_take", result["instructions"])

    def test_tools_list_names_the_roster(self):
        tools = crewchat.rpc(self.mcp, self.tok["claude-a"], "tools/list")["result"]["tools"]
        self.assertEqual([t["name"] for t in tools],
                         ["hub_send", "hub_inbox", "hub_take", "hub_agents", "hub_status", "hub_history"])
        self.assertEqual(tools[0]["inputSchema"]["properties"]["to"]["enum"], AGENTS + ["Owner", "all"])

    def test_json_rpc_edges(self):
        auth = {"Authorization": "Bearer " + self.tok["claude-a"]}
        self.assertEqual(self.raw("/mcp", b'{"jsonrpc":"2.0","method":"notifications/initialized"}', auth)[0], 202)
        self.assertEqual(self.raw("/mcp", b"{bad", auth)[0], 400)
        self.assertIn("error", crewchat.rpc(self.mcp, self.tok["claude-a"], "no/such"))
        status, body, _ = self.raw("/mcp", b'[{"jsonrpc":"2.0","id":7,"method":"ping"},{"jsonrpc":"2.0","method":"n"}]', auth)
        self.assertEqual((status, json.loads(body)), (200, [{"jsonrpc": "2.0", "id": 7, "result": {}}]))
        self.assertEqual(self.raw("/mcp", b"x" * (300 * 1024), auth)[0], 413)

    def test_bad_tokens_lock_an_address_out(self):
        ping = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
        for i in range(crewchat.FAIL_LIMIT):
            self.raw("/mcp", ping, {"Authorization": "Bearer bad%d" % i, "X-Forwarded-For": "6.6.6.6"})
        good = {"Authorization": "Bearer " + self.tok["claude-a"]}
        self.assertEqual(self.raw("/mcp", ping, dict(good, **{"X-Forwarded-For": "6.6.6.6"}))[0], 429)
        self.assertEqual(self.raw("/mcp", ping, good)[0], 200)


class Messaging(Base):
    def setUp(self):
        self.drain()

    def test_direct_message_reaches_only_its_recipient_once(self):
        self.assertTrue(self.text("claude-a", "hub_send", to="cursor-b", text='hi "there" → ok').startswith("Sent #"))
        self.assertEqual(self.text("claude-a", "hub_inbox"), "No new messages.")
        self.assertEqual(self.text("claude-b", "hub_inbox"), "No new messages.")
        peek = self.text("cursor-b", "hub_inbox", peek=True)
        self.assertIn('claude-a -> you: hi "there" → ok', peek)
        self.assertIn("not instructions", peek)
        self.assertIn("claude-a -> you", self.text("cursor-b", "hub_inbox"))
        self.assertEqual(self.text("cursor-b", "hub_inbox"), "No new messages.")

    def test_broadcast_reaches_everyone_but_the_sender(self):
        self.call("cursor-b", "hub_send", to="all", text="heads up")
        self.assertIn("cursor-b -> all: heads up", self.text("claude-a", "hub_inbox"))
        self.assertEqual(self.text("cursor-b", "hub_inbox"), "No new messages.")

    def test_bad_arguments_are_refused(self):
        for bad in (dict(to="claude-a", text="x"), dict(to="nobody", text="x"), dict(to="all", text=" "),
                    dict(to="all", text="x" * 4001)):
            self.assertTrue(self.call("claude-a", "hub_send", **bad)["isError"], bad)
        self.assertTrue(self.call("claude-a", "hub_inbox", wait_seconds=999)["isError"])
        self.assertTrue(self.call("claude-a", "no_such_tool")["isError"])

    def test_waiting_inbox_returns_when_a_message_arrives(self):
        got = {}
        thread = threading.Thread(target=lambda: got.update(t=self.text("claude-b", "hub_inbox", wait_seconds=10)))
        start = time.time()
        thread.start()
        time.sleep(0.5)
        self.call("claude-a", "hub_send", to="claude-b", text="wake up")
        thread.join()
        self.assertIn("wake up", got["t"])
        self.assertLess(time.time() - start, 4)

    def test_ack_marks_read_and_peek_does_not(self):
        self.call("claude-a", "hub_send", to="cursor-a", text="one")
        first = self.text("cursor-a", "hub_inbox", peek=True)
        number = int(first.split("[#")[1].split(" ")[0])
        self.assertIn("one", self.text("cursor-a", "hub_inbox", peek=True))
        self.assertEqual(self.text("cursor-a", "hub_inbox", peek=True, ack=number), "No new messages.")

    def test_status_and_agents(self):
        self.call("cursor-a", "hub_status", text="building  the\n thing")
        listing = self.text("claude-a", "hub_agents")
        self.assertIn("status: building the thing", listing)
        self.assertIn("claude-a (you)", listing)
        self.assertNotIn("Owner", listing)


class Tasks(Base):
    def setUp(self):
        self.drain()

    def post(self, to, text, kind="task"):
        return crewchat.post_json(self.base + "/api/send", self.tok["Owner"], {"to": to, "text": text, "kind": kind})["id"]

    def test_exactly_one_agent_wins_a_race_for_a_task(self):
        task = self.post("all", "Add a dark icon")
        self.assertIn("Owner -> all: [TASK, open] Add a dark icon", self.text("claude-b", "hub_inbox"))
        results = {}
        threads = [threading.Thread(target=lambda a=a: results.update({a: self.call(a, "hub_take", id=task)}))
                   for a in AGENTS]
        [t.start() for t in threads]
        [t.join() for t in threads]
        winners = [a for a, r in results.items() if not r["isError"]]
        self.assertEqual(len(winners), 1)
        for agent, result in results.items():
            if agent != winners[0]:
                self.assertIn("already taken by " + winners[0], result["content"][0]["text"])
        self.assertFalse(self.call(winners[0], "hub_take", id=task)["isError"])
        other = next(a for a in AGENTS if a != winners[0])
        self.assertIn("%s -> all: I am taking task #%d" % (winners[0], task), self.text(other, "hub_history"))
        self.assertIn("[TASK, taken by %s]" % winners[0], self.text(other, "hub_history"))

    def test_a_task_for_one_agent_is_only_that_agents(self):
        task = self.post("cursor-a", "Only for you")
        self.assertIn("was given to cursor-a", self.text("claude-a", "hub_take", id=task))
        self.assertFalse(self.call("cursor-a", "hub_take", id=task)["isError"])

    def test_only_tasks_can_be_taken(self):
        message = self.post("all", "just a note", kind="msg")
        self.assertTrue(self.call("claude-a", "hub_take", id=message)["isError"])
        self.assertTrue(self.call("claude-a", "hub_take", id=999999)["isError"])

    def test_agents_cannot_post_as_owner(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            crewchat.post_json(self.base + "/api/send", self.tok["claude-a"], {"to": "all", "text": "x"})
        self.assertEqual(ctx.exception.code, 403)

    def test_say_command(self):
        self.assertIn("Posted task #", cli("say", "--task", "--to", "claude-a", "Check", "the", "build"))
        self.assertIn("Owner -> you: [TASK, open] Check the build", self.text("claude-a", "hub_inbox"))
        self.assertIn("Posted message #", cli("say", "plain note"))


class ChatPage(Base):
    def test_pages_need_the_owner(self):
        status, _, headers = self.raw("/")
        self.assertEqual((status, headers["Location"]), (303, "/login"))
        self.assertEqual(self.raw("/api/poll")[0], 401)
        self.assertEqual(self.raw("/api/send", b"{}", {"Content-Type": "application/json"})[0], 401)
        status, body, headers = self.raw("/login")
        self.assertEqual(status, 200)
        self.assertIn(b"Sign in", body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Referrer-Policy"], "same-origin")

    def test_sign_in_codes(self):
        agent = {"Authorization": "Bearer " + self.tok["claude-a"]}
        self.assertEqual(self.raw("/api/login-code", b"{}", agent)[0], 403)
        self.assertEqual(self.raw("/login?code=WRONGCOD", headers={"X-Forwarded-For": "7.7.7.1"})[0], 401)
        code = crewchat.post_json(self.base + "/api/login-code", self.tok["Owner"], {})["code"]
        typed = ("code=%s-%s" % (code[:4].lower(), code[4:])).encode()
        form = {"Content-Type": "application/x-www-form-urlencoded"}
        # Browsers send "Origin: null" when a page's referrer policy is no-referrer: must be refused.
        self.assertEqual(self.raw("/login", typed, dict(form, Origin="null"))[0], 403)
        status, _, headers = self.raw("/login", typed, dict(form, Origin=self.base))
        self.assertEqual(status, 303)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertEqual(self.raw("/login?code=" + code, headers={"X-Forwarded-For": "7.7.7.2"})[0], 401)

    def test_chat_page_and_api(self):
        opener, _ = self.owner_browser()
        status, body, headers = self.raw("/", opener=opener)
        self.assertEqual(status, 200)
        self.assertIn(b"<title>crewchat</title>", body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        send = lambda obj, extra=None: self.raw(  # noqa: E731
            "/api/send", json.dumps(obj).encode(), dict({"Content-Type": "application/json"}, **(extra or {})), opener)
        self.assertEqual(send({"to": "all", "text": "x"}, {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(send({"to": "all", "text": "hello agents"}, {"Origin": self.base})[0], 200)
        for bad in ({"to": "Owner", "text": "x"}, {"to": "all", "text": " "}, {"to": "all"}):
            self.assertEqual(send(bad)[0], 400, bad)
        data = json.loads(self.raw("/api/poll?after=0&v=-1&wait=0", opener=opener)[1])
        self.assertEqual(data["project"], "Demo")
        self.assertEqual([a["agent"] for a in data["agents"]], AGENTS)
        self.assertEqual(data["messages"][-1]["text"], "hello agents")
        got = {}
        path = "/api/poll?after=%d&v=%d&wait=20" % (data["messages"][-1]["id"], data["version"])
        thread = threading.Thread(target=lambda: got.update(d=json.loads(self.raw(path, opener=opener)[1])))
        start = time.time()
        thread.start()
        time.sleep(0.5)
        self.call("cursor-b", "hub_send", to="Owner", text="a question")
        thread.join()
        self.assertLess(time.time() - start, 4)
        self.assertEqual(got["d"]["messages"][0]["text"], "a question")

    def test_ui_print_gives_a_code(self):
        self.assertRegex(cli("ui", "--print"), r"Sign-in code .*: [A-Z2-9]{4}-[A-Z2-9]{4}")


class Joining(Base):
    def project(self):
        folder = Path(tempfile.mkdtemp(prefix="project-", dir=TMP))
        subprocess.run(["git", "init", "-q", str(folder)], check=False)
        return folder

    def invite(self, agent, client):
        out = cli("invite", agent, "--client", client, "--local")
        line = next(l.strip() for l in out.splitlines() if l.strip().startswith("crewchat join"))
        return line.split()[1:]  # the join arguments

    def test_claude_join_writes_connection_and_hooks(self):
        folder = self.project()
        (folder / ".claude").mkdir()
        (folder / ".claude" / "settings.local.json").write_text(json.dumps(
            {"permissions": {"allow": ["Bash(ls:*)"]},
             "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}))
        args = self.invite("claude-a", "claude")
        out = cli(*args, "--project", str(folder))
        self.assertIn("claude-a joined the crewchat for Demo", out)
        mcp = json.loads((folder / ".mcp.json").read_text())["mcpServers"]["crewchat"]
        self.assertEqual(mcp["url"], self.mcp)
        self.assertEqual(mcp["headers"]["Authorization"], "Bearer " + self.tok["claude-a"])
        settings = json.loads((folder / ".claude" / "settings.local.json").read_text())
        self.assertEqual(settings["permissions"], {"allow": ["Bash(ls:*)"]})
        self.assertIn("crewchat", settings["enabledMcpjsonServers"])
        stop = [h["command"] for g in settings["hooks"]["Stop"] for h in g["hooks"]]
        self.assertEqual(stop[0], "echo mine")
        self.assertTrue(stop[1].endswith(" hook claude stop") and "crewchat.py" in stop[1])
        self.assertEqual(len(settings["hooks"]["UserPromptSubmit"]), 1)
        exclude = (folder / ".git" / "info" / "exclude").read_text()
        self.assertIn(".mcp.json", exclude)
        self.assertNotIn("do not commit", out)
        # Joining again replaces our hooks instead of stacking them.
        cli(*self.invite("claude-a", "claude"), "--project", str(folder))
        settings = json.loads((folder / ".claude" / "settings.local.json").read_text())
        self.assertEqual(len(settings["hooks"]["Stop"]), 2)
        self.assertEqual(settings["enabledMcpjsonServers"].count("crewchat"), 1)

    def test_cursor_join(self):
        folder = self.project()
        cli(*self.invite("cursor-a", "cursor"), "--project", str(folder))
        mcp = json.loads((folder / ".cursor" / "mcp.json").read_text())["mcpServers"]["crewchat"]
        self.assertEqual(mcp["headers"]["Authorization"], "Bearer " + self.tok["cursor-a"])
        hooks = json.loads((folder / ".cursor" / "hooks.json").read_text())
        self.assertEqual(hooks["version"], 1)
        self.assertTrue(hooks["hooks"]["stop"][0]["command"].endswith(" hook cursor stop"))

    def test_generic_join_prints_settings(self):
        out = cli(*self.invite("claude-b", "generic"))
        self.assertIn('"url": "%s"' % self.mcp, out)
        self.assertIn(self.tok["claude-b"], out)

    def test_a_join_code_works_once_and_wrong_codes_fail(self):
        args = self.invite("cursor-b", "generic")
        cli(*args)
        with self.assertRaises(SystemExit):
            cli(*args)
        with self.assertRaises(SystemExit):
            cli("join", "--url", self.base, "--code", "AAAA-AAAA", "--client", "generic")
        with self.assertRaises(SystemExit):
            cli("invite", "nobody")
        agent = {"Authorization": "Bearer " + self.tok["claude-a"], "Content-Type": "application/json"}
        self.assertEqual(self.raw("/api/invite", b'{"agent":"claude-a"}', agent)[0], 403)

    def test_agents_can_be_added_and_removed_while_running(self):
        cli("agent", "add", "late-joiner")
        token = crewchat.read_token("late-joiner")
        self.assertIn("late-joiner (you)", crewchat.rpc(
            self.mcp, token, "tools/call", {"name": "hub_agents", "arguments": {}})["result"]["content"][0]["text"])
        self.assertIn("late-joiner", cli("agent", "list"))
        cli("agent", "remove", "late-joiner")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            crewchat.rpc(self.mcp, token, "ping")
        self.assertEqual(ctx.exception.code, 401)
        for bad in ("Owner", "all", "has space", "9lives", ""):
            with self.assertRaises(SystemExit, msg=bad):
                cli("agent", "add", bad)


class Hooks(Base):
    """The hook command exactly as Claude Code and Cursor run it: a subprocess fed JSON on stdin."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.folder = Path(tempfile.mkdtemp(prefix="hooks-", dir=TMP))
        crewchat.install_claude(cls.folder, cls.base, cls.tok["claude-a"])
        crewchat.install_cursor(cls.folder, cls.base, cls.tok["cursor-a"])
        cls.state = tempfile.mkdtemp(prefix="hookstate-", dir=TMP)

    def hook(self, client, event, payload, project=None, listen="0"):
        env = dict(os.environ, CREWCHAT_PROJECT=str(project or self.folder), CREWCHAT_LISTEN=listen,
                   TMPDIR=self.state, TEMP=self.state, TMP=self.state)
        env.pop("CLAUDE_PROJECT_DIR", None)
        out = subprocess.run([sys.executable, str(ROOT / "crewchat.py"), "hook", client, event],
                             input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout

    def unread(self, agent):
        line = next(l for l in self.text("claude-b", "hub_agents").splitlines() if l.startswith(agent))
        return int(line.split("unread ")[1].split(";")[0])

    def setUp(self):
        self.drain()
        shutil.rmtree(self.state, ignore_errors=True)
        os.makedirs(self.state)

    def test_an_interrupted_turn_gets_the_message_again(self):
        cli("say", "--to", "claude-a", "m1")
        blocked = json.loads(self.hook("claude", "stop", {"session_id": "A"}))
        self.assertEqual(blocked["decision"], "block")
        self.assertIn("Owner -> you: m1", blocked["reason"])
        self.assertEqual(self.unread("claude-a"), 1)  # not confirmed yet
        # Session A dies. A new session's prompt hook delivers it again.
        again = self.hook("claude", "prompt", {"session_id": "B"})
        self.assertIn("m1", again)
        self.assertEqual(self.unread("claude-a"), 1)
        # B's next hook call proves it had a turn: confirmed, nothing new.
        self.assertEqual(self.hook("claude", "stop", {"session_id": "B"}), "")
        self.assertEqual(self.unread("claude-a"), 0)

    def test_no_duplicates_in_a_healthy_session(self):
        cli("say", "--to", "claude-a", "m2")
        self.assertIn("m2", self.hook("claude", "stop", {"session_id": "C"}))
        self.assertEqual(self.hook("claude", "stop", {"session_id": "C"}), "")
        cli("say", "--to", "claude-a", "m3")
        self.assertIn("m3", self.hook("claude", "prompt", {"session_id": "C"}))
        cli("say", "--to", "claude-a", "m4")
        out = self.hook("claude", "stop", {"session_id": "C"})
        self.assertIn("m4", out)
        self.assertNotIn("m3", out)

    def test_loop_guard_holds_messages_for_the_user(self):
        delivered = 0
        for i in range(crewchat.MAX_CHAIN + 2):
            cli("say", "--to", "claude-a", "loop %d" % i)
            delivered += bool(self.hook("claude", "stop", {"session_id": "D"}))
        self.assertEqual(delivered, crewchat.MAX_CHAIN)
        self.assertEqual(self.unread("claude-a"), 2)
        shown = self.hook("claude", "prompt", {"session_id": "D"})
        self.assertIn("loop %d" % (crewchat.MAX_CHAIN + 1), shown)
        cli("say", "--to", "claude-a", "after the prompt")
        self.assertIn("after the prompt", self.hook("claude", "stop", {"session_id": "D"}))

    def test_cursor_gets_a_followup_message(self):
        cli("say", "--to", "cursor-a", "for cursor")
        out = json.loads(self.hook("cursor", "stop", {"status": "completed", "loop_count": 0, "conversation_id": "c"}))
        self.assertIn("for cursor", out["followup_message"])
        cli("say", "--to", "cursor-a", "later")
        self.assertEqual(self.hook("cursor", "stop", {"status": "aborted", "loop_count": 0, "conversation_id": "c"}), "")
        self.assertEqual(self.hook("cursor", "stop", {"status": "completed", "loop_count": crewchat.MAX_CHAIN,
                                                     "conversation_id": "c"}), "")

    def test_listening_wakes_when_a_message_arrives(self):
        timer = threading.Timer(1.0, lambda: cli("say", "--to", "claude-a", "wake"))
        start = time.time()
        timer.start()
        out = self.hook("claude", "stop", {"session_id": "E"}, listen="20")
        timer.join()
        self.assertIn("wake", out)
        self.assertLess(time.time() - start, 8)

    def test_hooks_are_silent_when_not_connected_or_unreachable(self):
        empty = Path(tempfile.mkdtemp(prefix="empty-", dir=TMP))
        self.assertEqual(self.hook("claude", "stop", {}, project=empty), "")
        dead = Path(tempfile.mkdtemp(prefix="dead-", dir=TMP))
        crewchat.install_claude(dead, "http://127.0.0.1:1", "x")
        self.assertEqual(self.hook("claude", "stop", {}, project=dead), "")
        self.assertEqual(self.hook("claude", "stop", "not json at all"), "")

    def test_listen_command(self):
        folder = Path(tempfile.mkdtemp(prefix="listen-", dir=TMP))
        self.assertIn("off", cli("listen", "status", "--project", str(folder)))
        cli("listen", "on", "--minutes", "999", "--project", str(folder))
        self.assertEqual((folder / ".crewchat-listen").read_text().strip(), str(crewchat.MAX_LISTEN))
        cli("listen", "off", "--project", str(folder))
        self.assertFalse((folder / ".crewchat-listen").exists())


class Persistence(unittest.TestCase):
    def test_messages_read_state_and_task_owners_survive_a_restart(self):
        roster = crewchat.Roster()
        hub = crewchat.Hub(roster)
        task = hub.send("Owner", "all", "persist me", "task")
        hub.take(AGENTS[0], task["id"])
        hub.inbox(AGENTS[1], 0, False)
        hub.set_status(AGENTS[1], "busy")
        again = crewchat.Hub(roster)
        self.assertEqual(again.taken[str(task["id"])], AGENTS[0])
        self.assertEqual(again.status[AGENTS[1]], "busy")
        self.assertEqual(again._unread(AGENTS[1]), [])
        self.assertEqual(again.send("Owner", "all", "next")["id"], hub.next_id)


class Service(unittest.TestCase):
    def test_service_files_run_this_program(self):
        plist = crewchat.launchd_plist(keep_awake=True)
        self.assertIn("<string>/usr/bin/caffeinate</string>", plist)
        self.assertIn("crewchat.py</string>", plist)
        self.assertIn("<string>serve</string>", plist)
        self.assertIn("CREWCHAT_HOME", plist)
        self.assertNotIn("caffeinate", crewchat.launchd_plist(keep_awake=False))
        unit = crewchat.systemd_unit()
        self.assertIn('crewchat.py" serve', unit)
        self.assertIn("Restart=always", unit)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
