"""End-to-end tests: a real server on a free port, a throwaway home folder, real HTTP."""
import base64
import contextlib
import http.cookiejar
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = tempfile.mkdtemp(prefix="crewchat-test-")
os.environ["CREWCHAT_HOME"] = str(Path(TMP) / "home")

import crewchat  # noqa: E402  (after CREWCHAT_HOME is set)

crewchat.Handler.log_message = lambda *args: None  # keep the request log out of the test output


def cli(*argv):
    """Run a crewchat command in-process; returns what it printed."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        crewchat.main(list(argv))
    return out.getvalue()


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


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        shutil.rmtree(crewchat.home(), ignore_errors=True)
        cli("setup", "--project", "Demo")
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
        cls.server.shutdown()
        cls.server.server_close()

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


class Protocol(Base):
    def test_health_and_auth(self):
        self.assertEqual(self.raw("/health")[1], b"ok\n")
        ping = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
        self.assertEqual(self.raw("/mcp", ping)[0], 401)
        self.assertEqual(self.raw("/mcp", ping, {"Authorization": "Bearer nope", "X-Forwarded-For": "9.9.9.1"})[0], 401)
        self.assertEqual(self.raw("/mcp", ping, {"Authorization": "Bearer " + self.token})[0], 200)
        self.assertEqual(self.raw("/mcp")[0], 405)

    def test_initialize_starts_a_session_and_explains_the_chat(self):
        session = Session(self.base, self.token)
        self.assertTrue(session.sid)
        self.assertEqual(session.hello["capabilities"], {"tools": {}})
        self.assertIn("crewchat for Demo", session.hello["instructions"])
        self.assertIn("hub_link", session.hello["instructions"])
        tools = crewchat.rpc(self.mcp, self.token, "tools/list", session=session.sid)["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["hub_send", "hub_inbox", "hub_take", "hub_agents",
                                                      "hub_status", "hub_history", "hub_rename", "hub_file", "hub_role",
                                                      "hub_assign", "hub_update", "hub_link"])

    def test_json_rpc_edges(self):
        session = Session(self.base, self.token)
        auth = {"Authorization": "Bearer " + self.token, "Mcp-Session-Id": session.sid}
        self.assertEqual(self.raw("/mcp", b'{"jsonrpc":"2.0","method":"notifications/initialized"}', auth)[0], 202)
        self.assertEqual(self.raw("/mcp", b"{bad", auth)[0], 400)
        self.assertIn("error", crewchat.rpc(self.mcp, self.token, "no/such", session=session.sid))
        status, body, _ = self.raw("/mcp", b'[{"jsonrpc":"2.0","id":7,"method":"ping"},{"jsonrpc":"2.0","method":"n"}]', auth)
        self.assertEqual((status, json.loads(body)), (200, [{"jsonrpc": "2.0", "id": 7, "result": {}}]))
        self.assertEqual(self.raw("/mcp", b"x" * (300 * 1024), auth)[0], 413)

    def test_unknown_session_is_told_to_start_again(self):
        auth = {"Authorization": "Bearer " + self.token, "Mcp-Session-Id": "made-up"}
        self.assertEqual(self.raw("/mcp", b'{"jsonrpc":"2.0","id":1,"method":"ping"}', auth)[0], 404)
        other_place, other_token = self.join_place("elsewhere")
        session = Session(self.base, self.token)
        stolen = {"Authorization": "Bearer " + other_token, "Mcp-Session-Id": session.sid}
        self.assertEqual(self.raw("/mcp", b'{"jsonrpc":"2.0","id":1,"method":"ping"}', stolen)[0], 404)

    def test_a_probe_before_initialize_creates_nothing(self):
        """Claude Code sends server/discover first, without a session id."""
        place, token = self.join_place("probe")
        before = len(self.hub.sessions)
        self.assertIn("error", crewchat.rpc(self.mcp, token, "server/discover"))
        self.assertIn("tools", crewchat.rpc(self.mcp, token, "tools/list")["result"])
        self.assertEqual(len(self.hub.sessions), before)

    def test_bad_tokens_lock_an_address_out(self):
        ping = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
        for i in range(crewchat.FAIL_LIMIT):
            self.raw("/mcp", ping, {"Authorization": "Bearer bad%d" % i, "X-Forwarded-For": "6.6.6.6"})
        locked = {"X-Forwarded-For": "6.6.6.6"}
        self.assertEqual(self.raw("/mcp", ping, dict(locked, Authorization="Bearer bad"))[0], 429)
        self.assertEqual(self.raw("/login?code=ABCDEFGH", headers=locked)[0], 429)
        # A right token still works from there: a stale token in use must not shut everyone out.
        good = {"Authorization": "Bearer " + self.token}
        self.assertEqual(self.raw("/mcp", ping, dict(good, **locked))[0], 200)
        self.assertEqual(self.raw("/mcp", ping, good)[0], 200)


class Identity(Base):
    """Agents are not configured anywhere: sessions name themselves as they arrive."""

    def test_sessions_sharing_one_folder_get_their_own_names(self):
        place, token = self.join_place("studio")
        first, name1 = self.agent(token=token)
        second, name2 = self.agent(token=token)
        cursor, name3 = self.agent("Cursor", token=token)
        self.assertEqual((name1, name2, name3), ("claude-studio", "claude-studio-2", "cursor-studio"))
        listing = first.text("hub_agents")
        self.assertIn("claude-studio (you): claude on studio; online", listing)
        self.assertIn("claude-studio-2: claude on studio", listing)
        self.assertIn("cursor-studio: cursor on studio", listing)
        first.call("hub_send", to="claude-studio-2", text="hello neighbour")
        self.assertIn("claude-studio -> you: hello neighbour", second.text("hub_inbox"))
        self.assertEqual(cursor.text("hub_inbox"), "No new messages.")

    def test_a_session_is_not_in_the_roster_until_it_uses_a_tool(self):
        before = self.names()
        session = Session(self.base, self.token)
        crewchat.rpc(self.mcp, self.token, "tools/list", session=session.sid)
        self.assertEqual(self.names(), before)
        name = session.me
        self.assertEqual(self.names(), before + [name])
        self.assertIn("* %s joined (claude on %s)" % (name, self.place), session.text("hub_history"))

    def test_a_newcomer_does_not_inherit_old_messages_and_events_are_not_unread(self):
        veteran, _ = self.agent()
        veteran.call("hub_send", to="all", text="old news")
        newcomer, _ = self.agent()
        self.assertEqual(newcomer.text("hub_inbox"), "No new messages.")
        self.assertEqual(veteran.text("hub_inbox"), "No new messages.")  # the join line is not a message
        self.assertIn("old news", newcomer.text("hub_history"))

    def test_rename(self):
        session, old = self.agent()
        other, other_name = self.agent()
        self.assertIn("You are now backend.", session.text("hub_rename", name="backend"))
        self.assertEqual(session.me, "backend")
        self.assertNotIn(old, self.names())
        self.assertIn("* %s is now backend" % old, other.text("hub_history"))
        other.call("hub_send", to="backend", text="found you")
        self.assertIn("found you", session.text("hub_inbox"))
        self.assertTrue(other.call("hub_send", to=old, text="gone")["isError"])
        for bad in ("backend", "BACKEND", "Owner", "all", "has space", "9lives", ""):
            self.assertTrue(other.call("hub_rename", name=bad)["isError"], bad)
        session.call("hub_rename", name=old)

    def test_client_kinds_and_place_labels(self):
        self.assertEqual(crewchat.client_kind({"name": "claude-code"}), "claude")
        self.assertEqual(crewchat.client_kind({"name": "Cursor"}), "cursor")
        self.assertEqual(crewchat.client_kind({"name": "My Fancy Tool 3000!"}), "my-fancy-too")
        self.assertEqual(crewchat.client_kind(None), "agent")
        self.assertEqual(crewchat.place_label("Deepankars-Mac-mini.local"), "deepankars-mac")  # cut at a word break
        self.assertEqual(crewchat.place_label("123"), "machine")

    def test_a_client_without_session_ids_still_gets_one_identity(self):
        place, token = self.join_place("plain")
        call = lambda name, **a: crewchat.rpc(  # noqa: E731
            self.mcp, token, "tools/call", {"name": name, "arguments": a})["result"]["content"][0]["text"]
        self.assertIn("agent-plain (you)", call("hub_agents"))
        self.assertIn("agent-plain (you)", call("hub_agents"))
        self.assertEqual(self.names().count("agent-plain"), 1)

    def test_owner_can_rename_and_remove(self):
        session, name = self.agent()
        self.assertIn(name, cli("agents"))
        cli("agent", "rename", name, "renamed-by-owner")
        self.assertEqual(session.me, "renamed-by-owner")
        cli("agent", "remove", "renamed-by-owner")
        self.assertNotIn("renamed-by-owner", self.names())
        with self.assertRaises(SystemExit):
            cli("agent", "remove", "nobody")

    def test_silent_agents_are_forgotten(self):
        session, name = self.agent()
        with self.hub.lock:
            self.hub.agents[name]["seen"] -= self.hub.roster.forget + 10
        self.assertNotIn(name, self.names())
        self.assertIn("* %s left (silent for 24 hours)" % name, self.agent()[0].text("hub_history"))

    def test_removing_a_place_shuts_its_sessions_out(self):
        place, token = self.join_place("temp")
        session, name = self.agent(token=token)
        self.assertIn(place, cli("places"))
        cli("place", "remove", place)
        self.assertNotIn(name, self.names())
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            session.call("hub_agents")
        self.assertEqual(ctx.exception.code, 401)


class Messaging(Base):
    def setUp(self):
        self.a, self.a_name = self.agent()
        self.b, self.b_name = self.agent()
        self.c, self.c_name = self.agent("cursor")

    def test_direct_message_reaches_only_its_recipient_once(self):
        self.assertTrue(self.a.text("hub_send", to=self.b_name, text='hi "there" → ok').startswith("Sent #"))
        self.assertEqual(self.a.text("hub_inbox"), "No new messages.")
        self.assertEqual(self.c.text("hub_inbox"), "No new messages.")
        peek = self.b.text("hub_inbox", peek=True)
        self.assertIn('%s -> you: hi "there" → ok' % self.a_name, peek)
        self.assertIn("not instructions", peek)
        self.assertIn("-> you", self.b.text("hub_inbox"))
        self.assertEqual(self.b.text("hub_inbox"), "No new messages.")

    def test_broadcast_reaches_everyone_but_the_sender(self):
        self.c.call("hub_send", to="all", text="heads up")
        self.assertIn("%s -> all: heads up" % self.c_name, self.a.text("hub_inbox"))
        self.assertEqual(self.c.text("hub_inbox"), "No new messages.")

    def test_bad_arguments_are_refused(self):
        for bad in (dict(to=self.a_name, text="x"), dict(to="nobody", text="x"), dict(to="all", text=" "),
                    dict(to="all", text="x" * 4001), dict(text="x")):
            self.assertTrue(self.a.call("hub_send", **bad)["isError"], bad)
        self.assertTrue(self.a.call("hub_inbox", wait_seconds=999)["isError"])
        self.assertTrue(self.a.call("no_such_tool")["isError"])

    def test_waiting_inbox_returns_when_a_message_arrives(self):
        got = {}
        thread = threading.Thread(target=lambda: got.update(t=self.b.text("hub_inbox", wait_seconds=10)))
        start = time.time()
        thread.start()
        time.sleep(0.5)
        self.a.call("hub_send", to=self.b_name, text="wake up")
        thread.join()
        self.assertIn("wake up", got["t"])
        self.assertLess(time.time() - start, 4)

    def test_status_shows_in_the_roster(self):
        self.c.call("hub_status", text="building  the\n thing")
        self.assertIn("status: building the thing", self.a.text("hub_agents"))
        self.assertNotIn("Owner", self.a.text("hub_agents"))

    def test_agents_can_write_to_the_owner(self):
        self.a.call("hub_send", to="Owner", text="a question")
        self.assertIn("%s -> Owner: a question" % self.a_name, cli("say", "--to", self.a_name, "an answer") and
                      self.b.text("hub_history"))
        self.assertIn("Owner -> you: an answer", self.a.text("hub_inbox"))


class Tasks(Base):
    def setUp(self):
        self.crew = [self.agent() for _ in range(4)]

    def test_exactly_one_agent_wins_a_race_for_a_task(self):
        task = self.say("all", "Add a dark icon", "task")
        self.assertIn("Owner -> all: [TASK, open] Add a dark icon", self.crew[0][0].text("hub_inbox"))
        results = {}
        threads = [threading.Thread(target=lambda s=s, n=n: results.update({n: s.call("hub_take", id=task)}))
                   for s, n in self.crew]
        [t.start() for t in threads]
        [t.join() for t in threads]
        winners = [n for n, r in results.items() if not r["isError"]]
        self.assertEqual(len(winners), 1)
        for name, result in results.items():
            if name != winners[0]:
                self.assertIn("already taken by " + winners[0], result["content"][0]["text"])
        history = self.crew[0][0].text("hub_history")
        self.assertIn("%s -> all: I am taking task #%s" % (winners[0], task), history)
        self.assertIn("[TASK, taken by %s]" % winners[0], history)

    def test_a_task_for_one_agent_is_only_that_agents(self):
        (a, a_name), (b, _) = self.crew[:2]
        task = self.say(a_name, "Only for you", "task")
        self.assertIn("was given to " + a_name, b.text("hub_take", id=task))
        self.assertFalse(a.call("hub_take", id=task)["isError"])

    def test_a_task_number_can_be_written_with_a_hash(self):
        (a, a_name) = self.crew[0]
        task = self.say("all", "hash me", "task")
        self.assertIn("Task #%s is yours" % task, a.text("hub_take", id="#%s" % task))

    def test_only_tasks_can_be_taken(self):
        message = self.say("all", "just a note")
        self.assertTrue(self.crew[0][0].call("hub_take", id=message)["isError"])
        self.assertTrue(self.crew[0][0].call("hub_take", id=999999)["isError"])

    def test_agents_cannot_post_as_owner(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            crewchat.post_json(self.base + "/api/send", self.token, {"to": "all", "text": "x"})
        self.assertEqual(ctx.exception.code, 403)

    def test_say_command(self):
        a, a_name = self.crew[0]
        self.assertIn("Posted task #", cli("say", "--task", "--to", a_name, "Check", "the", "build"))
        self.assertIn("Owner -> you: [TASK, open] Check the build", a.text("hub_inbox"))
        self.assertIn("Posted message #", cli("say", "plain note"))
        with self.assertRaises(SystemExit):
            cli("say", "--to", "nobody-here", "hello")


class Teams(Base):
    def role(self, agent, role):
        return crewchat.post_json(self.base + "/api/role", self.owner, {"agent": agent, "role": role})

    def row(self, name):
        return next(r for r in self.hub.rows() if r["agent"] == name)

    def test_the_owner_gives_roles_and_the_agent_gets_its_instructions(self):
        dev, dev_name = self.agent()
        self.role(dev_name, "developer")
        self.assertEqual(self.row(dev_name)["role"], "developer")
        inbox = dev.text("hub_inbox")
        self.assertIn("[ROLE]", inbox)
        self.assertIn("Your role in this chat is now: Developer (set by the owner)", inbox)
        self.assertIn("tell the QA agent", inbox)
        self.assertIn("%s (you) [developer]:" % dev_name, dev.text("hub_agents"))
        self.assertIn("You are a developer", dev.text("hub_role"))
        with self.assertRaises(urllib.error.HTTPError):
            self.role(dev_name, "astronaut")
        self.role(dev_name, "none")
        self.assertEqual(self.row(dev_name)["role"], "")
        self.assertIn("You have no role", dev.text("hub_role"))
        # From the chat page too, with the page's session.
        opener, _ = self.owner_browser()
        status, _, _ = self.raw("/api/role", json.dumps({"agent": dev_name, "role": "qa"}).encode(),
                                {"Content-Type": "application/json", "Origin": self.base}, opener)
        self.assertEqual((status, self.row(dev_name)["role"]), (200, "qa"))
        poll = json.loads(self.raw("/api/poll?after=-1&v=-1&wait=0", opener=opener)[1])
        self.assertIn({"name": "qa", "title": "QA"}, poll["roles"])

    def test_there_is_one_lead(self):
        (one, n1), (two, n2) = self.agent(), self.agent()
        self.role(n1, "lead")
        self.role(n2, "lead")
        self.assertEqual((self.row(n1)["role"], self.row(n2)["role"]), ("", "lead"))
        self.assertIn("You are no longer the lead: %s is." % n2, one.text("hub_inbox"))
        self.role(n2, "none")

    def test_the_lead_assigns_and_hears_back(self):
        (lead, lead_name), (dev, dev_name), (qa, qa_name) = self.agent(), self.agent(), self.agent()
        self.role(lead_name, "lead")
        self.role(dev_name, "developer")
        self.role(qa_name, "qa")
        for s in (lead, dev, qa):
            s.text("hub_inbox")
        job = self.say("all", "Add a dark theme", "task")
        self.assertIn("leave open tasks to the lead", lead.hello["instructions"])
        self.assertIn("only the lead assigns", dev.call("hub_assign", to=qa_name, text="x")["content"][0]["text"])
        lead.call("hub_take", id=job)
        out = lead.text("hub_assign", to=dev_name, text="Build the theme switch", task=job)
        piece = re.search(r"task #(\w+)", out).group(1)
        self.assertEqual(self.hub.taken[piece], dev_name)
        got = dev.text("hub_inbox")
        self.assertIn("[TASK, assigned to you, part of #%s] Build the theme switch" % job, got)
        # Progress goes to the lead, and shows on the task.
        self.assertIn(lead_name, dev.text("hub_update", id=piece, status="review",
                                                                note="Ready: toggle in Settings"))
        self.assertEqual(self.hub.progress[piece]["status"], "review")
        self.assertIn("[UPDATE on #%s: ready for review] Ready: toggle in Settings" % piece, lead.text("hub_inbox"))
        self.assertIn("[%s: ready for review]" % dev_name, lead.text("hub_history", limit=10))
        # Developer and QA talk directly.
        dev.call("hub_send", to=qa_name, text="Theme switch is ready to test: Settings > Theme")
        qa_in = qa.text("hub_inbox")
        self.assertIn("ready to test", qa_in)
        qa.call("hub_send", to=dev_name, text="Bug: the switch resets on restart")
        self.assertIn("resets on restart", dev.text("hub_inbox"))
        self.assertIn("status must be one of", qa.call("hub_update", id=piece, status="sideways")["content"][0]["text"])
        # Done: the lead is told the developer is free, to hand it the next piece.
        dev.call("hub_update", id=piece, status="done", note="Merged")
        done = lead.text("hub_inbox")
        self.assertIn("[UPDATE on #%s: done] Merged" % piece, done)
        self.assertIn("%s is free. Check the work, then give it its next task with hub_assign" % dev_name, done)
        self.assertIn("give that agent its next task with hub_assign right away", crewchat.ROLES["lead"][1])
        for name in (lead_name, dev_name, qa_name):
            self.role(name, "none")

    def test_without_a_lead_updates_go_to_whoever_posted_the_task(self):
        dev, dev_name = self.agent()
        job = self.say(dev_name, "Fix the login test", "task")
        dev.call("hub_take", id=job)
        self.assertIn("the owner has been told", dev.text("hub_update", id=job, status="in_progress"))
        update = self.hub.history(1)[0]
        self.assertEqual((update["to"], update["kind"], update["text"]), ("Owner", "update", "in progress"))

    def test_a_task_from_the_owner_is_confirmed_to_the_owner_even_with_a_lead(self):
        (lead, lead_name), (dev, dev_name) = self.agent(), self.agent()
        self.role(lead_name, "lead")
        job = self.say(dev_name, "Work on T229", "task")
        taken = dev.text("hub_take", id=job)
        self.assertIn("Before any other work, send hub_update id=%s status=in_progress" % job, taken)
        self.assertIn("the owner has been told", dev.text("hub_update", id=job, status="in_progress",
                                                          note="Starting: reading the backup code"))
        update = self.hub.history(1)[0]
        self.assertEqual((update["to"], update["text"]), ("Owner", "Starting: reading the backup code"))
        self.assertIn("first send hub_update on it\n  with in_progress", dev.hello["instructions"])
        self.role(lead_name, "none")

    def test_an_agent_takes_a_role_when_told_to_and_custom_roles(self):
        session, name = self.agent()
        self.assertIn("Your role in this chat is now: Reviewer (set by %s)" % name,
                      session.text("hub_role", role="reviewer"))
        self.assertEqual(self.row(name)["role"], "reviewer")
        cli("roles", "add", "designer", "--prompt", "You design screens.", "--title", "Designer")
        self.assertIn("designer     Designer", cli("roles"))
        self.assertIn("You design screens.", cli("roles", "show", "designer"))
        cli("role", name, "designer")
        self.assertIn("You design screens.", session.text("hub_inbox"))
        cli("roles", "remove", "designer")
        self.assertNotIn("designer", cli("roles"))
        cli("role", name, "none")

    def test_the_loop_limit_comes_from_the_config(self):
        self.assertEqual(self.hub.roster.max_chain, crewchat.MAX_CHAIN)
        config = crewchat.load_config()
        config["max_chain"] = 25
        crewchat.save_config(config)
        self.hub.roster.refresh()
        try:
            self.assertEqual(self.hub.roster.max_chain, 25)
            hook = lambda chain: crewchat.post_json(self.base + "/api/hook", self.token,  # noqa: E731
                                                    {"key": "k" * 16, "wait": 0, "chain": chain})["capped"]
            self.assertEqual((hook(24), hook(25)), (False, True))
        finally:
            config.pop("max_chain")
            crewchat.save_config(config)


PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
                    "0000000c4944415408d763f8cfc000000301010018dd8db00000000049454e44ae426082")


class Files(Base):
    def upload(self, name, data, kind="", token=None):
        req = urllib.request.Request(self.base + "/api/upload", data=data, method="POST", headers={
            "Authorization": "Bearer " + (token or self.owner), "Content-Type": kind,
            "X-File-Name": urllib.parse.quote(name)})
        try:
            with urllib.request.urlopen(req, timeout=30) as answer:
                return json.loads(answer.read())
        except urllib.error.HTTPError as e:
            return {"status": e.code}

    def test_the_owner_shares_a_screenshot_and_an_agent_sees_it(self):
        session, name = self.agent()
        meta = self.upload("image.png", PNG, "image/png")
        self.assertEqual((meta["name"], meta["type"], meta["size"]), ("image.png", "image/png", len(PNG)))
        out = crewchat.post_json(self.base + "/api/send", self.owner, {"to": name, "text": "", "files": [meta["id"]]})
        msg = self.hub.history(1)[0]
        self.assertEqual((msg["id"], msg["text"], msg["files"]), (out["id"], "", [meta]))
        self.assertIn("[file %s: image.png, image/png, %d bytes; open it with hub_file]" % (meta["id"], len(PNG)),
                      session.text("hub_inbox"))
        content = session.call("hub_file", id=meta["id"])["content"]
        self.assertEqual(content[1], {"type": "image", "data": base64.b64encode(PNG).decode(), "mimeType": "image/png"})
        self.assertIn("saved at", content[0]["text"])

    def test_text_files_come_back_as_text_and_others_as_a_path(self):
        session, _ = self.agent()
        notes = self.upload("notes.md", "# Plan\nShip it".encode())
        self.assertEqual(notes["type"], "text/markdown")
        self.assertIn("# Plan\nShip it", session.text("hub_file", id=notes["id"]))
        pdf = self.upload("spec.pdf", b"%PDF-1.4 fake", "application/pdf")
        out = session.text("hub_file", id=pdf["id"])
        self.assertIn("Open it from that path", out)
        self.assertTrue(Path(re.search(r"saved at (.+?)\. Open", out).group(1)).is_file())
        self.assertIn("no shared file", session.text("hub_file", id="0123456789abcdef"))

    def test_an_agent_shares_a_file_it_made(self):
        session, name = self.agent()
        other, other_name = self.agent()
        shot = Path(tempfile.mkdtemp(dir=TMP)) / "bug.png"
        shot.write_bytes(PNG)
        # Only from a folder on this machine: a path from another machine's folder would be read here.
        far, far_token = self.join_place("laptop")
        remote, _ = self.agent(token=far_token)
        self.assertIn("another machine", remote.text("hub_send", to=name, text="x", files=[str(shot)]))
        crewchat.remember(shot.parent, self.place)
        self.assertIn("with 1 file(s)", session.text("hub_send", to=other_name, text="See the bug", files=[str(shot)]))
        got = other.text("hub_inbox")
        fid = re.search(r"\[file (\w+): bug.png", got).group(1)
        self.assertEqual(other.call("hub_file", id=fid)["content"][1]["data"], base64.b64encode(PNG).decode())
        self.assertIn("absolute paths", session.text("hub_send", to=other_name, text="x", files=["bug.png"]))
        self.assertIn("cannot read", session.text("hub_send", to=other_name, text="x", files=[str(shot) + ".gone"]))

    def test_the_page_shows_images_and_offers_everything_else_as_a_download(self):
        png = self.upload("shot.png", PNG, "image/png")
        svg = self.upload("logo.svg", b"<svg onload='alert(1)'/>", "image/svg+xml")
        self.assertEqual(self.raw("/files/%s/shot.png" % png["id"])[0], 401)
        opener, _ = self.owner_browser()
        status, body, headers = self.raw("/files/%s/shot.png" % png["id"], opener=opener)
        self.assertEqual((status, body, headers["Content-Type"]), (200, PNG, "image/png"))
        self.assertTrue(headers["Content-Disposition"].startswith("inline"))
        self.assertIn("sandbox", headers["Content-Security-Policy"])
        status, body, headers = self.raw("/files/%s/logo.svg" % svg["id"], opener=opener)
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertTrue(headers["Content-Disposition"].startswith("attachment"))
        self.assertEqual(self.raw("/files/nothing/x", opener=opener)[0], 404)

    def test_only_the_owner_uploads_and_size_is_limited(self):
        self.assertEqual(self.upload("a.txt", b"x", token=self.token)["status"], 403)
        self.assertEqual(self.upload("a.txt", b"x", token="wrong")["status"], 401)
        limit, crewchat.MAX_UPLOAD = crewchat.MAX_UPLOAD, 10
        try:
            self.assertEqual(self.upload("big.bin", b"x" * 11)["status"], 413)
        finally:
            crewchat.MAX_UPLOAD = limit
        with self.assertRaises(urllib.error.HTTPError):
            crewchat.post_json(self.base + "/api/send", self.owner, {"to": "all", "text": "x", "files": ["0123456789abcdef"]})

    def test_say_with_a_file(self):
        doc = Path(tempfile.mkdtemp(dir=TMP)) / "release notes.txt"
        doc.write_text("v1")
        cli("say", "--file", str(doc), "Please review")
        msg = self.hub.history(1)[0]
        self.assertEqual((msg["text"], msg["files"][0]["name"]), ("Please review", "release notes.txt"))

    def test_file_names_are_cleaned(self):
        self.assertEqual(crewchat.clean_filename("../../etc/pass:wd.png"), "passwd.png")
        self.assertEqual(crewchat.clean_filename("C:\\Users\\me\\shot.png"), "shot.png")
        self.assertEqual(crewchat.clean_filename(".."), "file")
        self.assertEqual([crewchat.file_type(n) for n in ("a.md", "b.ts", "c.png")],
                         ["text/markdown", "text/plain", "image/png"])


class Launching(Base):
    """Add an agent from the chat page: the launcher is replaced, so no window opens."""

    def setUp(self):
        self.launched = []
        self.saved = crewchat.launch_agent, crewchat.find_tool
        crewchat.launch_agent = lambda tool, folder, key, edits=False, root=None: self.launched.append(
            (tool, folder, key, edits))
        crewchat.find_tool = lambda tool, config: "/usr/local/bin/" + tool
        self.folder = Path(tempfile.mkdtemp(prefix="launch-", dir=TMP)).resolve()
        subprocess.run(["git", "init", "-q", str(self.folder)], check=False)
        cli("start", str(self.folder), "--no-open", "--no-service", "--place", "Studio")
        self.place = crewchat.joined_place(self.folder)[0]

    def tearDown(self):
        crewchat.launch_agent, crewchat.find_tool = self.saved

    def launch(self, **body):
        try:
            return crewchat.post_json(self.base + "/api/launch", self.owner,
                                      dict({"tool": "claude", "folder": str(self.folder)}, **body))
        except urllib.error.HTTPError as e:
            return {"status": e.code, "error": json.loads(e.read())["error"]}

    def test_the_page_lists_folders_and_tools(self):
        opener, _ = self.owner_browser()
        status, body, _ = self.raw("/api/launch", opener=opener)
        options = json.loads(body)
        self.assertIn({"path": str(self.folder), "name": self.folder.name, "place": self.place}, options["folders"])
        self.assertEqual([t["id"] for t in options["tools"]], ["claude", "cursor"])
        self.assertEqual(self.raw("/api/launch")[0], 401)

    def test_a_launched_agent_gets_its_name_role_and_task(self):
        self.assertEqual(self.launch(name="docs-writer", role="docs", task="Write the settings guide"), {"ok": True})
        tool, folder, key, edits = self.launched[-1]
        self.assertEqual((tool, folder, edits), ("claude", str(self.folder), False))
        self.assertTrue(key.startswith("start-"))
        self.assertIn("Owner started a new Claude Code agent in %s as docs-writer" % self.folder.name,
                      [m["text"] for m in self.hub.history(5)])
        # The new session checks in with its key, as the launch prompt tells it to.
        token = crewchat.read_token(self.place)
        session = Session(self.base, token)
        out = session.text("hub_link", key=key)
        self.assertIn("You are docs-writer, started from the crewchat by the owner.", out)
        self.assertIn("Your role's instructions and your first task are in hub_inbox", out)
        self.assertEqual(session.me, "docs-writer")
        row = next(r for r in self.hub.rows() if r["agent"] == "docs-writer")
        self.assertEqual(row["role"], "docs")
        inbox = session.text("hub_inbox")
        self.assertIn("Your role in this chat is now: Docs", inbox)
        self.assertIn("[TASK, assigned to you] Write the settings guide", inbox)
        # Its hooks use the same key (the launch passes it as CREWCHAT_LINK_KEY), so they reach it
        # without asking it to link again: messages get to it at the end of each turn.
        hooked = crewchat.post_json(self.base + "/api/hook", token, {"key": key})
        self.assertEqual(hooked.get("agent"), "docs-writer")
        self.assertNotIn("link", hooked)
        # A start key works once.
        again = Session(self.base, token).call("hub_link", key=key)["content"][0]["text"]
        self.assertIn("unknown or has expired", again)

    def test_bad_launches_are_refused(self):
        self.assertEqual(self.launch(folder="/etc")["status"], 400)
        self.assertEqual(self.launch(tool="vim")["status"], 400)
        self.assertIn("no role called", self.launch(role="astronaut")["error"])
        self.assertIn("reserved", self.launch(name="Owner")["error"])
        self.assertEqual(self.launched, [])
        self.assertEqual(self.hub.starts, {})

    def test_the_command_line_can_add_one(self):
        out = cli("agent", "add", "tester", "--folder", str(self.folder), "--role", "qa", "--accept-edits")
        self.assertIn("Opened a new Claude Code session in %s" % self.folder, out)
        self.assertEqual(self.launched[-1][3], True)

    def test_the_launch_command_carries_no_typed_text(self):
        command = crewchat.launch_command("/x/claude", "claude", "start-0123456789abcdef", accept_edits=True)
        self.assertEqual(command[:3], ["/x/claude", "--permission-mode", "acceptEdits"])
        self.assertEqual(command[3], crewchat.LAUNCH_PROMPT % "start-0123456789abcdef")
        self.assertRegex(command[3], r"^[A-Za-z0-9 .,:_-]+$")  # safe in any shell, cmd.exe included
        self.assertEqual(crewchat.launch_command("/x/cursor-agent", "cursor", "start-1", True)[1:2],
                         [crewchat.LAUNCH_PROMPT % "start-1"])
        script = crewchat.macos_script("/tmp/it's a folder", command, "start-0123456789abcdef")
        self.assertIn("cd '/tmp/it'\"'\"'s a folder' || exit 1", script)
        self.assertIn("export CREWCHAT_LINK_KEY=start-0123456789abcdef", script)
        self.assertIn("exec /x/claude --permission-mode acceptEdits 'You were started", script)


class Linking(Base):
    """Hooks reach an agent through a link key that the agent ties to itself with hub_link."""

    def hook(self, key, token=None, **extra):
        out = crewchat.post_json(self.base + "/api/hook", token or self.token, dict({"key": key}, **extra))
        self.assertFalse(out.pop("capped"))  # every answer says whether the loop limit was reached
        return out

    def test_link_then_messages_arrive_through_the_hook(self):
        session = Session(self.base, self.token)
        self.assertEqual(self.hook("key-first-0001"), {"link": True})
        note = session.text("hub_link", key="key-first-0001")
        name = session.me
        self.assertIn("Linked. You are %s." % name, note)
        self.assertIn("%s (you)" % name, note)
        self.assertEqual(self.hook("key-first-0001"), {"agent": name, "text": "", "last": 0, "owner": False})
        number = self.say(name, "through the hook")
        answer = self.hook("key-first-0001")
        self.assertIn("Owner -> you: through the hook", answer["text"])
        self.assertEqual(answer["last"], self.hub.seq_of(number))
        self.assertIn("through the hook", self.hook("key-first-0001")["text"])  # still unread
        self.assertEqual(self.hook("key-first-0001", ack=self.hub.seq_of(number))["text"], "")

    def test_a_resumed_conversation_gets_its_name_back(self):
        first = Session(self.base, self.token)
        first.call("hub_link", key="key-resume-0001")
        name = first.me
        before = self.names()
        # The tool restarts: new MCP session, same conversation, so the hook has the same key.
        again = Session(self.base, self.token)
        self.assertIn("This session is %s again." % name, again.text("hub_link", key="key-resume-0001"))
        self.assertEqual(again.me, name)
        self.assertEqual(self.names(), before)

    def test_a_reconnect_that_already_spoke_is_merged_back(self):
        first = Session(self.base, self.token)
        first.call("hub_link", key="key-merge-00001")
        name = first.me
        time.sleep(0.05)
        again = Session(self.base, self.token)
        stand_in = again.me  # used a tool before linking: it was given a name of its own
        self.assertNotEqual(stand_in, name)
        self.assertEqual(self.hook("key-merge-00001"), {"link": True, "again": True})  # the hook notices and asks
        again.call("hub_link", key="key-merge-00001")
        self.assertEqual(again.me, name)
        self.assertNotIn(stand_in, self.names())
        self.assertIn("* %s is %s (session resumed)" % (stand_in, name), again.text("hub_history"))
        self.assertIn("agent", self.hook("key-merge-00001"))

    def test_a_new_neighbour_makes_the_hook_ask_only_once(self):
        mine = Session(self.base, self.token)
        mine.call("hub_link", key="key-neighbour-01")
        name = mine.me
        time.sleep(0.05)
        neighbour = Session(self.base, self.token)
        self.assertEqual(self.hook("key-neighbour-01"), {"link": True, "again": True})
        self.assertIn("agent", self.hook("key-neighbour-01"))  # asked once, then carries on
        mine.call("hub_link", key="key-neighbour-01")  # the no-op re-link changes nothing
        self.assertEqual(mine.me, name)
        self.assertNotEqual(neighbour.me, name)

    def test_keys_do_not_cross_places_and_bad_keys_are_refused(self):
        session = Session(self.base, self.token)
        session.call("hub_link", key="key-private-001")
        other_place, other_token = self.join_place("other")
        self.assertEqual(self.hook("key-private-001", token=other_token), {"link": True})
        intruder = Session(self.base, other_token)
        intruder.call("hub_link", key="key-private-001")
        self.assertNotEqual(intruder.me, session.me)
        self.assertTrue(session.call("hub_link", key="short")["isError"])
        with self.assertRaises(urllib.error.HTTPError):
            self.hook("no")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            crewchat.post_json(self.base + "/api/hook", self.owner, {"key": "key-private-001"})
        self.assertEqual(ctx.exception.code, 403)


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
        place = {"Authorization": "Bearer " + self.token}
        self.assertEqual(self.raw("/api/login-code", b"{}", place)[0], 403)
        self.assertEqual(self.raw("/api/invite", b"{}", place)[0], 403)
        self.assertEqual(self.raw("/api/admin", b'{"op":"remove","name":"x"}', place)[0], 403)
        self.assertEqual(self.raw("/login?code=WRONGCOD", headers={"X-Forwarded-For": "7.7.7.1"})[0], 401)
        code = crewchat.post_json(self.base + "/api/login-code", self.owner, {})["code"]
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
        session, name = self.agent("cursor")
        opener, _ = self.owner_browser()
        status, body, headers = self.raw("/", opener=opener)
        self.assertEqual(status, 200)
        self.assertIn(b"<title>crewchat</title>", body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        send = lambda obj, extra=None: self.raw(  # noqa: E731
            "/api/send", json.dumps(obj).encode(), dict({"Content-Type": "application/json"}, **(extra or {})), opener)
        self.assertEqual(send({"to": "all", "text": "x"}, {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(send({"to": "all", "text": "hello agents"}, {"Origin": self.base})[0], 200)
        for bad in ({"to": "Owner", "text": "x"}, {"to": "all", "text": " "}, {"to": "all"}, {"to": "ghost", "text": "x"}):
            self.assertEqual(send(bad)[0], 400, bad)
        data = json.loads(self.raw("/api/poll?after=-1&v=-1&wait=0", opener=opener)[1])
        self.assertEqual(data["project"], "Demo")
        row = next(a for a in data["agents"] if a["agent"] == name)
        self.assertEqual((row["client"], row["place"], row["online"]), ("cursor", self.place, True))
        self.assertEqual(data["messages"][-1]["text"], "hello agents")
        self.assertTrue(any(m["kind"] == "event" and name + " joined" in m["text"] for m in data["messages"]))
        got = {}
        path = "/api/poll?after=%d&v=%d&wait=20" % (data["messages"][-1]["seq"], data["version"])
        thread = threading.Thread(target=lambda: got.update(d=json.loads(self.raw(path, opener=opener)[1])))
        start = time.time()
        thread.start()
        time.sleep(0.5)
        session.call("hub_send", to="Owner", text="a question")
        thread.join()
        self.assertLess(time.time() - start, 4)
        self.assertEqual(got["d"]["messages"][0]["text"], "a question")

    def test_a_page_that_was_away_catches_up_on_everything(self):
        opener, _ = self.owner_browser()
        poll = lambda after, v=-1: json.loads(self.raw(  # noqa: E731
            "/api/poll?after=%d&v=%d&wait=0" % (after, v), opener=opener)[1])
        mark = poll(-1)
        after = mark["messages"][-1]["seq"] if mark["messages"] else 0
        for i in range(crewchat.PAGE_BATCH + 20):
            self.say("all", "busy night %d" % i)
        latest = poll(-1)  # a new page: the latest messages only
        self.assertEqual(len(latest["messages"]), crewchat.PAGE_BATCH)
        self.assertFalse(latest["more"])
        seen = []
        first = poll(after, mark["version"])  # the page that was open: everything, in order
        self.assertTrue(first["more"])
        seen += first["messages"]
        rest = poll(seen[-1]["seq"], first["version"])
        self.assertFalse(rest["more"])
        seen += rest["messages"]
        self.assertEqual([m["text"] for m in seen], ["busy night %d" % i for i in range(crewchat.PAGE_BATCH + 20)])

    def test_ui_print_gives_a_code(self):
        self.assertRegex(cli("ui", "--print"), r"Sign-in code .*: [A-Z2-9]{4}-[A-Z2-9]{4}")

    def test_installs_as_an_app(self):
        status, body, headers = self.raw("/manifest.webmanifest")
        self.assertEqual((status, headers["Content-Type"]), (200, "application/manifest+json"))
        manifest = json.loads(body)
        self.assertEqual((manifest["display"], manifest["start_url"]), ("standalone", "/"))
        self.assertNotIn("Demo", body.decode())  # served without signing in: no project name
        for size in (192, 512):
            status, body, headers = self.raw("/icon-%d.png" % size)
            self.assertEqual((status, headers["Content-Type"], body[:8]), (200, "image/png", b"\x89PNG\r\n\x1a\n"))
            self.assertEqual(int.from_bytes(body[16:20], "big"), size)
        status, body, headers = self.raw("/sw.js")
        self.assertEqual(status, 200)
        self.assertIn(b"addEventListener('fetch'", body)
        _, login, headers = self.raw("/login")
        self.assertIn(b'<link rel="manifest" href="/manifest.webmanifest">', login)
        self.assertIn("manifest-src 'self'", headers["Content-Security-Policy"])
        opener, _ = self.owner_browser()
        self.assertIn(b"serviceWorker.register('/sw.js')", self.raw("/", opener=opener)[1])


class Joining(Base):
    def project(self):
        folder = Path(tempfile.mkdtemp(prefix="project-", dir=TMP))
        subprocess.run(["git", "init", "-q", str(folder)], check=False)
        return folder

    def invite(self, *extra):
        out = cli("invite", "--local", *extra)
        line = next(l.strip() for l in out.splitlines() if l.strip().startswith("crewchat join"))
        return line.split()[1:]  # the join arguments

    def test_one_join_connects_claude_code_and_cursor(self):
        folder = self.project()
        (folder / ".claude").mkdir()
        (folder / ".claude" / "settings.local.json").write_text(json.dumps(
            {"permissions": {"allow": ["Bash(ls:*)"]},
             "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}))
        out = cli(*self.invite("--place", "Work Laptop"), "--project", str(folder))
        self.assertIn('joined the crewchat for Demo as "work-laptop"', out)
        token = crewchat.read_token("work-laptop")
        mcp = json.loads((folder / ".mcp.json").read_text())["mcpServers"]["crewchat"]
        self.assertEqual((mcp["url"], mcp["headers"]["Authorization"]), (self.mcp, "Bearer " + token))
        cursor = json.loads((folder / ".cursor" / "mcp.json").read_text())["mcpServers"]["crewchat"]
        self.assertEqual(cursor["headers"]["Authorization"], "Bearer " + token)
        settings = json.loads((folder / ".claude" / "settings.local.json").read_text())
        self.assertEqual(settings["permissions"], {"allow": ["Bash(ls:*)"]})
        self.assertIn("crewchat", settings["enabledMcpjsonServers"])
        stop = [h["command"] for g in settings["hooks"]["Stop"] for h in g["hooks"]]
        self.assertEqual(stop[0], "echo mine")
        self.assertTrue(stop[1].endswith(" hook claude stop") and "crewchat.py" in stop[1])
        self.assertEqual(len(settings["hooks"]["UserPromptSubmit"]), 1)
        hooks = json.loads((folder / ".cursor" / "hooks.json").read_text())
        self.assertTrue(hooks["hooks"]["stop"][0]["command"].endswith(" hook cursor stop"))
        self.assertIn(".mcp.json", (folder / ".git" / "info" / "exclude").read_text())
        self.assertNotIn("do not commit", out)
        # Two sessions opened in that folder are two agents.
        one, two = Session(self.base, token), Session(self.base, token, "cursor")
        self.assertEqual((one.me, two.me), ("claude-work-laptop", "cursor-work-laptop"))
        # Joining again replaces our hooks instead of stacking them, and gets its own place.
        cli(*self.invite("--place", "Work Laptop"), "--project", str(folder))
        settings = json.loads((folder / ".claude" / "settings.local.json").read_text())
        self.assertEqual(len(settings["hooks"]["Stop"]), 2)
        self.assertEqual(settings["enabledMcpjsonServers"].count("crewchat"), 1)
        self.assertIn("work-laptop-2", crewchat.list_places())

    def test_join_for_one_client_only(self):
        folder = self.project()
        cli(*self.invite("--client", "cursor"), "--project", str(folder))
        self.assertTrue((folder / ".cursor" / "mcp.json").exists())
        self.assertFalse((folder / ".mcp.json").exists())

    def test_the_place_defaults_to_the_machine_name(self):
        folder = self.project()
        out = cli(*self.invite(), "--project", str(folder))
        place = re.search(r'as "([^"]+)"', out).group(1)
        self.assertRegex(place, r"^[a-z][a-z0-9-]{0,15}$")

    def test_generic_join_prints_settings(self):
        out = cli(*self.invite("--client", "generic", "--place", "other-tool"))
        self.assertIn('"url": "%s"' % self.mcp, out)
        self.assertIn(crewchat.read_token("other-tool"), out)

    def test_start_connects_the_folder_once(self):
        folder = self.project().resolve()
        out = cli("start", str(folder), "--no-open", "--no-service", "--place", "Desk")
        self.assertIn("The server is running at %s." % self.base, out)
        self.assertRegex(out, r'- (Connected|Added) %s(,| to this machine\'s chat, "Demo",) as "desk"\.' % re.escape(str(folder)))
        token = crewchat.read_token("desk")
        mcp = json.loads((folder / ".mcp.json").read_text())["mcpServers"]["crewchat"]
        self.assertEqual(mcp["headers"]["Authorization"], "Bearer " + token)
        self.assertTrue((folder / ".cursor" / "hooks.json").exists())
        places = crewchat.list_places()
        out = cli("start", str(folder), "--no-open", "--no-service")
        self.assertIn('%s is connected, as "desk".' % folder, out)
        self.assertEqual(crewchat.list_places(), places)  # no second place for the same folder

    def test_start_in_another_folder_joins_the_same_chat_under_the_folders_name(self):
        first = self.project().resolve()
        cli("start", str(first), "--no-open", "--no-service")
        other = (Path(tempfile.mkdtemp(dir=TMP)) / "notes-app").resolve()
        other.mkdir()
        out = cli("start", str(other), "--no-open", "--no-service")
        self.assertIn('- Added %s to this machine\'s chat, "Demo", as "notes-app".' % other, out)
        self.assertIn("every folder you run `crewchat start` in joins it", out)
        self.assertIn("claude-notes-app, claude-notes-app-2", out)
        self.assertIn("notes-app", crewchat.list_places())

    def test_the_banner_is_plain_when_not_on_a_terminal(self):
        self.assertEqual(crewchat.banner(io.StringIO()), "crewchat " + crewchat.__version__)

    def test_a_join_code_works_once_and_wrong_codes_fail(self):
        args = self.invite("--client", "generic")
        cli(*args)
        with self.assertRaises(SystemExit):
            cli(*args)
        with self.assertRaises(SystemExit):
            cli("join", "--url", self.base, "--code", "AAAA-AAAA", "--client", "generic")


class Hooks(Base):
    """The hook command exactly as Claude Code and Cursor run it: a subprocess fed JSON on stdin."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.folder = Path(tempfile.mkdtemp(prefix="hooks-", dir=TMP))
        crewchat.install_claude(cls.folder, cls.base, cls.token)
        crewchat.install_cursor(cls.folder, cls.base, cls.token)
        cls.state = tempfile.mkdtemp(prefix="hookstate-", dir=TMP)

    def hook(self, client, event, payload, project=None, listen="0", env_extra=None):
        env = dict(os.environ, CREWCHAT_PROJECT=str(project or self.folder), CREWCHAT_LISTEN=listen,
                   TMPDIR=self.state, TEMP=self.state, TMP=self.state, **(env_extra or {}))
        env.pop("CLAUDE_PROJECT_DIR", None)
        out = subprocess.run([sys.executable, str(ROOT / "crewchat.py"), "hook", client, event],
                             input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout

    def linked(self, conversation, client="claude-code"):
        """A session that has done what the first hook asks: called hub_link with its key."""
        session = Session(self.base, self.token, client)
        asked = self.hook("claude", "prompt", {"session_id": conversation})
        key = re.search(r'key "([^"]+)"', asked).group(1)
        session.call("hub_link", key=key)
        return session, session.me

    def unread(self, name):
        return next(row["unread"] for row in self.hub.rows() if row["agent"] == name)

    def setUp(self):
        shutil.rmtree(self.state, ignore_errors=True)
        os.makedirs(self.state)

    def test_the_first_hook_asks_the_agent_to_link(self):
        session = Session(self.base, self.token)
        asked = self.hook("claude", "prompt", {"session_id": "L"})
        self.assertIn("Call the hub_link tool once with key", asked)
        key = re.search(r'key "([^"]+)"', asked).group(1)
        # The stop hook does not insist: that would cost the agent a whole extra turn.
        self.assertEqual(self.hook("claude", "stop", {"session_id": "L"}), "")
        session.call("hub_link", key=key)
        self.say(session.me, "now it works")
        self.assertIn("now it works", self.hook("claude", "stop", {"session_id": "L"}))

    def test_a_launched_sessions_first_hook_may_be_its_stop_hook(self):
        key = self.hub.new_start()
        session = Session(self.base, self.token)
        session.call("hub_link", key=key)
        self.say(session.me, "before any prompt")
        reason = json.loads(self.hook("claude", "stop", {"session_id": "stop-first"},
                                      env_extra={"CREWCHAT_LINK_KEY": key}))["reason"]
        self.assertIn("before any prompt", reason)

    def test_a_session_started_from_the_chat_page_links_its_hooks_with_its_start_key(self):
        key = self.hub.new_start()
        launched = {"CREWCHAT_LINK_KEY": key}
        asked = self.hook("claude", "prompt", {"session_id": "launched"}, env_extra=launched)
        self.assertIn('key "%s"' % key, asked)  # one key for the launch prompt and the hook alike
        session = Session(self.base, self.token)
        session.call("hub_link", key=key)
        self.say(session.me, "first message for the launched agent")
        reason = json.loads(self.hook("claude", "stop", {"session_id": "launched"}, env_extra=launched))["reason"]
        self.assertIn("first message for the launched agent", reason)
        # Something the agent runs from its shell (`claude -p ...`, another session) inherits the
        # variable, but is not the agent: it gets a key of its own and stays out of the agent's mail.
        self.say(session.me, "third message, for the agent only")
        self.assertEqual(self.hook("claude", "stop", {"session_id": "nested-claude-p"}, env_extra=launched), "")
        reason = json.loads(self.hook("claude", "stop", {"session_id": "launched"}, env_extra=launched))["reason"]
        self.assertIn("third message, for the agent only", reason)
        # A bad value in the variable is ignored, not used as a key.
        self.assertIn("Call the hub_link tool", self.hook("claude", "prompt", {"session_id": "odd"},
                                                          env_extra={"CREWCHAT_LINK_KEY": "no spaces allowed"}))

    def test_a_session_that_never_links_is_left_alone(self):
        for _ in range(crewchat.MAX_LINK_PROMPTS):
            self.assertIn("Call the hub_link tool", self.hook("claude", "prompt", {"session_id": "N"}))
            self.assertEqual(self.hook("claude", "stop", {"session_id": "N"}), "")
        self.assertEqual(self.hook("claude", "prompt", {"session_id": "N"}), "")
        self.assertEqual(self.hook("claude", "stop", {"session_id": "N"}), "")

    def test_a_linked_session_that_loses_its_link_is_asked_by_the_stop_hook(self):
        # A listening agent has no prompt coming: if a reconnect unlinks it, the stop hook asks.
        session, name = self.linked("R")
        self.assertEqual(self.hook("claude", "stop", {"session_id": "R"}), "")
        Session(self.base, self.token)  # a newer session in the same place, not linked yet
        asked = json.loads(self.hook("claude", "stop", {"session_id": "R"}))["reason"]
        self.assertIn("Call the hub_link tool", asked)
        self.assertIn("Linked. You are %s" % name, session.text("hub_link", key=re.search(r'key "([^"]+)"', asked).group(1)))

    def test_hooks_can_be_switched_off_for_a_folder_or_a_session(self):
        folder = Path(tempfile.mkdtemp(prefix="quiet-", dir=TMP))
        crewchat.install_claude(folder, self.base, self.token)
        subprocess.run([sys.executable, str(ROOT / "crewchat.py"), "hooks", "off", "--project", str(folder)],
                       check=True, capture_output=True)
        self.assertEqual(self.hook("claude", "prompt", {"session_id": "Q"}, project=folder), "")
        self.assertEqual(self.hook("claude", "stop", {"session_id": "Q"}, project=folder), "")
        subprocess.run([sys.executable, str(ROOT / "crewchat.py"), "hooks", "on", "--project", str(folder)],
                       check=True, capture_output=True)
        self.assertIn("hub_link", self.hook("claude", "prompt", {"session_id": "Q"}, project=folder))
        os.environ["CREWCHAT_HOOKS"] = "off"
        try:
            self.assertEqual(self.hook("claude", "prompt", {"session_id": "Q2"}, project=folder), "")
        finally:
            del os.environ["CREWCHAT_HOOKS"]

    def test_a_listening_agent_gets_messages_that_follow_closely_in_one_turn(self):
        session, name = self.linked("B")
        self.hook("claude", "stop", {"session_id": "B"})
        threading.Timer(2, lambda: self.say(name, "first")).start()
        threading.Timer(4, lambda: self.say(name, "second")).start()
        out = json.loads(self.hook("claude", "stop", {"session_id": "B"}, listen="30"))["reason"]
        self.assertIn("first", out)
        self.assertIn("second", out)

    def test_two_sessions_in_one_folder_each_get_their_own_messages(self):
        (one, name1), (two, name2) = self.linked("S1"), self.linked("S2")
        self.assertNotEqual(name1, name2)
        self.say(name1, "for the first")
        self.say(name2, "for the second")
        out1, out2 = (self.hook("claude", "stop", {"session_id": s}) for s in ("S1", "S2"))
        self.assertIn("for the first", out1)
        self.assertNotIn("for the second", out1)
        self.assertIn("for the second", out2)
        self.assertNotIn("for the first", out2)

    def test_an_interrupted_turn_gets_the_message_again(self):
        session, name = self.linked("A")
        self.say(name, "m1")
        blocked = json.loads(self.hook("claude", "stop", {"session_id": "A"}))
        self.assertEqual(blocked["decision"], "block")
        self.assertIn("Owner -> you: m1", blocked["reason"])
        self.assertEqual(self.unread(name), 1)  # not confirmed yet
        # The turn is interrupted. The next prompt delivers it again.
        self.assertIn("m1", self.hook("claude", "prompt", {"session_id": "A"}))
        self.assertEqual(self.unread(name), 1)
        # The next hook call proves the agent had a turn: confirmed, nothing new.
        self.assertEqual(self.hook("claude", "stop", {"session_id": "A"}), "")
        self.assertEqual(self.unread(name), 0)

    def test_no_duplicates_in_a_healthy_session(self):
        session, name = self.linked("C")
        self.say(name, "m2")
        self.assertIn("m2", self.hook("claude", "stop", {"session_id": "C"}))
        self.assertEqual(self.hook("claude", "stop", {"session_id": "C"}), "")
        self.say(name, "m3")
        self.assertIn("m3", self.hook("claude", "prompt", {"session_id": "C"}))
        self.say(name, "m4")
        out = self.hook("claude", "stop", {"session_id": "C"})
        self.assertIn("m4", out)
        self.assertNotIn("m3", out)

    def test_loop_guard_stops_agents_chatting_forever_but_never_the_owner(self):
        session, name = self.linked("D")
        other, other_name = self.agent()
        delivered = 0
        for i in range(crewchat.MAX_CHAIN + 2):
            other.call("hub_send", to=name, text="loop %d" % i)
            delivered += bool(self.hook("claude", "stop", {"session_id": "D"}))
        self.assertEqual(delivered, crewchat.MAX_CHAIN)
        self.assertEqual(self.unread(name), 2)  # held back for the user
        # The owner still gets through, and the held messages come along.
        self.say(name, "owner speaking")
        out = self.hook("claude", "stop", {"session_id": "D"})
        self.assertIn("owner speaking", out)
        self.assertIn("loop %d" % (crewchat.MAX_CHAIN + 1), out)
        # ...and the count starts again.
        other.call("hub_send", to=name, text="after the owner")
        self.assertIn("after the owner", self.hook("claude", "stop", {"session_id": "D"}))

    def test_the_owner_can_keep_chatting_past_the_loop_guard(self):
        session, name = self.linked("O")
        for i in range(crewchat.MAX_CHAIN * 2):
            self.say(name, "owner %d" % i)
            self.assertIn("owner %d" % i, self.hook("claude", "stop", {"session_id": "O"}))

    def test_hook_tells_the_agent_to_answer_the_owner_in_the_chat(self):
        session, name = self.linked("W")
        self.say(name, "hello")
        reason = json.loads(self.hook("claude", "stop", {"session_id": "W"}))["reason"]
        self.assertIn("answer anything Owner wrote with hub_send", reason)
        self.assertIn("answer in the chat", session.hello["instructions"])
        self.assertLess(len(reason), 1200)

    def test_every_tool_says_what_it_changes_and_what_its_parameters_mean(self):
        # MCP clients and catalogues (Glama's tool rating) read these: annotations, and a description
        # for every parameter.
        for tool in crewchat.TOOLS:
            notes = tool["annotations"]
            self.assertIn("readOnlyHint", notes, tool["name"])
            self.assertFalse(notes["openWorldHint"], tool["name"])
            for name, spec in tool["inputSchema"]["properties"].items():
                self.assertTrue(spec.get("description"), "%s.%s" % (tool["name"], name))
        reads = {t["name"] for t in crewchat.TOOLS if t["annotations"]["readOnlyHint"]}
        self.assertEqual(reads, {"hub_agents", "hub_history", "hub_file"})
        session = Session(self.base, self.token)
        listed = crewchat.rpc(self.mcp, self.token, "tools/list", session=session.sid)["result"]["tools"]
        self.assertEqual(listed[0]["annotations"], crewchat.TOOLS[0]["annotations"])

    def test_the_instructions_fit_what_claude_code_keeps(self):
        # Claude Code keeps about the first 2,000 characters of a server's instructions.
        session = Session(self.base, self.token)
        text = session.hello["instructions"]
        self.assertLess(len(text) + 60, 2000)
        self.assertIn(crewchat.TRUST_NOTE, text)

    def test_cursor_is_asked_to_link_and_then_gets_followups(self):
        session = Session(self.base, self.token, "cursor")
        first = json.loads(self.hook("cursor", "stop", {"status": "completed", "loop_count": 0, "conversation_id": "c"}))
        key = re.search(r'key "([^"]+)"', first["followup_message"]).group(1)
        session.call("hub_link", key=key)
        name = session.me
        self.assertTrue(name.startswith("cursor-"))
        self.say(name, "for cursor")
        out = json.loads(self.hook("cursor", "stop", {"status": "completed", "loop_count": 1, "conversation_id": "c"}))
        self.assertIn("for cursor", out["followup_message"])
        other, _ = self.agent()
        other.call("hub_send", to=name, text="later")
        self.assertEqual(self.hook("cursor", "stop", {"status": "aborted", "loop_count": 0, "conversation_id": "c"}), "")
        self.assertEqual(self.hook("cursor", "stop", {"status": "completed", "loop_count": crewchat.MAX_CHAIN,
                                                     "conversation_id": "c"}), "")

    def test_listening_wakes_when_a_message_arrives(self):
        session, name = self.linked("E")
        timer = threading.Timer(1.0, lambda: self.say(name, "wake"))
        start = time.time()
        timer.start()
        out = self.hook("claude", "stop", {"session_id": "E"}, listen="20")
        timer.join()
        self.assertIn("wake", out)
        self.assertLess(time.time() - start, 8)

    def test_a_listening_hook_survives_the_server_being_away(self):
        """Pointed at a dead address it keeps retrying for its listen time, then gives up quietly."""
        dead = Path(tempfile.mkdtemp(prefix="away-", dir=TMP))
        crewchat.install_claude(dead, "http://127.0.0.1:1", "x")
        start = time.time()
        self.assertEqual(self.hook("claude", "stop", {"session_id": "away"}, project=dead, listen="9"), "")
        self.assertGreater(time.time() - start, 3)

    def test_hooks_are_silent_when_not_connected_or_unreachable(self):
        empty = Path(tempfile.mkdtemp(prefix="empty-", dir=TMP))
        self.assertEqual(self.hook("claude", "stop", {}, project=empty), "")
        dead = Path(tempfile.mkdtemp(prefix="dead-", dir=TMP))
        crewchat.install_claude(dead, "http://127.0.0.1:1", "x")
        self.assertEqual(self.hook("claude", "stop", {}, project=dead), "")
        self.assertEqual(self.hook("claude", "prompt", "not an object"), self.hook("claude", "prompt", "not an object"))

    def test_listen_command(self):
        folder = Path(tempfile.mkdtemp(prefix="listen-", dir=TMP))
        self.assertIn("on by default", cli("listen", "status", "--project", str(folder)))
        self.assertEqual(crewchat.listen_seconds(folder, "claude"), crewchat.DEFAULT_LISTEN)
        self.assertEqual(crewchat.listen_seconds(folder, "cursor"), 0)
        cli("listen", "on", "--minutes", "999", "--project", str(folder))
        self.assertEqual((folder / ".crewchat-listen").read_text().strip(), str(crewchat.MAX_LISTEN))
        self.assertEqual(crewchat.listen_seconds(folder, "cursor"), crewchat.MAX_LISTEN)
        cli("listen", "off", "--project", str(folder))
        self.assertEqual(crewchat.listen_seconds(folder, "claude"), 0)
        self.assertIn("off", cli("listen", "status", "--project", str(folder)))
        cli("listen", "default", "--project", str(folder))
        self.assertFalse((folder / ".crewchat-listen").exists())

    def test_after_the_loop_guard_only_the_owner_wakes_a_listening_agent(self):
        session, name = self.linked("G")
        other, _ = self.agent()
        for i in range(crewchat.MAX_CHAIN):
            other.call("hub_send", to=name, text="ping %d" % i)
            self.assertTrue(self.hook("claude", "stop", {"session_id": "G"}))
        threading.Timer(0.5, lambda: other.call("hub_send", to=name, text="agent chatter")).start()
        threading.Timer(2.0, lambda: self.say(name, "owner wakes you")).start()
        start = time.time()
        out = self.hook("claude", "stop", {"session_id": "G"}, listen="20")
        self.assertGreater(time.time() - start, 1.8)
        self.assertIn("owner wakes you", out)
        self.assertIn("agent chatter", out)  # held messages come along with the owner's

    def test_cards_show_what_an_agent_is_doing(self):
        session, name = self.linked("H")
        doing = lambda: next(r["activity"] for r in self.hub.rows() if r["agent"] == name)  # noqa: E731
        self.hook("claude", "prompt", {"session_id": "H"})
        self.assertEqual(doing(), "working")
        got = {}
        thread = threading.Thread(target=lambda: got.update(out=self.hook("claude", "stop", {"session_id": "H"},
                                                                          listen="20")))
        thread.start()
        for _ in range(50):
            if doing() == "listening":
                break
            time.sleep(0.1)
        self.assertEqual(doing(), "listening")
        self.assertIn(name + " (you): claude on mac; online, waiting for messages", session.text("hub_agents"))
        self.say(name, "go")
        thread.join()
        self.assertIn("go", got["out"])
        self.assertEqual(doing(), "working")
        # A listening hook that ends without a message counts as listening for a moment (it asks
        # again at once), then as idle.
        grace, crewchat.LISTEN_GRACE = crewchat.LISTEN_GRACE, 0.5
        try:
            self.hook("claude", "stop", {"session_id": "H"}, listen="1")
            self.assertEqual(doing(), "listening")
            time.sleep(0.7)
            self.assertEqual(doing(), "idle")
        finally:
            crewchat.LISTEN_GRACE = grace
        self.hook("claude", "stop", {"session_id": "H"})
        self.assertEqual(doing(), "idle")


class Persistence(Base):
    def test_everything_survives_a_restart(self):
        session, name = self.agent()
        session.call("hub_link", key="key-persist-001")
        session.call("hub_status", text="busy")
        task = self.say("all", "persist me", "task")
        session.call("hub_take", id=task)
        session.call("hub_inbox")
        again = crewchat.Hub(crewchat.Roster())  # what a restarted server loads
        self.assertEqual(again.agent_for(session.sid, create=False), name)
        self.assertEqual(again.links["key-persist-001"], name)
        self.assertEqual(again.taken[str(task)], name)
        self.assertEqual(again.agents[name]["status"], "busy")
        self.assertEqual(again._unread(name), [])
        self.assertEqual(again.send("Owner", "all", "next")["seq"], self.hub.next_seq)


class Upgrade(unittest.TestCase):
    def test_history_written_by_0_2_still_loads(self):
        old_home = os.environ["CREWCHAT_HOME"]
        folder = Path(tempfile.mkdtemp(prefix="upgrade-", dir=TMP))
        os.environ["CREWCHAT_HOME"] = str(folder)
        try:
            (folder / "config.json").write_text(json.dumps({"project": "Old"}))
            (folder / "messages.jsonl").write_text("\n".join(json.dumps(m) for m in (
                {"id": 1, "ts": 1.0, "from": "Owner", "to": "all", "text": "old one", "kind": "msg"},
                {"id": 2, "ts": 2.0, "from": "Owner", "to": "all", "text": "old task", "kind": "task"},
            )) + "\n")
            (folder / "state.json").write_text(json.dumps({"taken": {"2": "someone"}}))
            hub = crewchat.Hub(crewchat.Roster())
            self.assertEqual([(m["id"], m["seq"]) for m in hub.messages], [("1", 1), ("2", 2)])
            self.assertEqual(hub.taken, {"2": "someone"})
            self.assertEqual(hub.send("Owner", "all", "new")["id"], "3")
        finally:
            os.environ["CREWCHAT_HOME"] = old_home

    def test_setup_again_keeps_other_settings(self):
        old_home = os.environ["CREWCHAT_HOME"]
        os.environ["CREWCHAT_HOME"] = str(Path(tempfile.mkdtemp(prefix="resetup-", dir=TMP)))
        try:
            cli("setup", "--project", "One", "--port", "8799")
            config = crewchat.load_config()
            config.update(cloud=True, cloud_ttl_hours=12)
            crewchat.save_config(config)
            cli("setup", "--project", "Two")
            config = crewchat.load_config()
            self.assertEqual((config["project"], config["port"], config["cloud"], config["cloud_ttl_hours"]),
                             ("Two", 8799, True, 12))
        finally:
            os.environ["CREWCHAT_HOME"] = old_home

    def test_installers_install_this_version(self):
        for name, pattern in (("install.sh", r'VERSION="\$\{CREWCHAT_VERSION:-([0-9.]+)\}"'),
                              ("install.ps1", r'else \{ "([0-9.]+)" \}')):
            found = re.search(pattern, (ROOT / name).read_text(encoding="utf-8"))
            self.assertEqual(found and found.group(1), crewchat.__version__, name)

    def test_message_numbers_as_agents_write_them(self):
        for written, clean in ((12, "12"), ("12", "12"), ("#12", "12"), ("A12", "A12"), (" #AB3 ", "AB3")):
            self.assertEqual(crewchat.clean_id(written), clean)
        for bad in ("", None, "12a", "ABCD1", "1.5", "#", "x" * 20):
            with self.assertRaises(crewchat.HubError, msg=bad):
                crewchat.clean_id(bad)


class Service(unittest.TestCase):
    def test_windows_task_runs_this_program_without_a_window(self):
        task = crewchat.windows_task()
        self.assertEqual(task[:4], ["schtasks", "/Create", "/TN", "crewchat"])
        self.assertIn("ONLOGON", task)
        action = task[-1]
        self.assertIn("crewchat.py", action)
        self.assertIn("serve --log", action)
        self.assertIn("CREWCHAT_HOME", action)  # the tests run with a custom home

    def windows(self, schtasks_ok):
        """crewchat's Windows service code with a stand-in registry and Task Scheduler."""
        from unittest import mock
        values = {}

        class Key:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def query(key, name):
            if name not in values:
                raise FileNotFoundError(name)
            return values[name], 1

        def delete(key, name):
            if name not in values:
                raise FileNotFoundError(name)
            del values[name]

        winreg = mock.Mock(HKEY_CURRENT_USER=1, REG_SZ=1, OpenKey=lambda *a: Key(), CreateKey=lambda *a: Key(),
                           QueryValueEx=query, DeleteValue=delete,
                           SetValueEx=lambda key, name, _, kind, value: values.__setitem__(name, value))
        calls = []

        def run(command):
            calls.append(command)
            task = command[:2] in (["schtasks", "/Create"], ["schtasks", "/Query"]) and schtasks_ok
            out = ("<Command>set CREWCHAT_HOME=%s&amp;&amp; serve</Command>" % crewchat.home()) if task else ""
            return mock.Mock(returncode=0 if task or command[1] in ("/End", "/Delete", "/Run") else 1,
                             stdout=out, stderr="" if task else "ERROR: Access is denied.")

        patches = [mock.patch.dict(sys.modules, {"winreg": winreg}), mock.patch.object(crewchat, "run", run),
                   mock.patch.object(crewchat, "start_background"),
                   mock.patch.object(crewchat, "server_up", return_value=False),
                   mock.patch.object(crewchat, "load_config", return_value={"port": 1})]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return values, calls

    def test_without_an_administrator_windows_starts_it_from_the_startup_programs(self):
        values, calls = self.windows(schtasks_ok=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            crewchat.cmd_service_windows(crewchat.argparse.Namespace(action="install", keep_awake=False))
        self.assertEqual(values["crewchat"], crewchat.windows_command())
        self.assertIn("from your startup programs", out.getvalue())
        self.assertIn("which need no administrator", out.getvalue())
        self.assertNotIn("Access is denied", out.getvalue())  # Windows' own words read like a failure
        crewchat.start_background.assert_called_once()  # running now, not only after the next log on
        self.assertEqual(crewchat.windows_service(), "run")
        with contextlib.redirect_stdout(io.StringIO()):
            crewchat.cmd_service_windows(crewchat.argparse.Namespace(action="uninstall", keep_awake=False))
        self.assertNotIn("crewchat", values)
        self.assertIsNone(crewchat.windows_service())

    def test_with_task_scheduler_there_is_no_startup_entry_as_well(self):
        values, calls = self.windows(schtasks_ok=True)
        values["crewchat"] = "an old entry"
        with contextlib.redirect_stdout(io.StringIO()):
            crewchat.cmd_service_windows(crewchat.argparse.Namespace(action="install", keep_awake=False))
        self.assertNotIn("crewchat", values)
        self.assertIn(["schtasks", "/Run", "/TN", "crewchat"], calls)
        self.assertEqual(crewchat.windows_service(), "task")

    def test_a_startup_entry_for_another_chat_is_not_ours(self):
        values, _ = self.windows(schtasks_ok=False)
        values["crewchat"] = 'cmd /c "set CREWCHAT_HOME=C:\\elsewhere&& pythonw crewchat.py serve"'
        self.assertIsNone(crewchat.windows_service())
        values["crewchat"] = crewchat.windows_command()
        self.assertEqual(crewchat.windows_service(), "run")

    def start(self, up, installed, no_service=False, install_fails=False, config=None):
        """ensure_server with stand-ins for the server and the login service; what it did."""
        from unittest import mock
        did = []
        state = {"up": up}

        def stop(config):
            did.append("stop")
            state["up"] = False
            return True

        def install(args):
            did.append("install")
            if install_fails:
                sys.exit("Access is denied")
            state["up"] = True

        def background():
            did.append("background")
            state["up"] = True

        with mock.patch.object(crewchat, "server_up", lambda config: state["up"]), \
                mock.patch.object(crewchat, "service_installed", return_value=installed), \
                mock.patch.object(crewchat, "stop_server", stop), mock.patch.object(crewchat, "cmd_service", install), \
                mock.patch.object(crewchat, "start_background", background), \
                mock.patch.object(crewchat, "save_config", lambda config: did.append("saved service=%s"
                                                                                    % config["service"])), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            crewchat.ensure_server(dict({"port": 1}, **(config or {})),
                                   crewchat.argparse.Namespace(no_service=no_service, keep_awake=False))
        return did, out.getvalue()

    def test_start_leaves_a_running_server_alone(self):
        # One the owner runs in a terminal (`crewchat serve`), or a second chat's: never replaced.
        for installed in (True, False):
            did, out = self.start(up=True, installed=installed)
            self.assertEqual(did, [])
            self.assertIn("The server is running", out)

    def test_start_sets_up_login_unless_the_owner_turned_it_off(self):
        self.assertEqual(self.start(up=False, installed=False)[0], ["install"])
        did, out = self.start(up=False, installed=False, config={"service": False})
        self.assertEqual(did, ["background"])  # `crewchat service uninstall` is respected
        self.assertIn("until you log out", out)
        # --no-service is the same choice, and is remembered for upgrades (`crewchat resume`).
        self.assertEqual(self.start(up=False, installed=False, no_service=True)[0],
                         ["saved service=False", "background"])
        self.assertEqual(self.start(up=False, installed=False, no_service=True, config={"service": False})[0],
                         ["background"])

    def test_start_runs_the_server_in_the_background_when_login_cannot_be_set_up(self):
        did, out = self.start(up=False, installed=False, install_fails=True)
        self.assertEqual(did, ["install", "background"])
        self.assertIn("Could not register it to start at login", out)

    def resume(self, up=False, ours=False, present=False, config=None, install_fails=False):
        """cmd_resume with stand-ins; what it did."""
        from unittest import mock
        did = []
        state = {"up": up}

        def install(args):
            did.append("install")
            if install_fails:
                sys.exit("Access is denied")
            state["up"] = True

        def start(config):
            did.append("start")
            state["up"] = True

        with mock.patch.object(crewchat, "load_config", return_value=dict({"port": 1}, **(config or {}))), \
                mock.patch.object(crewchat, "server_up", lambda config: state["up"]), \
                mock.patch.object(crewchat, "service_installed", return_value=ours), \
                mock.patch.object(crewchat, "service_present", return_value=present or ours), \
                mock.patch.object(crewchat, "cmd_service", install), mock.patch.object(crewchat, "start_server", start), \
                contextlib.redirect_stdout(io.StringIO()):
            crewchat.cmd_resume(None)
        self.assertTrue(state["up"], "resume must never leave the chat down")
        return did

    def test_after_an_upgrade_the_server_comes_back_and_starts_at_login(self):
        self.assertEqual(self.resume(up=True), [])
        self.assertEqual(self.resume(ours=True), ["start"])  # through its own login service
        self.assertEqual(self.resume(), ["install"])  # none yet: set it up, which starts it
        self.assertEqual(self.resume(present=True), ["start"])  # another chat's: leave that alone
        self.assertEqual(self.resume(config={"service": False}), ["start"])  # turned off by the owner
        self.assertEqual(self.resume(install_fails=True), ["install", "start"])

    def test_the_owners_choice_about_starting_at_login_is_remembered(self):
        from unittest import mock
        saved = {}
        with mock.patch.object(crewchat, "load_config", return_value={"port": 1}), \
                mock.patch.object(crewchat, "save_config", lambda config: saved.update(config)):
            crewchat.remember_service_choice("uninstall")
            self.assertIs(saved["service"], False)
            crewchat.remember_service_choice("install")
            self.assertIs(saved["service"], True)
            saved.clear()
            crewchat.remember_service_choice("status")
            self.assertEqual(saved, {})

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



class Lifecycle(Base):
    """Updates and uninstalling, as far as they run in this process."""

    def test_versions_compare_as_numbers(self):
        self.assertGreater(crewchat.version_tuple("0.10.0"), crewchat.version_tuple("0.9.7"))
        self.assertEqual(crewchat.version_tuple("v1.2.3"), (1, 2, 3))
        self.assertEqual(crewchat.version_tuple("nightly"), ())

    def test_a_newer_release_reaches_the_page(self):
        path = crewchat.home() / "update.json"
        try:
            path.write_text(json.dumps({"latest": "99.0.0"}))
            self.assertEqual(crewchat.newer_version(), "99.0.0")
            path.write_text(json.dumps({"latest": crewchat.__version__}))
            self.assertEqual(crewchat.newer_version(), "")
            path.write_text("not json")
            self.assertEqual(crewchat.newer_version(), "")
        finally:
            path.unlink()
        opener, _ = self.owner_browser()
        poll = lambda: json.loads(self.raw("/api/poll?after=-1&v=-1&wait=0", opener=opener)[1])  # noqa: E731
        self.hub.set_update("99.0.0")
        self.assertEqual((poll()["update"], poll()["releases"]), ("99.0.0", crewchat.RELEASES))
        self.hub.set_update("")
        self.assertEqual(poll()["update"], "")
        page = self.raw("/", opener=opener)[1].decode()
        self.assertIn('id="update"', page)
        self.assertIn("crewchat update", page)

    def test_uninstall_takes_crewchat_out_of_a_folder_and_leaves_the_rest(self):
        mine = Path(tempfile.mkdtemp(dir=TMP))
        crewchat.install_claude(mine, self.base, self.token)
        crewchat.install_cursor(mine, self.base, self.token)
        (mine / ".crewchat-listen").write_text("0\n")
        self.assertEqual(set(crewchat.disconnect_folder(mine)), {
            ".mcp.json", ".claude/settings.local.json", ".cursor/mcp.json", ".cursor/hooks.json", ".crewchat-listen"})
        for rel in (".mcp.json", ".claude/settings.local.json", ".cursor/mcp.json", ".cursor/hooks.json",
                    ".crewchat-listen"):
            self.assertFalse((mine / rel).exists(), rel)
        # A folder with the owner's own servers and hooks keeps them.
        shared = Path(tempfile.mkdtemp(dir=TMP))
        (shared / ".claude").mkdir()
        (shared / ".mcp.json").write_text(json.dumps({"mcpServers": {"other": {"type": "http", "url": "x"}}}))
        (shared / ".claude" / "settings.local.json").write_text(json.dumps({
            "permissions": {"allow": ["Bash(ls)"]}, "enabledMcpjsonServers": ["other"],
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}))
        crewchat.install_claude(shared, self.base, self.token)
        crewchat.disconnect_folder(shared)
        self.assertEqual(json.loads((shared / ".mcp.json").read_text()),
                         {"mcpServers": {"other": {"type": "http", "url": "x"}}})
        settings = json.loads((shared / ".claude" / "settings.local.json").read_text())
        self.assertEqual(settings["enabledMcpjsonServers"], ["other"])
        self.assertEqual(settings["permissions"], {"allow": ["Bash(ls)"]})
        self.assertEqual(settings["hooks"], {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]})
        self.assertEqual(crewchat.disconnect_folder(shared), [])  # nothing of ours left

    def test_machine_names_are_cut_at_a_word_break(self):
        self.assertEqual(crewchat.place_label("Deepankars-Mac-mini.local"), "deepankars-mac")
        self.assertEqual(crewchat.place_label("ATSLAP-73"), "atslap-73")
        self.assertEqual(crewchat.place_label("averyveryverylongmachinename"), "averyveryverylon")
        self.assertEqual(crewchat.place_label("my-mac"), "my-mac")
        self.assertEqual(crewchat.place_label("123"), "machine")

    def test_update_installs_from_pypi_first_then_the_release_file(self):
        specs = crewchat.update_specs("1.2.3")
        self.assertRegex(specs[0], r"^crewchat(\[cloud\])?==1\.2\.3$")
        self.assertTrue(specs[1].endswith("/releases/download/v1.2.3/crewchat-1.2.3-py3-none-any.whl"))
        self.assertRegex(crewchat.update_specs("")[0], r"^crewchat(\[cloud\])?$")
        self.assertEqual(len(crewchat.update_specs("")), 1)

    def test_the_windows_update_waits_installs_with_uv_and_resumes_without_running_a_download(self):
        script = crewchat.windows_update_script(r"C:\Users\o'neil\.local\bin\uv.exe",
                                                crewchat.update_specs("1.2.3"),
                                                r"C:\Users\o'neil\.local\bin\crewchat.exe",
                                                r"C:\Users\o'neil\AppData\Roaming\uv\tools\crewchat", 4242)
        lines = script.splitlines()
        self.assertEqual(lines[1], "Wait-Process -Id 4242 -ErrorAction SilentlyContinue")
        self.assertIn("'C:\\Users\\o''neil\\AppData\\Roaming\\uv\\tools\\crewchat'", script)  # quoted
        installs = [l for l in lines if "tool install" in l]
        self.assertEqual(len(installs), 2)
        self.assertIn("==1.2.3'", installs[0])
        self.assertIn("resume", script)
        lowered = script.lower()
        for risky in ("irm ", "iex", "invoke-expression", "invoke-restmethod", "downloadstring", "-enc"):
            self.assertNotIn(risky, lowered)

    def test_update_refuses_a_copy_the_installer_did_not_put_there(self):
        with self.assertRaises(SystemExit):
            cli("update")


if __name__ == "__main__":
    unittest.main()
