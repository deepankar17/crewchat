"""End-to-end tests: a real server on a free port, a throwaway home folder, real HTTP."""
import argparse
import base64
import contextlib
import http.cookiejar
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
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
crewchat.user_home = lambda: Path(TMP) / "user"  # never the real ~/.claude.json or ~/.cursor
os.environ.pop("CLAUDE_CONFIG_DIR", None)
os.environ.pop("CODEX_HOME", None)  # Codex's settings: under user_home too


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
                                                      "hub_status", "hub_history", "hub_rename", "hub_profile", "hub_file",
                                                      "hub_role",
                                                      "hub_tasks", "hub_task_add", "hub_assign", "hub_update",
                                                      "hub_ask", "hub_answer", "hub_link"])

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

    def test_an_agent_is_asked_to_name_its_session(self):
        session = Session(self.base, self.token)
        key = self.hub.new_start()
        linked = session.text("hub_link", key=key)
        name = session.me
        self.assertIn("If your app lets you name this session (the Claude app does), name it %s." % name, linked)
        crewchat.post_json(self.base + "/api/agent", self.owner, {"op": "rename", "agent": name, "new": "docs-writer"})
        inbox = session.text("hub_inbox")
        self.assertIn("You are now docs-writer.", inbox)
        self.assertIn("name it docs-writer", inbox)
        self.assertIn("name it its-own-name", session.text("hub_rename", name="its-own-name"))  # renaming itself too

    def place_names(self):
        """Name new agents after their tool and folder, as before 0.14, for this test."""
        config = crewchat.load_config()
        config["agent_names"] = "place"
        crewchat.save_config(config)
        self.hub.roster.refresh(force=True)

        def back():
            config.pop("agent_names", None)
            crewchat.save_config(config)
            self.hub.roster.refresh(force=True)
        self.addCleanup(back)

    def test_new_agents_get_first_names_of_their_own(self):
        place, token = self.join_place("names")
        names = [self.agent(client, token=token)[1] for client in ("claude-code", "claude-code", "Cursor")]
        self.assertEqual(len(set(n.lower() for n in names)), 3)
        for name in names:
            self.assertIn(name, crewchat.AGENT_NAMES)
        with self.hub.lock:  # every first name taken: tool and folder, as before
            taken = {n: {} for n in crewchat.AGENT_NAMES}
            saved, self.hub.agents = self.hub.agents, dict(self.hub.agents, **taken)
            try:
                self.assertEqual(self.hub._agent_name("names", "claude"), "claude-names")
            finally:
                self.hub.agents = saved

    def test_a_first_name_that_still_holds_tasks_is_not_given_again(self):
        with self.hub.lock:
            saved = dict(self.hub.taken)
            self.hub.taken.update({"t%d" % i: n for i, n in enumerate(crewchat.AGENT_NAMES)})  # forgotten holders
            try:
                self.assertNotIn(self.hub._agent_name("x", "claude"), crewchat.AGENT_NAMES)
            finally:
                self.hub.taken = saved

    def test_a_name_clash_with_another_machine_is_settled_by_one_side(self):
        session, name = self.agent()
        sync = mock.Mock(device_id="m-zzz", shared=True)
        with mock.patch.object(self.hub, "sync", sync):
            self.hub.set_remote("m-aaa", "other", [{"agent": name.lower(), "client": "claude", "place": "x"}], 1)
        try:
            self.assertNotIn(name, self.hub.agents)  # the larger id gives way: this machine renamed its agent
            self.assertIn("You are now %s." % session.me, session.text("hub_inbox"))
        finally:
            self.hub.drop_remote("m-aaa")
        other, other_name = self.agent()
        with mock.patch.object(self.hub, "sync", mock.Mock(device_id="m-aaa", shared=True)):
            self.hub.set_remote("m-zzz", "other", [{"agent": other_name, "client": "claude", "place": "x"}], 1)
        try:
            self.assertIn(other_name, self.hub.agents)  # the smaller id keeps its name: the other machine gives way
        finally:
            self.hub.drop_remote("m-zzz")

    def test_names_are_matched_in_any_case(self):
        session, name = self.agent()
        crewchat.post_json(self.base + "/api/send", self.owner, {"to": name.lower(), "text": "lower case"})
        self.assertIn("lower case", session.text("hub_inbox"))

    def test_the_setting_and_the_page_know_the_names(self):
        try:
            cli("setup", "--agent-names", "place")
            self.assertEqual(crewchat.load_config()["agent_names"], "place")
        finally:
            cli("setup", "--agent-names", "people")
        self.assertEqual(crewchat.load_config()["agent_names"], "people")
        self.assertEqual(self.hub.poll(-1, -1, 0)["names"], crewchat.AGENT_NAMES)

    def test_sessions_sharing_one_folder_get_their_own_names(self):
        self.place_names()
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
        me = lambda: next(l for l in call("hub_agents").splitlines() if " (you)" in l).split(" (you)")[0]  # noqa: E731
        first = me()
        self.assertEqual(me(), first)  # the same agent again, not a new one
        self.assertEqual(self.names().count(first), 1)

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


class Advisor(Base):
    """An agent with the advisor role answers the others' hub_ask questions, which wait for it."""

    def role(self, agent, role):
        return crewchat.post_json(self.base + "/api/role", self.owner, {"agent": agent, "role": role})

    def ask_in_background(self, session, **arguments):
        out = {}
        thread = threading.Thread(target=lambda: out.update(session.call("hub_ask", **arguments)))
        thread.start()
        return thread, out

    def test_an_agent_asks_the_advisor_and_gets_the_answer_back(self):
        dev, dev_name = self.agent()
        self.assertIn("this chat has no advisor", dev.text("hub_ask", question="Which approach?"))
        adv, adv_name = self.agent()
        self.role(adv_name, "advisor")
        try:
            self.assertIn("You are the advisor", adv.text("hub_inbox"))
            thread, out = self.ask_in_background(dev, question="Cache in memory or on disk? It restarts often.")
            deadline = time.time() + 10
            inbox = ""
            while "[QUESTION]" not in inbox and time.time() < deadline:
                inbox += adv.text("hub_inbox", wait_seconds=1)
            self.assertIn("%s -> you: [QUESTION] Cache in memory or on disk?" % dev_name, inbox)
            ask_id = re.search(r"hub_answer id=(\w+)", inbox).group(1)
            self.assertIn("a verdict is for a question about a task", adv.text("hub_answer", id=ask_id, text="x",
                                                                                 verdict="approve"))
            self.assertIn("Answered #%s" % ask_id, adv.text("hub_answer", id=ask_id, text="On disk: it restarts."))
            thread.join(10)
            self.assertIn("%s answered" % adv_name, out["content"][0]["text"])
            self.assertIn("On disk: it restarts.", out["content"][0]["text"])
            # Also in the inbox (it is never lost if the asker stopped waiting), and in the chat.
            self.assertIn("[ANSWER to #%s] On disk" % ask_id, dev.text("hub_inbox"))
            self.assertIn("you cannot answer your own question", dev.text("hub_answer", id=ask_id, text="me too"))
            self.assertIn("is already answered", adv.text("hub_answer", id=ask_id, text="again"))
            self.assertIn("not a question asked with hub_ask", adv.text("hub_answer", id="0", text="x"))
        finally:
            self.role(adv_name, "none")

    def test_a_question_without_an_answer_in_time_comes_to_the_inbox(self):
        dev, dev_name = self.agent()
        adv, adv_name = self.agent()
        self.role(adv_name, "advisor")
        try:
            question, answer = self.hub.ask(dev_name, "Quick one?", wait=0)
            self.assertIsNone(answer)
            adv.text("hub_answer", id=question["id"], text="Later answer")
            self.assertIn("Later answer", dev.text("hub_inbox"))
        finally:
            self.role(adv_name, "none")

    def test_only_the_advisor_asked_answers_and_agents_cannot_make_themselves_advisor(self):
        dev, dev_name = self.agent()
        (adv, adv_name), (other, other_name) = self.agent(), self.agent()
        self.role(adv_name, "advisor")
        self.role(other_name, "advisor")
        try:
            self.assertIn("only the owner gives the advisor role", dev.text("hub_role", role="advisor"))
            question, _ = self.hub.ask(dev_name, "Which one?", to=adv_name, wait=0)
            self.assertIn("only they (or Owner) answer it", other.text("hub_answer", id=question["id"], text="x"))
            self.assertIn("Answered", adv.text("hub_answer", id=question["id"], text="This one."))
        finally:
            self.role(adv_name, "none")
            self.role(other_name, "none")

    def test_an_advisor_on_another_machine_is_asked(self):
        dev, dev_name = self.agent()
        self.hub.remote["laptop-device"] = {"device": "laptop", "updated": time.time(),
                                            "agents": [{"agent": "claude-laptop", "role": "advisor"}]}
        try:
            question, _ = self.hub.ask(dev_name, "Remote advice?", wait=0)
            self.assertEqual(question["to"], "claude-laptop")
        finally:
            del self.hub.remote["laptop-device"]

    def test_the_advisor_check_before_done(self):
        dev, dev_name = self.agent()
        adv, adv_name = self.agent()
        task = self.say(dev_name, "Ship the cache", "task")
        dev.call("hub_take", id=task)
        set_check = lambda value: crewchat.post_json(self.base + "/api/task", self.owner,  # noqa: E731
                                                     {"op": "advisor", "value": value})["advisor"]
        self.assertEqual(set_check(""), "off")
        self.assertEqual(set_check("on"), "on")
        try:
            self.assertIn("The chat has no advisor yet", dev.text("hub_update", id=task, status="done"))
            self.role(adv_name, "advisor")
            self.assertIn("advisor check before done", dev.text("hub_update", id=task, status="done"))
            bystander, _ = self.agent()
            self.assertIn("only the agent holding task", bystander.text("hub_ask", question="Done?", task=task))
            question, _ = self.hub.ask(dev_name, "Done? Tests pass.", task=task, wait=0)
            adv.text("hub_answer", id=question["id"], text="Add a test for expiry.", verdict="changes")
            self.assertIn("advisor check before done", dev.text("hub_update", id=task, status="done"))
            question, _ = self.hub.ask(dev_name, "Added the expiry test.", task=task, wait=0)
            # A late answer to an older check does not count: only the newest check does.
            old, _ = question, None
            question, _ = self.hub.ask(dev_name, "Final round.", task=task, wait=0)
            adv.text("hub_answer", id=old["id"], text="Good.", verdict="approve")
            self.assertIn("advisor check before done", dev.text("hub_update", id=task, status="done"))
            adv.text("hub_answer", id=question["id"], text="Good.", verdict="approve")
            self.assertIn("advisor: approved", dev.text("hub_tasks", view="all"))
            # Work started again: the approval was on other work, so it no longer counts.
            dev.text("hub_update", id=task, status="in_progress", note="one more fix")
            self.assertIn("advisor check before done", dev.text("hub_update", id=task, status="done"))
            question, _ = self.hub.ask(dev_name, "Fixed.", task=task, wait=0)
            adv.text("hub_answer", id=question["id"], text="Good.", verdict="approve")
            self.assertIn("marked done", dev.text("hub_update", id=task, status="done"))
            self.assertIn("[QUESTION before done, task #%s]" % task, dev.text("hub_history"))
        finally:
            self.assertEqual(set_check("off"), "off")
            self.role(adv_name, "none")
        self.assertIn("Tasks can be marked done without the advisor's check", dev.text("hub_history"))


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
        qa = next(r for r in poll["roles"] if r["name"] == "qa")
        self.assertEqual((qa["title"], qa["picture"], qa["source"]), ("QA", "art:scientist", "pack"))
        self.assertNotIn("description", qa)  # instructions come from /api/roles, not every poll

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


class RolePacks(unittest.TestCase):
    """Role packs and the owner's changes to a project's roles, on a config with no server."""

    def setUp(self):
        self.packs = dict(crewchat.ROLE_PACKS)

    def change(self, config, op, **data):
        return crewchat.change_roles(config, dict(data, op=op), self.packs)[0]

    def test_a_project_from_before_packs_has_the_software_roles(self):
        self.assertEqual(crewchat.role_texts(crewchat.role_table({}, self.packs)), crewchat.ROLES)
        self.assertEqual(crewchat.project_pack({"pack": "gone"}, self.packs), "software")
        old = {"roles": {"designer": {"title": "Designer", "prompt": "You design."}}}
        row = crewchat.role_table(old, self.packs)["designer"]
        self.assertEqual((row["label"], row["description"], row["source"]), ("Designer", "You design.", "yours"))

    def test_every_pack_has_a_lead_and_an_advisor_and_says_what_its_roles_do(self):
        for pid, pack in self.packs.items():
            self.assertIn(pack["crew"], crewchat.CREWS)
            if pid == "blank":
                self.assertEqual(pack["roles"], {})
                continue
            self.assertLessEqual({"lead", "advisor"}, set(pack["roles"]), pid)
            for name, role in pack["roles"].items():
                self.assertEqual(crewchat.clean_role(role)["description"], role["description"], (pid, name))
                self.assertEqual(crewchat.clean_picture(role["picture"]), role["picture"], (pid, name))
                self.assertTrue(role["picture"].startswith("art:"), (pid, name))  # the 3D pictures
        lead = self.packs["youtube"]["roles"]["lead"]
        self.assertEqual(lead["label"], "Producer")
        self.assertIn("hub_assign", lead["description"])
        self.assertIn("Never publish, post", self.packs["business"]["roles"]["support"]["description"])
        self.assertEqual([r["label"] for r in self.packs["instagram"]["roles"].values()][:5],
                         ["Content planner", "Caption writer", "Visual designer", "Community manager", "Analytics"])

    def test_create_edit_duplicate_delete(self):
        config = {}
        role = self.change(config, "create", name="Designer", label="Designer", description="You design.",
                           picture="preset:palette", colour="#ABCDEF", tool="cursor")["role"]
        self.assertEqual(role, {"name": "designer", "label": "Designer", "description": "You design.",
                                "picture": "preset:palette", "colour": "#abcdef", "tool": "cursor"})
        with self.assertRaisesRegex(crewchat.HubError, "already"):
            self.change(config, "create", name="designer", description="Again.")
        with self.assertRaisesRegex(crewchat.HubError, "description"):
            self.change(config, "create", name="empty", description="  ")
        with self.assertRaisesRegex(crewchat.HubError, "letters, digits"):
            self.change(config, "create", name="../x", description="x")
        with self.assertRaisesRegex(crewchat.HubError, "no tool called no-such-tool"):
            self.change(config, "edit", name="designer", tool="no-such-tool")
        edited = self.change(config, "edit", name="designer", label="Screen designer", tool="")["role"]
        self.assertEqual((edited["label"], edited["description"], edited["tool"]), ("Screen designer", "You design.", ""))
        copy = self.change(config, "duplicate", name="designer")["role"]
        self.assertEqual((copy["name"], copy["label"]), ("designer-copy", "Screen designer (copy)"))
        self.assertEqual(self.change(config, "duplicate", name="designer")["role"]["name"], "designer-copy-2")
        self.assertEqual(self.change(config, "delete", name="designer-copy"), {"deleted": "designer-copy"})
        self.assertNotIn("designer-copy", config["roles"])

    def test_a_pack_role_can_be_changed_and_deleted_and_the_pack_changed(self):
        config = {}
        self.change(config, "edit", name="qa", label="Tester")
        self.assertEqual(crewchat.role_table(config, self.packs)["qa"]["source"], "yours")
        self.change(config, "delete", name="docs")
        self.assertEqual(config["roles"]["docs"], {"deleted": True})
        self.assertNotIn("docs", crewchat.role_table(config, self.packs))
        with self.assertRaisesRegex(crewchat.HubError, "no role called docs"):
            self.change(config, "edit", name="docs", label="Docs")
        listing, effects = crewchat.change_roles(config, {"op": "pack", "pack": "youtube"}, self.packs)
        self.assertEqual((config["pack"], config["roles"], effects), ("youtube", {}, {"changed": True}))
        self.assertEqual([r["name"] for r in listing["roles"]][:2], ["lead", "researcher"])
        with self.assertRaisesRegex(crewchat.HubError, "no pack called nope"):
            self.change(config, "pack", pack="nope")

    def test_export_and_import(self):
        source = {"pack": "business"}
        exported = self.change(source, "export")
        self.assertEqual((exported["crewchat"], exported["version"]), ("roles", 1))
        self.assertEqual(set(exported["roles"]), set(self.packs["business"]["roles"]))
        self.assertEqual(set(self.change(source, "export", names=["lead"])["roles"]), {"lead"})
        target = {}
        imported = self.change(target, "import", roles=json.loads(json.dumps(exported)))["imported"]
        self.assertIn("bookkeeping", imported)
        table = crewchat.role_table(target, self.packs)
        self.assertEqual(table["lead"]["label"], "Manager")  # an imported role replaces the pack's
        self.assertEqual(table["developer"]["label"], "Developer")  # the rest of the pack stays
        self.assertEqual(self.change({}, "import", roles=[{"name": "x", "description": "Do x."}])["imported"], ["x"])

    def test_a_malformed_import_keeps_nothing(self):
        for bad, why in ((None, "nothing to import"), ({}, "nothing to import"), ("text", "nothing to import"),
                         ([1, 2], "list of roles"), ({"ok": {"description": "Fine."}, "bad": {"label": "No text"}},
                                                     "bad has no description"),
                         ({"../up": {"description": "x"}}, "letters, digits"),
                         ({"r%d" % i: {"description": "x"} for i in range(51)}, "at most 50")):
            config = {}
            with self.assertRaisesRegex(crewchat.HubError, why):
                self.change(config, "import", roles=bad)
            self.assertFalse(config.get("roles"))

    def test_a_pack_saved_from_its_label_alone(self):
        config = {"pack": "software"}
        self.assertEqual(self.change(config, "save_pack", label="Night shift")["pack"]["id"], "night-shift")
        self.assertEqual(self.change(config, "save_pack", label="2024 crew")["pack"]["id"], "pack")
        with self.assertRaisesRegex(crewchat.HubError, "built-in"):
            self.change(config, "save_pack", label="YouTube")

    def test_a_saved_pack_is_replaced_only_when_asked(self):
        config = {"pack": "software"}
        packs = dict(self.packs, crew=dict(crewchat.clean_pack({"label": "Crew", "roles": {}}), saved=True))
        with self.assertRaisesRegex(crewchat.HubError, "already"):
            crewchat.change_roles(config, {"op": "save_pack", "label": "Crew!"}, packs)
        self.assertEqual(crewchat.change_roles(config, {"op": "save_pack", "label": "Crew!", "replace": True}, packs)[0]["pack"]["id"], "crew")

    def test_saved_packs(self):
        config = {"pack": "instagram"}
        self.change(config, "edit", name="captions", label="Copywriter")
        pack = self.change(config, "save_pack", id="My-Studio", label="My studio")["pack"]
        self.assertEqual((pack["id"], pack["label"], pack["crew"]), ("my-studio", "My studio", "general"))
        self.assertEqual(pack["roles"]["captions"]["label"], "Copywriter")
        with self.assertRaisesRegex(crewchat.HubError, "built-in"):
            self.change(config, "save_pack", id="youtube")
        with tempfile.TemporaryDirectory() as base:
            Path(base, "config.json").write_text(json.dumps({"packs": {
                "my-studio": pack, "youtube": pack, "bad": {"roles": "no"}, "../x": pack}}))
            packs = crewchat.role_packs(base)
            self.assertTrue(packs["my-studio"]["saved"])
            self.assertEqual(packs["youtube"], crewchat.ROLE_PACKS["youtube"])  # a built-in can't be replaced
            self.assertNotIn("bad", packs)
            self.assertNotIn("../x", packs)
            table = crewchat.role_table({"pack": "my-studio"}, packs)
            self.assertEqual(table["captions"]["label"], "Copywriter")
        self.assertEqual(crewchat.role_packs(os.path.join(TMP, "nowhere")), crewchat.ROLE_PACKS)

    def test_a_role_written_with_capitals_can_still_be_changed(self):
        config = {"roles": {"Designer": {"title": "Designer", "prompt": "You design."}}}
        self.assertIn("designer", crewchat.role_table(config, self.packs))
        self.assertEqual(self.change(config, "edit", name="Designer", label="Screens")["role"]["label"], "Screens")

    def test_roles_from_elsewhere_are_reduced_to_what_the_page_can_draw(self):
        row = crewchat.clean_role({"label": "  Two\n words " + "x" * 80, "description": "d" * 9000,
                                   "picture": "javascript:alert(1)", "colour": "red", "tool": "constructor"})
        self.assertEqual(len(row["label"]), crewchat.MAX_ROLE_LABEL)
        self.assertTrue(row["label"].startswith("Two words "))
        self.assertEqual(len(row["description"]), crewchat.MAX_ROLE_TEXT)
        self.assertEqual((row["picture"], row["colour"], row["tool"]), ("", "", ""))
        self.assertIsNone(crewchat.clean_role("not a dict"))


class Roles(Base):
    """The owner's /api/roles, and what agents and the page see of it."""

    def setUp(self):
        self.root = crewchat.home()
        self.saved = crewchat.load_config(self.root)

    def tearDown(self):
        crewchat.save_config(self.saved, self.root)
        mine = crewchat.load_config()
        mine.pop("packs", None)
        crewchat.save_config(mine)
        self.hub.roster.refresh(force=True)

    def op(self, token=None, **body):
        return self.op_at("/api/roles", token, **body)

    def poll(self):
        return self.hub.poll(-1, -1, 0)

    def test_only_the_owner_changes_roles(self):
        self.assertEqual(self.op(token=self.token, op="list")["status"], 403)
        listing = self.op(op="list")
        self.assertEqual(listing["pack"], "software")
        self.assertIn("youtube", [p["id"] for p in listing["packs"]])
        self.assertIn("op is", self.op(op="explode")["error"])

    def test_a_new_role_reaches_the_page_and_the_agents(self):
        role = self.op(op="create", name="designer", label="Designer", description="You design screens.",
                       picture="preset:palette", tool="cursor")["role"]
        self.assertEqual(role["name"], "designer")
        page = {r["name"]: r for r in self.poll()["roles"]}
        self.assertEqual((page["designer"]["title"], page["designer"]["tool"]), ("Designer", "cursor"))
        session, name = self.agent()
        crewchat.post_json(self.base + "/api/role", self.owner, {"agent": name, "role": "designer"})
        self.assertIn("You design screens.", session.text("hub_inbox"))
        self.assertIn("Your role: Designer.", session.text("hub_role"))

    def test_a_role_deleted_while_an_agent_has_it(self):
        session, name = self.agent()
        crewchat.post_json(self.base + "/api/role", self.owner, {"agent": name, "role": "docs"})
        session.text("hub_inbox")
        self.assertEqual(self.op(op="delete", name="docs"), {"deleted": "docs"})
        self.assertEqual(next(r for r in self.hub.rows() if r["agent"] == name)["role"], "")
        self.assertIn("You no longer have a role", session.text("hub_inbox"))
        self.assertNotIn("docs", [r["name"] for r in self.poll()["roles"]])
        self.assertIn("no role called docs", self.op(op="edit", name="docs", label="x")["error"])

    def test_a_pack_sets_the_crew_and_takes_away_roles_it_does_not_have(self):
        session, name = self.agent()
        crewchat.post_json(self.base + "/api/role", self.owner, {"agent": name, "role": "qa"})
        session.text("hub_inbox")
        self.assertEqual(self.poll()["crew"], "software")
        listing = self.op(op="pack", pack="youtube")
        self.assertEqual(listing["pack"], "youtube")
        poll = self.poll()
        self.assertEqual((poll["pack"], poll["crew"]), ("youtube", "general"))
        self.assertEqual(next(r for r in self.hub.rows() if r["agent"] == name)["role"], "")  # no qa in youtube
        self.assertIn("You no longer have a role", session.text("hub_inbox"))
        self.assertEqual(next(r for r in poll["roles"] if r["name"] == "lead")["title"], "Producer")
        self.op(op="pack", pack="software")
        self.assertEqual(self.poll()["crew"], "software")

    def test_a_role_another_machine_gave_is_kept_when_this_one_changes_its_roles(self):
        session, name = self.agent()
        with self.hub.lock:
            self.hub.agents[name]["role"] = "researcher"  # from a linked machine on the YouTube pack
        self.op(op="delete", name="docs")
        self.op(op="pack", pack="business")
        self.assertEqual(next(r for r in self.hub.rows() if r["agent"] == name)["role"], "researcher")
        self.assertNotIn("You no longer have a role", session.text("hub_inbox"))
        with self.hub.lock:
            self.hub.agents[name]["role"] = ""

    def test_the_command_line_without_a_server(self):
        with mock.patch.object(crewchat, "local_url", lambda config=None: "http://127.0.0.1:9"):  # nothing listens
            out = cli("roles", "add", "offline-role", "--prompt", "You work offline.")
            self.assertIn("Role offline-role saved", out)
            self.assertIn("agents keep the role", cli("roles", "remove", "offline-role"))
        self.assertNotIn("offline-role", crewchat.role_table(crewchat.load_config(self.root), crewchat.role_packs()))

    def test_a_request_of_the_wrong_shape_is_an_error_not_a_crash(self):
        self.assertIn("no tool called", self.op(op="create", name="x", description="x", tool=["cursor"])["error"])
        self.assertIn("names start with a letter", self.op(op="create", name=["x"], description="x")["error"])

    def test_the_owner_is_told_about_a_bad_picture_colour_or_tool(self):
        for field, value, why in (("picture", "preset:nope", "is not a picture"), ("colour", "red", "#rrggbb"),
                                  ("tool", "cursr", "no tool called cursr")):
            self.assertIn(why, self.op(op="create", name="x", description="x", **{field: value})["error"])
            self.assertIn(why, self.op(op="edit", name="qa", **{field: value})["error"])
        self.assertNotIn("x", [r["name"] for r in self.op(op="list")["roles"]])

    def test_the_owner_makes_a_project_from_the_page(self):
        made = crewchat.post_json(self.base + "/api/projects", self.owner, {"op": "create", "name": " Cooking  show ",
                                                                             "pack": "youtube"})
        self.assertEqual(made["name"], "Cooking show")
        found = crewchat.find_chat(made["id"])
        self.assertEqual(crewchat.load_config(found["root"])["pack"], "youtube")
        self.assertIn(made["id"], [p.id for p in self.server.RequestHandlerClass.projects.all()])
        for body, why in (({"op": "create", "name": "Cooking show"}, "already"), ({"op": "create", "name": ""}, "1 to 60"),
                          ({"op": "create", "name": "x", "pack": "nope"}, "no pack called nope"), ({"op": "drop"}, "op must be")):
            self.assertIn(why, self.op_at("/api/projects", **body)["error"])
        self.assertEqual(self.op_at("/api/projects", token=self.token, op="create", name="sneaky")["status"], 403)

    def op_at(self, path, token=None, **body):
        try:
            return crewchat.post_json(self.base + path, token or self.owner, body)
        except urllib.error.HTTPError as e:
            return dict(json.loads(e.read()), status=e.code)

    def test_save_a_pack_and_start_another_project_from_it(self):
        self.op(op="edit", name="qa", label="Tester")
        self.assertEqual(self.op(op="save_pack", id="studio", label="Studio")["pack"]["id"], "studio")
        self.assertIn("studio", [p["id"] for p in self.op(op="list")["packs"]])
        self.assertIn("studio", cli("roles", "packs"))

    def test_the_command_line(self):
        self.assertIn("Pack: software", cli("roles"))
        cli("roles", "add", "designer", "--prompt", "You design.", "--label", "Designer", "--picture", "palette",
            "--colour", "#112233", "--tool", "claude")
        row = crewchat.role_table(crewchat.load_config(self.root), crewchat.role_packs())["designer"]
        self.assertEqual((row["picture"], row["colour"], row["tool"]), ("preset:palette", "#112233", "claude"))
        cli("roles", "add", "designer", "--label", "Screen designer")  # a change keeps the instructions
        self.assertIn("Screen designer\n\nYou design.", cli("roles", "show", "designer"))
        path = os.path.join(TMP, "roles.json")
        cli("roles", "export", "designer", "--file", path)
        self.assertEqual(list(json.loads(Path(path).read_text())["roles"]), ["designer"])
        cli("roles", "export", "--file", path)
        self.assertIn("qa", json.loads(Path(path).read_text())["roles"])
        cli("roles", "remove", "designer")
        self.assertIn("Imported", cli("roles", "import", path))
        self.assertIn("designer", cli("roles"))
        cli("roles", "pack", "business")
        self.assertIn("Manager", cli("roles"))


class Activity(Base):
    """What an agent is doing, from its tool calls, in a few safe words."""

    def test_a_tool_call_in_a_few_safe_words(self):
        say = crewchat.tool_summary
        self.assertEqual(say("claude", {"tool_name": "Edit", "tool_input": {"file_path": "/x/app/Login.kt"}}), "Editing Login.kt")
        self.assertEqual(say("claude", {"tool_name": "Read", "tool_input": {"file_path": "/secret/dir/.env"}}), "Reading .env")
        self.assertEqual(say("claude", {"tool_name": "Bash", "tool_input": {"command": "TOKEN=abc npm test --token=xyz"}}),
                         "Running npm test")
        self.assertEqual(say("claude", {"tool_name": "Bash", "tool_input": {"command": "cd app && ./gradlew assemble"}}),
                         "Running gradlew assemble")
        self.assertEqual(say("claude", {"tool_name": "Bash", "tool_input": {"command": "curl -H 'Auth: s3cret' x"}}), "Running curl")
        self.assertEqual(say("claude", {"tool_name": "Grep", "tool_input": {"pattern": "password"}}), "Searching the code")
        self.assertEqual(say("claude", {"tool_name": "mcp__crewchat__hub_send"}), "")
        self.assertEqual(say("cursor", {"file_path": "/a/b/Theme.kt"}), "Editing Theme.kt")
        self.assertEqual(say("cursor", {"command": "pytest -q"}), "Running pytest")
        self.assertEqual(say("claude", {"tool_name": "<img src=x>"}), "")
        # Never an argument: only a known tool's subcommand; quotes and escapes read as the shell does.
        bash = lambda cmd: say("claude", {"tool_name": "Bash", "tool_input": {"command": cmd}})  # noqa: E731
        self.assertEqual(bash("echo hunter2 | tee x"), "Running echo")
        self.assertEqual(bash("cat credentials"), "Running cat")
        self.assertEqual(bash('PGPASSWORD="a; secretword more" psql -h db'), "Running psql")
        self.assertEqual(bash("X=a\\ secretpart npm test"), "Running npm test")
        self.assertEqual(bash("ghp_abcdefghijklmnopqrstuvwxyz0123456789"), "")
        self.assertEqual(bash("git commit -m 'a secret message'"), "Running git commit")
        self.assertEqual(bash("echo 'unbalanced"), "")
        for cmd, said in (("cd x\nnpm test", "Running npm test"), ("sudo npm install", "Running npm install"),
                          ("env X=1 npm test", "Running npm test"), ("timeout 60 npm test", "Running npm test"),
                          ("nohup npm start &", "Running npm start"), ("pytest secretdir", "Running pytest"),
                          ("npx some-internal-pkg", "Running npx"), ("make deploy-prod-db", "Running make")):
            self.assertEqual(bash(cmd), said, cmd)
        self.assertEqual(say("claude", {"tool_name": "Edit", "tool_input": {"file_path": "C:\\Users\\me\\Login.kt"}}),
                         "Editing Login.kt")
        self.assertEqual(say("claude", {"tool_name": "Read", "tool_input": {"file_path": ["a", "b"]}}), "Reading")
        self.assertEqual(say("cursor", {"tool_name": "hub_send"}), "")
        # Gemini CLI's and Codex's own tool names and arguments.
        self.assertEqual(say("gemini", {"tool_name": "replace", "tool_input": {"file_path": "/x/Login.kt"}}), "Editing Login.kt")
        self.assertEqual(say("gemini", {"tool_name": "read_file", "tool_input": {"absolute_path": "/x/.env"}}), "Reading .env")
        self.assertEqual(say("gemini", {"tool_name": "run_shell_command", "tool_input": {"command": "npm test --x=1"}}),
                         "Running npm test")
        self.assertEqual(say("qwen", {"tool_name": "edit", "tool_input": {"file_path": "/x/App.kt"}}), "Editing App.kt")
        self.assertEqual(say("opencode", {"tool_name": "read", "tool_input": {"filePath": "/x/main.py"}}), "Reading main.py")
        self.assertEqual(say("opencode", {"tool_name": "bash", "tool_input": {"command": "npm test"}}), "Running npm test")
        self.assertEqual(say("codex", {"tool_name": "shell", "tool_input": {"command": ["bash", "-lc", "pytest -q x"]}}),
                         "Running pytest")
        self.assertEqual(say("codex", {"tool_name": "exec_command", "tool_input": {"command": ["git", "status"]}}),
                         "Running git status")
        self.assertEqual(say("cursor", {"tool_name": "browser_navigate", "command": "npx @playwright/mcp"}), "Using browser_navigate")
        for leaked in ("abc", "xyz", "s3cret", "password", "/x/app", "/secret"):
            for out in (say("claude", {"tool_name": "Bash", "tool_input": {"command": "TOKEN=abc npm test --token=xyz"}}),
                        say("claude", {"tool_name": "Bash", "tool_input": {"command": "curl -H 'Auth: s3cret' x"}})):
                self.assertNotIn(leaked, out)

    def test_the_page_shows_it_while_fresh(self):
        session, name = self.agent()
        key = "k" * 16
        session.text("hub_link", key=key)
        hook = lambda **body: crewchat.post_json(self.base + "/api/hook", self.token, dict({"key": key}, **body))  # noqa: E731
        hook(event="tool", doing="Editing Login.kt")
        row = lambda: next(r for r in self.hub.rows() if r["agent"] == name)  # noqa: E731
        self.assertEqual(row()["doing"], "Editing Login.kt")
        with self.hub.lock:
            self.hub.agents[name]["doing_at"] -= crewchat.DOING_FRESH + 1
        self.assertEqual(row()["doing"], "")  # old news
        hook(event="tool", doing="Running pytest")
        hook(event="stop", wait=0)  # a turn ended: what it last did is old news too
        self.assertEqual(row()["doing"], "")
        config = crewchat.load_config()
        config["activity"] = "off"
        crewchat.save_config(config)
        self.hub.roster.refresh(force=True)
        try:
            hook(event="tool", doing="Editing Secret.kt")
            self.assertEqual(row()["doing"], "")  # the owner keeps it off the page
        finally:
            config.pop("activity")
            crewchat.save_config(config)
            self.hub.roster.refresh(force=True)


class NeedsYou(Base):
    """What waits on the owner: blocked tasks, reviews, tasks given back, unread messages."""

    def add(self, title, **kw):
        return crewchat.post_json(self.base + "/api/task", self.owner, dict({"op": "add", "title": title}, **kw))["id"]

    def test_what_needs_the_owner_and_in_what_order(self):
        a, a_name = self.agent()
        b, b_name = self.agent()
        api = self.add("Design the API")
        client = self.add("Build the client", depends=api)
        self.add("Ship it", depends=client)
        review = self.add("Write the changelog")
        back = self.add("Clean the logs")
        a.text("hub_take", id=api)
        a.text("hub_update", id=api, status="blocked", note="Which auth: tokens or cookies?")
        b.text("hub_take", id=review)
        b.text("hub_update", id=review, status="review", note="Ready for a look")
        b.text("hub_take", id=back)
        b.text("hub_update", id=back, status="todo", note="No time, sorry")
        a.text("hub_send", to="Owner", text="A question for you")
        needs = self.hub.needs_you()
        kinds = [(n["kind"], n.get("task") or n.get("text")) for n in needs]
        self.assertEqual(kinds[:4], [("blocked", api), ("review", review), ("message", "A question for you"),
                                     ("given_back", back)])
        blocked = needs[0]
        self.assertEqual((blocked["agent"], blocked["text"], blocked["blocking"]), (a_name, "Which auth: tokens or cookies?", 2))
        self.assertEqual(needs[3]["text"], "No time, sorry")
        # Once handled, an item leaves the list.
        a.text("hub_update", id=api, status="in_progress", note="Going with tokens")
        crewchat.post_json(self.base + "/api/task", self.owner, {"op": "status", "task": review, "status": "done"})
        msg = needs[2]
        self.assertEqual(crewchat.post_json(self.base + "/api/read", self.owner, {"seq": msg["seq"]})["read"], msg["seq"])
        left = [n["kind"] for n in self.hub.needs_you()]
        self.assertNotIn("blocked", left)
        self.assertNotIn("review", left)
        self.assertNotIn("message", left)
        self.assertIn("given_back", left)
        b.text("hub_take", id=back)
        self.assertEqual(self.hub.needs_you(), [])

    def test_an_answered_question_waits_on_the_agent_not_the_owner(self):
        a, a_name = self.agent()
        mid = self.add("Pick the icons")
        a.text("hub_take", id=mid)
        a.text("hub_update", id=mid, status="blocked", note="Outline or filled?")
        self.assertIn(mid, [n.get("task") for n in self.hub.needs_you()])
        self.hub.send(crewchat.OWNER, a_name, "Re #%s: Outline" % mid)  # as the answer box in Needs you sends it
        self.assertNotIn(mid, [n.get("task") for n in self.hub.needs_you()])  # the agent's turn now
        self.hub.send(crewchat.OWNER, a_name, "Re #%s1: about another task" % mid)
        a.text("hub_update", id=mid, status="blocked", note="Which size?")  # asks again: it needs the owner again
        self.assertIn(mid, [n.get("task") for n in self.hub.needs_you()])
        b, b_name = self.agent()
        self.hub.send(crewchat.OWNER, b_name, "Re #%s: Small" % mid)  # to another agent: not an answer to this one
        self.hub.send(crewchat.OWNER, "all", "Re #%s: Small" % mid)
        self.assertIn(mid, [n.get("task") for n in self.hub.needs_you()])
        review = self.add("Polish the icons")
        a.text("hub_take", id=review)
        a.text("hub_update", id=review, status="review", note="Have a look")
        self.hub.send(crewchat.OWNER, a_name, "Re #%s: thanks" % review)
        self.assertIn(review, [n.get("task") for n in self.hub.needs_you()])  # a review waits for Mark done
        for t in (mid, review):
            crewchat.post_json(self.base + "/api/task", self.owner, {"op": "status", "task": t, "status": "done"})

    def test_a_note_is_credited_to_the_agent_that_wrote_it(self):
        a, a_name = self.agent()
        b, b_name = self.agent()
        mid = self.add("Migrate the database")
        a.text("hub_take", id=mid)
        b.text("hub_update", id=mid, status="blocked", note="OK to drop the old tables?")  # not b's task
        item = next(n for n in self.hub.needs_you() if n.get("task") == mid)
        self.assertEqual((item["agent"], item["holder"], item["text"]), (b_name, a_name, "OK to drop the old tables?"))
        a.text("hub_update", id=mid, status="in_progress", note="carrying on")

    def test_what_the_owner_blocked_is_not_listed_back(self):
        mid = self.add("Wait for the designer")
        crewchat.post_json(self.base + "/api/task", self.owner, {"op": "status", "task": mid, "status": "blocked"})
        self.assertNotIn(mid, [n.get("task") for n in self.hub.needs_you()])
        crewchat.post_json(self.base + "/api/task", self.owner, {"op": "remove", "task": mid})

    def test_a_long_chain_of_tasks_is_counted_without_recursion(self):
        first = prev = self.add("Step 0")
        for i in range(1, 1200):
            prev = self.add("Step %d" % i, depends=prev)
        a, _ = self.agent()
        a.text("hub_take", id=first)
        a.text("hub_update", id=first, status="blocked", note="stuck")
        item = next(n for n in self.hub.needs_you() if n.get("task") == first)
        self.assertEqual(item["blocking"], 1199)
        a.text("hub_update", id=first, status="done", note="unstuck")

    def test_the_poll_and_the_tabs_say_so(self):
        a, _ = self.agent()
        a.text("hub_send", to="Owner", text="Look at this")
        poll = json.loads(self.raw("/api/poll?after=-1&v=-1&wait=0", opener=self.owner_browser()[0])[1])
        self.assertTrue(any(n["kind"] == "message" for n in poll["needs"]))
        self.assertGreaterEqual(next(p for p in poll["projects"] if p["id"] == poll["chat"])["needs"], 1)
        self.assertIn("seq is", self.op_at("/api/read", seq="x")["error"])
        self.assertEqual(self.op_at("/api/read", token=self.token, seq=1)["status"], 403)
        crewchat.post_json(self.base + "/api/read", self.owner, {"seq": 10 ** 9})  # never past the last message
        self.assertEqual(self.hub.owner_cursor, self.hub.next_seq - 1)

    def op_at(self, path, token=None, **body):
        try:
            return crewchat.post_json(self.base + path, token or self.owner, body)
        except urllib.error.HTTPError as e:
            return dict(json.loads(e.read()), status=e.code)


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
        # Only from the agent's own project folder: not ~/.ssh, not another folder on the machine.
        elsewhere = Path(tempfile.mkdtemp(dir=TMP)) / "id_rsa"
        elsewhere.write_text("not a real key")
        self.assertIn("outside your project folder", session.text("hub_send", to=other_name, text="x",
                                                                  files=[str(elsewhere)]))
        doc = shot.parent / "notes" / "../notes.txt"
        (shot.parent / "notes").mkdir()
        (shot.parent / "notes.txt").write_text("plan")
        self.assertIn("with 1 file(s)", session.text("hub_send", to=other_name, text="x", files=[str(doc)]))
        # Credentials never, even inside the project, nor from the home folder's key stores.
        (shot.parent / ".netrc").write_text("machine x login y password z")
        self.assertIn("holds credentials", session.text("hub_send", to=other_name, text="x",
                                                        files=[str(shot.parent / ".netrc")]))
        fake_home = shot.parent  # as if the project folder were the user's home
        (fake_home / ".ssh").mkdir()
        (fake_home / ".ssh" / "id_ed25519").write_text("not a real key")
        with mock.patch.object(Path, "home", return_value=fake_home):
            self.assertIn("holds credentials", session.text("hub_send", to=other_name, text="x",
                                                            files=[str(fake_home / ".ssh" / "id_ed25519")]))

    def test_a_removed_place_takes_its_folders_with_it(self):
        # Its name can be given out again; the new place must not inherit the old folder.
        place, _ = self.join_place("old-laptop")
        folder = Path(tempfile.mkdtemp(dir=TMP))
        crewchat.remember(folder, place)
        crewchat.post_json(self.base + "/api/admin", self.owner, {"op": "remove-place", "place": place})
        self.assertNotIn(str(folder), crewchat.load_config().get("folders") or {})

    def test_an_agent_cannot_share_crewchats_keys(self):
        # Agents on linked machines can open a shared file, so a talked-into agent must not leak a token.
        session, name = self.agent()
        folder = Path(tempfile.mkdtemp(dir=TMP))
        crewchat.remember(folder, self.place)  # the agent's folder is on this machine
        inside = [crewchat.token_path("Owner"), crewchat.home() / "config.json"]
        upper = Path(str(crewchat.home()).upper()) / "config.json"
        if upper.exists():  # a case-insensitive disk (macOS, Windows): the same folder by another name
            inside.append(upper)
        link = folder / "notes.txt"
        try:
            link.symlink_to(crewchat.token_path("Owner"))  # a harmless-looking name for the owner's token
            inside.append(link)
        except OSError:
            pass  # Windows without the right to make symlinks
        for path in inside:
            self.assertIn("crewchat's own folder", session.text("hub_send", to=name, text="x", files=[str(path)]))
        config = folder / ".mcp.json"
        config.write_text(json.dumps({"headers": {"Authorization": "Bearer " + self.token}}))
        copy = folder / "backup.txt"
        copy.write_text("old token: %s\n" % crewchat.read_token("Owner"))
        for path in (config, copy):
            self.assertIn("contains a crewchat token", session.text("hub_send", to=name, text="x", files=[str(path)]))
        shot = folder / "shots" / "2026-10-13.png"
        shot.parent.mkdir()
        shot.write_bytes(PNG)
        latest = folder / "latest.png"
        try:
            latest.symlink_to(shot)
        except OSError:
            latest = shot
        self.assertIn("with 1 file", session.text("hub_send", to="all", text="x", files=[str(latest)]))
        self.assertEqual(self.hub.history(1)[0]["files"][0]["name"], latest.name)  # the name the agent gave

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
        self.real_launch_agent = crewchat.launch_agent
        self.models, self.advisors = [], []
        crewchat.launch_agent = lambda tool, folder, key, permissions="ask", root=None, model="", advisor="": (
            self.launched.append((tool, folder, key, permissions)), self.models.append(model),
            self.advisors.append(advisor))
        crewchat.find_tool = lambda tool, config: "/usr/local/bin/" + tool
        self.folder = Path(tempfile.mkdtemp(prefix="launch-", dir=TMP)).resolve()
        subprocess.run(["git", "init", "-q", str(self.folder)], check=False)
        cli("start", str(self.folder), "--no-open", "--no-service", "--place", "Studio", "--chat", "demo")
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
        self.assertEqual([t["id"] for t in options["tools"]], ["claude", "cursor", "codex", "gemini", "qwen", "opencode"])
        self.assertEqual(self.raw("/api/launch")[0], 401)

    def test_a_launched_agent_gets_its_name_role_and_task(self):
        self.assertEqual(self.launch(name="docs-writer", role="docs", task="Write the settings guide"), {"ok": True})
        tool, folder, key, permissions = self.launched[-1]
        self.assertEqual((tool, folder, permissions), ("claude", str(self.folder), "ask"))
        self.assertTrue(key.startswith("start-"))
        self.assertIn("Owner started a new Claude Code agent in %s as docs-writer" % self.folder.name,
                      [m["text"] for m in self.hub.history(5)])
        # The new session checks in with its key, as the launch prompt tells it to.
        token = crewchat.read_token(self.place)
        session = Session(self.base, token)
        out = session.text("hub_link", key=key)
        self.assertIn("You are docs-writer, in the project Demo, started from the crewchat by the owner.", out)
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
        # The answer can be lost on the way (a connection reset), and the agent's tool retries: the
        # same session, or a new connection from the same folder, gets the same answer and agent.
        self.assertIn("You are docs-writer", session.text("hub_link", key=key))
        retry = Session(self.base, token)
        self.assertIn("Your role's instructions and your first task are in hub_inbox", retry.text("hub_link", key=key))
        self.assertEqual(retry.me, "docs-writer")
        # A start key works for that one agent only: another folder cannot use it.
        place, other = self.join_place("elsewhere-launch")
        again = Session(self.base, other).call("hub_link", key=key)["content"][0]["text"]
        self.assertIn("unknown or has expired", again)
        with self.hub.lock:
            self.hub.redeemed[key]["until"] = 0  # ten minutes on, it is spent for good
        self.assertIn("unknown or has expired", Session(self.base, token).text("hub_link", key=key))

    def test_new_agent_with_a_model_a_picture_a_colour_and_instructions(self):
        out = self.launch(name="picture-tester", role="qa", task="Test the login", model="sonnet", picture="preset:flask",
                          colour="#AABBCC", instructions="Ask before touching api/.")
        self.assertEqual(out, {"ok": True})
        self.assertEqual(self.models[-1], "sonnet")
        key = self.launched[-1][2]
        session = Session(self.base, crewchat.read_token(self.place))
        note = session.text("hub_link", key=key)
        self.assertIn("the owner's instructions", note)
        row = next(r for r in self.hub.rows() if r["agent"] == "picture-tester")
        self.assertEqual((row["picture"], row["colour"], row["permissions"]), ("preset:flask", "#aabbcc", "ask"))
        self.assertEqual(self.hub.agents["picture-tester"]["model"], "sonnet")  # Reopen starts it on the same model
        inbox = session.text("hub_inbox")
        self.assertIn("Ask before touching api/.", inbox)
        self.assertLess(inbox.index("Ask before touching api/."), inbox.index("Test the login"))  # before the task
        self.assertIn("no model called gpt", self.launch(model="gpt")["error"])
        self.assertIn("no model called sonnet", self.launch(tool="cursor", model="sonnet")["error"])
        self.assertIn("no picture preset", self.launch(picture="preset:nope")["error"])
        self.assertIn("#rrggbb", self.launch(colour="red")["error"])
        self.assertEqual(crewchat.launch_command("claude", "claude", "start-x", model="opus")[-3:-1], ["--model", "opus"])

    def test_new_agent_with_claude_codes_own_advisor(self):
        out = self.launch(name="advised", model="sonnet", advisor="opus")
        self.assertEqual(out, {"ok": True})
        self.assertEqual(self.advisors[-1], "opus")
        Session(self.base, crewchat.read_token(self.place)).call("hub_link", key=self.launched[-1][2])
        self.assertEqual(self.hub.agents["advised"]["advisor"], "opus")  # Reopen starts it with the same advisor
        self.assertEqual(self.launch(name="unadvised"), {"ok": True})
        self.assertEqual(self.advisors[-1], "")  # none unless chosen
        for bad in ("haiku", "opus; rm -rf ~", "gpt"):
            self.assertIn("no advisor called", self.launch(advisor=bad)["error"])
        self.assertIn("no advisor called opus", self.launch(tool="cursor", advisor="opus")["error"])
        self.assertEqual(crewchat.launch_command("claude", "claude", "start-x", model="sonnet", advisor="opus")[-5:-1],
                         ["--model", "sonnet", "--advisor", "opus"])
        self.assertNotIn("--advisor", crewchat.launch_command("claude", "claude", "start-x"))
        self.assertEqual([m[0] for m in crewchat.tool_advisors("claude")], ["fable", "opus", "sonnet"])
        self.assertEqual(crewchat.tool_advisors("cursor"), [])

    def agent_op(self, **body):
        try:
            return crewchat.post_json(self.base + "/api/agent", self.owner, body)
        except urllib.error.HTTPError as e:
            return {"status": e.code, "error": json.loads(e.read())["error"]}

    def linked(self, **launch):
        self.assertEqual(self.launch(**launch), {"ok": True})
        session = Session(self.base, crewchat.read_token(self.place))
        session.text("hub_link", key=self.launched[-1][2])
        return session.me

    def test_a_new_agent_gets_a_random_3d_picture_unless_initials_are_asked_for(self):
        pictures = [self.hub.agents[self.linked()]["picture"] for _ in range(4)]
        self.assertTrue(all(p.startswith("art:") and p[4:] in crewchat.ART_PICTURES for p in pictures), pictures)
        self.assertEqual(len(set(pictures)), 4)  # one nobody here has, while any is free
        self.assertEqual(self.hub.agents[self.linked(picture="initials")]["picture"], "")
        self.assertEqual(self.hub.agents[self.linked(picture="art:owl")]["picture"], "art:owl")

    def test_an_agent_with_no_recorded_session_starts_again_as_itself(self):
        name = self.linked(role="docs")
        self.assertIsNone(self.hub.agents[name].get("session"))  # its hooks never reported one
        row = lambda: next(r for r in self.hub.rows() if r["agent"] == name)  # noqa: E731
        self.assertTrue(row()["startable"])
        self.assertEqual(self.agent_op(op="start", agent=name), {"ok": True})
        self.assertFalse(row()["startable"])  # started a moment ago: not twice
        self.assertIn("is running or starting already", self.agent_op(op="start", agent=name)["error"])
        tool, folder, again, _ = self.launched[-1]
        self.assertEqual((tool, folder), ("claude", str(self.folder)))  # in its folder
        self.assertEqual(self.hub.links[again], name)  # with a link key of its own (its start key has expired)
        self.assertFalse(again.startswith("start-"))
        self.assertIn("Owner started a new session for %s in %s" % (name, self.folder.name),
                      [m["text"] for m in self.hub.history(5)])
        session = Session(self.base, crewchat.read_token(self.place))
        self.assertIn("This session is %s again" % name, session.text("hub_link", key=again))
        self.assertEqual(session.me, name)
        self.assertEqual(self.hub.agents[name]["role"], "docs")
        self.assertNotIn("starting", self.hub.agents[name])  # it checked in: no more waiting for it
        with self.hub.lock:
            self.hub.waiting[name] = 1  # its stop hook is waiting for messages: it is open
        try:
            self.assertFalse(row()["startable"])
            self.assertIn("is running or starting already", self.agent_op(op="start", agent=name)["error"])
        finally:
            with self.hub.lock:
                self.hub.waiting.pop(name, None)
        self.assertEqual(self.agent_op(op="start", agent="nobody")["status"], 400)
        with self.hub.lock:  # a recorded conversation, closed: Reopen is the way back
            self.hub.agents[name]["session"] = {"id": "0" * 8 + "-0000-0000-0000-" + "0" * 12, "pid": 999999,
                                                "started": "x", "app": "terminal", "tool": "claude", "since": 0}
        try:
            self.assertIn("use Reopen", self.agent_op(op="start", agent=name)["error"])
        finally:
            with self.hub.lock:
                self.hub.agents[name].pop("session", None)

    def test_a_start_the_server_runs_never_starts_or_registers_a_server(self):
        saved, up = os.environ.get("CREWCHAT_FROM_SERVER"), crewchat.server_up
        os.environ["CREWCHAT_FROM_SERVER"] = "1"
        crewchat.server_up = lambda config: False  # the server did not answer the child in time
        before = crewchat.load_config()
        try:
            with self.assertRaises(SystemExit) as e, contextlib.redirect_stdout(io.StringIO()):
                crewchat.ensure_server(dict(before), argparse.Namespace(no_service=True, keep_awake=False))
            self.assertIn("did not answer", str(e.exception))
            self.assertEqual(crewchat.load_config().get("service"), before.get("service"))  # not turned off
        finally:
            crewchat.server_up = up
            if saved is None:
                os.environ.pop("CREWCHAT_FROM_SERVER", None)
            else:
                os.environ["CREWCHAT_FROM_SERVER"] = saved

    def test_a_folder_is_connected_from_the_page(self):
        def connect(path):
            try:
                return crewchat.post_json(self.base + "/api/folders", self.owner, {"op": "connect", "path": path})
            except urllib.error.HTTPError as e:
                return {"status": e.code, "error": json.loads(e.read())["error"]}
        self.assertIn("type the folder's path", connect("  ")["error"])
        self.assertIn("full path", connect("code/app")["error"])
        self.assertIn("there is no folder", connect(str(self.folder / "missing"))["error"])
        self.assertIn("a project's own folder", connect("~")["error"])
        self.assertIn("a project's own folder", connect(str(Path.home().parent))["error"])  # above home too
        self.assertIn("a project's own folder", connect(str(crewchat.home()))["error"])
        self.assertIn("a project's own folder", connect(str(crewchat.home() / "tokens"))["error"])
        folder = Path(tempfile.mkdtemp(prefix="connect-", dir=TMP)).resolve()
        out = connect(str(folder))  # `crewchat start` in it
        self.assertEqual(out["path"], str(folder))
        self.assertIn(str(folder), [f["path"] for f in out["folders"]])
        opener, _ = self.owner_browser()
        options = json.loads(self.raw("/api/launch", opener=opener)[1])
        self.assertIn(str(folder), [f["path"] for f in options["folders"]])
        elsewhere = Path(tempfile.mkdtemp(prefix="elsewhere-", dir=TMP)).resolve()
        cli("start", str(elsewhere), "--no-open", "--no-service", "--chat", "connect-other")
        self.assertIn("is in the project \"connect-other\"", connect(str(elsewhere))["error"])  # one project per folder

    def test_browse_and_make_folders_from_the_page(self):
        def op(**body):
            try:
                return crewchat.post_json(self.base + "/api/folders", self.owner, body)
            except urllib.error.HTTPError as e:
                return {"status": e.code, "error": json.loads(e.read())["error"]}
        top = Path(tempfile.mkdtemp(prefix="browse-", dir=TMP)).resolve()
        for name in ("beta", "Alpha", ".git"):
            (top / name).mkdir()
        (top / "notes.txt").write_text("a file, not a folder")
        out = op(op="list", path=str(top))
        self.assertEqual(out["path"], str(top))
        self.assertEqual([f["name"] for f in out["folders"]], ["Alpha", "beta"])  # no hidden ones, no files
        self.assertEqual(out["folders"][0]["path"], str(top / "Alpha"))
        self.assertEqual(out["parent"], str(top.parent))
        self.assertEqual(op(op="list")["path"], str(Path.home().resolve()))  # home to begin with
        nearest = op(op="list", path=str(top / "nope" / "deeper"))  # not there yet: the nearest folder above it
        self.assertEqual((nearest["path"], nearest["missing"]), (str(top), str(top / "nope" / "deeper")))
        self.assertEqual(out["missing"], "")
        # crewchat's own folder is not offered, nor can a folder be made in it.
        self.assertNotIn(str(crewchat.home().resolve()), [f["path"] for f in op(op="list", path=str(crewchat.home().parent))["folders"]])
        self.assertIn("crewchat's own folder", op(op="mkdir", path=str(crewchat.home()), name="x")["error"])
        made = op(op="mkdir", path=str(top), name="my app")
        self.assertEqual(made, {"path": str(top / "my app")})
        self.assertTrue((top / "my app").is_dir())
        self.assertIn("already", op(op="mkdir", path=str(top), name="my app")["error"])
        for bad in ("", "..", "a/b", "a\\b", "c:d", ".hidden", "app.", "a*b", 'a"b', "a|b"):
            self.assertIn("folder's name", op(op="mkdir", path=str(top), name=bad)["error"], bad)
        self.assertEqual(op(op="rename")["status"], 400)
        # Connect makes a missing folder when asked to (not its missing parents).
        self.assertIn("there is no folder", op(op="connect", path=str(top / "new-app"))["error"])
        self.assertIn("could not make", op(op="connect", path=str(top / "a" / "b"), create=True)["error"])
        out = op(op="connect", path=str(top / "new-app"), create=True)
        self.assertEqual(out["path"], str(top / "new-app"))
        self.assertIn(str(top / "new-app"), [f["path"] for f in out["folders"]])
        self.assertTrue((top / "new-app" / ".mcp.json").is_file())  # connected: crewchat start ran in it
        self.assertEqual(self.raw("/api/folders", data=b'{"op": "list"}')[0], 401)  # the owner's only

    def test_more_agent_tools_join_by_their_mcp_settings(self):
        folder = Path(tempfile.mkdtemp(prefix="more-tools-", dir=TMP)).resolve()
        subprocess.run(["git", "init", "-q", str(folder)], check=False)
        (folder / "opencode.json").write_text(json.dumps({"theme": "mine"}))  # the user's own settings stay
        cli("start", str(folder), "--no-open", "--no-service", "--chat", "demo")  # every tool is "installed" here
        place, token, clients = crewchat.joined_place(folder)
        self.assertEqual(clients, ["claude", "cursor", "gemini", "qwen", "opencode"])  # Codex's chat is the .mcp.json
        gemini = json.loads((folder / ".gemini" / "settings.json").read_text())["mcpServers"]["crewchat"]
        self.assertEqual(gemini, {"httpUrl": self.base + "/mcp", "headers": {"Authorization": "Bearer " + token}, "trust": True})
        self.assertEqual(json.loads((folder / ".qwen" / "settings.json").read_text())["mcpServers"]["crewchat"]["trust"], True)
        opencode = json.loads((folder / "opencode.json").read_text())
        self.assertEqual(opencode["theme"], "mine")
        self.assertEqual(opencode["mcp"]["crewchat"], {"url": self.base + "/mcp", "type": "remote", "enabled": True,
                                                      "headers": {"Authorization": "Bearer " + token}})
        self.assertEqual(crewchat.client_config(folder, "gemini"), (self.base, token))
        excluded = (folder / ".git" / "info" / "exclude").read_text()
        for rel in (".gemini/settings.json", ".qwen/settings.json", "opencode.json"):  # the token stays out of git
            self.assertIn(rel, excluded)
        # Codex: once, in its own settings, `crewchat stdio` (which reads this folder's .mcp.json).
        codex = crewchat.codex_config().read_text()
        self.assertIn("[mcp_servers.crewchat]", codex)
        self.assertIn('"stdio", "--local-only"]', codex)
        self.assertEqual(codex.count("[mcp_servers.crewchat]"), 1)
        # Run again with a tool asked for (or installed since), a connected folder gets it too.
        shutil.rmtree(folder / ".qwen")
        cli("start", str(folder), "--no-open", "--no-service", "--chat", "demo", "--client", "qwen")
        self.assertEqual(crewchat.connection_entry(folder, "qwen")[1], token)
        # Taken out again, leaving the user's own.
        crewchat.disconnect_folder(folder)
        self.assertFalse((folder / ".gemini" / "settings.json").exists())
        self.assertEqual(json.loads((folder / "opencode.json").read_text()), {"theme": "mine"})

    def test_a_committed_settings_file_gets_no_token(self):
        folder = Path(tempfile.mkdtemp(prefix="committed-", dir=TMP)).resolve()
        git = lambda *a: subprocess.run(["git", "-C", str(folder)] + list(a), capture_output=True, check=True)  # noqa: E731
        git("init", "-q")
        (folder / "opencode.json").write_text('{"theme": "team"}')
        git("add", "opencode.json")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "team settings")
        out = cli("start", str(folder), "--no-open", "--no-service", "--chat", "demo", "--client", "opencode")
        self.assertIn("opencode.json is committed", out)
        self.assertEqual((folder / "opencode.json").read_text(), '{"theme": "team"}')

    def test_a_tool_connected_later_is_added_at_launch_or_refused(self):
        tool = lambda t, c: "/bin/" + t  # noqa: E731  (every tool installed)
        shutil.rmtree(self.folder / ".gemini")  # as if Gemini CLI was installed after the folder joined
        self.assertNotIn("gemini", crewchat.joined_place(self.folder)[2])
        with mock.patch.object(crewchat, "find_tool", side_effect=tool), \
                mock.patch.object(crewchat, "tool_help", return_value=""):
            crewchat.agent_tool("gemini", str(self.folder), "ask", self.hub.home)
            self.assertIn("gemini", crewchat.joined_place(self.folder)[2])  # its settings got crewchat
            with mock.patch.object(crewchat, "install_client", return_value=[]):
                shutil.rmtree(self.folder / ".qwen", ignore_errors=True)
                with self.assertRaises(crewchat.HubError) as e:
                    crewchat.agent_tool("qwen", str(self.folder), "ask", self.hub.home)
                self.assertIn("could not add itself to Qwen Code's settings", str(e.exception))

    def test_stdio_finds_the_chat_in_any_tools_settings(self):
        folder = Path(tempfile.mkdtemp(prefix="gemini-only-", dir=TMP)).resolve()
        cli("start", str(folder), "--no-open", "--no-service", "--chat", "demo", "--client", "gemini")
        self.assertFalse((folder / ".mcp.json").exists())
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "codex-mcp-client"}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        env = dict(os.environ, HOME=str(crewchat.user_home()), USERPROFILE=str(crewchat.user_home()))
        done = subprocess.run([sys.executable, str(ROOT / "crewchat.py"), "stdio", "--local-only", "--project", str(folder)],
                              input=("\n".join(json.dumps(r) for r in requests) + "\n").encode(), capture_output=True,
                              env=env, timeout=60)
        answers = [json.loads(line) for line in done.stdout.decode().splitlines() if line.strip()]
        tools = next(a for a in answers if a.get("id") == 2)["result"]["tools"]
        self.assertIn("hub_link", [t["name"] for t in tools], done.stderr)

    def test_codex_settings_get_one_block_of_crewchats_own(self):
        path = crewchat.codex_config()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('model = "gpt-6"\n\n[mcp_servers.other]\ncommand = "other"\n')
        self.assertTrue(crewchat.install_codex())
        self.assertFalse(crewchat.install_codex())  # already there
        text = path.read_text()
        self.assertTrue(text.startswith('model = "gpt-6"\n\n[mcp_servers.other]\ncommand = "other"\n\n# crewchat: begin'))
        with mock.patch.object(crewchat, "run_command", return_value=["/tools/crewchat/bin/crewchat"]):
            self.assertTrue(crewchat.install_codex())  # brought up to this copy's command
        self.assertIn('command = "/tools/crewchat/bin/crewchat"\nargs = ["stdio", "--local-only"]', path.read_text())
        self.assertEqual(path.read_text().count("[mcp_servers.crewchat]"), 1)
        try:
            import tomllib  # Python 3.11+: Codex must still be able to read its settings
        except ImportError:
            tomllib = None
        if tomllib:
            with mock.patch.object(crewchat, "run_command", return_value=['C:\\Users\\Ünï "q"\\crewchat.exe']):
                crewchat.install_codex()
            parsed = tomllib.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(parsed["mcp_servers"]["crewchat"], {"command": 'C:\\Users\\Ünï "q"\\crewchat.exe',
                                                                 "args": ["stdio", "--local-only"]})
            self.assertEqual(parsed["mcp_servers"]["other"], {"command": "other"})
        self.assertTrue(crewchat.uninstall_codex())
        self.assertEqual(path.read_text(), 'model = "gpt-6"\n\n[mcp_servers.other]\ncommand = "other"\n')
        self.assertFalse(crewchat.uninstall_codex())
        for theirs in ('[mcp_servers.crewchat]\ncommand = "uvx"\n', 'mcp_servers = { crewchat = { command = "x" } }\n',
                       '[mcp_servers."crewchat"]\ncommand = "x"\n', 'mcp_servers.crewchat.command = "x"\n',
                       '[mcp_servers]\ncrewchat = { command = "x" }\n', '[mcp_servers.crewchat.env]\nA = "b"\n',
                       'mcp_servers = { other = { command = "x" } }\n'):
            path.write_text(theirs)
            self.assertFalse(crewchat.install_codex())  # a server the user set up is theirs
            self.assertEqual(path.read_text(), theirs)
        path.unlink()

    def test_codex_runs_crewchats_hooks_and_keeps_its_review_of_them(self):
        path = crewchat.codex_config()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
        self.assertTrue(crewchat.install_codex())
        text = path.read_text()
        for event in ("UserPromptSubmit", "Stop", "PostToolUse"):
            self.assertEqual(text.count("[[hooks.%s]]" % event), 1, event)
        try:
            import tomllib
        except ImportError:
            tomllib = None
        if tomllib:
            stop = tomllib.loads(text)["hooks"]["Stop"][0]["hooks"][0]
            self.assertEqual(stop, {"type": "command", "command": crewchat.hook_command("codex", "stop"),
                                    "timeout": crewchat.MAX_LISTEN + 100})
        # Codex keeps the owner's review of the hooks in our block; a newer copy keeps it too.
        state = '[hooks.state."crewchat:stop"]\ntrusted_hash = "abc"\n'
        path.write_text(text.replace(crewchat.CODEX_END, state + crewchat.CODEX_END))
        with mock.patch.object(crewchat, "run_command", return_value=["/tools/crewchat/bin/crewchat"]):
            self.assertTrue(crewchat.install_codex())
            self.assertFalse(crewchat.install_codex())
        self.assertEqual(path.read_text().count(state), 1)
        self.assertEqual(path.read_text().count(crewchat.CODEX_END), 1)
        if tomllib:
            self.assertEqual(tomllib.loads(path.read_text())["hooks"]["state"], {"crewchat:stop": {"trusted_hash": "abc"}})
        self.assertTrue(crewchat.uninstall_codex())
        self.assertEqual(path.read_text(), state)
        # The user's own hooks written inline: a table of ours could not join them, so the server only.
        theirs = '[hooks]\nStop = [{ hooks = [{ type = "command", command = "mine" }] }]\n'
        path.write_text(theirs)
        self.assertTrue(crewchat.install_codex())
        self.assertNotIn("[[hooks.", path.read_text())
        self.assertIn("[mcp_servers.crewchat]", path.read_text())
        path.unlink()

    def test_gemini_and_qwen_settings_get_crewchats_hooks_beside_the_users(self):
        ours = lambda s: {event: [h for g in groups for h in g["hooks"] if crewchat.is_our_hook(h["command"])]  # noqa: E731
                          for event, groups in s["hooks"].items()}
        for client, rel, (prompt, stop, tool) in (
                ("gemini", ".gemini/settings.json", ("BeforeAgent", "AfterAgent", "AfterTool")),
                ("qwen", ".qwen/settings.json", ("UserPromptSubmit", "Stop", "PostToolUse"))):
            folder = Path(tempfile.mkdtemp(prefix=client + "-hooks-", dir=TMP)).resolve()
            mine = {"matcher": "write_file", "hooks": [{"type": "command", "command": "echo mine"}]}
            (folder / rel).parent.mkdir()
            (folder / rel).write_text(json.dumps({"hooks": {tool: [mine]}}))
            cli("start", str(folder), "--no-open", "--no-service", "--chat", "demo", "--client", client)
            settings = json.loads((folder / rel).read_text())
            self.assertEqual({e: [h["name"] for h in hs] for e, hs in ours(settings).items()},
                             {prompt: ["crewchat-prompt"], stop: ["crewchat-stop"], tool: ["crewchat-tool"]}, client)
            self.assertIn(" hook %s stop" % client, ours(settings)[stop][0]["command"])
            # Milliseconds: Gemini CLI's always are, and Qwen Code reads 1000 or more as milliseconds.
            self.assertEqual(ours(settings)[stop][0]["timeout"], (crewchat.MAX_LISTEN + 100) * 1000)
            self.assertGreaterEqual(min(h["timeout"] for hs in ours(settings).values() for h in hs), 1000)
            self.assertEqual(settings["hooks"][tool][0], mine)
            crewchat.set_settings_hooks(settings, client)  # again: replaced, not added
            self.assertEqual(sum(len(hs) for hs in ours(settings).values()), 3)
            del settings["hooks"][stop]  # a folder connected before crewchat had them: hooks refresh adds them
            (folder / rel).write_text(json.dumps(settings))
            self.assertIn(rel, crewchat.update_hooks(folder))
            self.assertIn(stop, json.loads((folder / rel).read_text())["hooks"])
            crewchat.disconnect_folder(folder)
            self.assertEqual(json.loads((folder / rel).read_text()), {"hooks": {tool: [mine]}})

    def test_opencode_gets_crewchats_plugin(self):
        folder = Path(tempfile.mkdtemp(prefix="opencode-plugin-", dir=TMP)).resolve()
        subprocess.run(["git", "init", "-q", str(folder)], check=True)
        cli("start", str(folder), "--no-open", "--no-service", "--chat", "demo", "--client", "opencode")
        plugin = folder / crewchat.OPENCODE_PLUGIN_FILE
        text = plugin.read_text()
        self.assertTrue(text.startswith(crewchat.OPENCODE_PLUGIN_MARK))
        self.assertIn(json.dumps(crewchat.run_command() + ["hook", "opencode"]), text)
        self.assertIn(crewchat.OPENCODE_PLUGIN_FILE, (folder / ".git" / "info" / "exclude").read_text())  # never committed
        if shutil.which("node"):  # the plugin is a module OpenCode can load
            check = subprocess.run(["node", "--check", "--input-type=module"], input=text, capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stderr)
        plugin.write_text(text.replace(json.dumps(crewchat.run_command() + ["hook", "opencode"]),
                                       '["/old/crewchat", "hook", "opencode"]'))
        self.assertIn(crewchat.OPENCODE_PLUGIN_FILE, crewchat.update_hooks(folder))  # brought up to this copy
        self.assertEqual(plugin.read_text(), text)
        self.assertEqual(crewchat.update_hooks(folder), [])
        crewchat.disconnect_folder(folder)
        self.assertFalse((folder / ".opencode").exists())  # its folders too, once empty
        mine = folder / ".opencode" / "plugins" / "crewchat.js"  # a file of the same name the user wrote: theirs
        mine.parent.mkdir(parents=True)
        mine.write_text("export const Mine = async () => ({})\n")
        crewchat.disconnect_folder(folder)
        self.assertTrue(mine.exists())

    def test_how_the_other_tools_are_started(self):
        prompt = crewchat.LAUNCH_PROMPT % "start-k"
        self.assertEqual(crewchat.launch_command("gemini", "gemini", "start-k", "edits"),
                         ["gemini", "--approval-mode", "auto_edit", "-i", prompt])
        self.assertEqual(crewchat.launch_command("qwen", "qwen", "start-k", "all"), ["qwen", "--yolo", "-i", prompt])
        self.assertEqual(crewchat.launch_command("qwen", "qwen", "start-k", "edits")[1:3], ["--approval-mode", "auto-edit"])
        self.assertEqual(crewchat.launch_command("opencode", "opencode", "start-k"), ["opencode", "--prompt", prompt])
        self.assertEqual(crewchat.launch_command("codex", "codex", "start-k", "edits"), ["codex", "--full-auto", prompt])
        # A copy that does not know the option is refused rather than started wrongly.
        self.assertEqual(crewchat.fit_options("--yolo --approval-mode <default|yolo>", "gemini", "edits")[1], ["auto_edit"])
        self.assertEqual(crewchat.fit_options("--approval-mode default|auto_edit|yolo", "gemini", "edits")[1], [])
        self.assertEqual(self.launch(tool="opencode", permissions="all")["status"], 400)  # OpenCode: its own settings
        with mock.patch.object(crewchat, "find_tool", side_effect=lambda t, c: "/bin/" + t if t in ("claude", "codex") else None):
            self.assertEqual(crewchat.clients_for("all"), ["claude", "cursor", "codex"])  # the others: when installed
        self.assertEqual(crewchat.clients_for("qwen"), ["qwen"])

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
        self.assertEqual(self.launched[-1][3], "edits")  # --accept-edits, as before
        cli("agent", "add", "--tool", "cursor", "--folder", str(self.folder), "--permissions", "auto")
        self.assertEqual(self.launched[-1][3], "auto")

    def test_permissions_are_checked_per_tool(self):
        for tool, permissions, ok in (("claude", "edits", True), ("cursor", "edits", False), ("cursor", "all", True),
                                      ("claude", "root", False)):
            body = {"tool": tool, "folder": str(self.folder), "permissions": permissions}
            if ok:
                crewchat.post_json(self.base + "/api/launch", self.owner, body)
                self.assertEqual(self.launched[-1][3], permissions)
            else:
                with self.assertRaises(urllib.error.HTTPError):
                    crewchat.post_json(self.base + "/api/launch", self.owner, body)
        crewchat.post_json(self.base + "/api/launch", self.owner, {"tool": "claude", "folder": str(self.folder),
                                                                     "accept_edits": True})  # a page from before
        self.assertEqual(self.launched[-1][3], "edits")

    def test_an_older_agent_command_line_is_started_with_what_it_knows(self):
        new_cursor = "Options:\n  --approve-mcps  ...\n  --trust  ...\n  --auto-review ...\n  -f, --force ..."
        self.assertEqual(crewchat.fit_options(new_cursor, "cursor", "auto"), (["--trust"], []))
        old_cursor = "Options:\n  -f, --force  Force allow commands"
        self.assertEqual(crewchat.fit_options(old_cursor, "cursor", "ask"), ([], []))  # it asks at its prompts instead
        self.assertEqual(crewchat.fit_options(old_cursor, "cursor", "auto"), ([], ["--auto-review"]))
        old_claude = '--permission-mode <mode>  (choices: "acceptEdits", "bypassPermissions", "default", "plan")'
        self.assertEqual(crewchat.fit_options(old_claude, "claude", "auto"), ([], ["auto"]))
        self.assertEqual(crewchat.fit_options(old_claude, "claude", "edits"), ([], []))
        self.assertEqual(crewchat.fit_options("", "cursor", "all"), (["--trust"], []))  # no help: as is
        with self.assertRaises(urllib.error.HTTPError):
            crewchat.post_json(self.base + "/api/launch", self.owner, {"tool": "claude", "folder": str(self.folder),
                                                                         "permissions": ["all"]})  # not a choice

    def test_a_cursor_agent_gets_crewchats_mcp_server_approved_and_no_other(self):
        calls = []
        fake = lambda command, **kw: calls.append((command, kw.get("cwd"))) or mock.Mock(  # noqa: E731
            returncode=0, stdout="--trust --approve-mcps", stderr="")
        with mock.patch.object(crewchat, "find_tool", lambda tool, config: "/x/cursor-agent"), \
                mock.patch.object(crewchat.subprocess, "run", fake), mock.patch.object(crewchat, "run", fake), \
                mock.patch.object(crewchat.subprocess, "Popen", lambda *a, **kw: calls.append((a[0], kw.get("cwd")))), \
                mock.patch.object(crewchat.shutil, "which", lambda name: "/usr/bin/" + name):  # Linux: a terminal
            self.real_launch_agent("cursor", str(self.folder), "start-0123456789abcdef", "ask", self.hub.home)
        self.assertIn((["/x/cursor-agent", "mcp", "enable", "crewchat"], str(self.folder)), calls)
        self.assertFalse(any("--approve-mcps" in " ".join(map(str, c[0])) for c in calls))

    def closed_session(self, app="terminal", tool="claude"):
        """An agent whose recorded session is closed (its process number is free)."""
        _, name = self.agent()
        self.hub.agents[name]["session"] = {"id": "sess-0003-abc", "pid": 999999, "started": "x", "app": app,
                                            "tool": tool, "folder": str(self.folder), "since": 1.0}
        self.hub.agents[name]["permissions"] = "edits"  # as it was started
        self.hub.links["link-" + name] = name  # as its hooks' hub_link left it
        return name

    def reopened(self, **body):
        opened = []
        with mock.patch.object(crewchat, "open_terminal", lambda folder, command, key, title="", root=None:
                               opened.append((folder, command, key, title))):
            out = crewchat.post_json(self.base + "/api/agent", self.owner, dict({"op": "reopen"}, **body))
        return out, opened

    def test_reopen_resumes_the_same_conversation_with_its_own_key(self):
        name = self.closed_session()
        key = next(k for k, v in self.hub.links.items() if v == name)
        out, opened = self.reopened(agent=name)
        self.assertEqual(out, {"ok": True})
        folder, command, used, title = opened[0]
        self.assertEqual((folder, used, title), (str(self.folder), key, name))  # its own key: same name, no relink
        self.assertEqual(command[-2:], ["--resume=sess-0003-abc", crewchat.REOPEN_PROMPT])  # one argument; it gets to work
        self.assertIn("acceptEdits", command)  # the permissions it was started with
        self.assertIn("Owner reopened %s" % name, self.hub_history())
        # Until the reopened session reports, a second click does not open a second copy.
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.reopened(agent=name)
        self.assertIn("being reopened", json.loads(caught.exception.read())["error"])
        # An agent whose link was lost still comes back as itself: a new key tied to its name.
        self.hub.agents[name]["session"].pop("reopening")
        self.hub.links = {k: v for k, v in self.hub.links.items() if v != name}
        _, opened = self.reopened(agent=name)
        self.assertEqual(self.hub.links[opened[0][2]], name)

    def test_cursor_sessions_can_be_stopped_but_not_reopened_yet(self):
        name = self.closed_session(tool="cursor")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.reopened(agent=name)
        self.assertIn("cannot reopen", json.loads(caught.exception.read())["error"])
        self.assertFalse(next(r for r in self.hub.rows() if r["agent"] == name)["session"]["reopen"])

    def test_reopen_is_refused_while_it_runs(self):
        name = self.closed_session()
        with mock.patch.object(crewchat, "process_running", lambda session, table=None: True):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.reopened(agent=name)
        self.assertIn("still running", json.loads(caught.exception.read())["error"])

    def test_reopen_from_the_claude_app_says_where_it_opens(self):
        name = self.closed_session(app="claude-app")
        self.reopened(agent=name)
        self.assertIn("in a Terminal window", self.hub_history())

    def test_switch_model_stops_then_reopens_with_the_model(self):
        name = self.closed_session()
        session = Session(self.base, self.token)  # it holds a task, which a model switch must not free
        session.call("hub_link", key="link-" + name)
        task = self.say("all", "kept across the switch", "task")
        session.call("hub_take", id=task)
        steps, running = [], [True]
        def stop(pid):
            steps.append("stop")
            running[0] = False
            return True
        with mock.patch.object(crewchat, "process_running", lambda session, table=None: running[0]), \
                mock.patch.object(crewchat, "stop_process", stop), \
                mock.patch.object(crewchat, "tool_models", lambda tool: [["opus", "Opus"], ["sonnet", "Sonnet"]]), \
                mock.patch.object(crewchat, "open_terminal", lambda folder, command, key, title="", root=None:
                                  steps.append(command)):
            crewchat.post_json(self.base + "/api/agent", self.owner, {"op": "model", "agent": name, "model": "sonnet"})
        self.assertEqual(steps[0], "stop")
        self.assertEqual(steps[1][-4:], ["--model", "sonnet", "--resume=sess-0003-abc", crewchat.REOPEN_PROMPT])
        self.assertEqual(self.hub.taken.get(task), name)  # still its task
        self.assertIn("Owner reopened %s on sonnet" % name, self.hub_history())

    def test_switch_model_checks_it_can_reopen_before_it_stops(self):
        name = self.closed_session()
        self.hub.agents[name]["session"]["folder"] = "/no/longer/connected"
        stopped = []
        with mock.patch.object(crewchat, "process_running", lambda session, table=None: True), \
                mock.patch.object(crewchat, "stop_process", lambda pid: stopped.append(pid) or True), \
                mock.patch.object(crewchat, "tool_models", lambda tool: [["sonnet", "Sonnet"]]):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                crewchat.post_json(self.base + "/api/agent", self.owner, {"op": "model", "agent": name, "model": "sonnet"})
        self.assertIn("no longer connected", json.loads(caught.exception.read())["error"])
        self.assertEqual(stopped, [])  # still running, as it was

    def test_reopen_and_switch_model_keep_its_advisor(self):
        name = self.closed_session()
        self.hub.agents[name]["advisor"] = "fable"
        _, opened = self.reopened(agent=name)
        self.assertEqual(opened[0][1][-4:-2], ["--advisor", "fable"])
        self.hub.agents[name]["session"].pop("reopening", None)
        with mock.patch.object(crewchat, "process_running", lambda session, table=None: False), \
                mock.patch.object(crewchat, "open_terminal", lambda folder, command, key, title="", root=None:
                                  opened.append((folder, command, key, title))):
            crewchat.post_json(self.base + "/api/agent", self.owner, {"op": "model", "agent": name, "model": "sonnet"})
        self.assertEqual(opened[-1][1][-6:-2], ["--model", "sonnet", "--advisor", "fable"])
        self.hub.agents[name]["advisor"] = "haiku"  # one this copy does not offer (an older or edited state)
        self.hub.agents[name]["session"].pop("reopening", None)
        _, opened = self.reopened(agent=name)
        self.assertNotIn("--advisor", opened[0][1])

    def test_reopen_keeps_the_permissions_it_was_started_with(self):
        crewchat.post_json(self.base + "/api/launch", self.owner, {"tool": "claude", "folder": str(self.folder),
                                                                     "name": "auto-one", "permissions": "auto"})
        key = self.launched[-1][2]
        Session(self.base, self.token).call("hub_link", key=key)  # the launched session checks in
        self.assertEqual(self.hub.agents["auto-one"]["permissions"], "auto")
        with self.hub.lock, mock.patch.object(crewchat.Hub, "agent_folders", lambda hub, agent: ["/here"]):
            self.hub._keep_session("auto-one", {"id": "sess-0012-abc", "pid": 999999, "started": "x",
                                                "tool": "claude", "folder": str(self.folder)})
        _, opened = self.reopened(agent="auto-one")
        self.assertIn("auto", opened[0][1][opened[0][1].index("--permission-mode") + 1])

    def test_switch_model_refuses_a_model_the_tool_does_not_list(self):
        name = self.closed_session()
        with mock.patch.object(crewchat, "tool_models", lambda tool: [["opus", "Opus"]]):
            for bad in ("opus; rm -rf ~", "gpt-9"):
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    crewchat.post_json(self.base + "/api/agent", self.owner, {"op": "model", "agent": name, "model": bad})
                self.assertIn("no model", json.loads(caught.exception.read())["error"])

    def test_models_are_claude_codes_aliases(self):
        self.assertEqual([m[0] for m in crewchat.tool_models("claude")], ["fable", "opus", "sonnet", "haiku"])
        self.assertEqual(crewchat.tool_models("cursor"), [])  # it cannot be switched from here yet

    def test_the_window_is_titled_with_the_agents_name(self):
        script = crewchat.macos_script("/tmp/x", ["/x/claude"], "start-0123456789abcdef", title="it's me")
        self.assertIn("printf '\\033]0;%s\\007' 'it'\"'\"'s me'", script)

    def hub_history(self):
        return "\n".join(m["text"] for m in self.hub.messages[-5:])

    def test_the_launch_command_carries_no_typed_text(self):
        command = crewchat.launch_command("/x/claude", "claude", "start-0123456789abcdef", "edits")
        self.assertEqual(command[:3], ["/x/claude", "--permission-mode", "acceptEdits"])
        self.assertEqual(command[3], crewchat.LAUNCH_PROMPT % "start-0123456789abcdef")
        self.assertRegex(command[3], r"^[A-Za-z0-9 .,:_-]+$")  # safe in any shell, cmd.exe included
        self.assertEqual(crewchat.launch_command("/x/claude", "claude", "k", "all")[1:3], ["--permission-mode", "bypassPermissions"])
        # Cursor's agent never stops at the folder's trust prompt, and is never told to approve every MCP
        # server the folder lists (only crewchat's is approved); "all" lets it run any command.
        self.assertEqual(crewchat.launch_command("/x/cursor-agent", "cursor", "start-1")[1:],
                         ["--trust", crewchat.LAUNCH_PROMPT % "start-1"])
        self.assertEqual(crewchat.launch_command("/x/cursor-agent", "cursor", "k", "all")[2], "--force")
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
        self.assertIn("Linked. You are %s, in the project Demo." % name, note)
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
        self.assertIn("This session is %s again, in the project Demo." % name, again.text("hub_link", key="key-resume-0001"))
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


class AgentCards(Base):
    """The owner's actions on an agent's card: rename, remove, listening."""

    def op(self, **body):
        try:
            return crewchat.post_json(self.base + "/api/agent", self.owner, body)
        except urllib.error.HTTPError as e:
            return json.loads(e.read())

    def test_rename_remove_and_listening(self):
        session, name = self.agent()
        self.assertEqual(self.op(op="listen", agent=name, mode="off"), {"ok": True})
        self.assertEqual(next(r for r in self.hub.rows() if r["agent"] == name)["listen"], "off")
        self.assertEqual(self.op(op="rename", agent=name, new="page-named"), {"ok": True})
        self.assertIn("page-named", self.names())
        self.assertEqual(self.hub.agents["page-named"]["listen"], "off")  # the setting goes with the name
        self.assertIn("reserved", self.op(op="rename", agent="page-named", new="Owner")["error"])
        self.assertIn("listening is on, off or default", self.op(op="listen", agent="page-named", mode="loud")["error"])
        task = self.say("all", "held by the one we remove", "task")
        session.call("hub_take", id=task)
        self.assertEqual(self.op(op="remove", agent="page-named"), {"ok": True})
        self.assertNotIn("page-named", self.names())
        row = next(r for r in self.hub.sheet_rows() if r["id"] == task)
        self.assertEqual((row["status"], row["holder"], row["reclaimed"]), ("todo", "", "page-named"))  # free at once
        self.assertIn("no agent called nobody", self.op(op="listen", agent="nobody", mode="on")["error"])
        self.assertIn("op is rename, remove, listen, stop, reopen, start, model or profile", self.op(op="kick", agent=name)["error"])

    def test_cards_never_wait_for_the_process_list(self):
        _, name = self.agent()
        self.hub.agents[name]["session"] = {"id": "sess-0011-abc", "pid": 999999, "started": "x", "app": "terminal",
                                            "tool": "claude", "folder": "/tmp", "since": time.time()}
        crewchat._PROCESSES.clear()
        with mock.patch.object(crewchat, "process_table", mock.Mock(side_effect=AssertionError("ps under the lock"))):
            self.assertFalse(next(r for r in self.hub.rows() if r["agent"] == name)["session"]["running"])
        before = self.hub.version  # the server's refresher finds it running, and the page hears it
        table = {999999: (1, "x", "/x/claude --resume sess-0011-abc")}
        with mock.patch.object(crewchat, "process_table", lambda pids=None: table):
            crewchat.refresh_processes(mock.Mock(all=lambda: [mock.Mock(hub=self.hub)]))
        self.assertGreater(self.hub.version, before)
        self.assertTrue(next(r for r in self.hub.rows() if r["agent"] == name)["session"]["running"])

    def test_windows_stop_is_forced(self):
        calls = []
        with mock.patch.object(crewchat.os, "name", "nt"), \
                mock.patch.object(crewchat.subprocess, "run", lambda c, **kw: calls.append(c) or mock.Mock(returncode=0)):
            crewchat.stop_process(4242)
        self.assertEqual(calls[0], ["taskkill", "/PID", "4242", "/T", "/F"])

    def stand_in(self, name, app="terminal"):
        """A real process that looks like the agent's claude (it is sleep), recorded as its session."""
        proc = subprocess.Popen(["claude", "60"], executable="/bin/sleep")
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        started = crewchat.process_table()[proc.pid][1]
        self.hub.agents[name]["session"] = {"id": "sess-0002-abc", "pid": proc.pid, "started": started, "app": app,
                                            "tool": "claude", "folder": "/tmp", "since": time.time()}
        crewchat._PROCESSES.clear(); crewchat._PROCESSES.update(crewchat.process_table([proc.pid]))  # as the server's look would
        return proc

    @unittest.skipIf(os.name == "nt", "a stand-in process with another name needs POSIX exec")
    def test_stop_ends_the_agents_process_and_frees_its_tasks(self):
        session, name = self.agent()
        task = self.say("all", "held when stopped", "task")
        session.call("hub_take", id=task)
        proc = self.stand_in(name)
        self.assertTrue(next(r for r in self.hub.rows() if r["agent"] == name)["session"]["running"])
        self.assertEqual(self.op(op="stop", agent=name), {"ok": True})
        proc.wait(timeout=5)
        row = next(r for r in self.hub.sheet_rows() if r["id"] == task)
        self.assertEqual((row["status"], row["reclaimed"]), ("todo", name))
        self.assertIn(name, self.names())  # stopped, not removed
        self.assertFalse(next(r for r in self.hub.rows() if r["agent"] == name)["session"]["running"])
        self.assertIn("is not running", self.op(op="stop", agent=name)["error"])

    @unittest.skipIf(os.name == "nt", "a stand-in process with another name needs POSIX exec")
    def test_stop_does_not_touch_a_reused_process_number(self):
        _, name = self.agent()
        proc = self.stand_in(name)
        self.hub.agents[name]["session"]["started"] = "Mon Jan  1 00:00:00 2024"  # another process had that number
        self.assertIn("is not running", self.op(op="stop", agent=name)["error"])
        self.assertIsNone(proc.poll())
        other = subprocess.Popen(["sleep", "60"])  # the right start time, but not an agent's command
        self.addCleanup(other.kill)
        self.hub.agents[name]["session"].update(pid=other.pid, started=crewchat.process_table()[other.pid][1])
        self.assertIn("is not running", self.op(op="stop", agent=name)["error"])
        self.assertIsNone(other.poll())
        _, never = self.agent()
        self.assertIn("no session recorded for %s" % never, self.op(op="stop", agent=never)["error"])

    def test_an_agent_on_another_machine_is_changed_there(self):
        self.hub.set_remote("dev-x", "laptop", [{"agent": "far-away", "client": "cursor", "place": "x", "seen": 1,
                                                 "listen": "on"}], 1)
        try:
            for body in ({"op": "rename", "new": "near"}, {"op": "remove"}, {"op": "listen", "mode": "on"},
                         {"op": "stop"}, {"op": "reopen"}):
                self.assertIn("runs on laptop", self.op(agent="far-away", **body)["error"])
            self.assertEqual(next(r for r in self.hub.rows() if r["agent"] == "far-away")["listen"], "on")  # shown here
        finally:
            self.hub.drop_remote("dev-x")

    def test_the_page_needs_the_owner(self):
        _, name = self.agent()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            crewchat.post_json(self.base + "/api/agent", self.token, {"op": "remove", "agent": name})
        self.assertIn(caught.exception.code, (401, 403))
        self.assertIn(name, self.names())


class Profiles(Base):
    """An agent's picture and colour, its tool's brand and its machine's OS."""

    def test_every_known_client_has_a_brand_row(self):
        for client in crewchat.KNOWN_CLIENTS + ("antigravity", "grok"):
            row = crewchat.tool_info(client)
            self.assertRegex(row["colour"], r"^#[0-9a-f]{6}$", client)
            self.assertTrue(1 <= len(row["mark"]) <= 2, client)
        self.assertEqual(crewchat.tool_info("claude")["colour"], "#d97757")
        self.assertEqual(crewchat.tool_info("something-new"), crewchat.TOOLS_INFO["agent"])
        self.assertIn(crewchat.os_name(), ("mac", "windows", "linux", "other"))

    def op(self, **body):
        try:
            return crewchat.post_json(self.base + "/api/agent", self.owner, body)
        except urllib.error.HTTPError as e:
            return json.loads(e.read())

    def row(self, name):
        return next(r for r in self.hub.rows() if r["agent"] == name)

    def test_owner_sets_a_preset_and_a_colour(self):
        _, name = self.agent()
        self.assertTrue(self.row(name)["picture"].startswith("art:"))  # a random 3D picture to begin with
        self.assertEqual(self.row(name)["colour"], "")
        self.assertEqual(self.row(name)["os"], crewchat.os_name())
        self.assertEqual(self.op(op="profile", agent=name, picture="preset:rocket", colour="#FF8800"), {"ok": True})
        self.assertEqual((self.row(name)["picture"], self.row(name)["colour"]), ("preset:rocket", "#ff8800"))
        self.assertEqual(self.op(op="profile", agent=name, colour=""), {"ok": True})  # back to the brand colour
        self.assertEqual((self.row(name)["picture"], self.row(name)["colour"]), ("preset:rocket", ""))
        self.assertIn("no picture preset", self.op(op="profile", agent=name, picture="preset:nope")["error"])
        self.assertIn("#rrggbb", self.op(op="profile", agent=name, colour="red")["error"])
        self.assertIn("#rrggbb", self.op(op="profile", agent=name, colour="#12345g")["error"])

    def test_the_built_in_3d_pictures(self):
        status, body, headers = self.raw("/pictures/bot.webp")  # not secret: no sign-in needed
        self.assertEqual((status, headers["Content-Type"], body[:4], body[8:12]), (200, "image/webp", b"RIFF", b"WEBP"))
        self.assertIn("max-age", headers["Cache-Control"])
        for bad in ("/pictures/nope.webp", "/pictures/bot.png", "/pictures/../files/x"):
            self.assertEqual(self.raw(bad)[0], 404, bad)
        session, name = self.agent()
        self.assertEqual(self.op(op="profile", agent=name, picture="art:owl"), {"ok": True})
        self.assertEqual(self.row(name)["picture"], "art:owl")
        self.assertIn("no picture called nope", self.op(op="profile", agent=name, picture="art:nope")["error"])
        self.assertIn("picture art:fox", session.text("hub_profile", picture="fox"))
        self.assertEqual(crewchat.clean_picture("art:../x"), "")
        self.assertEqual(set(crewchat.ART_DATA), set(crewchat.ART_PICTURES))
        self.assertEqual(self.hub.poll(-1, -1, 0)["art"]["bot"], "Robot")
        self.assertIn("picture preset:robot", session.text("hub_profile", picture="robot"))  # the emoji, still

    def test_uploaded_picture_limits(self):
        _, name = self.agent()
        png = self.hub.add_file("me.png", b"\x89PNG" + b"0" * 100, "image/png")
        big = self.hub.add_file("big.png", b"\x89PNG" + b"0" * (512 * 1024), "image/png")
        pdf = self.hub.add_file("cv.pdf", b"%PDF-1.4", "application/pdf")
        self.assertEqual(self.op(op="profile", agent=name, picture="file:" + png["id"]), {"ok": True})
        self.assertEqual(self.row(name)["picture"], "file:" + png["id"])
        self.assertIn("512 KB", self.op(op="profile", agent=name, picture="file:" + big["id"])["error"])
        self.assertIn("PNG, JPEG or WebP", self.op(op="profile", agent=name, picture="file:" + pdf["id"])["error"])
        self.assertIn("no shared file", self.op(op="profile", agent=name, picture="file:" + "0" * 16)["error"])
        self.assertIn("no shared file", self.op(op="profile", agent=name, picture="file:../state")["error"])

    def test_profile_survives_rename_restart_and_travels(self):
        _, name = self.agent()
        self.op(op="profile", agent=name, picture="preset:star", colour="#123456")
        self.assertEqual(self.op(op="rename", agent=name, new="starry"), {"ok": True})
        self.assertEqual(self.row("starry")["picture"], "preset:star")
        again = crewchat.Hub(crewchat.Roster())  # what a restarted server loads
        self.assertEqual((again.agents["starry"]["picture"], again.agents["starry"]["colour"]), ("preset:star", "#123456"))
        mine = next(r for r in self.hub.local_state() if r["agent"] == "starry")
        self.assertEqual((mine["picture"], mine["colour"], mine["os"]), ("preset:star", "#123456", crewchat.os_name()))
        self.hub.set_remote("dev-x", "pc", [dict(mine, agent="faraway", os="windows"),
                                            dict(mine, agent="odd", picture="<img>", colour="red", os="beos")],
                            time.time())
        try:
            far = self.row("faraway")
            self.assertEqual((far["picture"], far["colour"], far["os"], far["remote"]),
                             ("preset:star", "#123456", "windows", True))
            odd = self.row("odd")  # whatever another machine sends, the page gets only what it can draw
            self.assertEqual((odd["picture"], odd["colour"], odd["os"]), ("", "", "other"))
            self.assertIn("runs on pc", self.op(op="profile", agent="faraway", colour="#000000")["error"])
        finally:
            self.hub.drop_remote("dev-x")
        self.op(op="remove", agent="starry")

    def test_agent_sets_its_own_profile(self):
        session, name = self.agent()
        self.assertIn("picture preset:camera", session.text("hub_profile", picture="camera", colour="#00AA00"))
        self.assertEqual((self.row(name)["picture"], self.row(name)["colour"]), ("preset:camera", "#00aa00"))
        self.assertIn("picture initials", session.text("hub_profile", picture="initials"))
        self.assertEqual((self.row(name)["picture"], self.row(name)["colour"]), ("", "#00aa00"))
        self.assertIn("the tool's colour", session.text("hub_profile", colour=""))
        with tempfile.TemporaryDirectory() as folder:
            picture = Path(folder) / "me.png"
            picture.write_bytes(b"\x89PNG" + b"0" * 64)
            with mock.patch.object(crewchat.Hub, "agent_folders", return_value=[Path(folder)]):
                session.text("hub_profile", file=str(picture))
            fid = self.row(name)["picture"].partition(":")[2]
            self.assertEqual(self.hub.file(fid)[0]["type"], "image/png")
            with mock.patch.object(crewchat.Hub, "agent_folders", return_value=[]):  # a folder from another machine
                answer = session.call("hub_profile", file=str(picture))
            self.assertTrue(answer["isError"])
            self.assertIn("another machine", answer["content"][0]["text"])
        self.assertTrue(session.call("hub_profile", picture="nope")["isError"])

    def test_agent_picture_is_checked_before_it_is_kept(self):
        session, _ = self.agent()
        before = set(os.listdir(self.hub.home / "files")) if (self.hub.home / "files").exists() else set()
        with tempfile.TemporaryDirectory() as folder:
            big, text = Path(folder) / "big.png", Path(folder) / "notes.txt"
            big.write_bytes(b"\x89PNG" + b"0" * (512 * 1024))
            text.write_text("not a picture")
            with mock.patch.object(crewchat.Hub, "agent_folders", return_value=[Path(folder)]):
                self.assertIn("512 KB", session.call("hub_profile", file=str(big))["content"][0]["text"])
                self.assertIn("PNG, JPEG or WebP", session.call("hub_profile", file=str(text))["content"][0]["text"])
        after = set(os.listdir(self.hub.home / "files")) if (self.hub.home / "files").exists() else set()
        self.assertEqual(after, before)  # nothing kept for a refused picture
        self.assertTrue(session.call("hub_profile", colour="blue")["isError"])


class ChatPage(Base):
    def test_pages_need_the_owner(self):
        for path in ("/", "/classic"):
            status, _, headers = self.raw(path)
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
        self.assertIn(b'id="panel"', body)  # the page from 0.14
        self.assertEqual(headers["Cache-Control"], "no-store")
        status, classic, _ = self.raw("/classic", opener=opener)  # the page before it, for one release
        self.assertEqual(status, 200)
        self.assertIn(b'id="views"', classic)
        self.assertNotIn(b'id="panel"', classic)
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


class CommandsTab(Base):
    """The chat page's connection line and Commands tab (help for every command, and Run for the safe ones)."""

    def run_cmd(self, body, headers=None):
        status, raw, _ = self.raw("/api/run", json.dumps(body).encode(),
                                  dict({"Authorization": "Bearer " + self.owner, "Content-Type": "application/json"},
                                       **(headers or {})))
        return status, json.loads(raw)

    def test_poll_says_how_machines_are_linked(self):
        opener, _ = self.owner_browser()
        data = json.loads(self.raw("/api/poll?after=-1&v=-1&wait=0", opener=opener)[1])
        self.assertEqual(data["link"], {"via": "none"})
        self.assertIn(b'id="link"', self.raw("/", opener=opener)[1])

    def test_link_info_says_what_the_link_reports(self):
        class Link:
            def link_status(self):
                return {"via": "cloud", "me": "mac", "online": True, "approved": True}

        hub = lambda sync: type("Hub", (), {"sync": sync})()  # noqa: E731
        self.assertEqual(crewchat.link_info(hub(Link()), shared=False),
                         {"via": "cloud", "me": "mac", "online": True, "approved": True, "shared": False})
        self.assertEqual(crewchat.link_info(hub(None)), {"via": "none"})

    def test_every_runnable_command_parses(self):
        """A Run button that no longer matches the command line would only fail when tapped."""
        parser = crewchat.build_parser()
        for usage, spec in crewchat.RUNNABLE.items():
            argv = [("laptop" if part == "{NAME}" else "6b56 fa65 a4af 57c4" if part.startswith("{") else part)
                    for part in spec["argv"]] + (["--chat", "demo"] if spec.get("chat") else [])
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    parser.parse_args(argv)
                except SystemExit:
                    self.fail("the page runs `crewchat %s`, which does not parse" % " ".join(argv))

    def test_commands_need_the_owner(self):
        self.assertEqual(self.raw("/api/commands")[0], 401)
        self.assertEqual(self.raw("/api/commands", headers={"Authorization": "Bearer " + self.token})[0], 401)
        place = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"}
        self.assertEqual(self.raw("/api/run", b'{"usage": "projects"}', place)[0], 403)
        opener, _ = self.owner_browser()
        status, body, _ = self.raw("/api/commands", opener=opener)
        self.assertEqual(status, 200)
        groups = json.loads(body)["groups"]
        rows = {c["usage"]: c for g in groups for c in g["commands"]}
        self.assertTrue(rows["projects"]["run"])
        self.assertFalse(rows["uninstall [--yes] [--purge]"]["run"])
        self.assertIn("service install [--keep-awake]", rows)
        approve = rows["cloud approve NAME --fingerprint FINGERPRINT"]
        self.assertEqual((approve["needs"], approve["ask"]), (["NAME", "FINGERPRINT"], True))
        # The page refuses a cross-site POST, like every other owner action.
        status, _, _ = self.raw("/api/run", b'{"usage": "projects"}',
                                {"Content-Type": "application/json", "Origin": "https://evil.example"}, opener)
        self.assertEqual(status, 403)

    def test_every_command_action_and_option_is_listed(self):
        """The Commands tab lists everything the command line takes: each command, each of its actions
        (service install, tasks assign, ...) and each of its options (--keep-awake, ...)."""
        usages = [c["usage"] for g in crewchat.commands_catalog() for c in g["commands"]]
        words = lambda usage: set(re.split(r"[\s\[\]|]+", usage)) - {""}  # noqa: E731
        sub = next(a for a in crewchat.build_parser()._actions if isinstance(a, argparse._SubParsersAction))
        missing = []
        for name, parser in sub.choices.items():
            if name == "hook":  # run by the hooks themselves
                continue
            mine = [words(u) for u in usages if u.split()[0] == name]
            if not mine:
                missing.append(name)
                continue
            for action in parser._actions:
                if action.help == argparse.SUPPRESS or isinstance(action, argparse._HelpAction):
                    continue
                wanted = [o for o in action.option_strings if o.startswith("--") and o != "--chat"][:1]
                if not action.option_strings and action.choices:
                    wanted = list(action.choices)
                for word in wanted:
                    if not any(word in w for w in mine):
                        missing.append("%s %s" % (name, word))
        self.assertEqual(missing, [], "add these to COMMANDS in crewchat.py")

    def test_runs_listed_commands_only(self):
        status, answer = self.run_cmd({"usage": "projects"})
        self.assertEqual((status, answer["code"]), (200, 0), answer)
        self.assertIn("Demo", answer["output"])
        status, answer = self.run_cmd({"usage": "tasks [list]"})
        self.assertEqual((status, answer["code"]), (200, 0), answer)
        for usage in ("uninstall [--yes] [--purge]", "stop", "uninstall", "projects; rm -rf /", ""):
            self.assertEqual(self.run_cmd({"usage": usage})[0], 400, usage)
        approve = "cloud approve NAME --fingerprint FINGERPRINT"
        for name in ("", "--purge", "../x", "a" * 80, "x\ny"):
            values = {"NAME": name, "FINGERPRINT": "6b56 fa65 a4af 57c4"}
            self.assertEqual(self.run_cmd({"usage": approve, "values": values})[0], 400, name)
        self.assertEqual(self.run_cmd({"usage": approve, "values": {"NAME": "laptop"}})[0], 400)  # no fingerprint
        argv = []
        real = crewchat.subprocess.run
        try:  # the page passes both values, so the command never stops to ask at a terminal it lacks
            crewchat.subprocess.run = lambda cmd, **kw: argv.extend(cmd) or type("Done", (), {
                "stdout": "", "stderr": "", "returncode": 0})()
            crewchat.run_listed_command(approve, {"NAME": "laptop", "FINGERPRINT": "6b56 fa65 a4af 57c4"})
        finally:
            crewchat.subprocess.run = real
        self.assertEqual(argv[-5:], ["cloud", "approve", "laptop", "--fingerprint", "6b56 fa65 a4af 57c4"])

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
        allow = settings["permissions"]["allow"]
        self.assertEqual(allow[0], "Bash(ls:*)")  # the user's own rule stays
        self.assertIn("mcp__crewchat__hub_inbox", allow)  # crewchat's tools do not ask...
        self.assertIn("mcp__crewchat__hub_send", allow)  # ...sending too: a file comes only from its own folder (#33)
        self.assertNotIn("mcp__crewchat", allow)
        cursor_permissions = json.loads((folder / ".cursor" / "cli.json").read_text())["permissions"]
        cursor_allow = cursor_permissions["allow"]
        self.assertEqual(cursor_permissions["deny"], [])  # Cursor's agent refuses a cli.json without one
        self.assertIn("Mcp(crewchat:hub_update)", cursor_allow)
        self.assertIn("Mcp(crewchat:hub_send)", cursor_allow)
        self.assertIn("crewchat", settings["enabledMcpjsonServers"])
        stop = [h["command"] for g in settings["hooks"]["Stop"] for h in g["hooks"]]
        self.assertEqual(stop[0], "echo mine")
        self.assertTrue(stop[1].endswith(" hook claude stop") and "crewchat.py" in stop[1])
        self.assertEqual(len(settings["hooks"]["UserPromptSubmit"]), 1)
        hooks = json.loads((folder / ".cursor" / "hooks.json").read_text())
        self.assertTrue(hooks["hooks"]["stop"][0]["command"].endswith(" hook cursor stop"))
        self.assertIn(".mcp.json", (folder / ".git" / "info" / "exclude").read_text())
        self.assertNotIn("do not commit", out)
        # Two sessions opened in that folder are two agents, each with a first name of its own.
        one, two = Session(self.base, token), Session(self.base, token, "cursor")
        self.assertEqual({one.me in crewchat.AGENT_NAMES, two.me in crewchat.AGENT_NAMES, one.me != two.me}, {True})
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
        out = cli("start", str(folder), "--no-open", "--no-service", "--place", "Desk", "--chat", "Demo")
        self.assertIn("The server is running at %s." % self.base, out)
        self.assertRegex(out, r'- (Connected|Added) %s to the project "Demo", as "desk"\.' % re.escape(str(folder)))
        token = crewchat.read_token("desk")
        mcp = json.loads((folder / ".mcp.json").read_text())["mcpServers"]["crewchat"]
        self.assertEqual(mcp["headers"]["Authorization"], "Bearer " + token)
        self.assertTrue((folder / ".cursor" / "hooks.json").exists())
        places = crewchat.list_places()
        out = cli("start", str(folder), "--no-open", "--no-service")
        self.assertIn('%s is connected to the project "Demo", as "desk".' % folder, out)
        self.assertEqual(crewchat.list_places(), places)  # no second place for the same folder

    def test_a_new_folder_is_a_new_project_and_projects_are_apart(self):
        first = self.project().resolve()
        cli("start", str(first), "--no-open", "--no-service", "--chat", "demo")
        app = (Path(tempfile.mkdtemp(dir=TMP)) / "notes-app").resolve()
        app.mkdir()
        out = cli("start", str(app), "--no-open", "--no-service")
        self.assertIn('Created the project "notes-app"', out)
        self.assertIn('- Connected %s to the project "notes-app", as ' % app, out)
        entry = crewchat.find_chat("notes-app")
        self.assertEqual(entry["root"], crewchat.home() / "projects" / "notes-app")
        place = crewchat.list_places(entry["root"])[0]
        # Its own token, even where the first project has a folder by the same name.
        self.assertNotEqual(crewchat.read_token(place, entry["root"]), crewchat.read_token(place))
        # Another folder joins it with --chat, named after itself.
        site = (Path(tempfile.mkdtemp(dir=TMP)) / "site").resolve()
        site.mkdir()
        out = cli("start", str(site), "--no-open", "--no-service", "--chat", "notes-app")
        self.assertIn('- Added %s to the project "notes-app", as "site".' % site, out)
        self.assertIn("notes-app  [--chat notes-app]", cli("projects"))
        # Agents in different projects do not see or reach each other.
        here, _ = self.agent()
        # Names are random per project, so both could get the same one: give this one its own.
        here_name = "first-project-agent"
        self.assertIn("You are now %s." % here_name, here.text("hub_rename", name=here_name))
        token = crewchat.read_token(place, entry["root"])
        there = Session(self.base, token)
        there_name = there.me
        self.assertNotIn(here_name, there.text("hub_agents"))
        self.assertIn("nobody here is called", there.text("hub_send", to=here_name, text="hello?"))
        crewchat.post_json(self.base + "/api/send", self.owner, {"to": "all", "text": "for the first project"})
        self.assertNotIn("for the first project", there.text("hub_inbox"))
        crewchat.post_json(self.base + "/api/send", self.owner, {"to": "all", "text": "for notes-app"},
                           headers={"X-Crewchat-Chat": "notes-app"})
        self.assertIn("for notes-app", there.text("hub_inbox"))
        self.assertNotIn("for notes-app", here.text("hub_inbox"))
        # An agent's token is its project's only, whatever project it asks for.
        ping = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
        status = self.raw("/mcp", ping, {"Authorization": "Bearer " + token, "X-Crewchat-Chat": crewchat.list_projects()[0]["id"]})[0]
        self.assertEqual(status, 403)
        status = self.raw("/mcp", ping, {"Authorization": "Bearer " + token, "X-Crewchat-Chat": "notes-app"})[0]
        self.assertEqual(status, 200)
        self.assertEqual(self.raw("/mcp", ping, {"Authorization": "Bearer " + self.owner, "X-Crewchat-Chat": "nope"})[0], 404)

    def test_the_banner_is_plain_when_not_on_a_terminal(self):
        self.assertEqual(crewchat.banner(io.StringIO()), "crewchat " + crewchat.__version__)

    def test_a_join_code_works_once_and_wrong_codes_fail(self):
        args = self.invite("--client", "generic")
        cli(*args)
        with self.assertRaises(SystemExit):
            cli(*args)
        with self.assertRaises(SystemExit):
            cli("join", "--url", self.base, "--code", "AAAA-AAAA", "--client", "generic")


class OtherNames(Base):
    """crewchat registered under a name of the user's or a directory's (`claude mcp add --scope user
    crewchat-mcp -- uvx crewchat stdio`): its tools are still allowed without asking, and in a folder
    with no chat it is a server with no tools, not a failed one."""

    def setUp(self):
        self.user = crewchat.user_home()
        shutil.rmtree(self.user, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.user, True)

    def project(self):
        folder = Path(tempfile.mkdtemp(prefix="project-", dir=TMP)).resolve()
        subprocess.run(["git", "init", "-q", str(folder)], check=False)
        return folder

    def register(self, folder=None, **servers):
        """User-scope servers for Claude Code, or local-scope ones for folder."""
        path = self.user / ".claude.json"
        data = json.loads(path.read_text()) if path.exists() else {}
        target = data.setdefault("projects", {}).setdefault(str(folder), {}) if folder else data
        target.setdefault("mcpServers", {}).update(servers)
        self.user.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def stdio(self, folder, lines=b"", home=None):
        env = dict(os.environ, HOME=str(home or self.user), USERPROFILE=str(home or self.user))  # Windows: USERPROFILE
        return subprocess.run([sys.executable, str(ROOT / "crewchat.py"), "stdio", "--project", str(folder)],
                              input=lines, capture_output=True, env=env, timeout=60)

    def test_what_counts_as_crewchat(self):
        yes = crewchat.is_crewchat_server
        self.assertTrue(yes({"command": "uvx", "args": ["crewchat", "stdio"]}))
        self.assertTrue(yes({"command": "uvx", "args": ["crewchat@latest", "stdio"]}))
        self.assertTrue(yes({"command": "crewchat", "args": ["stdio", "--project", "/x"]}))
        self.assertTrue(yes({"command": "/usr/bin/python3", "args": ["/opt/crewchat/crewchat.py", "stdio"]}))
        self.assertTrue(yes({"command": "C:\\Tools\\crewchat.exe", "args": ["stdio"]}))
        self.assertTrue(yes({"type": "http", "url": self.mcp}, urls=(self.mcp,)))
        self.assertTrue(yes({"url": "https://elsewhere/mcp", "headers": {"Authorization": "Bearer t0k"}}, tokens={"t0k"}))
        self.assertFalse(yes({"command": "uvx", "args": ["crewchat", "serve"]}))
        self.assertFalse(yes({"command": "npx", "args": ["crewchat-lookalike", "stdio"]}))
        self.assertFalse(yes({"url": "https://elsewhere/mcp", "headers": {"Authorization": "Bearer other"}},
                             urls=(self.mcp,), tokens={"t0k"}))
        self.assertFalse(yes("crewchat"))

    def test_a_server_named_otherwise_is_allowed_and_the_double_is_noted(self):
        folder = self.project()
        self.register(**{"crewchat-mcp": {"type": "stdio", "command": "uvx", "args": ["crewchat", "stdio"]},
                         "other": {"command": "npx", "args": ["other-mcp", "stdio"]}})
        self.register(folder, chat={"type": "http", "url": self.mcp, "headers": {}},
                      **{"bad name!": {"command": "crewchat", "args": ["stdio"]}})
        (self.user / ".cursor").mkdir()
        (self.user / ".cursor" / "mcp.json").write_text(json.dumps(
            {"mcpServers": {"crew": {"command": "/usr/local/bin/crewchat", "args": ["stdio"]}}}))
        out = cli("start", str(folder), "--no-open", "--no-service", "--place", "Named", "--chat", "Demo")
        allow = json.loads((folder / ".claude" / "settings.local.json").read_text())["permissions"]["allow"]
        for name in ("crewchat", "crewchat-mcp", "chat"):
            self.assertIn("mcp__%s__hub_send" % name, allow)
            self.assertIn("mcp__%s__hub_inbox" % name, allow)
        self.assertFalse([r for r in allow if r.startswith(("mcp__other__", "mcp__bad"))])
        cursor = json.loads((folder / ".cursor" / "cli.json").read_text())["permissions"]["allow"]
        self.assertIn("Mcp(crew:hub_send)", cursor)
        self.assertIn("Mcp(crewchat:hub_send)", cursor)
        # crewchat twice for one client: start says so, and how to keep one.
        self.assertIn("Claude Code sees crewchat 3 times: crewchat (.mcp.json), chat (~/.claude.json (local scope)), "
                      "crewchat-mcp (~/.claude.json (user scope))", out)
        self.assertIn("`claude mcp remove --scope local chat`", out)
        self.assertIn("Cursor sees crewchat 2 times", out)
        # Taking crewchat out of the folder takes every name's rules with it.
        crewchat.disconnect_folder(folder)
        settings = folder / ".claude" / "settings.local.json"
        left = json.loads(settings.read_text()).get("permissions", {}).get("allow", []) if settings.exists() else []
        self.assertFalse([r for r in left if "__hub_" in r])

    def test_one_registration_says_nothing(self):
        folder = self.project()
        out = cli("start", str(folder), "--no-open", "--no-service", "--place", "Single", "--chat", "Demo")
        self.assertNotIn("sees crewchat", out)
        self.assertIsNone(crewchat.duplicate_note(folder, "claude"))

    def test_a_name_given_later_is_allowed_when_its_server_starts(self):
        folder = self.project()
        cli("start", str(folder), "--no-open", "--no-service", "--place", "Later", "--chat", "Demo")
        settings = folder / ".claude" / "settings.local.json"
        self.assertNotIn("mcp__crewchat-mcp__hub_send", settings.read_text())
        self.register(**{"crewchat-mcp": {"command": "uvx", "args": ["crewchat", "stdio"]}})
        done = self.stdio(folder)  # the client starts it: no input, so it relays nothing and ends
        self.assertEqual(done.returncode, 0, done.stderr)
        allow = json.loads(settings.read_text())["permissions"]["allow"]
        self.assertIn("mcp__crewchat-mcp__hub_send", allow)
        self.assertEqual(allow.count("mcp__crewchat__hub_send"), 1)
        self.assertEqual(crewchat.allow_registered(folder), [])  # nothing left to add

    def test_stdio_in_a_folder_with_no_chat_is_a_server_with_no_tools(self):
        folder = Path(tempfile.mkdtemp(prefix="elsewhere-", dir=TMP)).resolve()
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": 3, "method": "ping"},
                    [{"jsonrpc": "2.0", "id": 4, "method": "prompts/list"}, {"jsonrpc": "2.0", "id": 5, "method": "x"}]]
        done = self.stdio(folder, ("\n".join(json.dumps(r) for r in requests) + "\nnot json\n").encode())
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(b"is not connected to a crewchat", done.stderr)
        answers = [json.loads(line) for line in done.stdout.decode().splitlines()]
        hello = answers[0]["result"]
        self.assertEqual((hello["protocolVersion"], hello["serverInfo"]["name"]), ("2025-06-18", "crewchat"))
        self.assertIn("run `crewchat start` in this folder", hello["instructions"])
        self.assertEqual(answers[1], {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}})
        self.assertEqual(answers[2]["result"], {})
        self.assertEqual([a.get("result") for a in answers[3]], [{"prompts": []}, None])
        self.assertEqual(answers[3][1]["error"]["code"], -32601)
        self.assertEqual(answers[4]["error"]["code"], -32700)
        self.assertEqual(len(answers), 5)  # nothing for the notification

    def test_stdio_registered_for_every_folder_follows_only_this_machines_chat(self):
        repo = Path(tempfile.mkdtemp(prefix="cloned-", dir=TMP)).resolve()  # a repository with its own .mcp.json
        (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"crewchat": {
            "url": "https://chat.example/mcp", "headers": {"Authorization": "Bearer theirs"}}}}))
        self.assertFalse(crewchat.local_folder(repo))
        env = dict(os.environ, HOME=str(self.user), USERPROFILE=str(self.user))
        done = subprocess.run([sys.executable, str(ROOT / "crewchat.py"), "stdio", "--local-only", "--project", str(repo)],
                              input=b"", capture_output=True, env=env, timeout=60)
        self.assertIn(b"is not connected to a crewchat", done.stderr)  # not relayed to chat.example
        mine = Path(tempfile.mkdtemp(prefix="mine-", dir=TMP)).resolve()
        cli("start", str(mine), "--no-open", "--no-service", "--chat", "demo", "--client", "claude")
        self.assertTrue(crewchat.local_folder(mine))

    def test_the_chat_tools_are_not_shown_as_activity_by_any_name(self):
        for name in ("mcp__crewchat-mcp__hub_send", "mcp__chat__hub_inbox", "hub_send", "Mcp(crew:hub_update)"):
            self.assertEqual(crewchat.tool_summary("claude", {"tool_name": name}), "", name)


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
        here = mock.patch.object(crewchat.Hub, "agent_folders", lambda hub, agent: ["/here"])  # its folder is this machine's
        here.start()
        self.addCleanup(here.stop)

    def test_a_tool_call_is_a_sign_of_life_at_most_once_a_minute(self):
        session, name = self.linked("busy-turn")
        self.say(name, "for later")
        self.hub.agents[name]["seen"] = 0
        self.assertEqual(self.hook("claude", "tool", {"session_id": "busy-turn"}), "")  # never says anything
        first = self.hub.agents[name]["seen"]
        self.assertGreater(first, time.time() - 30)
        self.assertEqual(self.unread(name), 1)  # nothing handed over or confirmed: that is the stop hook's
        self.hub.agents[name]["seen"] = 0
        self.hook("claude", "tool", {"session_id": "busy-turn"})
        self.assertEqual(self.hub.agents[name]["seen"], 0)  # within the minute: no call to the server
        self.assertEqual(self.hook("claude", "tool", {"session_id": "never-linked"}), "")  # no key yet: quiet

    def test_a_session_is_reported_once(self):
        session, name = self.linked("sess-0001-abc")
        self.hook("claude", "stop", {"session_id": "sess-0001-abc"},
                  env_extra={"CREWCHAT_TEST_PROCESS": '{"pid": 4242, "started": "x", "app": "terminal"}'})
        kept = self.hub.agents[name]["session"]
        self.assertEqual((kept["id"], kept["pid"], kept["started"], kept["app"], kept["tool"]),
                         ("sess-0001-abc", 4242, "x", "terminal", "claude"))
        self.assertEqual(Path(kept["folder"]).resolve(), self.folder.resolve())
        # Its recorded process is gone (4242 is not running here), but it was asked a moment ago: not on
        # every turn (a process it cannot find would cost a lookup each time)...
        self.hook("claude", "stop", {"session_id": "sess-0001-abc"},
                  env_extra={"CREWCHAT_TEST_PROCESS": '{"pid": 999, "started": "y", "app": "terminal"}'})
        self.assertEqual(self.hub.agents[name]["session"]["pid"], 4242)
        # ...but again once ten minutes have passed.
        self.hub._asked[name] = 0
        self.hook("claude", "stop", {"session_id": "sess-0001-abc"},
                  env_extra={"CREWCHAT_TEST_PROCESS": '{"pid": 777, "started": "z", "app": "terminal"}'})
        self.assertEqual(self.hub.agents[name]["session"]["pid"], 777)
        # An id that is not one is never kept: it would reach a command line on Reopen, as words or as
        # an option of its own.
        for bad in ("x; rm -rf /", "--dangerously-skip-permissions"):
            _, bad_name = self.linked(bad)
            self.hook("claude", "stop", {"session_id": bad},
                      env_extra={"CREWCHAT_TEST_PROCESS": '{"pid": 4243, "started": "x", "app": "terminal"}'})
            self.assertNotIn("session", self.hub.agents[bad_name])
            with self.hub.lock:  # nor straight from a hook's report
                self.hub._keep_session(bad_name, {"id": bad, "pid": 4243, "started": "x", "folder": "/x"})
            self.assertNotIn("session", self.hub.agents[bad_name])

    def test_the_process_list_is_read_in_one_locale_and_time_zone(self):
        seen = {}
        def run(command, **kw):
            seen.update(command=command, env=kw.get("env") or {})
            return mock.Mock(stdout="", returncode=0)
        with mock.patch.object(crewchat.os, "name", "posix"), mock.patch.object(crewchat.subprocess, "run", run):
            crewchat.process_table()
        self.assertEqual((seen["env"].get("LC_ALL"), seen["env"].get("TZ")), ("C", "UTC0"))  # hook and server agree
        self.assertIn("-A", seen["command"])

    def test_a_reopened_session_reports_its_new_process(self):
        session, name = self.linked("sess-0009-abc")
        env = lambda pid: {"CREWCHAT_TEST_PROCESS": '{"pid": %d, "started": "x", "app": "terminal"}' % pid}  # noqa: E731
        self.hook("claude", "stop", {"session_id": "sess-0009-abc"}, env_extra=env(4242))
        self.assertEqual(self.hub.agents[name]["session"]["pid"], 4242)
        # Reopened (claude --resume keeps the id): the server asks for the report again, and gets the new process.
        self.hub.agents[name]["session"]["reopening"] = time.time()
        self.hook("claude", "prompt", {"session_id": "sess-0009-abc"}, env_extra=env(5555))
        kept = self.hub.agents[name]["session"]
        self.assertEqual(kept["pid"], 5555)
        self.assertNotIn("reopening", kept)

    def test_an_agent_from_another_machine_keeps_no_session_here(self):
        session, name = self.linked("sess-0010-abc")
        with mock.patch.object(crewchat.Hub, "agent_folders", lambda hub, agent: []):
            self.hook("claude", "stop", {"session_id": "sess-0010-abc"},
                      env_extra={"CREWCHAT_TEST_PROCESS": '{"pid": 4242, "started": "x", "app": "terminal"}'})
        self.assertNotIn("session", self.hub.agents[name])

    def test_the_hook_finds_its_agent_among_its_ancestors(self):
        app = "/Applications/Claude.app/Contents/MacOS/Claude"
        table = {10: (9, "Tue Oct 13 22:39:29 2026", "/bin/zsh -c crewchat hook claude stop"),
                 9: (8, "Tue Oct 13 22:39:29 2026", "/Users/x/Library/Application Support/Claude/claude-code/2.1/"
                                                    "claude.app/Contents/MacOS/claude --output-format stream-json"),
                 8: (7, "Tue Oct 13 22:38:52 2026", "/Applications/Claude.app/Contents/Helpers/disclaimer --pgroup"),
                 7: (1, "Tue Oct 13 22:38:00 2026", app)}
        with mock.patch.object(crewchat.os, "getppid", lambda: 10), mock.patch.dict(os.environ, {"CREWCHAT_TEST_PROCESS": ""}):
            self.assertEqual(crewchat.agent_process("claude", table),
                             {"pid": 9, "started": "Tue Oct 13 22:39:29 2026", "app": "claude-app"})
            self.assertIsNone(crewchat.agent_process("cursor", table))  # no cursor-agent above it
            # Windows: a quoted program path, or Claude Code installed with npm (node runs its cli.js).
            win = {10: (9, "a", "cmd /c hook"), 9: (1, "b", '"C:\\Users\\x\\.local\\bin\\claude.exe" --resume s')}
            self.assertEqual(crewchat.agent_process("claude", win)["pid"], 9)
            npm = {10: (9, "a", "cmd /c hook"),
                   9: (1, "b", '"C:\\Program Files\\nodejs\\node.exe" C:\\Users\\x\\npm\\node_modules\\'
                               '@anthropic-ai\\claude-code\\cli.js')}
            self.assertEqual(crewchat.agent_process("claude", npm)["pid"], 9)
            self.assertFalse(crewchat.SESSION_RE.fullmatch("abcdefgh\n"))
            cursor = {10: (9, "a", "sh -c hook"), 9: (1, "b", "/Users/x/.local/bin/cursor-agent --trust")}
            self.assertEqual(crewchat.agent_process("cursor", cursor), {"pid": 9, "started": "b", "app": "terminal"})
            for client, command in (("codex", "/usr/local/lib/node_modules/@openai/codex/vendor/x/codex/codex resume"),
                                    ("codex", "node /usr/local/bin/codex"),
                                    ("gemini", "node /usr/local/bin/gemini -i hello"),
                                    ("gemini", "node /opt/homebrew/lib/node_modules/@google/gemini-cli/dist/index.js"),
                                    ("qwen", "node /usr/local/bin/qwen"),
                                    ("qwen", "node /usr/lib/node_modules/@qwen-code/qwen-code/cli.js -i hi"),
                                    ("opencode", "/Users/x/.opencode/bin/opencode --prompt hi")):
                table = {10: (9, "a", "sh -c hook"), 9: (1, "b", command)}
                self.assertEqual(crewchat.agent_process(client, table)["pid"], 9, command)
                self.assertIsNone(crewchat.agent_process("cursor" if client == "codex" else "codex", table))
            self.assertIsNone(crewchat.agent_process("zed", table))  # a tool it cannot tell: no report

    def test_listening_set_on_the_chat_page_wins_over_the_folder(self):
        session, name = self.linked("page-listen")
        agent = lambda **body: crewchat.post_json(self.base + "/api/agent", self.owner, body)  # noqa: E731
        agent(op="listen", agent=name, mode="on")
        began = time.time()
        self.hook("claude", "prompt", {"session_id": "page-listen"})
        self.assertLess(time.time() - began, 10)  # listening is for the end of a turn: a prompt never waits
        threading.Timer(2, lambda: self.say(name, "worth the wait")).start()
        began = time.time()
        out = self.hook("claude", "stop", {"session_id": "page-listen"}, listen="0")  # the folder says check once
        self.assertIn("worth the wait", out)
        self.assertGreater(time.time() - began, 1.5)  # it waited for the message
        agent(op="listen", agent=name, mode="off")
        began = time.time()
        self.assertEqual(self.hook("claude", "stop", {"session_id": "page-listen"}, listen="600"), "")
        self.assertLess(time.time() - began, 10)  # the folder says wait ten minutes; the page says no
        agent(op="listen", agent=name, mode="default")
        self.assertEqual(self.hub.agents[name]["listen"], "")

    def test_a_launched_cursor_agent_is_alive_before_its_first_turn_ends(self):
        key = self.hub.new_start()
        session = Session(self.base, self.token, "cursor")
        session.call("hub_link", key=key)
        name = session.me
        self.hub.agents[name]["seen"] = 0
        self.hook("cursor", "tool", {"conversation_id": "first-turn"}, env_extra={"CREWCHAT_LINK_KEY": key})
        self.assertGreater(self.hub.agents[name]["seen"], time.time() - 30)  # no stop hook ran yet

    def test_codex_gemini_qwen_and_opencode_get_messages_as_their_hooks_expect(self):
        folder = Path(tempfile.mkdtemp(prefix="other-hooks-", dir=TMP))
        crewchat.install_claude(folder, self.base, self.token)  # Codex's chat: the folder's .mcp.json
        crewchat.install_mcp_settings(folder, self.base, self.token, "gemini")
        crewchat.install_mcp_settings(folder, self.base, self.token, "qwen")
        crewchat.install_mcp_settings(folder, self.base, self.token, "opencode")
        for client, prompt_event in (("codex", "UserPromptSubmit"), ("gemini", "BeforeAgent"), ("qwen", "UserPromptSubmit"),
                                     ("opencode", None)):
            conversation = "other-%s-0001" % client
            asked = self.hook(client, "prompt", {"session_id": conversation}, project=folder)
            if prompt_event:
                asked = json.loads(asked)
                self.assertEqual(asked["hookSpecificOutput"]["hookEventName"], prompt_event)  # strict JSON, or ignored
                asked = asked["hookSpecificOutput"]["additionalContext"]
            key = re.search(r'key "([^"]+)"', asked).group(1)  # OpenCode's plugin adds the text to the message
            session = Session(self.base, self.token, client)
            session.call("hub_link", key=key)
            self.say(session.me, "hello %s" % client)
            told = json.loads(self.hook(client, "stop", {"session_id": conversation}, project=folder))
            self.assertEqual(told["decision"], "block")  # the reason starts the next turn
            self.assertIn("hello %s" % client, told["reason"])
            self.assertEqual(self.hook(client, "stop", {"session_id": conversation}, project=folder), "")  # nothing new
            self.assertEqual(self.hook(client, "stop", {"session_id": "other-never-linked"}, project=folder), "")

    def test_upgrading_adds_new_hooks_and_keeps_the_users_own(self):
        folder = Path(tempfile.mkdtemp(prefix="old-hooks-", dir=TMP))
        mine = {"type": "command", "command": "echo mine"}
        (folder / ".claude").mkdir()
        (folder / ".claude" / "settings.local.json").write_text(json.dumps({"hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "python crewchat.py hook claude stop"}]}],
            "PostToolUse": [{"hooks": [mine]}]}}))
        (folder / ".cursor").mkdir()
        (folder / ".cursor" / "hooks.json").write_text(json.dumps({"version": 1, "hooks": {
            "stop": [{"command": "python crewchat.py hook cursor stop"}]}}))
        self.assertEqual(crewchat.update_hooks(folder), [".claude/settings.local.json", ".cursor/hooks.json",
                                                        ".cursor/cli.json"])
        self.assertEqual(json.loads((folder / ".cursor" / "cli.json").read_text())["permissions"]["deny"], [])
        # A cli.json written before Cursor wanted a deny list gets one, and nothing else changes.
        (folder / ".cursor" / "cli.json").write_text(json.dumps({"permissions": {
            "allow": crewchat.crewchat_rules(crewchat.CURSOR_ALLOW)}}))
        self.assertTrue(crewchat.allow_cursor_tools(folder))
        self.assertEqual(json.loads((folder / ".cursor" / "cli.json").read_text())["permissions"],
                         {"allow": crewchat.crewchat_rules(crewchat.CURSOR_ALLOW), "deny": []})
        self.assertFalse(crewchat.allow_cursor_tools(folder))  # up to date
        self.assertEqual(json.loads((folder / ".claude" / "settings.local.json").read_text())["permissions"],
                         {"allow": crewchat.crewchat_rules(crewchat.CLAUDE_ALLOW)})  # after an upgrade too
        claude = json.loads((folder / ".claude" / "settings.local.json").read_text())["hooks"]
        self.assertEqual(sorted(claude), ["PostToolUse", "Stop", "UserPromptSubmit"])
        tool = [h["command"] for g in claude["PostToolUse"] for h in g["hooks"]]
        self.assertEqual((len(tool), tool[0]), (2, "echo mine"))
        self.assertTrue(tool[1].endswith(" hook claude tool"))
        cursor = json.loads((folder / ".cursor" / "hooks.json").read_text())["hooks"]
        self.assertEqual(sorted(cursor), ["afterFileEdit", "afterMCPExecution", "afterShellExecution", "stop"])
        self.assertEqual(crewchat.update_hooks(folder), [])  # up to date: nothing written
        other = Path(tempfile.mkdtemp(prefix="not-ours-", dir=TMP))
        (other / ".claude").mkdir()
        (other / ".claude" / "settings.local.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [mine]}]}}))
        self.assertEqual(crewchat.update_hooks(other), [])  # a folder crewchat did not connect
        subprocess.run(["git", "init", "-q", str(folder)], check=True)
        (folder / ".cursor" / "hooks.json").write_text(json.dumps({"version": 1, "hooks": {
            "stop": [{"command": "python crewchat.py hook cursor stop"}]}}))
        subprocess.run(["git", "-C", str(folder), "add", ".cursor/hooks.json"], check=True)
        self.assertEqual(crewchat.update_hooks(folder), [])  # committed in the repository: left for `crewchat start`

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
        self.assertEqual(reads, {"hub_agents", "hub_history", "hub_file", "hub_tasks"})
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
        self.assertIn("cursor on", session.text("hub_agents"))
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


class NewProject(unittest.TestCase):
    """create_project on Windows, where a new folder can be held for a moment."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(dir=TMP))
        crewchat.save_config({"project": "Main"}, self.base)

    @contextlib.contextmanager
    def held(self, times):
        """Refuse to move a new project folder into place the first `times` times; yields the moves."""
        real, moves = os.replace, []

        def replace(src, dst):
            if Path(src).name.startswith(".new-"):
                moves.append(dst)
                if len(moves) <= times:
                    raise PermissionError(13, "Access is denied")
            return real(src, dst)

        with mock.patch.object(crewchat.os, "replace", replace), mock.patch.object(crewchat.time, "sleep"):
            yield moves

    def test_the_move_waits_while_windows_holds_the_new_folder(self):
        with self.held(2) as moves:
            entry = crewchat.create_project("t3", self.base)
        self.assertEqual((entry["id"], entry["name"], len(moves)), ("t3", "t3", 3))
        self.assertEqual(sorted(p.name for p in (self.base / "projects").iterdir()), ["t3"])

    def test_a_folder_left_in_the_way_is_passed_over(self):
        (self.base / "projects" / "t3").mkdir(parents=True)  # no config: not a project
        entry = crewchat.create_project("t3", self.base)
        self.assertEqual((entry["id"], entry["name"]), ("t3-2", "t3"))

    def test_a_half_made_project_is_not_a_project_and_is_cleared_later(self):
        left = self.base / "projects" / ".new-thyu0zj5"  # what a failed create left before 0.11.2
        (left / "tokens").mkdir(parents=True)
        crewchat.save_config({"project": "t3"}, left)
        self.assertIsNone(crewchat.find_chat("t3", self.base))
        os.utime(str(left), (time.time() - 3600,) * 2)
        entry = crewchat.create_project("t3", self.base)
        self.assertEqual(entry["id"], "t3")
        self.assertEqual(sorted(p.name for p in (self.base / "projects").iterdir()), ["t3"])

    def test_a_move_that_never_works_leaves_nothing_behind(self):
        with self.held(float("inf")) as moves:
            with self.assertRaises(PermissionError):
                crewchat.create_project("t3", self.base)
        self.assertEqual(len(moves), 50)
        self.assertEqual(list((self.base / "projects").iterdir()), [])


CLAUDE_LINKS = ("documentationUrl", "supportUrl", "privacyPolicyUrl")


class Plugin(unittest.TestCase):
    """The plugin in plugin/, offered to Claude Code (.claude-plugin/) and Cursor (.cursor-plugin/)."""

    def test_the_plugin_matches_this_release(self):
        manifests = {}
        for client in (".claude-plugin", ".cursor-plugin"):
            with self.subTest(client):
                market = json.loads((ROOT / client / "marketplace.json").read_text(encoding="utf-8"))
                entry = next(p for p in market["plugins"] if p["name"] == "crewchat")
                self.assertEqual(entry["source"], "./plugin")
                plugin = ROOT / entry["source"]
                manifests[client] = manifest = json.loads((plugin / client / "plugin.json").read_text(encoding="utf-8"))
                self.assertEqual(manifest["name"], "crewchat")
                # Setup only: `crewchat start` writes each folder's MCP config and hooks, with its
                # token; a plugin shipping them too would connect sessions twice, in every project.
                self.assertFalse({"hooks", "rules", "mcpServers"} & set(manifest))
                self.assertFalse((plugin / "mcp.json").exists() or (plugin / ".mcp.json").exists()
                                 or (plugin / "hooks").exists())
        skill = (ROOT / "plugin" / "skills" / "setup" / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(skill.startswith("---\nname: setup\ndescription: "))
        claude, cursor = manifests[".claude-plugin"], manifests[".cursor-plugin"]
        self.assertEqual(claude["version"], crewchat.__version__)
        self.assertNotIn("version", cursor)  # optional for Cursor, so not one more place to bump
        # Cursor's schema has author {name, email}: no url.
        self.assertEqual(cursor["author"], {"name": claude["author"]["name"]})
        # Claude's directory reads the documentation, support and privacy links from the manifest.
        self.assertEqual({k: v for k, v in cursor.items() if k not in ("logo", "author")},
                         {k: v for k, v in claude.items() if k not in ("version", "author") + CLAUDE_LINKS})
        for key in CLAUDE_LINKS:
            self.assertTrue(claude[key].startswith("https://github.com/deepankar17/crewchat/"))
        self.assertTrue((ROOT / "docs" / "privacy.md").is_file())
        self.assertEqual((ROOT / "plugin" / cursor["logo"]).read_bytes(), (ROOT / "docs" / "images" / "logo.svg").read_bytes())

    def test_the_mcp_registry_entry_matches_this_release(self):
        entry = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
        self.assertEqual(entry["version"], crewchat.__version__)
        self.assertEqual([(p["identifier"], p["version"]) for p in entry["packages"]], [("crewchat", crewchat.__version__)])
        self.assertLessEqual(len(entry["description"]), 100)
        # The registry checks that the PyPI description (README.md) names the entry.
        self.assertIn("<!-- mcp-name: %s -->" % entry["name"], (ROOT / "README.md").read_text(encoding="utf-8"))


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

    def test_a_command_typed_in_a_terminal_says_a_newer_release_is_out(self):
        class Terminal(io.StringIO):
            def isatty(self):
                return True

        def told(command, terminal=True, env=None):
            out = Terminal() if terminal else io.StringIO()
            with mock.patch.object(crewchat.sys, "stderr", out), mock.patch.dict(os.environ, env or {}):
                if not env:
                    os.environ.pop("CREWCHAT_NO_UPDATE_CHECK", None)
                crewchat.tell_update(command)
            return out.getvalue()

        path = crewchat.home() / "update.json"
        try:
            path.write_text(json.dumps({"latest": "99.0.0"}))
            self.assertIn("crewchat 99.0.0 is available", told("agents"))
            self.assertIn("run `crewchat update`", told("tasks"))
            self.assertEqual(told("agents", terminal=False), "")  # captured: scripts, the page's Run, CI
            for command in ("hook", "stdio", "serve", "update", "start", "status"):
                self.assertEqual(told(command), "", command)
            self.assertEqual(told("agents", env={"CREWCHAT_NO_UPDATE_CHECK": "1"}), "")
            path.write_text(json.dumps({"latest": crewchat.__version__}))
            self.assertEqual(told("agents"), "")
        finally:
            path.unlink()

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

    def test_hooks_refresh_points_every_connected_folder_at_this_copy(self):
        folder = Path(tempfile.mkdtemp(prefix="rehook-", dir=TMP)).resolve()
        cli("start", str(folder), "--no-open", "--no-service", "--chat", "rehook")
        hooks = lambda: (folder / ".claude" / "settings.local.json").read_text() + (folder / ".cursor" / "hooks.json").read_text()  # noqa: E731
        self.assertIn(crewchat.self_command()[1], hooks())
        # A reinstall moved crewchat (here: another Python's folder): the old hooks point nowhere.
        moved = ["/tools/crewchat/bin/python", "/tools/crewchat/lib/python3.12/site-packages/crewchat.py"]
        with mock.patch.object(crewchat, "self_command", return_value=moved):
            out = cli("hooks", "refresh")
        self.assertIn("Updated the hooks in 1 connected folder", out)
        self.assertIn("/tools/crewchat/lib/python3.12/site-packages/crewchat.py hook claude stop", hooks())
        self.assertIn("/tools/crewchat/lib/python3.12/site-packages/crewchat.py hook cursor", hooks())
        self.assertNotIn(crewchat.self_command()[1], hooks())
        cli("hooks", "refresh")  # and back
        # With the server off no restart follows (update, install.sh), so the login service is
        # brought up to this copy too; with it running, the restart that follows does that.
        with mock.patch.object(crewchat, "refresh_service") as service:
            with mock.patch.object(crewchat, "server_up", return_value=False):
                cli("hooks", "refresh")
            self.assertEqual(service.call_count, 1)
            cli("hooks", "refresh")
            self.assertEqual(service.call_count, 1)

    def test_a_hook_gives_up_soon_on_a_server_that_is_off(self):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()  # nothing listens there: crewchat stopped, the folder used on its own
        began = time.time()
        with mock.patch.object(crewchat, "SERVER_GONE_SECS", 1):
            with self.assertRaises((urllib.error.URLError, OSError)):
                crewchat.hook_check("http://127.0.0.1:%d" % port, "token", "key", 1800, 0)  # listening 30 min
        self.assertLess(time.time() - began, 10)  # not the 30 minutes it would have listened

    def test_an_installed_crewchat_runs_by_its_own_launcher(self):
        tool = Path(tempfile.mkdtemp(prefix="uv-tool-", dir=TMP)).resolve()
        (tool / "uv-receipt.toml").write_text("")
        site = tool / "lib" / "python3.12" / "site-packages"
        site.mkdir(parents=True)
        exe = tool / ("Scripts/crewchat.exe" if os.name == "nt" else "bin/crewchat")
        with mock.patch.object(crewchat, "__file__", str(site / "crewchat.py")):
            self.assertIsNone(crewchat.launcher())  # not there (yet): Python and the script, as before
            exe.parent.mkdir(parents=True)
            exe.write_text("")
            self.assertEqual(crewchat.launcher(), exe)
            launched = exe.as_posix()
            # Hooks and the login service run it: its path stays put when uv rebuilds the tool on another
            # Python, and a crewchat that is gone fails in the shell, not with Python's exit 2 ("block").
            self.assertEqual(crewchat.hook_command("claude", "stop"), "%s hook claude stop" % launched)
            self.assertTrue(crewchat.is_our_hook(crewchat.hook_command("cursor", "stop")))
            self.assertIn('ExecStart="%s" serve' % launched, crewchat.systemd_unit())
            self.assertIn("<string>%s</string>\n    <string>serve</string>" % launched, crewchat.launchd_plist(False))
        # A checkout runs its script with this Python, as before.
        self.assertTrue(crewchat.hook_command("claude", "stop").endswith(crewchat.self_command()[1] + " hook claude stop"))

    def test_a_hook_gives_up_on_a_server_stopped_while_it_listened(self):
        answers = iter([{"text": ""}])

        def post(url, token, body, timeout=10):
            try:
                return next(answers)  # it answered once, then crewchat was stopped
            except StopIteration:
                raise urllib.error.URLError("connection refused")
        began = time.time()
        with mock.patch.object(crewchat, "SERVER_GONE_SECS", 1), mock.patch.object(crewchat, "post_json", side_effect=post):
            with self.assertRaises(urllib.error.URLError):
                crewchat.hook_check("http://127.0.0.1:1", "token", "key", 1800, 0)
        self.assertLess(time.time() - began, 10)

    @unittest.skipIf(os.name == "nt", "Windows' logon task is not rewritten")
    def test_restart_rewrites_a_login_service_another_copy_wrote(self):
        user = Path(tempfile.mkdtemp(prefix="user-home-", dir=TMP))
        mac = sys.platform == "darwin"
        path = (user / "Library" / "LaunchAgents" / (crewchat.SERVICE_LABEL + ".plist") if mac
                else user / ".config" / "systemd" / "user" / "crewchat.service")
        path.parent.mkdir(parents=True)
        ran = []
        with mock.patch.object(crewchat.Path, "home", return_value=user), \
                mock.patch.object(crewchat, "run", side_effect=lambda c: ran.append(c) or subprocess.CompletedProcess(c, 0, "", "")):
            with mock.patch.object(crewchat, "run_command", return_value=["/gone/python3.14/crewchat.py"]):
                path.write_text(crewchat.launchd_plist(True) if mac else crewchat.systemd_unit())  # before the update
            with mock.patch.object(crewchat, "run_command", return_value=["/tools/crewchat/bin/crewchat"]):
                with mock.patch.object(crewchat, "launcher", return_value=None):  # a checkout: leaves it alone
                    crewchat.refresh_service()
                self.assertIn("/gone/", path.read_text())
                self.assertEqual(ran, [])
                with mock.patch.object(crewchat, "launcher", return_value=Path("/tools/crewchat/bin/crewchat")):
                    crewchat.refresh_service()
                text = path.read_text()
                self.assertIn("/tools/crewchat/bin/crewchat", text)
                self.assertNotIn("/gone/", text)
                if mac:
                    self.assertIn("/usr/bin/caffeinate", text)  # --keep-awake kept
                    self.assertTrue(any("bootout" in c for c in ran))
                ran.clear()
                with mock.patch.object(crewchat, "launcher", return_value=Path("/tools/crewchat/bin/crewchat")):
                    crewchat.refresh_service()  # up to date: left alone
                self.assertEqual(ran, [])

    def test_update_runs_the_new_copy_not_this_one(self):
        ran = []

        def run(command, **kw):
            ran.append(list(command))
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(crewchat, "installed_by_installer", return_value=True), \
                mock.patch.object(crewchat, "find_uv", return_value="/bin/uv"), \
                mock.patch.object(crewchat, "latest_release", return_value="99.0.0"), \
                mock.patch.object(crewchat, "server_up", return_value=True), \
                mock.patch.object(crewchat, "launcher", return_value=Path("/new/bin/crewchat")), \
                mock.patch.object(crewchat.subprocess, "run", side_effect=run), \
                mock.patch.dict(os.environ, {}), contextlib.redirect_stdout(io.StringIO()):
            os.environ.pop("CREWCHAT_SOURCE", None)
            if os.name == "nt":
                self.skipTest("Windows updates in a window of its own")
            crewchat.cmd_update(argparse.Namespace(version=None))
        self.assertEqual(ran[0][:2], ["/bin/uv", "tool"])
        # This process's script may be gone (uv rebuilt the tool, perhaps on another Python).
        self.assertEqual(ran[1:], [["/new/bin/crewchat", "hooks", "refresh"], ["/new/bin/crewchat", "restart"]])

    def test_uninstall_takes_crewchat_out_of_a_folder_and_leaves_the_rest(self):
        mine = Path(tempfile.mkdtemp(dir=TMP))
        crewchat.install_claude(mine, self.base, self.token)
        crewchat.install_cursor(mine, self.base, self.token)
        (mine / ".crewchat-listen").write_text("0\n")
        self.assertEqual(set(crewchat.disconnect_folder(mine)), {
            ".mcp.json", ".claude/settings.local.json", ".cursor/mcp.json", ".cursor/hooks.json", ".cursor/cli.json",
            ".crewchat-listen"})
        for rel in (".mcp.json", ".claude/settings.local.json", ".cursor/mcp.json", ".cursor/hooks.json",
                    ".cursor/cli.json", ".crewchat-listen"):
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
        self.assertIn("*> $null", installs[0])  # PyPI quietly: the GitHub release is the fallback
        self.assertNotIn("*> $null", installs[1])
        self.assertIn("resume", script)
        lowered = script.lower()
        for risky in ("irm ", "iex", "invoke-expression", "invoke-restmethod", "downloadstring", "-enc"):
            self.assertNotIn(risky, lowered)

    def test_project_rules_and_skills_reach_folders_and_agents(self):
        folder = Path(tempfile.mkdtemp(prefix="guided-", dir=TMP)).resolve()
        subprocess.run(["git", "init", "-q", str(folder)], check=False)
        (folder / "AGENTS.md").write_text("# Mine\n")
        cli("start", str(folder), "--no-open", "--no-service", "--chat", "demo")
        skill = folder / crewchat.GUIDE_SKILL
        self.assertTrue(skill.read_text().startswith("---\nname: crewchat\n"))
        self.assertIn("alwaysApply: true", (folder / crewchat.GUIDE_RULE).read_text())
        self.assertIn(crewchat.GUIDE_BEGIN, (folder / "AGENTS.md").read_text())
        session, name = self.agent(token=crewchat.read_token(crewchat.joined_place(folder)[0]))
        session.text("hub_inbox")
        out = crewchat.post_json(self.base + "/api/rules", self.owner, {"rules": "Run the tests first."})
        self.assertGreaterEqual(out["folders"], 1)
        self.assertIn("Run the tests first.", skill.read_text())
        self.assertIn("New rules for this project", session.text("hub_inbox"))
        self.assertIn("Run the tests first.", Session(self.base, self.token).text("hub_link", key="key-rules-0001"))
        # Skills: shared, installed, validated, removed; a folder's own skill is never touched.
        own = folder / ".claude" / "skills" / "deploy"
        own.mkdir(parents=True)
        (own / "SKILL.md").write_text("mine")
        crewchat.post_json(self.base + "/api/skills", self.owner,
                           {"op": "add", "name": "checklist", "files": {"SKILL.md": "---\nname: checklist\n---\n1."}})
        crewchat.post_json(self.base + "/api/skills", self.owner,
                           {"op": "add", "name": "deploy", "files": {"SKILL.md": "theirs"}})
        self.assertTrue((folder / ".claude/skills/checklist/SKILL.md").exists())
        self.assertEqual((own / "SKILL.md").read_text(), "mine")
        for bad in ({"name": "Bad Name", "files": {"SKILL.md": "x"}}, {"name": "crewchat", "files": {"SKILL.md": "x"}},
                    {"name": "esc", "files": {"SKILL.md": "x", "../../evil": "x"}}, {"name": "nofile", "files": {}}):
            with self.assertRaises(urllib.error.HTTPError) as e:
                crewchat.post_json(self.base + "/api/skills", self.owner, dict(bad, op="add"))
            self.assertEqual(e.exception.code, 400)
        self.assertFalse((crewchat.home() / "evil").exists())
        crewchat.post_json(self.base + "/api/skills", self.owner, {"op": "remove", "name": "checklist"})
        self.assertFalse((folder / ".claude/skills/checklist").exists())
        # Only the owner sets them.
        with self.assertRaises(urllib.error.HTTPError) as e:
            crewchat.post_json(self.base + "/api/rules", self.token, {"rules": "agents rule"})
        self.assertEqual(e.exception.code, 403)
        crewchat.post_json(self.base + "/api/rules", self.owner, {"rules": ""})
        self.assertEqual(crewchat.remove_guides(folder)[-1], "AGENTS.md (crewchat's section)")
        self.assertEqual((folder / "AGENTS.md").read_text(), "# Mine\n")
        crewchat.post_json(self.base + "/api/skills", self.owner, {"op": "remove", "name": "deploy"})

    def test_add_an_agent_says_how_to_get_a_missing_tool(self):
        saved = crewchat.find_tool
        crewchat.find_tool = lambda tool, config: "/usr/local/bin/claude" if tool == "claude" else None
        try:
            tools = crewchat.launch_options({"folders": {}})["tools"]
        finally:
            crewchat.find_tool = saved
        self.assertEqual((tools[0]["id"], tools[0]["label"], "missing" in tools[0]), ("claude", "Claude Code", False))
        # What each tool may do without asking, as the page offers it: the server's own list.
        self.assertEqual([p[0] for p in tools[0]["permissions"]], ["ask", "edits", "auto", "all"])
        self.assertEqual([p[0] for p in tools[1]["permissions"]], ["ask", "auto", "all"])
        self.assertEqual(tools[1]["id"], "cursor")
        self.assertIn("cursor.com/install", tools[1]["missing"])

    def test_the_task_sheet(self):
        hub = self.hub
        a, a_name = self.agent()
        b, b_name = self.agent()
        add = lambda title, **kw: crewchat.post_json(self.base + "/api/task", self.owner,  # noqa: E731
                                                     dict({"op": "add", "title": title}, **kw))["id"]
        login = add("Build the login screen", areas="app/login/")
        tests = add("Write login tests", depends=login, areas="app/login/tests/")
        sign = add("Sign the build", where="nowhere-machine")
        found = a.text("hub_task_add", title="Fix the crash on rotate", areas="app/main/")
        crash = re.search(r"#(\w+)", found).group(1)
        nxt = a.text("hub_tasks")
        self.assertIn("#%s [todo] Build the login screen" % login, nxt)
        self.assertNotIn("Write login tests", nxt)  # waits for the login screen
        self.assertNotIn("Sign the build", nxt)  # needs another machine
        self.assertIn("cannot be taken yet: it waits for #%s" % login, a.text("hub_take", id=tests))
        self.assertIn("is yours", a.text("hub_take", id=login))
        # Another task over the same files cannot start while the first is in hand.
        overlap = add("Restyle the login screen", areas="app/login/style/")
        self.assertIn("overlap #%s" % login, b.text("hub_take", id=overlap))
        a.text("hub_update", id=login, status="done", note="built")
        self.assertIn("Write login tests", b.text("hub_tasks"))
        b.text("hub_take", id=tests)
        b.text("hub_update", id=tests, status="todo", note="no time")  # given back
        sheet = {r["id"]: r for r in hub.sheet_rows()}
        self.assertEqual((sheet[login]["status"], sheet[tests]["status"], sheet[tests]["holder"]), ("done", "todo", ""))
        self.assertEqual(sheet[sign]["status"], "todo")
        # The lead (or the owner) hands out a task already on the sheet.
        with self.assertRaises(crewchat.HubError):
            hub.assign_row(a_name, b_name, tests)  # a is not the lead
        crewchat.post_json(self.base + "/api/task", self.owner, {"op": "assign", "task": tests, "agent": b_name})
        self.assertEqual(hub.sheet_rows()[[r["id"] for r in hub.sheet_rows()].index(tests)]["holder"], b_name)
        self.assertIn("Task #%s is yours" % tests, b.text("hub_inbox"))
        # The owner orders, settles and removes tasks; that bookkeeping is not news for agents.
        a.text("hub_inbox")
        crewchat.post_json(self.base + "/api/task", self.owner, {"op": "move", "task": crash, "where": "top"})
        self.assertEqual(hub.sheet_rows()[0]["id"], crash)
        # The board drops a task before another one, or at the end.
        order = lambda: [r["id"] for r in hub.sheet_rows()]  # noqa: E731
        move = lambda where: crewchat.post_json(self.base + "/api/task", self.owner,  # noqa: E731
                                                {"op": "move", "task": crash, "where": where})
        move("before:" + sign)
        self.assertEqual(order()[order().index(crash) + 1], sign)
        move("end")
        self.assertEqual(order()[-1], crash)
        move("before:" + order()[0])
        self.assertEqual(order()[0], crash)
        with self.assertRaises(urllib.error.HTTPError):
            move("before:nope")
        before = order()
        move("before:" + crash)  # its own place
        move("before:" + order()[order().index(crash) + 1])  # just above the next one: where it is
        self.assertEqual(order(), before)
        move("end"); move("end")  # already last
        self.assertEqual(order()[-1], crash)
        crewchat.post_json(self.base + "/api/task", self.owner, {"op": "remove", "task": sign})
        self.assertNotIn(sign, [r["id"] for r in hub.sheet_rows()])
        self.assertEqual(a.text("hub_inbox"), "No new messages.")
        crewchat.post_json(self.base + "/api/task", self.owner, {"op": "status", "task": overlap, "status": "blocked"})
        self.assertEqual({r["id"]: r for r in hub.sheet_rows()}[overlap]["status"], "blocked")
        # The sheet outlives the chat's memory: an old task keeps its state.
        with hub.lock:
            kept = list(hub.messages)
            hub.messages = [m for m in hub.messages if m["id"] != login]
        try:
            self.assertEqual({r["id"]: r for r in hub.sheet_rows()}[login]["status"], "done")
        finally:
            with hub.lock:
                hub.messages = kept
        # Agents cannot use the owner's sheet controls.
        with self.assertRaises(urllib.error.HTTPError) as e:
            crewchat.post_json(self.base + "/api/task", self.token, {"op": "remove", "task": crash})
        self.assertEqual(e.exception.code, 403)
        self.assertIn("The task sheet, in order:", cli("tasks"))

    def test_a_project_folder_removed_by_hand_stops_working(self):
        entry = crewchat.create_project("short-lived")
        project = crewchat.Projects()
        self.assertIsNotNone(project.get(entry["id"]))
        shutil.rmtree(entry["root"])
        self.assertIsNone(project.get(entry["id"]))

    def test_a_skill_file_cannot_name_a_drive_or_leave_its_folder(self):
        # On Windows a backslash is a folder separator (a\\b is a subfolder, which is fine); elsewhere
        # it is part of a name that would become a path on a Windows machine, so it is refused.
        for bad in ("C:foo", "../x", "/etc/x") + (() if os.name == "nt" else ("a\\b",)):
            with self.assertRaises(crewchat.HubError, msg=bad):
                self.hub.add_skill("guarded", {"SKILL.md": "x", bad: "y"})
        self.assertNotIn("guarded", crewchat.project_skills(crewchat.home()))

    def test_a_task_given_back_is_claimed_under_a_new_key(self):
        tid = self.say("all", "changes hands", "task")
        self.assertEqual(self.hub.claim_key(tid), tid)
        a, a_name = self.agent()
        a.text("hub_take", id=tid)
        a.text("hub_update", id=tid, status="todo", note="back")
        self.assertEqual(self.hub.claim_key(tid), tid + "@1")
        # Only its holder gives a task back: anyone else's "todo" is refused, and the key stays.
        b, b_name = self.agent()
        a.text("hub_take", id=tid)
        self.assertIn("you do not hold task #%s" % tid, b.text("hub_update", id=tid, status="todo", note="not mine"))
        self.assertEqual(self.hub.claim_key(tid), tid + "@1")
        self.assertEqual(self.hub.taken[tid], a_name)

    def later(self, minutes):
        """The clock, minutes from now (for the hub's own calls; other threads see it too, briefly)."""
        then = time.time() + minutes * 60
        return mock.patch.object(crewchat.time, "time", lambda: then)

    def test_a_task_whose_holder_goes_silent_is_reclaimed(self):
        hub = self.hub
        tid = self.say("all", "goes quiet", "task")
        a, a_name = self.agent()
        b, b_name = self.agent()
        a.text("hub_take", id=tid)
        self.assertIn("silent for 60 min", cli("tasks", "reclaim"))  # the default
        cli("tasks", "reclaim", "off")
        with self.later(600):
            hub.reclaim_silent()
        self.assertEqual(hub.taken[tid], a_name)  # off: however long it is silent
        for bad in ("5", "\u00b2", "soon"):  # too short to tell a crash from a long build; not a number
            with self.assertRaises(SystemExit):
                cli("tasks", "reclaim", bad)
        self.assertIn("silent for 60 min", cli("tasks", "reclaim", "60"))
        try:
            with self.later(59):
                hub.reclaim_silent()
            self.assertEqual(hub.taken[tid], a_name)
            with self.later(61):
                hub.reclaim_silent()
            row = next(r for r in hub.sheet_rows() if r["id"] == tid)
            # Like giving it back: on the sheet again, under a new claim key, and said in the chat.
            self.assertEqual((row["status"], row["holder"], row["reclaimed"]), ("todo", "", a_name))
            self.assertEqual(hub.claim_key(tid), tid + "@1")
            self.assertIn("crewchat -> all: [UPDATE on #%s: given back] Task #%s reclaimed from %s (silent for 61 min)"
                          % (tid, tid, a_name), b.text("hub_history"))
            self.assertIn("reclaimed from %s" % a_name, cli("tasks"))
            # Back from its silence, the agent is told; it may take the task again while it is free.
            self.assertIn("reclaimed from you while you were silent; it is back on the sheet",
                          a.text("hub_update", id=tid, status="done", note="finished"))
            taken = b.text("hub_take", id=tid)
            self.assertIn("no sign of life (no tool call, no hook) for 60 min", taken)  # told when it takes it
            self.assertIn("for 60 min", b.text("hub_tasks", view="all"))
            self.assertIn("%s holds it now" % b_name, a.text("hub_update", id=tid, status="in_progress", note="still on it"))
            self.assertIn("already taken by " + b_name, a.text("hub_take", id=tid))
            row = next(r for r in hub.sheet_rows() if r["id"] == tid)
            self.assertEqual((row["status"], row["holder"], row["reclaimed"]), ("doing", b_name, a_name))
            # Once it has the task again, it reports on it as before.
            b.text("hub_update", id=tid, status="todo", note="over to you")
            self.assertIn("is yours", a.text("hub_take", id=tid))
            self.assertIn("marked done", a.text("hub_update", id=tid, status="done", note="finished"))
            self.assertEqual(next(r for r in hub.sheet_rows() if r["id"] == tid)["reclaimed"], "")
            saved = json.loads((hub.home / "state.json").read_text(encoding="utf-8"))
            self.assertNotIn("reclaimed", saved["sheet"][tid])
        finally:
            cli("tasks", "reclaim", "off")
        self.assertEqual(hub.reclaim, 0)
        self.assertEqual(crewchat.Hub(hub.roster).reclaim, 0)  # off stays off after a restart
        fresh = Path(tempfile.mkdtemp(prefix="fresh-", dir=TMP))
        crewchat.save_config({"project": "fresh"}, fresh)
        self.assertEqual(crewchat.Hub(crewchat.Roster(fresh))._project_reclaim(), crewchat.RECLAIM_DEFAULT)  # never set

    def test_reclaim_waits_for_a_listening_holder_and_follows_a_tasks_own_setting(self):
        hub = self.hub
        long = self.say("all", "a long build", "task")
        quick = self.say("all", "a quick fix", "task")
        blocked = self.say("all", "needs the owner", "task")
        a, a_name = self.agent()
        b, b_name = self.agent()
        c, c_name = self.agent()
        a.text("hub_take", id=long)
        b.text("hub_take", id=quick)
        c.text("hub_take", id=blocked)
        c.text("hub_update", id=blocked, status="blocked", note="which colour?")
        self.assertIn("Task #%s: tasks stay with their holder" % long, cli("tasks", "reclaim", "off", long))
        cli("tasks", "reclaim", "30", quick)
        self.assertIn("never reclaimed", cli("tasks"))
        with self.later(45):
            hub.reclaim_silent()
        self.assertEqual((hub.taken.get(long), hub.taken.get(quick)), (a_name, None))
        self.assertIn("follows the project's setting", cli("tasks", "reclaim", "default", long))
        cli("tasks", "reclaim", "20")
        try:
            hub.waiting[a_name] = 1  # its hook is waiting for messages right now: it is there
            try:
                with self.later(45):
                    hub.reclaim_silent()
            finally:
                del hub.waiting[a_name]
            self.assertEqual(hub.taken.get(long), a_name)
            self.assertEqual(hub.taken.get(blocked), c_name)  # blocked waits for a person, not its holder
            # With several machines, only the one the task was posted on reclaims it.
            hub.sheet[long]["origin"] = "elsewhere"
            hub.sync = mock.Mock(device_id="here", shared=False)
            try:
                with self.later(45):
                    hub.reclaim_silent()
            finally:
                hub.sync = None
                hub.sheet[long]["origin"] = ""
            self.assertEqual(hub.taken.get(long), a_name)
            with self.later(45):  # taking it does not wait for the server's next check
                self.assertIn("is yours", b.text("hub_take", id=long))
            self.assertEqual(hub.sheet[long]["reclaimed"], a_name)
            # The project's setting is a message, so one set on another machine applies here too.
            hub.ingest({"id": "other-machine-1", "from": "Owner", "to": "all", "kind": "plan", "text": "", "reclaim": "45"})
            self.assertIn("silent for 45 min", cli("tasks", "reclaim"))
            # A value no number reads as (from a machine that let it through) falls back on the project's.
            hub.ingest({"id": "other-machine-2", "from": "Owner", "to": "all", "kind": "plan", "text": "",
                        "task": quick, "reclaim": "\u00b2"})
            with self.later(45):
                hub.reclaim_silent()
        finally:
            cli("tasks", "reclaim", "off")

    def test_the_server_checks_every_project_for_silent_holders(self):
        class Done(Exception):
            pass
        broken, fine = mock.Mock(), mock.Mock()
        broken.hub.reclaim_silent.side_effect = RuntimeError("bad state")
        projects = mock.Mock(all=lambda: [broken, fine])
        with mock.patch.object(crewchat.time, "sleep", side_effect=[None, Done()]), \
                contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(Done):
            crewchat.reclaim_checks(projects)
        fine.hub.reclaim_silent.assert_called_once_with()  # one project's error does not stop the others
        self.assertIn("could not check for silent task holders: bad state", err.getvalue())

    def test_areas_overlap_by_path_prefix(self):
        self.assertTrue(crewchat.areas_overlap(["app/login/"], ["app/login/tests/"]))
        self.assertTrue(crewchat.areas_overlap(["app"], ["app/"]))
        self.assertFalse(crewchat.areas_overlap(["app/login/"], ["app/logout/"]))
        self.assertFalse(crewchat.areas_overlap([], ["app/"]))
        self.assertEqual(crewchat.split_list("#A1, A2,-"), ["A1", "A2"])

    def test_update_refuses_a_copy_the_installer_did_not_put_there(self):
        with self.assertRaises(SystemExit):
            cli("update")


if __name__ == "__main__":
    unittest.main()
