"""End-to-end tests: a real server on a free port, a throwaway home folder, real HTTP."""
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
        # What waits to be saved (Hub._save_soon) is written now, as a stopping server does: a timer left
        # behind would write this class's state into the next class's home.
        crewchat.flush_all(cls.server.RequestHandlerClass.projects)
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
        self.assertEqual(session.hello["capabilities"], {"tools": {"listChanged": True}})  # a lead's list grows
        self.assertIn("crewchat for Demo", session.hello["instructions"])
        self.assertIn("hub_link", session.hello["instructions"])
        tools = crewchat.rpc(self.mcp, self.token, "tools/list", session=session.sid)["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["hub_send", "hub_inbox", "hub_task", "hub_agents",
                                                      "hub_status", "hub_history", "hub_rename", "hub_profile", "hub_file",
                                                      "hub_role", "hub_ask", "hub_answer", "hub_link"])

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
        self.assertEqual(self.hub.boot()["names"], crewchat.AGENT_NAMES)

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

    def test_a_renamed_agent_keeps_its_tokens_and_its_day(self):
        # hub_rename is what agents are told to do: the tokens it used, and its edits and states, go with it.
        session, old = self.agent()
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with self.hub.lock:
            self.hub.usage.setdefault(day, {})[old] = {"claude-sonnet": [1000, 200, 0, 0, 0]}
        self.hub.events.add_edit(old, "app/renamed.py")
        try:
            session.call("hub_rename", name="tokens-kept")
            self.assertNotIn(old, self.hub.usage[day])
            self.assertEqual(self.hub._usage_row("tokens-kept")["today"], 1200)
            self.assertEqual({e["agent"] for e in self.hub.events.edits() if e["path"] == "app/renamed.py"}, {"tokens-kept"})
        finally:
            session.call("hub_rename", name=old)
            self.hub.usage[day].pop(old, None)

    def test_an_edit_the_event_store_cannot_take_still_reaches_the_code_map(self):
        # A locked events.sqlite (a checkpoint, a virus scanner) or a full disk: the hook must not fail, or the
        # agent's hook takes edit tracking as off and stops sending them.
        import sqlite3
        with mock.patch.object(self.hub.events, "add_edit", side_effect=sqlite3.OperationalError("database is locked")), \
                mock.patch("sys.stderr"):
            with self.hub.lock:
                self.hub._edited("maya", "app/locked.py")
        self.assertIn("app/locked.py", self.hub._map_edited)

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
        dev.call("hub_task", action="take", id=task)
        set_check = lambda value: crewchat.post_json(self.base + "/api/task", self.owner,  # noqa: E731
                                                     {"op": "advisor", "value": value})["advisor"]
        self.assertEqual(set_check(""), "off")
        self.assertEqual(set_check("on"), "on")
        try:
            self.assertIn("The chat has no advisor yet", dev.text("hub_task", action="update", id=task, status="done"))
            self.role(adv_name, "advisor")
            self.assertIn("advisor check before done", dev.text("hub_task", action="update", id=task, status="done"))
            bystander, _ = self.agent()
            self.assertIn("only the agent holding task", bystander.text("hub_ask", question="Done?", task=task))
            question, _ = self.hub.ask(dev_name, "Done? Tests pass.", task=task, wait=0)
            adv.text("hub_answer", id=question["id"], text="Add a test for expiry.", verdict="changes")
            self.assertIn("advisor check before done", dev.text("hub_task", action="update", id=task, status="done"))
            question, _ = self.hub.ask(dev_name, "Added the expiry test.", task=task, wait=0)
            # A late answer to an older check does not count: only the newest check does.
            old, _ = question, None
            question, _ = self.hub.ask(dev_name, "Final round.", task=task, wait=0)
            adv.text("hub_answer", id=old["id"], text="Good.", verdict="approve")
            self.assertIn("advisor check before done", dev.text("hub_task", action="update", id=task, status="done"))
            adv.text("hub_answer", id=question["id"], text="Good.", verdict="approve")
            self.assertIn("advisor: approved", dev.text("hub_task", action="view", show="all"))
            # Work started again: the approval was on other work, so it no longer counts.
            dev.text("hub_task", action="update", id=task, status="in_progress", note="one more fix")
            self.assertIn("advisor check before done", dev.text("hub_task", action="update", id=task, status="done"))
            question, _ = self.hub.ask(dev_name, "Fixed.", task=task, wait=0)
            adv.text("hub_answer", id=question["id"], text="Good.", verdict="approve")
            self.assertIn("marked done", dev.text("hub_task", action="update", id=task, status="done"))
            self.assertIn("[QUESTION before done, task #%s]" % task, dev.text("hub_history"))
        finally:
            self.assertEqual(set_check("off"), "off")
            self.role(adv_name, "none")
        self.assertIn("Tasks can be marked done without the advisor's check", dev.text("hub_history"))


class PollAfter(unittest.TestCase):
    def test_the_messages_after_a_number_are_found_by_halving(self):
        hub = crewchat.Hub.__new__(crewchat.Hub)
        for seqs in ([], [1], [1, 2, 3], [2, 5, 9, 10, 40], list(range(1, 2001)), [3, 7] + list(range(100, 160))):
            hub.messages = [{"seq": n} for n in seqs]
            for after in [-1, 0] + seqs + [n + 1 for n in seqs] + [10 ** 6]:
                self.assertEqual(hub.messages[hub._after(after):], [m for m in hub.messages if m["seq"] > after],
                                 (seqs[:5], after))


class EditPaths(unittest.TestCase):
    def test_only_paths_inside_the_project_are_sent_and_kept(self):
        root = Path(tempfile.mkdtemp(prefix="edits-", dir=TMP))
        (root / "src").mkdir()
        call = lambda tool, **args: {"tool_name": tool, "tool_input": args}  # noqa: E731
        self.assertEqual(crewchat.edited_path("claude", call("Edit", file_path=str(root / "src" / "a.py")), root), "src/a.py")
        self.assertEqual(crewchat.edited_path("claude", call("Write", file_path="src/new.py"), root), "src/new.py")
        self.assertEqual(crewchat.edited_path("claude", call("NotebookEdit", notebook_path=str(root / "n.ipynb")), root), "n.ipynb")
        self.assertEqual(crewchat.edited_path("gemini", call("replace", file_path=str(root / "b.py")), root), "b.py")
        self.assertEqual(crewchat.edited_path("cursor", {"file_path": str(root / "c.ts")}, root), "c.ts")
        for outside in ("/etc/hosts", str(root / ".." / "elsewhere.py"), "../up.py", str(root)):
            self.assertEqual(crewchat.edited_path("claude", call("Edit", file_path=outside), root), "", outside)
        self.assertEqual(crewchat.edited_path("claude", call("Read", file_path=str(root / "a.py")), root), "")
        self.assertEqual(crewchat.edited_path("claude", call("Bash", command="rm -rf /"), root), "")
        self.assertEqual(crewchat.clean_edit_path("src\\win\\path.cs"), "src/win/path.cs")
        self.assertEqual(crewchat.clean_edit_path("./a//b.py"), "a/b.py")
        for bad in ("/abs.py", "C:/x.py", "a/../../b.py", "a\nb", "", "x" * 400, None, 3):
            self.assertEqual(crewchat.clean_edit_path(bad), "", bad)

    def test_timelines_from_recorded_states(self):
        store = crewchat.EventStore(Path(tempfile.mkdtemp(prefix="timeline-", dir=TMP)) / "events.sqlite")
        for ts, agent, state in ((50, "maya", "idle"), (100, "maya", "working"), (160, "maya", "listening"),
                                 (400, "maya", "working"), (500, "maya", "listening"),
                                 (120, "teo", "working"), (130, "teo", "working")):
            store.add_activity(agent, state, ts)
        lines = store.timelines(since=90, until=1000, grace=15)
        self.assertEqual(lines["maya"], [[90, 100, "idle"],  # the state at 90: the last one before it
                                         [100, 160, "working"], [160, 400, "listening"], [400, 500, "working"],
                                         [500, 515, "listening"], [515, 1000, "idle"]])  # stopped asking: idle
        self.assertEqual(lines["teo"], [[120, 1000, "working"]])  # one state again is one piece
        self.assertEqual(store.timelines(since=2000, until=3000)["maya"], [[2000, 2015, "listening"],
                                                                          [2015, 3000, "idle"]])

    def test_two_builds_at_once_count_each_message_once(self):
        # The server builds every project's totals at start; the page may ask for them before that is done.
        log = Path(tempfile.mkdtemp(prefix="analytics-", dir=TMP)) / "messages.jsonl"
        log.write_text("".join(json.dumps({"id": "A%d" % n, "seq": n, "ts": 1000 + n, "from": "maya", "to": "all",
                                           "text": "hi"}) + "\n" for n in range(1, 41)))
        analytics = crewchat.Analytics(log)
        analytics.cutoff = 40
        real = crewchat.log_message
        with mock.patch.object(crewchat, "log_message", lambda raw: (time.sleep(0.002), real(raw))[1]):
            builds = [threading.Thread(target=analytics.build) for _ in range(2)]
            for build in builds:
                build.start()
            for build in builds:
                build.join()
        self.assertEqual(analytics.total, 40)

    def test_the_event_store(self):
        store = crewchat.EventStore(Path(tempfile.mkdtemp(prefix="events-", dir=TMP)) / "events.sqlite")
        store.add_edit("maya", "app/a.kt", "12", ts=100)
        store.add_edit("teo", "app/a.kt", "", ts=200)
        store.add_edit("maya", "app/b.kt", "12", ts=300)
        self.assertEqual([e["path"] for e in store.edits(since=150)], ["app/a.kt", "app/b.kt"])
        self.assertEqual([e["ts"] for e in store.edits(task="12")], [100, 300])
        self.assertEqual(store.files(), [  # top: who edited it most (on a tie, last)
            {"path": "app/a.kt", "edits": 2, "agents": ["maya", "teo"], "top": "teo", "last": 200},
            {"path": "app/b.kt", "edits": 1, "agents": ["maya"], "top": "maya", "last": 300}])
        # Written on the hook path, under the hub's lock: one sync per row (WAL, still durable), and a task's
        # edits found by index rather than by reading them all.
        db = store._conn()
        self.assertEqual((db.execute("PRAGMA journal_mode").fetchone()[0], db.execute("PRAGMA synchronous").fetchone()[0]),
                         ("wal", 2))  # 2: FULL, as the records cannot be rebuilt
        plan = db.execute("EXPLAIN QUERY PLAN SELECT ts FROM edits WHERE ts >= 0 AND task = '12'").fetchall()
        self.assertIn("edits_task", " ".join(str(row) for row in plan))
        store.close()
        self.assertEqual(len(crewchat.EventStore(store.path).edits()), 3)  # kept on disk


class Faces(unittest.TestCase):
    """A new agent's face, made of parts (a skin, hair, a hat, glasses, clothes, ...) at random, and drawn as SVG."""

    def test_every_part_draws_a_well_formed_picture(self):
        import xml.etree.ElementTree as ET
        codes = []
        for i, (_, n) in enumerate(crewchat.MIX_PARTS):  # each option of each part, the others at their first
            for option in range(n):
                parts = [0] * len(crewchat.MIX_PARTS)
                parts[i] = option
                codes.append(".".join(map(str, parts)))
        codes += [crewchat.new_mix()[4:] for _ in range(300)]
        for code in codes:
            svg = crewchat.mix_svg(code)
            root = ET.fromstring(svg)
            self.assertEqual((root.tag, root.get("viewBox")), ("{http://www.w3.org/2000/svg}svg", "0 0 128 128"), code)
            self.assertNotIn(b"<script", svg)

    def test_a_code_is_one_number_per_part_in_its_range(self):
        last = ".".join(str(n - 1) for _, n in crewchat.MIX_PARTS)
        self.assertIsNotNone(crewchat.mix_parts(last))
        too_far = ".".join(str(n) for _, n in crewchat.MIX_PARTS)
        for bad in ("", "1.2.3", too_far, last + ".0", "a" + last[1:], "0" + last, "<svg>", "-" + last):
            self.assertIsNone(crewchat.mix_parts(bad), bad)
            self.assertIsNone(crewchat.mix_svg(bad), bad)
        self.assertEqual(crewchat.clean_picture("mix:" + last), "mix:" + last)
        self.assertEqual(crewchat.clean_picture("mix:" + too_far), "")  # another machine's odd one: initials
        self.assertIsNotNone(crewchat.mix_parts(last.rsplit(".", 1)[0]))  # a code from before looks (eleven parts)
        self.assertIsNone(crewchat.mix_parts(last.rsplit(".", 2)[0]))  # but not ten

    def test_a_face_suits_its_name(self):
        for name, want in crewchat.NAME_LOOKS.items():
            for _ in range(5):
                parts = crewchat.mix_parts(crewchat.new_mix(0, name=name)[4:])
                self.assertIn(parts[4], crewchat.MIX_HAIR_FOR[want], name)  # hair that goes with the name
                self.assertTrue(crewchat.mix_fits(parts, name))
                if name not in crewchat.INDIAN_NAMES:
                    self.assertIn(parts[11], crewchat.MIX_LOOK_FOR[want], name)  # lashes for "f"; a beard maybe for "m"
        self.assertIn("Maya", crewchat.NAME_LOOKS)
        self.assertNotIn("Noor", crewchat.NAME_LOOKS)  # a name for anyone: any face
        self.assertTrue(set(crewchat.NAME_LOOKS) <= set(crewchat.AGENT_NAMES))
        maya = crewchat.mix_parts(crewchat.new_mix(0, name="Maya")[4:])
        self.assertFalse(crewchat.mix_fits(maya, "Teo"))
        self.assertTrue(crewchat.mix_fits(maya, "Noor") and crewchat.mix_fits(maya, "Someone"))

    def test_an_indian_name_gets_an_indian_face(self):
        self.assertTrue(crewchat.INDIAN_NAMES <= set(crewchat.AGENT_NAMES))
        self.assertEqual(len(set(crewchat.AGENT_NAMES)), len(crewchat.AGENT_NAMES))
        bindis = 0
        for name in crewchat.INDIAN_NAMES:
            for _ in range(8):
                parts = crewchat.mix_parts(crewchat.new_mix(0, name=name)[4:])
                self.assertIn(parts[1], crewchat.MIX_INDIAN["skin"], name)  # a warm brown skin
                self.assertIn(parts[5], crewchat.MIX_INDIAN["haircolour"], name)  # black or dark brown hair
                if crewchat.NAME_LOOKS.get(name) == "f":
                    self.assertIn(parts[11], (1, 3), name)  # lashes, and half the time a bindi
                    bindis += parts[11] == 3
                else:
                    self.assertNotEqual(parts[11], 3, name)
        self.assertGreater(bindis, 0)
        self.assertIn(b"#c8283c", crewchat.mix_svg("0.0.0.0.0.0.0.0.0.0.0.3"))  # the bindi drawn
        for _ in range(200):  # never on a face whose name does not call for it
            self.assertNotEqual(crewchat.mix_parts(crewchat.new_mix(0, name="Noor")[4:])[11], 3)
        light = [0] * 12
        light[11] = 1
        self.assertFalse(crewchat.mix_fits(light, "Priya"))  # the lightest skin tone is not one an Indian name gets

    def test_a_new_face_works_at_the_machines_device_and_is_nobody_elses(self):
        for device in range(3):
            self.assertEqual(crewchat.mix_parts(crewchat.new_mix(device)[4:])[10], device)  # the device part
        taken = {crewchat.new_mix(0) for _ in range(50)}
        self.assertNotIn(crewchat.new_mix(0, taken), taken)
        self.assertIn(crewchat.device_kind(), (0, 1, 2))
        try:
            crewchat.device_kind.cache_clear()
            with mock.patch.dict(os.environ, {"CLAUDE_CODE_REMOTE": "true"}):
                self.assertEqual(crewchat.device_kind(), 2)  # a cloud session sits on a cloud
        finally:
            crewchat.device_kind.cache_clear()


class FacesOnThePage(Base):
    """The server draws a face from its code, for the page; the dice on the page give an agent a new one."""

    def test_the_server_draws_a_face_and_refuses_a_bad_code(self):
        code = crewchat.new_mix()[4:]
        with urllib.request.urlopen(self.base + "/pictures/mix/%s.svg" % code) as r:
            self.assertEqual(r.headers["Content-Type"], "image/svg+xml")
            self.assertEqual(r.headers["Content-Security-Policy"], "default-src 'none'")
            self.assertEqual(r.read(), crewchat.mix_svg(code))
        for bad in ("9.9.9", "..%2Fart"):
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(self.base + "/pictures/mix/%s.svg" % bad)
            self.assertEqual(e.exception.code, 404)

    def test_a_renamed_agent_gets_a_face_that_suits_its_new_name(self):
        name = self.hub.agent_for(self.hub.open_session("macbook", "claude"))
        self.hub.rename(name, "Maya")
        self.hub.set_profile("Maya", "mix:new")
        face = self.hub.agents["Maya"]["picture"]
        self.assertTrue(crewchat.mix_fits(crewchat.mix_parts(face[4:]), "Maya"))
        self.hub.rename("Maya", "Teo")
        after = self.hub.agents["Teo"]["picture"]
        self.assertNotEqual(after, face)
        self.assertTrue(crewchat.mix_fits(crewchat.mix_parts(after[4:]), "Teo"))
        self.assertEqual(crewchat.mix_parts(after[4:])[10], crewchat.mix_parts(face[4:])[10])  # at the same device
        self.hub.rename("Teo", "Noor")
        self.assertEqual(self.hub.agents["Noor"]["picture"], after)  # it suits a name for anyone: kept
        self.hub.set_profile("Noor", "art:owl")
        self.hub.rename("Noor", "Maya")
        self.assertEqual(self.hub.agents["Maya"]["picture"], "art:owl")  # a picture chosen stays
        self.hub.remove("Maya")

    def test_the_dice_give_an_agent_a_new_face(self):
        name = self.hub.agent_for(self.hub.open_session("macbook", "claude"))
        before = self.hub.agents[name]["picture"]
        self.hub.set_profile(name, "mix:new")
        after = self.hub.agents[name]["picture"]
        self.assertTrue(after.startswith("mix:") and crewchat.mix_parts(after[4:]) and after != before)
        with self.assertRaises(crewchat.HubError):
            self.hub.set_profile(name, "mix:1.2")


class AnalyticsTotals(unittest.TestCase):
    """/api/analytics' numbers, worked out from the whole message log (roadmap X1)."""

    def hub(self, lines=()):
        root = Path(tempfile.mkdtemp(prefix="analytics-", dir=TMP))
        crewchat.save_config({"project": "numbers"}, root)
        (root / "messages.jsonl").write_text("".join(json.dumps(m) + "\n" for m in lines))
        return crewchat.Hub(crewchat.Roster(root))

    def test_a_day_of_work_in_numbers(self):
        t0 = time.time() - 3600
        msg = lambda seq, sender, to, kind="msg", **kw: dict({"id": str(seq), "seq": seq, "ts": t0 + seq * 60,  # noqa: E731
                                                            "from": sender, "to": to, "kind": kind, "text": "x"}, **kw)
        lines = [msg(1, "Owner", "all", "task"), msg(2, "Maya", "all", "take", task="1"),
                 msg(3, "Maya", "Owner", "update", task="1", status="in_progress"),
                 msg(4, "Maya", "Owner", "update", task="1", status="blocked"),
                 msg(6, "Maya", "Owner", "update", task="1", status="in_progress"),
                 msg(7, "Maya", "Owner", "update", task="1", status="review"),
                 msg(8, "Maya", "Owner", "update", task="1", status="done"),
                 msg(9, "Owner", "all", "task"), msg(10, "Teo", "all", "take", task="9"),
                 msg(11, "Teo", "Owner", "update", task="9", status="review"),
                 msg(12, "Teo", "Owner", "update", task="9", status="in_progress"),
                 msg(13, "Teo", "Owner", "update", task="9", status="review"),
                 msg(14, "crewchat", "all", "update", task="9", status="todo", reclaimed="Teo"),
                 msg(15, "Wren", "Teo", "answer", answers="9", verdict="approve"),
                 msg(16, "Teo", "Maya", files=[{"id": "a" * 16}, {"id": "b" * 16}]),
                 msg(17, "crewchat", "all", "event")]
        hub = self.hub(lines)
        report = hub.analytics.report(hub)
        self.assertEqual(report["messages"], 15)  # the event (who joined) is not a message
        tasks = report["tasks"]
        self.assertEqual((tasks["posted"], tasks["taken"], tasks["done"]), (2, 2, 1))
        self.assertEqual(tasks["time_to_take"], 60)
        self.assertEqual(tasks["cycle_time"], 7 * 60)
        self.assertEqual(tasks["time_blocked"], 2 * 60)  # blocked at 4, going again at 6
        self.assertEqual(tasks["review_pass_rate"], 0.5)  # #1 passed its first review, #9 did not
        self.assertEqual((report["reclaims"], report["files"], report["verdicts"]), (1, 2, {"approve": 1}))
        self.assertIn(["Maya", "Owner", 5], report["pairs"])
        self.assertEqual(report["agents"]["Teo"]["messages"], 5)  # the take, three updates, the files
        self.assertNotIn("crewchat", report["agents"])
        self.assertEqual(sum(h["messages"] for h in report["hours"]), 15)
        # New messages count as they come, once.
        with hub.lock:
            hub._append("Maya", "all", "and one more", "msg")
        self.assertEqual(hub.analytics.report(hub)["messages"], 16)

    def test_a_task_still_in_review_has_not_failed_it(self):
        t0 = time.time() - 3600
        msg = lambda seq, sender, kind, **kw: dict({"id": str(seq), "seq": seq, "ts": t0 + seq * 60, "from": sender,  # noqa: E731
                                                    "to": "all", "kind": kind, "text": "x"}, **kw)
        hub = self.hub([msg(1, "Owner", "task"), msg(2, "Maya", "take", task="1"),
                        msg(3, "Maya", "update", task="1", status="review")])
        self.assertIsNone(hub.analytics.report(hub)["tasks"]["review_pass_rate"])  # no review has ended yet: no rate
        with hub.lock:
            hub._append("Maya", "all", "x", "update", task="1", status="done")
        self.assertEqual(hub.analytics.report(hub)["tasks"]["review_pass_rate"], 1.0)

    def test_the_whole_log_counts_not_only_what_is_in_memory(self):
        n = crewchat.KEEP_MESSAGES + 500
        hub = self.hub([{"id": str(i), "seq": i, "ts": 1.0 + i, "from": "Maya", "to": "all", "kind": "msg", "text": "x"}
                        for i in range(1, n + 1)])
        with hub.lock:
            hub._append("Teo", "all", "after the start", "msg")  # before the build: still counted once
        self.assertEqual(len(hub.messages), crewchat.KEEP_MESSAGES)
        report = hub.analytics.report(hub)
        self.assertEqual(report["messages"], n + 1)
        self.assertEqual(hub.analytics.report(hub)["messages"], n + 1)  # built once


class TokenPrices(unittest.TestCase):
    def test_prices_and_transcripts(self):
        self.assertEqual(crewchat.model_price("claude-opus-5-5"), crewchat.PRICES["claude-opus-5-5"])
        self.assertEqual(crewchat.model_price("claude-opus-5-5[1m]"), crewchat.PRICES["claude-opus-5-5"])
        self.assertEqual(crewchat.model_price("claude-haiku-4-5-20251001"), crewchat.PRICES["claude-haiku-4-5"])
        self.assertEqual(crewchat.model_price("claude-opus-5"), crewchat.PRICES["claude-opus-5"])  # not 5-5's
        self.assertIsNone(crewchat.model_price("gpt-9"))
        self.assertEqual(crewchat.usage_cost({"gpt-9": [1, 1, 1, 1, 1]}), (0.0, ["gpt-9"]))
        path = Path(tempfile.mkdtemp(prefix="transcript-", dir=TMP)) / "t.jsonl"
        line = lambda mid, n: json.dumps({"type": "assistant", "message": {"id": mid, "model": "m",  # noqa: E731
                                                                         "usage": {"input_tokens": n, "output_tokens": n}}})
        path.write_text(line("a", 1) + "\n" + line("a", 1) + "\n" + line("b", 2) + "\n" + line("c", 3)[:20])  # c: half written
        found, offset, last = crewchat.transcript_usage(path)
        self.assertEqual((found, last), ({"m": [3, 3, 0, 0, 0]}, "b"))
        with open(path, "a") as f:
            f.write(line("c", 3)[20:] + "\n" + json.dumps({"type": "assistant", "message": {"id": "s", "model": "<synthetic>",
                                                                                          "usage": {"input_tokens": 0}}}) + "\n")
        self.assertEqual(crewchat.transcript_usage(path, offset, last)[0], {"m": [3, 3, 0, 0, 0]})  # c, finished; nothing for 0s
        path.write_text(line("z", 5) + "\n")  # a shorter, new transcript: read from its start
        self.assertEqual(crewchat.transcript_usage(path, offset, last)[0], {"m": [5, 5, 0, 0, 0]})


class TrimmedTools(Base):
    """What every agent loads is short (roadmap P2): one hub_task for the task sheet, and inbox and history
    answers that fit a cap without losing a message."""

    def test_the_tool_definitions_are_short_and_task_tools_are_one(self):
        self.assertLess(len(json.dumps(crewchat.TOOLS)), 7000)
        names = [t["name"] for t in crewchat.TOOLS]
        self.assertIn("hub_task", names)
        self.assertFalse(set(names) & set(crewchat.OLD_TASK_TOOLS))
        guide = crewchat.RULES
        for action in crewchat.TASK_ACTIONS:  # how to use them is in the guide every folder gets
            self.assertIn("`%s`" % action, guide)

    def test_an_old_task_tool_says_to_restart_the_session(self):
        session, _ = self.agent()
        for old, action in crewchat.OLD_TASK_TOOLS.items():
            out = session.call(old, id="1")
            self.assertTrue(out["isError"], old)
            text = out["content"][0]["text"]
            self.assertIn("now hub_task with action %s" % action, text)
            self.assertIn("restart it", text)

    def test_hub_task_takes_one_action_at_a_time(self):
        session, name = self.agent()
        self.assertIn("action is view, add, take, assign, areas, update", session.text("hub_task", action="peek"))
        self.assertIn("action is", session.text("hub_task"))
        added = session.text("hub_task", action="add", title="Write the release notes", areas="docs/")
        tid = re.search(r"#(\w+)", added).group(1)
        self.assertIn("Write the release notes", session.text("hub_task", action="view", show="all"))
        self.assertIn("is yours", session.text("hub_task", action="take", id=tid))
        self.assertIn("marked in progress", session.text("hub_task", action="update", id=tid, status="in_progress", note="on it"))
        self.assertIn("not both", session.text("hub_task", action="assign", to=name, id=tid, title="x"))

    def test_a_long_inbox_comes_in_parts_and_loses_nothing(self):
        sender, _ = self.agent()
        reader, reader_name = self.agent()
        reader.text("hub_inbox")  # what was there before
        texts = ["Message %02d: %s" % (i, "word " * 80) for i in range(50)]  # about 20,000 characters in all
        for t in texts:
            sender.call("hub_send", to=reader_name, text=t)
        self.assertIn("more unread", reader.text("hub_inbox", peek=True))
        first = reader.text("hub_inbox", peek=True)
        self.assertEqual(reader.text("hub_inbox", peek=True), first)  # peek marks nothing read
        seen, parts = [], 0
        while True:
            out = reader.text("hub_inbox")
            if out == "No new messages.":
                break
            parts += 1
            body = out.split("\n\n", 1)[1].split("\n\n")[0]
            self.assertLessEqual(len(body), crewchat.MAX_TOOL_OUTPUT)
            seen += re.findall(r"Message \d\d: ", out)
        self.assertGreater(parts, 2)
        self.assertEqual(seen, ["Message %02d: " % i for i in range(50)])  # every one, once, in order
        big = "x" * crewchat.MAX_TEXT
        sender.call("hub_send", to=reader_name, text=big)
        self.assertIn(big, reader.text("hub_inbox"))  # one message bigger than the cap: whole, never cut
        history = reader.text("hub_history", limit=50)
        self.assertIn("older left out", history)
        self.assertIn(big, history)  # the newest that fit

    def test_messages_show_a_short_time(self):
        now = time.time()
        self.assertEqual(crewchat.short_time(now), time.strftime("%H:%M", time.gmtime(now)))
        self.assertEqual(crewchat.short_time(now - 3 * 86400), time.strftime("%m-%d %H:%M", time.gmtime(now - 3 * 86400)))
        msg = {"id": "7", "ts": now, "kind": "msg", "from": "Maya", "to": "all", "text": "hi"}
        self.assertTrue(crewchat.fmt(msg, "Teo", True).startswith("[#7 %s]" % crewchat.short_time(now)))
        self.assertIn("UTC", crewchat.fmt(msg, "Teo"))  # hooks keep the full time

    def test_the_old_task_tools_leave_the_allow_lists(self):
        settings = {"permissions": {"allow": ["mcp__crewchat__hub_take", "mcp__crewchat__hub_send", "Bash(ls)"]}}
        self.assertTrue(crewchat.allow_rule(settings, crewchat.CLAUDE_ALLOW))
        allow = settings["permissions"]["allow"]
        self.assertNotIn("mcp__crewchat__hub_take", allow)
        self.assertIn("mcp__crewchat__hub_task", allow)
        self.assertIn("Bash(ls)", allow)  # the user's own stay
        self.assertFalse(crewchat.allow_rule(settings, crewchat.CLAUDE_ALLOW))  # up to date


class Tasks(Base):
    def setUp(self):
        self.crew = [self.agent() for _ in range(4)]

    def test_exactly_one_agent_wins_a_race_for_a_task(self):
        task = self.say("all", "Add a dark icon", "task")
        self.assertIn("Owner -> all: [TASK, open] Add a dark icon", self.crew[0][0].text("hub_inbox"))
        results = {}
        threads = [threading.Thread(target=lambda s=s, n=n: results.update({n: s.call("hub_task", action="take", id=task)}))
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
        self.assertIn("was given to " + a_name, b.text("hub_task", action="take", id=task))
        self.assertFalse(a.call("hub_task", action="take", id=task)["isError"])

    def test_a_task_number_can_be_written_with_a_hash(self):
        (a, a_name) = self.crew[0]
        task = self.say("all", "hash me", "task")
        self.assertIn("Task #%s is yours" % task, a.text("hub_task", action="take", id="#%s" % task))

    def test_only_tasks_can_be_taken(self):
        message = self.say("all", "just a note")
        self.assertTrue(self.crew[0][0].call("hub_task", action="take", id=message)["isError"])
        self.assertTrue(self.crew[0][0].call("hub_task", action="take", id=999999)["isError"])

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


class TaskAreas(Base):
    """Roadmap C2: a task's areas suggested from the files its holder edited (C0), accepted by the owner or the
    lead, and an overlap check that sees imports (C1)."""

    def setUp(self):
        self.crew = [self.agent() for _ in range(2)]
        maps = mock.patch.object(self.hub, "_maps", None)  # the code map: none unless a test makes one
        maps.start()
        self.addCleanup(maps.stop)

    def task(self, title, areas=""):
        return crewchat.post_json(self.base + "/api/task", self.owner, {"op": "add", "title": title, "areas": areas})["id"]

    def sheet(self, mid):
        return self.hub.sheet[mid]

    def suggestions(self, mid):
        return [m for m in self.hub.messages if m.get("task") == mid and m.get("suggested")]

    def test_the_folders_it_edited_are_suggested_when_it_goes_to_review(self):
        (a, a_name), _ = self.crew
        mid = self.task("Fix the login", "login/")
        a.call("hub_task", action="take", id=mid)
        for path in ("login/form.kt", "session/store.kt", "session/keys/token.kt", "docs/login.md", "README.md"):
            self.hub.events.add_edit(a_name, path, mid)
        self.hub.events.add_edit(a_name, "unrelated/file.kt", "")  # not while it held the task
        a.text("hub_task", action="update", id=mid, status="review", note="ready")
        self.assertEqual(self.sheet(mid)["suggested"], ["README.md", "docs/", "session/"])  # login/ it has already
        [notice] = self.suggestions(mid)
        self.assertEqual((notice["from"], notice["to"]), (crewchat.SERVER_NAME, crewchat.OWNER))  # no lead: the owner
        self.assertIn("outside its areas: README.md, docs/, session/", notice["text"])
        self.assertIn("crewchat tasks areas %s accept" % mid, notice["text"])
        self.assertIn("suggested files README.md, docs/, session/", cli("tasks"))
        crewchat.post_json(self.base + "/api/task", self.owner, {"op": "status", "task": mid, "status": "done"})
        self.assertEqual(len(self.suggestions(mid)), 1)  # done, with nothing new since: not suggested again
        out = crewchat.post_json(self.base + "/api/task", self.owner, {"op": "areas", "task": mid, "accept": True})
        self.assertEqual(out["areas"], ["login/", "README.md", "docs/", "session/"])
        self.assertNotIn("suggested", self.sheet(mid))
        with self.assertRaises(urllib.error.HTTPError):
            crewchat.post_json(self.base + "/api/task", self.owner, {"op": "areas", "task": mid, "accept": True})

    def test_the_lead_accepts_them_with_hub_task(self):
        (a, a_name), (lead, lead_name) = self.crew
        crewchat.post_json(self.base + "/api/role", self.owner, {"agent": lead_name, "role": "lead"})
        self.addCleanup(crewchat.post_json, self.base + "/api/role", self.owner, {"agent": lead_name, "role": "none"})
        mid = self.task("Add search")
        a.call("hub_task", action="take", id=mid)
        self.hub.events.add_edit(a_name, "web/search/box.tsx", mid)
        a.text("hub_task", action="update", id=mid, status="review", note="ready")
        inbox = lead.text("hub_inbox")
        self.assertIn("but has no areas: web/search/", inbox)
        self.assertIn("hub_task action=areas id=%s accept=true" % mid, inbox)
        self.assertIn("only the lead or the owner", a.text("hub_task", action="areas", id=mid, accept=True))
        self.assertIn("Task #%s's files: web/search/." % mid, lead.text("hub_task", action="areas", id=mid, accept=True))
        self.assertIn("Task #%s's files: web/, api/." % mid, lead.text("hub_task", action="areas", id=mid, areas="web/,api/"))
        self.assertIn("files web/, api/", lead.text("hub_task", action="view", show="all"))
        self.assertIn("Task #%s's files: web/." % mid, cli("tasks", "areas", mid, "web/"))

    def test_an_edit_goes_to_the_task_its_holder_works_on_now(self):
        (a, a_name), _ = self.crew
        first, second, third = self.task("Holding one"), self.task("Holding two"), self.task("Holding three")
        for mid in (first, second):
            a.call("hub_task", action="take", id=mid)
        with self.hub.lock:
            self.assertEqual(self.hub._usage_task(a_name), second)  # taken last
        a.text("hub_task", action="update", id=first, status="in_progress", note="on it")
        with self.hub.lock:
            self.assertEqual(self.hub._usage_task(a_name), first)  # reported on last
        a.call("hub_task", action="take", id=third)
        with self.hub.lock:
            self.assertEqual(self.hub._usage_task(a_name), third)

    def test_nothing_is_suggested_without_edit_tracking(self):
        (a, a_name), _ = self.crew
        mid = self.task("Quiet one", "quiet/")
        a.call("hub_task", action="take", id=mid)
        self.hub.events.add_edit(a_name, "elsewhere/x.py", mid)
        with mock.patch.object(crewchat.Hub, "edits_on", lambda hub: False):
            a.text("hub_task", action="update", id=mid, status="review", note="ready")
        self.assertEqual(self.suggestions(mid), [])
        a.text("hub_task", action="update", id=mid, status="in_progress", note="again")
        a.text("hub_task", action="update", id=mid, status="review", note="ready")  # tracking on: suggested now
        self.assertEqual(self.sheet(mid)["suggested"], ["elsewhere/"])

    def test_a_changed_interface_is_told_to_the_tasks_that_import_it(self):
        # Roadmap C3, done when: renaming a parameter of a function another agent's area imports sends exactly
        # one notice; a change to a body alone sends none.
        code = Path(tempfile.mkdtemp(prefix="iface-", dir=TMP)).resolve()
        files = {"paylib/parse.py": "def read(path):\n    return path\n", "paylib/__init__.py": "",
                 "payapp/main.py": "from paylib.parse import read\nread('x')\n", "paydocs/notes.py": ""}
        for rel, text in files.items():
            (code / rel).parent.mkdir(parents=True, exist_ok=True)
            (code / rel).write_text(text)
        folders = mock.patch.object(crewchat.Hub, "project_folders", lambda hub, place=None: [code])
        folders.start()
        self.addCleanup(folders.stop)
        import crewchat_map
        self.hub._maps = crewchat_map.Maps(Path(tempfile.mkdtemp(prefix="iface-maps-", dir=TMP)))
        self.addCleanup(self.hub._maps.close)
        self.hub.update_map()
        (a, a_name), (b, b_name) = self.crew
        c, c_name = self.agent()
        # A works on scripts and changes the parser on the way; B's task is on the app, which imports it.
        mine, theirs, docs = self.task("The scripts", "payscripts/"), self.task("The app", "payapp/"), \
            self.task("Notes", "paydocs/")
        for agent, mid in ((a, mine), (b, theirs), (c, docs)):
            self.assertNotIn("Error", agent.text("hub_task", action="take", id=mid))
        for agent in (a, b, c):
            agent.text("hub_inbox")

        def edit(text):
            (code / "paylib/parse.py").write_text(text)
            later = time.time() + 30 + len(self.hub.messages)
            os.utime(code / "paylib/parse.py", (later, later))
            with self.hub.lock:
                self.hub._edited(a_name, "paylib/parse.py")
            self.hub.update_map()

        start = time.time()
        edit("def read(source):\n    return source\n")
        edit("def read(source):\n    return source.strip()\n")  # still at it: one notice, when it settles
        self.assertEqual(self.hub.interface_notices(self.hub._maps, now=start + 10), [])  # not settled yet
        [notice] = self.hub.interface_notices(self.hub._maps, now=start + crewchat.NOTICE_SETTLE + 5)
        self.assertEqual((notice["to"], notice["task"], notice["changed"]), (b_name, theirs, "paylib/parse.py"))
        inbox = b.text("hub_inbox")
        self.assertIn("%s changed paylib/parse.py, which your task #%s uses: read(path) is now read(source)" % (
            a_name, theirs), inbox)
        self.assertNotIn("paylib/parse.py", a.text("hub_inbox") + c.text("hub_inbox"))  # not its editor, nor others
        edit("def read(source):\n    return source.lower()\n")  # a body only
        self.assertEqual(self.hub.interface_notices(self.hub._maps, now=start + 10000), [])

    def test_taking_or_giving_a_task_warns_of_the_code_others_work_on(self):
        # Roadmap C4: a note naming the other task and its holder, firm on certain links, a hint on guesses;
        # never in the way.
        code = Path(tempfile.mkdtemp(prefix="blast-", dir=TMP)).resolve()
        # The guess: a service injected by a name two files define (a guess with either readers, C9's too).
        files = {"blastlib/parse.py": "def read():\n    pass\n", "blastlib/__init__.py": "",
                 "blastapp/main.py": "from blastlib.parse import read\n",
                 "blastng/cart.component.ts": "@Component({ selector: 'app-cart', template: `<p></p>` })\n"
                                              "export class CartComponent { constructor(private prices: PriceService) {} }\n",
                 "blastng/a/price.service.ts": "@Injectable()\nexport class PriceService {}\n",
                 "blastng/b/price.service.ts": "@Injectable()\nexport class PriceService {}\n", "blastdocs/notes.py": ""}
        for rel, text in files.items():
            (code / rel).parent.mkdir(parents=True, exist_ok=True)
            (code / rel).write_text(text)
        import crewchat_map
        maps = crewchat_map.Maps(Path(tempfile.mkdtemp(prefix="blast-maps-", dir=TMP)))
        self.addCleanup(maps.close)
        maps.check([code])
        self.hub._maps = maps
        (a, a_name), (b, b_name) = self.crew
        pricing, cart = self.task("Pricing rules", "blastng/a/"), self.task("The cart", "blastng/cart.component.ts")
        lib, app, docs = self.task("The parser", "blastlib/"), self.task("The app", "blastapp/"), self.task("Notes", "blastdocs/")
        a.call("hub_task", action="take", id=pricing)
        out = b.text("hub_task", action="take", id=cart)  # a guess only (a name defined twice): taken, with a hint
        self.assertIn("Task #%s is yours" % cart, out)
        self.assertIn("Heads up: #%s (%s) may touch code this task uses (a guess from names)" % (pricing, a_name), out)
        a.call("hub_task", action="take", id=lib)
        # The owner gives the app to b: certain, so a firm note, in the message b gets; nothing stopped.
        crewchat.post_json(self.base + "/api/task", self.owner, {"op": "assign", "task": app, "agent": b_name})
        self.assertEqual(self.hub.taken[app], b_name)
        inbox = b.text("hub_inbox")
        self.assertIn("Heads up: #%s (%s) works on code that this task's files import or are imported by" % (lib, a_name), inbox)
        self.assertIn("agree any change to what they share with %s first" % a_name, inbox)
        self.assertNotIn("Heads up", a.text("hub_task", action="take", id=docs))  # nothing it shares: nothing said
        self.hub._maps = None
        self.assertEqual(self.hub.blast_radius(b_name, cart), "")  # no map: no note

    def test_joining_agents_get_a_brief_of_the_code(self):
        # Roadmap C6: a skill (and a Cursor rule) made from the code map and the sheet, stamped with the commit;
        # AGENTS.md only points to it (owner, 18 Oct: not every turn's cost).
        code = Path(tempfile.mkdtemp(prefix="brief-", dir=TMP)).resolve()
        for rel, text in {"brieflib/core.py": "class Engine:\n    def run(self):\n        pass\n", "brieflib/__init__.py": "",
                          "briefapp/main.py": "from brieflib.core import Engine\n", "AGENTS.md": "# Our notes\n"}.items():
            (code / rel).parent.mkdir(parents=True, exist_ok=True)
            (code / rel).write_text(text)
        folders = mock.patch.object(crewchat.Hub, "project_folders", lambda hub, place=None: [code])
        folders.start()
        self.addCleanup(folders.stop)
        import crewchat_map
        self.hub._maps = crewchat_map.Maps(Path(tempfile.mkdtemp(prefix="brief-maps-", dir=TMP)))
        self.addCleanup(self.hub._maps.close)
        (a, a_name), _ = self.crew
        mid = self.task("Speed up the engine", "brieflib/")
        a.call("hub_task", action="take", id=mid)
        self.hub.update_map()
        brief = (code / crewchat.BRIEF_SKILL).read_text()
        self.assertTrue(brief.startswith("---\nname: crewchat-brief\n"))
        self.assertIn("at commit `none`", brief)  # not a git folder: stamped as such
        self.assertIn("- `brieflib/core.py`: used by 1; defines Engine", brief)
        self.assertIn("- `briefapp/main.py`", brief)  # where it starts
        self.assertIn("- #%s Speed up the engine: %s, in progress; files brieflib/" % (mid, a_name), brief)
        self.assertIn("alwaysApply: false", (code / crewchat.BRIEF_RULE).read_text())
        self.assertIn("read %s when you join" % crewchat.BRIEF_SKILL, (code / "AGENTS.md").read_text())
        self.assertNotIn("used by 1", (code / "AGENTS.md").read_text())  # a pointer, not the brief
        with mock.patch.object(crewchat_map.CodeMap, "overview") as overview:
            self.assertEqual(self.hub.update_map(), {})
        self.assertFalse(overview.called)  # no edit, no commit, the same sheet: not even made again
        self.assertEqual(self.hub.write_briefs(self.hub._maps), [])  # nothing new: nothing written
        a.text("hub_task", action="update", id=mid, status="done", note="faster")
        self.assertEqual(self.hub.write_briefs(self.hub._maps), [str(code)])  # the sheet moved: written again
        self.assertNotIn("#%s Speed up the engine" % mid, (code / crewchat.BRIEF_SKILL).read_text())
        crewchat.remove_guides(code)
        self.assertFalse((code / crewchat.BRIEF_SKILL).exists() or (code / crewchat.BRIEF_RULE).exists())

    def test_suggested_areas_travel_as_a_message_field(self):
        mid = self.task("From another machine")
        self.assertTrue(self.hub.ingest({"id": "far-%s" % mid, "ts": time.time(), "from": crewchat.SERVER_NAME,
                                         "to": crewchat.OWNER, "text": "suggested", "kind": "msg", "task": mid,
                                         "suggested": "api/,docs/"}))
        self.assertEqual(self.sheet(mid)["suggested"], ["api/", "docs/"])

    def test_tasks_whose_code_imports_the_others_do_not_run_at_once(self):
        # Done when: two tasks that share no prefix, one importing the other's code, overlap; without the map
        # (C0 alone) only prefixes count.
        code = Path(tempfile.mkdtemp(prefix="areas-", dir=TMP)).resolve()
        for rel, text in {"shopapp/main.py": "from shoplib.parse import read\n", "shoplib/__init__.py": "",
                          "shoplib/parse.py": "def read():\n    pass\n", "shoptools/run.py": "import json\n"}.items():
            (code / rel).parent.mkdir(parents=True, exist_ok=True)
            (code / rel).write_text(text)
        (a, a_name), (b, b_name) = self.crew
        first, second, third = self.task("Change the parser", "shoplib/"), self.task("Change the app", "shopapp/"), \
            self.task("Change the tools", "shoptools/")
        a.call("hub_task", action="take", id=first)
        free = lambda: [r["id"] for r in self.hub.sheet_rows(b_name, "next")]  # noqa: E731
        self.assertIn(second, free())  # no map: shopapp/ and shoplib/ share no prefix
        import crewchat_map
        maps = crewchat_map.Maps(Path(tempfile.mkdtemp(prefix="areas-maps-", dir=TMP)))
        self.addCleanup(maps.close)
        maps.check([code])
        self.hub._maps = maps
        self.assertNotIn(second, free())
        self.assertIn(third, free())
        self.assertIn("import, or are imported by, those of #%s, which %s is working on" % (first, a_name),
                      b.text("hub_task", action="take", id=second))
        a.text("hub_task", action="update", id=first, status="done", note="done")
        self.assertIn(second, free())  # the parser is done: the app task may start


class LeadTools(Base):
    """Owner, 18 Oct: the lead and the advisor get hub_code (C5 history, C7 split); every other agent's tools stay
    as short as they are. A session whose role changes is told its tool list changed (MCP list_changed)."""

    def test_a_new_lead_on_an_older_protocol_is_told_to_start_again_for_hub_code(self):
        for streams, told in ((False, True), (True, False)):
            sid = self.hub.open_session("macbook", "claude")
            name = self.hub.agent_for(sid)
            with self.hub.lock:
                self.hub.sessions[sid]["streams"] = streams  # what its initialize said (list_changed from 2025-03-26)
            msg = self.hub.set_role(crewchat.OWNER, name, "lead")
            self.assertEqual("start a new session to have it" in msg["text"], told, streams)
            self.hub.set_role(crewchat.OWNER, name, "")
            self.hub.remove(name)

    def role(self, name, role):
        crewchat.post_json(self.base + "/api/role", self.owner, {"agent": name, "role": role})

    def tools(self, session):
        return [t["name"] for t in crewchat.rpc(session.mcp, self.token, "tools/list", {}, session=session.sid)["result"]["tools"]]

    def call_stream(self, session, tool, **arguments):
        """A tool call, every message of the answer: notifications first, the result last."""
        body = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
        return crewchat.post_json(session.mcp, self.token, body, headers={"Mcp-Session-Id": session.sid}, stream=True)

    def test_only_the_lead_and_the_advisor_have_hub_code(self):
        self.assertLessEqual(len(json.dumps(crewchat.TOOLS)), 7000)  # everyone's, as P2 left it
        self.assertLessEqual(len(json.dumps(crewchat.TOOLS + crewchat.LEAD_TOOLS)), 7700)
        self.assertTrue(Session(self.base, self.token).hello["capabilities"]["tools"]["listChanged"])
        session, name = self.agent()
        self.assertNotIn("hub_code", self.tools(session))
        self.assertIn("is for the lead and the advisor", session.text("hub_code", action="history", path="app/x.py"))
        self.assertEqual([m.get("method") for m in self.call_stream(session, "hub_agents")], [None])  # nothing new
        self.role(name, "lead")
        told = self.call_stream(session, "hub_agents")  # its next answer says its tools changed
        self.assertEqual(told[0], {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        self.assertIn("result", told[-1])
        self.assertIn("hub_code", self.tools(session))
        self.assertEqual(len(self.call_stream(session, "hub_agents")), 1)  # told once
        self.assertIn("hub_code split", session.text("hub_role"))  # the lead's instructions say how
        self.role(name, "none")
        self.assertEqual(self.call_stream(session, "hub_agents")[0].get("method"), "notifications/tools/list_changed")
        self.assertNotIn("hub_code", self.tools(session))
        # A client on the 2024-11-05 protocol, which knew no answers as event streams, is not told: plain JSON.
        old = Session(self.base, self.token)
        old_name = old.me
        with self.hub.lock:
            self.hub.sessions[old.sid]["streams"] = False
        self.tools(old)
        self.role(old_name, "advisor")
        self.assertEqual([m.get("method") for m in self.call_stream(old, "hub_agents")], [None])
        self.assertIn("hub_code", self.tools(old))  # it gets the tool when it asks again (a new session)
        self.role(old_name, "none")

    def test_a_files_history_says_who_for_which_task_and_what_was_said(self):
        # Roadmap C5.
        (lead, lead_name), (dev, dev_name) = self.agent(), self.agent()
        self.role(lead_name, "advisor")
        task = self.say("all", "Fix the sync of history/engine.py", "task")
        dev.call("hub_task", action="take", id=task)
        with self.hub.lock:
            for _ in range(3):
                self.hub.events.add_edit(dev_name, "history/engine.py", task)
            self.hub.events.add_edit(lead_name, "history/engine.py", "")
            self.hub.events.add_edit(dev_name, "history/other.py", task)
        dev.call("hub_task", action="update", id=task, status="review", note="engine.py retries now")
        dev.call("hub_send", to="all", text="heads up: engine.py changed its retry loop")
        out = lead.text("hub_code", action="history", path="history/engine.py")
        self.assertIn("history/engine.py: edited 4 times, by %s (3), %s (1)." % (dev_name, lead_name), out)
        self.assertIn("Tasks: #%s Fix the sync of history/engine.py (review, %s)." % (task, dev_name), out)
        self.assertIn("[REVIEW] engine.py retries now", out)
        self.assertIn("heads up: engine.py changed its retry loop", out)  # named in a message
        self.assertIn("edited 5 times", lead.text("hub_code", action="history", path="history/"))  # a folder
        self.assertIn("no edits recorded", lead.text("hub_code", action="history", path="nowhere/x.py"))
        self.hub.chat_index.close()
        (self.hub.home / crewchat.HISTORY_FILE).unlink()  # derived: deleted, it is built again
        self.assertIn("[REVIEW] engine.py retries now", lead.text("hub_code", action="history", path="history/engine.py"))

    def test_a_task_split_into_groups_that_hang_together(self):
        # Roadmap C7: groups by imports, then folders.
        code = Path(tempfile.mkdtemp(prefix="split-", dir=TMP)).resolve()
        files = {"splitapp/sync/engine.py": "from splitapp.sync import queue\n", "splitapp/sync/queue.py": "",
                 "splitapp/sync/__init__.py": "", "splitapp/ui/view.py": "from splitapp.ui import theme\n",
                 "splitapp/ui/theme.py": "", "splitapp/ui/__init__.py": "", "splitapp/__init__.py": ""}
        for rel, text in files.items():
            (code / rel).parent.mkdir(parents=True, exist_ok=True)
            (code / rel).write_text(text)
        import crewchat_map
        maps = crewchat_map.Maps(Path(tempfile.mkdtemp(prefix="split-maps-", dir=TMP)))
        self.addCleanup(maps.close)
        maps.check([code])
        saved, self.hub._maps = self.hub._maps, maps
        self.addCleanup(setattr, self.hub, "_maps", saved)
        lead, lead_name = self.agent()
        self.role(lead_name, "lead")
        self.addCleanup(self.role, lead_name, "none")
        task = crewchat.post_json(self.base + "/api/task", self.owner, {"op": "add", "title": "The app", "areas": "splitapp/"})["id"]
        out = lead.text("hub_code", action="split", id=task, count=2)
        self.assertIn("Files of task #%s (splitapp/) in 2 groups" % task, out)
        self.assertRegex(out, r"(?m)^1\. splitapp/(sync|ui)/ \(\d files\): ")
        self.assertRegex(out, r"(?m)^2\. splitapp/(sync|ui)/ ")
        self.assertIn("is not on the task sheet", lead.text("hub_code", action="split", id="999999"))

    def test_the_stdio_relay_passes_the_notice_on(self):
        session, name = self.agent()
        relay = crewchat.StdioRelay(self.base + "/mcp", self.token)
        relay.session = session.sid
        out = io.BytesIO()
        with mock.patch.object(sys, "stdout", mock.Mock(buffer=out)):
            relay.send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            self.role(name, "advisor")
            relay.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "hub_agents", "arguments": {}}})
        lines = [json.loads(l) for l in out.getvalue().decode().splitlines()]
        self.assertEqual([l.get("id", l.get("method")) for l in lines], [1, "notifications/tools/list_changed", 2])


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
        boot = json.loads(self.raw("/api/boot", opener=opener)[1])
        qa = next(r for r in boot["roles"] if r["name"] == "qa")
        self.assertEqual((qa["title"], qa["picture"], qa["source"]), ("QA", "art:scientist", "pack"))
        self.assertNotIn("description", qa)  # instructions come from /api/roles

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
        self.assertIn("With a [lead] in the chat, leave it to the lead", lead.hello["instructions"])
        self.assertIn("only the lead assigns", dev.call("hub_task", action="assign", to=qa_name, title="x")["content"][0]["text"])
        lead.call("hub_task", action="take", id=job)
        out = lead.text("hub_task", action="assign", to=dev_name, title="Build the theme switch", parent=job)
        piece = re.search(r"task #(\w+)", out).group(1)
        self.assertEqual(self.hub.taken[piece], dev_name)
        got = dev.text("hub_inbox")
        self.assertIn("[TASK, assigned to you, part of #%s] Build the theme switch" % job, got)
        # Progress goes to the lead, and shows on the task.
        self.assertIn(lead_name, dev.text("hub_task", action="update", id=piece, status="review",
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
        self.assertIn("status must be one of", qa.call("hub_task", action="update", id=piece, status="sideways")["content"][0]["text"])
        # Done: the lead is told the developer is free, to hand it the next piece.
        dev.call("hub_task", action="update", id=piece, status="done", note="Merged")
        done = lead.text("hub_inbox")
        self.assertIn("[UPDATE on #%s: done] Merged" % piece, done)
        self.assertIn("%s is free. Check the work, then give it its next task with hub_task assign" % dev_name, done)
        self.assertIn("give that agent its next task with hub_task assign right away", crewchat.ROLES["lead"][1])
        for name in (lead_name, dev_name, qa_name):
            self.role(name, "none")

    def test_without_a_lead_updates_go_to_whoever_posted_the_task(self):
        dev, dev_name = self.agent()
        job = self.say(dev_name, "Fix the login test", "task")
        dev.call("hub_task", action="take", id=job)
        self.assertIn("the owner has been told", dev.text("hub_task", action="update", id=job, status="in_progress"))
        update = self.hub.history(1)[0]
        self.assertEqual((update["to"], update["kind"], update["text"]), ("Owner", "update", "in progress"))

    def test_a_task_from_the_owner_is_confirmed_to_the_owner_even_with_a_lead(self):
        (lead, lead_name), (dev, dev_name) = self.agent(), self.agent()
        self.role(lead_name, "lead")
        job = self.say(dev_name, "Work on T229", "task")
        taken = dev.text("hub_task", action="take", id=job)
        self.assertIn("Before any other work, send hub_task action=update id=%s status=in_progress" % job, taken)
        self.assertIn("the owner has been told", dev.text("hub_task", action="update", id=job, status="in_progress",
                                                          note="Starting: reading the backup code"))
        update = self.hub.history(1)[0]
        self.assertEqual((update["to"], update["text"]), ("Owner", "Starting: reading the backup code"))
        self.assertIn("first hub_task update it to\n  in_progress", dev.hello["instructions"])
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
        self.assertIn("hub_task assign", lead["description"])
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

    def test_the_poll_carries_a_tag_and_api_boot_the_rest(self):
        # What changes only with the server or the roles comes from /api/boot (roadmap P4); the poll says
        # when to fetch it again.
        self.assertEqual(self.raw("/api/boot")[0], 401)
        opener, _ = self.owner_browser()
        boot = json.loads(self.raw("/api/boot", opener=opener)[1])
        moved = {"roles", "statuses", "tools", "presets", "art", "pictures", "models", "advisors", "names", "images",
                 "max_upload", "releases"}
        self.assertEqual(set(boot), moved | {"tag"})
        poll = json.loads(self.raw("/api/poll?after=-1&v=-1&wait=0", opener=opener)[1])
        self.assertFalse(set(poll) & moved)
        self.assertEqual(poll["boot"], boot["tag"])
        self.assertEqual(self.poll()["boot"], boot["tag"])  # the same while nothing changes
        self.op(op="create", name="tagger", label="Tagger", description="You tag things.")
        try:
            changed = self.hub.boot()
            self.assertNotEqual(changed["tag"], boot["tag"])  # a role edited: the page fetches again
            self.assertEqual(self.poll()["boot"], changed["tag"])
            self.assertIn("tagger", [r["name"] for r in changed["roles"]])
        finally:
            self.op(op="delete", name="tagger")
        self.assertEqual(self.hub.boot()["tag"], boot["tag"])

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
        page = {r["name"]: r for r in self.hub.boot()["roles"]}
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
        self.assertNotIn("docs", [r["name"] for r in self.hub.boot()["roles"]])
        self.assertIn("no role called docs", self.op(op="edit", name="docs", label="x")["error"])

    def test_a_pack_sets_the_crew_and_takes_away_roles_it_does_not_have(self):
        session, name = self.agent()
        crewchat.post_json(self.base + "/api/role", self.owner, {"agent": name, "role": "qa"})
        session.text("hub_inbox")
        self.assertEqual(self.poll()["crew"], "software")
        listing = self.op(op="pack", pack="youtube")
        self.assertEqual(listing["pack"], "youtube")
        poll, boot = self.poll(), self.hub.boot()
        self.assertEqual((poll["pack"], poll["crew"]), ("youtube", "general"))
        self.assertEqual(next(r for r in self.hub.rows() if r["agent"] == name)["role"], "")  # no qa in youtube
        self.assertIn("You no longer have a role", session.text("hub_inbox"))
        self.assertEqual(next(r for r in boot["roles"] if r["name"] == "lead")["title"], "Producer")
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
        a.text("hub_task", action="take", id=api)
        a.text("hub_task", action="update", id=api, status="blocked", note="Which auth: tokens or cookies?")
        b.text("hub_task", action="take", id=review)
        b.text("hub_task", action="update", id=review, status="review", note="Ready for a look")
        b.text("hub_task", action="take", id=back)
        b.text("hub_task", action="update", id=back, status="todo", note="No time, sorry")
        a.text("hub_send", to="Owner", text="A question for you")
        needs = self.hub.needs_you()
        kinds = [(n["kind"], n.get("task") or n.get("text")) for n in needs]
        self.assertEqual(kinds[:4], [("blocked", api), ("review", review), ("message", "A question for you"),
                                     ("given_back", back)])
        blocked = needs[0]
        self.assertEqual((blocked["agent"], blocked["text"], blocked["blocking"]), (a_name, "Which auth: tokens or cookies?", 2))
        self.assertEqual(needs[3]["text"], "No time, sorry")
        # Once handled, an item leaves the list.
        a.text("hub_task", action="update", id=api, status="in_progress", note="Going with tokens")
        crewchat.post_json(self.base + "/api/task", self.owner, {"op": "status", "task": review, "status": "done"})
        msg = needs[2]
        self.assertEqual(crewchat.post_json(self.base + "/api/read", self.owner, {"seq": msg["seq"]})["read"], msg["seq"])
        left = [n["kind"] for n in self.hub.needs_you()]
        self.assertNotIn("blocked", left)
        self.assertNotIn("review", left)
        self.assertNotIn("message", left)
        self.assertIn("given_back", left)
        b.text("hub_task", action="take", id=back)
        self.assertEqual(self.hub.needs_you(), [])

    def test_an_answered_question_waits_on_the_agent_not_the_owner(self):
        a, a_name = self.agent()
        mid = self.add("Pick the icons")
        a.text("hub_task", action="take", id=mid)
        a.text("hub_task", action="update", id=mid, status="blocked", note="Outline or filled?")
        self.assertIn(mid, [n.get("task") for n in self.hub.needs_you()])
        self.hub.send(crewchat.OWNER, a_name, "Re #%s: Outline" % mid)  # as the answer box in Needs you sends it
        self.assertNotIn(mid, [n.get("task") for n in self.hub.needs_you()])  # the agent's turn now
        self.hub.send(crewchat.OWNER, a_name, "Re #%s1: about another task" % mid)
        a.text("hub_task", action="update", id=mid, status="blocked", note="Which size?")  # asks again: it needs the owner again
        self.assertIn(mid, [n.get("task") for n in self.hub.needs_you()])
        b, b_name = self.agent()
        self.hub.send(crewchat.OWNER, b_name, "Re #%s: Small" % mid)  # to another agent: not an answer to this one
        self.hub.send(crewchat.OWNER, "all", "Re #%s: Small" % mid)
        self.assertIn(mid, [n.get("task") for n in self.hub.needs_you()])
        review = self.add("Polish the icons")
        a.text("hub_task", action="take", id=review)
        a.text("hub_task", action="update", id=review, status="review", note="Have a look")
        self.hub.send(crewchat.OWNER, a_name, "Re #%s: thanks" % review)
        self.assertIn(review, [n.get("task") for n in self.hub.needs_you()])  # a review waits for Mark done
        for t in (mid, review):
            crewchat.post_json(self.base + "/api/task", self.owner, {"op": "status", "task": t, "status": "done"})

    def test_a_note_is_credited_to_the_agent_that_wrote_it(self):
        a, a_name = self.agent()
        b, b_name = self.agent()
        mid = self.add("Migrate the database")
        a.text("hub_task", action="take", id=mid)
        b.text("hub_task", action="update", id=mid, status="blocked", note="OK to drop the old tables?")  # not b's task
        item = next(n for n in self.hub.needs_you() if n.get("task") == mid)
        self.assertEqual((item["agent"], item["holder"], item["text"]), (b_name, a_name, "OK to drop the old tables?"))
        a.text("hub_task", action="update", id=mid, status="in_progress", note="carrying on")

    def test_what_the_owner_blocked_is_not_listed_back(self):
        mid = self.add("Wait for the designer")
        crewchat.post_json(self.base + "/api/task", self.owner, {"op": "status", "task": mid, "status": "blocked"})
        self.assertNotIn(mid, [n.get("task") for n in self.hub.needs_you()])
        crewchat.post_json(self.base + "/api/task", self.owner, {"op": "remove", "task": mid})

    def test_the_page_hears_which_tasks_were_taken_off_the_sheet(self):
        mid = self.add("Write the release notes")
        self.assertNotIn(mid, self.hub.poll(-1, -1, 0)["removed"])
        crewchat.post_json(self.base + "/api/task", self.owner, {"op": "remove", "task": mid})
        self.assertIn(mid, self.hub.poll(-1, -1, 0)["removed"])  # its card in the chat says so, not "Open"

    def test_a_long_chain_of_tasks_is_counted_without_recursion(self):
        first = prev = self.add("Step 0")
        for i in range(1, 1200):
            prev = self.add("Step %d" % i, depends=prev)
        a, _ = self.agent()
        a.text("hub_task", action="take", id=first)
        a.text("hub_task", action="update", id=first, status="blocked", note="stuck")
        item = next(n for n in self.hub.needs_you() if n.get("task") == first)
        self.assertEqual(item["blocking"], 1199)
        a.text("hub_task", action="update", id=first, status="done", note="unstuck")

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
        self.launched, self.opened_env = [], []
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

    def test_new_agent_says_what_a_tool_still_needs(self):
        home = Path(tempfile.mkdtemp(prefix="tool-home-", dir=TMP)).resolve()
        folder = str(self.folder)
        answers = {}  # what each tool's own status command says: (exit code, output)
        ready = lambda tool, folder="": [(i["state"], i["text"], i["fix"]) for i in  # noqa: E731
                                         crewchat.tool_readiness(tool, folder, fresh=True)]
        with mock.patch.object(crewchat, "user_home", lambda: home), \
                mock.patch.dict(os.environ, {"CODEX_HOME": str(home / ".codex")}), \
                mock.patch.object(crewchat, "status_of", lambda command: answers.get(command[1], (None, ""))):
            for key in ("GEMINI_API_KEY", "OPENAI_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GENAI_USE_GCA",
                        "CLAUDE_CONFIG_DIR"):
                os.environ.pop(key, None)
            # Signed in, or not, as each tool's own status command says.
            answers["auth"] = (0, '{"loggedIn": false, "authMethod": "none"}')
            self.assertEqual(ready("claude")[0], ("todo", "Claude Code is not signed in", "claude, then /login"))
            answers["auth"] = (0, 'warning\n{"loggedIn": true, "authMethod": "claude.ai"}')
            self.assertEqual(ready("claude")[0][:2], ("ok", "Signed in"))
            answers["status"] = (0, "Not logged in")
            self.assertEqual(ready("cursor")[0], ("todo", "Cursor is not signed in", "cursor-agent login"))
            answers["status"] = (0, "\u2713 Logged in as someone")
            self.assertEqual(ready("cursor")[0][:2], ("ok", "Signed in"))
            answers["login"] = (1, "Not logged in")
            self.assertEqual(ready("codex")[0], ("todo", "Codex CLI is not signed in", "codex login"))
            (home / ".codex").mkdir()
            (home / ".codex" / "config.toml").write_text('model_provider = "ollama"\n')
            self.assertEqual(ready("codex")[0][:2], ("ok", "Uses its own model provider (ollama)"))
            answers["login"] = (0, "Logged in using an API key - ***")
            self.assertEqual(ready("codex")[0][:2], ("ok", "Signed in with an API key"))
            # Gemini CLI and Qwen Code: by their settings and credentials.
            self.assertEqual(ready("gemini")[0][0], "todo")
            (home / ".gemini").mkdir()
            (home / ".gemini" / "settings.json").write_text('{"security": {"auth": {"selectedType": "oauth-personal"}}}')
            self.assertEqual(ready("gemini")[0][:2], ("todo", "Gemini CLI is set to sign in, but has not yet"))
            (home / ".gemini" / "oauth_creds.json").write_text("{}")
            self.assertEqual(ready("gemini")[0][:2], ("ok", "Signed in with Google"))
            (home / ".qwen").mkdir()
            (home / ".qwen" / "settings.json").write_text('{"security": {"auth": {"selectedType": "openai"}}}')
            self.assertEqual(ready("qwen")[0][0], "note")  # a key crewchat cannot see from here
            with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "x"}):
                self.assertEqual(ready("qwen")[0][:2], ("ok", "Set up (openai)"))
            answers["auth"] = (0, "\u2514  0 credentials")
            self.assertEqual(ready("opencode")[0][0], "note")  # it has free models of its own
            answers["auth"] = (0, "\u2514  \x1b[90m2\x1b[0m credentials")
            self.assertEqual(ready("opencode")[0][:2], ("ok", "Signed in to 2 providers"))
            # In the folder: what each asks once (trust, crewchat's hooks), and whether it already did.
            answers["auth"] = (0, '{"loggedIn": true}')
            self.assertEqual(ready("claude", folder)[1][0], "note")
            (home / ".claude.json").write_text(json.dumps({"projects": {str(self.folder.parent): {"hasTrustDialogAccepted": True}}}))
            self.assertEqual(ready("claude", folder)[1][:2], ("ok", "Trusts this folder"))  # a folder it is inside
            crewchat.install_codex()
            self.assertEqual([i[0] for i in ready("codex", folder)[1:]], ["note", "note"])
            with open(home / ".codex" / "config.toml", "a") as f:
                f.write('\n[projects.%s]\ntrust_level = "trusted"\n\n[hooks.state."%s:stop:0:0"]\ntrusted_hash = "h"\n'
                        % (json.dumps(folder), crewchat.codex_config()))
            self.assertEqual([i[:2] for i in ready("codex", folder)[1:]],
                             [("ok", "Trusts this folder"), ("ok", "crewchat's hooks are approved")])
            self.assertEqual([i[0] for i in ready("gemini", folder)[1:]], ["note", "note"])
            (home / ".gemini" / "trustedFolders.json").write_text(json.dumps({folder + "/inside": "TRUST_PARENT"}))
            (home / ".gemini" / "trusted_hooks.json").write_text(json.dumps(
                {folder: ["crewchat-stop:" + crewchat.hook_command("gemini", "stop")]}))
            self.assertEqual([i[0] for i in ready("gemini", folder)[1:]], ["ok", "ok"])
            self.assertEqual(len(ready("qwen", folder)), 1)  # Qwen Code checks trust only when told to
            self.assertEqual(ready("opencode", folder)[1][0], "note")  # its plugin not set up yet
            (self.folder / ".opencode" / "node_modules").mkdir()
            self.assertEqual(ready("opencode", folder)[1][:2], ("ok", "crewchat's plugin is set up"))
            crewchat.uninstall_codex()
            # Not installed: how to get it, and nothing else.
            with mock.patch.object(crewchat, "find_tool", lambda tool, config: None):
                self.assertEqual(ready("codex", folder), [("todo", "Codex CLI is not installed here", "npm install -g @openai/codex")])
        # The page asks the server, for this project's folders only, and only as the owner.
        opener, _ = self.owner_browser()
        with mock.patch.object(crewchat, "tool_readiness", lambda tool, folder, config, fresh=False: [
                {"state": "ok", "text": "%s in %s%s" % (tool, folder or "-", " again" if fresh else ""), "fix": ""}]):
            out = json.loads(self.raw("/api/tool-check?tool=gemini&folder=" + urllib.parse.quote(folder), opener=opener)[1])
            self.assertEqual(out["items"][0]["text"], "gemini in %s" % folder)
            out = json.loads(self.raw("/api/tool-check?tool=gemini&fresh=1&folder=/etc", opener=opener)[1])
            self.assertEqual(out["items"][0]["text"], "gemini in - again")  # not one of its folders: not looked at
        self.assertEqual(self.raw("/api/tool-check?tool=vim", opener=opener)[0], 400)
        self.assertEqual(self.raw("/api/tool-check?tool=claude")[0], 401)

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

    def test_a_new_agent_gets_a_face_of_its_own_unless_initials_are_asked_for(self):
        pictures = [self.hub.agents[self.linked()]["picture"] for _ in range(4)]
        self.assertTrue(all(p.startswith("mix:") and crewchat.mix_parts(p[4:]) for p in pictures), pictures)
        self.assertEqual(len(set(pictures)), 4)  # one nobody here wears
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
        self.assertIn("const REOPEN_PROMPT = %s;" % json.dumps(crewchat.REOPEN_PROMPT), text)  # what Reopen asks
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
        # What --full-auto was: Codex no longer has that option, but has its workspace sandbox and approvals.
        self.assertEqual(crewchat.launch_command("codex", "codex", "start-k", "edits"),
                         ["codex", "--sandbox", "workspace-write", "--ask-for-approval", "on-request", prompt])
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
        with mock.patch.object(crewchat, "open_terminal", lambda folder, command, key, title="", root=None, env=None:
                               opened.append((folder, command, key, title)) or self.opened_env.append(env)):
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

    def test_the_other_tools_reopen_their_own_conversations(self):
        for tool, resume in (("codex", ["resume", "--sandbox", "workspace-write", "--ask-for-approval", "on-request",
                                        "sess-0003-abc", crewchat.REOPEN_PROMPT]),
                             ("gemini", ["--approval-mode", "auto_edit", "--resume=sess-0003-abc", "-i", crewchat.REOPEN_PROMPT]),
                             ("qwen", ["--approval-mode", "auto-edit", "--resume=sess-0003-abc", "-i", crewchat.REOPEN_PROMPT]),
                             ("opencode", ["--session=sess-0003-abc"])):
            name = self.closed_session(tool=tool)
            if tool == "opencode":
                self.hub.agents[name]["permissions"] = "ask"  # all it has
            self.assertTrue(next(r for r in self.hub.rows() if r["agent"] == name)["session"]["reopen"], tool)
            out, opened = self.reopened(agent=name)
            self.assertEqual(out, {"ok": True})
            self.assertEqual(opened[0][1], ["/usr/local/bin/" + tool] + resume, tool)
            env = self.opened_env[-1] or {}
            if tool == "opencode":  # OpenCode does not send a prompt with the session it continues: its plugin does
                self.assertRegex(env.get("CREWCHAT_REOPEN", ""), r"^sess-0003-abc:[0-9a-f]{16}$")
            else:
                self.assertNotIn("CREWCHAT_REOPEN", env)
        # OpenCode's session ids have an underscore; never an option, nor a line of its own.
        self.assertTrue(crewchat.SESSION_RE.fullmatch("ses_ebba941c8ffecgoR55RwC2KYIa"))
        for bad in ("_ses_ebba941c8ffe", "-ses_ebba941c8ffe", "ses ebba941c8ffe", "ses_ebba941c8ffe\n"):
            self.assertFalse(crewchat.SESSION_RE.fullmatch(bad), bad)
        script = crewchat.macos_script("/tmp/x", ["/x/opencode"], "link-0123456789", env={"CREWCHAT_REOPEN": "s:n"})
        self.assertIn("export CREWCHAT_LINK_KEY=link-0123456789\nexport CREWCHAT_REOPEN=s:n", script)

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
        session.call("hub_task", action="take", id=task)
        steps, running = [], [True]
        def stop(pid):
            steps.append("stop")
            running[0] = False
            return True
        with mock.patch.object(crewchat, "process_running", lambda session, table=None: running[0]), \
                mock.patch.object(crewchat, "stop_process", stop), \
                mock.patch.object(crewchat, "tool_models", lambda tool: [["opus", "Opus"], ["sonnet", "Sonnet"]]), \
                mock.patch.object(crewchat, "open_terminal", lambda folder, command, key, title="", root=None, env=None:
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
                mock.patch.object(crewchat, "open_terminal", lambda folder, command, key, title="", root=None, env=None:
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
        session.call("hub_task", action="take", id=task)
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
        session.call("hub_task", action="take", id=task)
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
        self.assertTrue(self.row(name)["picture"].startswith("mix:"))  # a face of its own to begin with
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
        self.assertEqual(self.hub.boot()["art"]["bot"], "Robot")
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

    def test_analytics_are_the_owners_only(self):
        self.assertEqual(self.raw("/api/analytics", b"{}", {"Content-Type": "application/json"})[0], 401)
        self.assertEqual(self.raw("/api/analytics", b"{}", {"Content-Type": "application/json",
                                                           "Authorization": "Bearer " + self.token})[0], 403)
        report = crewchat.post_json(self.base + "/api/analytics", self.owner, {"days": 3})
        for key in ("messages", "hours", "days", "pairs", "tasks", "agents", "usage", "verdicts"):
            self.assertIn(key, report)
        status, body, _ = self.raw("/api/analytics", b'{"days": "x"}', {"Content-Type": "application/json",
                                                                       "Authorization": "Bearer " + self.owner})
        self.assertEqual((status, json.loads(body)["error"]), (400, "days is a number"))

    def test_the_page_is_compressed_and_a_reload_is_a_304(self):
        opener, _ = self.owner_browser()
        status, body, headers = self.raw("/", headers={"Accept-Encoding": "gzip, deflate, br"}, opener=opener)
        self.assertEqual((status, headers["Content-Encoding"]), (200, "gzip"))
        self.assertEqual(gzip.decompress(body), crewchat.PAGE_BYTES)
        self.assertLess(len(body), len(crewchat.PAGE_BYTES) / 3)
        self.assertEqual(headers["Vary"], "Accept-Encoding")
        etag = headers["ETag"]
        for k in ("Content-Security-Policy", "X-Frame-Options", "X-Content-Type-Options", "Referrer-Policy"):
            self.assertEqual(headers[k], crewchat.PAGE_HEADERS[k], k)  # the page's protections, unchanged
        # A reload: the browser asks with the ETag it kept, and gets nothing but headers.
        for asked in (etag, "W/" + etag, '"other", ' + etag):
            status, body, headers = self.raw("/", headers={"If-None-Match": asked, "Accept-Encoding": "gzip"}, opener=opener)
            self.assertEqual((status, body), (304, b""), asked)
            self.assertEqual(headers["ETag"], etag)
            self.assertEqual(headers["Content-Security-Policy"], crewchat.PAGE_HEADERS["Content-Security-Policy"])
            self.assertLess(len(str(headers)), 1024)  # the whole answer: under 1 KB
        status, body, _ = self.raw("/", headers={"If-None-Match": '"an older page"'}, opener=opener)
        self.assertEqual((status, body), (200, crewchat.PAGE_BYTES))  # changed: the whole page, uncompressed here
        for refused in ("identity", "gzip;q=0", "br"):
            self.assertNotIn("Content-Encoding", self.raw("/", headers={"Accept-Encoding": refused}, opener=opener)[2], refused)
        # Signed out, a kept page's ETag gets the sign-in page, never a 304.
        status, _, headers = self.raw("/", headers={"If-None-Match": etag})
        self.assertEqual((status, headers["Location"]), (303, "/login"))
        self.assertEqual(json.loads(self.raw("/api/poll?after=-1&v=-1&wait=0", opener=opener)[1]) and
                         self.raw("/api/boot", opener=opener)[2]["Cache-Control"], "no-store")  # the chat's data never kept

    def test_the_analytics_view_is_its_own_script_fetched_when_opened(self):
        # Roadmap X2: the view's code is not in the page every phone loads; the page fetches it when it opens.
        self.assertIn('id="view-analytics"', crewchat.CHAT_PAGE)
        self.assertNotIn("window.crewAnalytics = {", crewchat.CHAT_PAGE)
        self.assertIn("window.crewAnalytics = {", crewchat.ANALYTICS_JS)
        self.assertEqual(self.raw("/analytics.js")[0], 401)  # signed out
        self.assertEqual(self.raw("/analytics.js", headers={"Authorization": "Bearer " + self.token})[0], 401)  # an agent
        opener, _ = self.owner_browser()
        status, body, headers = self.raw("/analytics.js", headers={"Accept-Encoding": "gzip"}, opener=opener)
        self.assertEqual((status, headers["Content-Type"], headers["Content-Encoding"]),
                         (200, "text/javascript; charset=utf-8", "gzip"))
        self.assertEqual(gzip.decompress(body), crewchat.ANALYTICS_BYTES)
        self.assertEqual(headers["Content-Security-Policy"], crewchat.PAGE_HEADERS["Content-Security-Policy"])
        status, body, _ = self.raw("/analytics.js", headers={"If-None-Match": headers["ETag"]}, opener=opener)
        self.assertEqual((status, body), (304, b""))  # opened again: the browser's copy

    def test_the_analytics_report_has_the_days_messages_and_the_files_edited(self):
        session, name = self.agent()
        task = self.say("all", "Polish the analytics", "task")
        session.call("hub_task", action="take", id=task)
        with self.hub.lock:
            for path in ("app/a.kt", "app/a.kt", "app/b.kt"):
                self.hub.events.add_edit(name, path, task)
            self.hub.events.add_edit("someone-else", "app/b.kt", task)
            self.hub.events.add_edit("someone-else", "app/b.kt", task)
        report = crewchat.post_json(self.base + "/api/analytics", self.owner, {"days": 1})
        rows = [r for r in report["stream"] if r[4] == task]
        self.assertEqual([r[3] for r in rows[:2]], ["task", "take"])  # [ts, from, to, kind, task, status]
        self.assertEqual(rows[0][1:3], ["Owner", "all"])
        self.assertFalse(any(r[3] in ("event", "plan") for r in report["stream"]))
        files = {f["path"]: f for f in report["edits"]}
        self.assertEqual((files["app/a.kt"]["edits"], files["app/a.kt"]["top"]), (2, name))
        self.assertEqual(files["app/b.kt"]["top"], "someone-else")  # who edited it most
        with mock.patch.object(crewchat.Hub, "edits_on", lambda hub: False):
            self.assertEqual(crewchat.post_json(self.base + "/api/analytics", self.owner, {"days": 1})["edits"], [])

    def test_chat_page_and_api(self):
        session, name = self.agent("cursor")
        opener, _ = self.owner_browser()
        status, body, headers = self.raw("/", opener=opener)
        self.assertEqual(status, 200)
        self.assertIn(b"<title>crewchat</title>", body)
        self.assertIn(b'id="panel"', body)  # the page from 0.14
        self.assertEqual(headers["Cache-Control"], "private, no-cache")  # kept, and asked about each time
        self.assertEqual(self.raw("/classic", opener=opener)[0], 404)  # the page before 0.14 is gone
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


class WebPush(Base):
    ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc123"

    def projects(self):
        return self.server.RequestHandlerClass.projects

    def store(self):
        return crewchat.push_store(self.projects().base)

    def tearDown(self):
        store = self.store()
        for sub in list(store.subs):
            store.forget(sub["endpoint"])

    def test_the_key_and_its_signatures(self):
        d = 0xC9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721  # RFC 6979, A.2.5
        self.assertEqual(crewchat.p256_public(d).hex(),
                         "04" "60fed4ba255a9d31c961eb74c6356d68c049b8923b61fa6ce669622e60f29fb6"
                         "7903fe1008b8bc99a41ae9e95628bc64f2f1b20c2d7e9f5177a3c294d4462299")
        message = b"a push"
        sig = crewchat.es256_sign(d, message)
        r, s_ = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
        n, z = crewchat._N, int.from_bytes(crewchat.hashlib.sha256(message).digest(), "big")
        w = pow(s_, n - 2, n)  # ECDSA verification, by the book
        point = crewchat._p256_add(crewchat._p256_mul(z * w % n), crewchat._p256_mul(r * w % n, crewchat._p256_mul(d)))
        self.assertEqual(point[0] % n, r)
        self.assertNotEqual(crewchat.es256_sign(d, message), sig)  # a fresh nonce each time
        try:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import ec, utils
        except ImportError:
            return
        key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), crewchat.p256_public(d))
        key.verify(utils.encode_dss_signature(r, s_), message, ec.ECDSA(hashes.SHA256()))

    def test_only_push_services_are_posted_to(self):
        for ok in (self.ENDPOINT, "https://web.push.apple.com/QGuA", "https://updates.push.services.mozilla.com/wpush/v2/x",
                   "https://wns2-par02p.notify.windows.com/w/?token=x"):
            self.assertTrue(crewchat.push_host_ok(ok), ok)
        for bad in ("http://fcm.googleapis.com/x", "https://evil.example/x", "https://fcm.googleapis.com.evil.example/x",
                    "https://127.0.0.1/x", "https://user@fcm.googleapis.com/x", "https://fcm.googleapis.com:8443/x",
                    "https://push.apple.com.evil/x", "https://evilpush.apple.com/x", "https://x/" + "a" * 1000, "", None):
            self.assertFalse(crewchat.push_host_ok(bad or ""), bad)

    def test_subscriptions_and_the_vapid_headers(self):
        store = self.store()
        key = store.public_key()
        self.assertEqual(len(base64.urlsafe_b64decode(key + "=")), 65)
        if os.name != "nt":  # Windows has no such modes: the file is in the user's own profile
            self.assertEqual(oct(os.stat(store.path).st_mode & 0o777), "0o600")
        store.set_mode(self.ENDPOINT, "demo", "all")
        store.set_mode(self.ENDPOINT, "other", "mentions")
        self.assertEqual(store.wanted("demo"), [(self.ENDPOINT, "all")])
        self.assertEqual(crewchat.PushStore(store.path.parent).subs, store.subs)  # kept on disk
        self.assertEqual(crewchat.PushStore(store.path.parent).public_key(), key)
        with self.assertRaises(crewchat.HubError):
            store.set_mode("https://evil.example/x", "demo", "all")
        with self.assertRaises(crewchat.HubError):
            store.set_mode(self.ENDPOINT, "demo", "loud")
        headers = store.headers(self.ENDPOINT)
        jwt, k = re.match(r"vapid t=([^,]+), k=(.+)$", headers["Authorization"]).groups()
        self.assertEqual(k, key)
        head, claims, sig = jwt.split(".")
        claims = json.loads(base64.urlsafe_b64decode(claims + "=" * (-len(claims) % 4)))
        self.assertEqual(claims["aud"], "https://fcm.googleapis.com")
        self.assertGreater(claims["exp"], time.time())
        self.assertEqual(len(base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))), 64)
        store.set_mode(self.ENDPOINT, "demo", "off")
        store.set_mode(self.ENDPOINT, "other", "off")
        self.assertEqual(store.subs, [])  # no project left: the browser is forgotten

    def test_what_is_new_is_pushed_only_while_no_page_is_open(self):
        store, seen, sent = self.store(), {}, []
        store.set_mode(self.ENDPOINT, self.projects().primary.id, "all")
        send = lambda st, endpoint: sent.append(endpoint)  # noqa: E731
        self.hub.page_seen = 0
        session, name = self.agent()
        session.call("hub_send", to="Owner", text="first, before the server looked")
        crewchat.push_round(self.projects(), store, seen, send)
        self.assertEqual((sent, store.take(self.ENDPOINT)), ([], []))  # what was there already is not news
        session.call("hub_send", to="Owner", text="Can I merge?")
        crewchat.push_round(self.projects(), store, seen, send)
        time.sleep(0.2)
        self.assertEqual(sent, [self.ENDPOINT])
        notes = store.take(self.ENDPOINT)
        self.assertEqual((notes[0]["title"], notes[0]["body"], notes[0]["url"]), ("Demo: Message · " + name, "Can I merge?", "/"))
        self.assertEqual(store.take(self.ENDPOINT), [])  # handed over once
        self.hub.page_seen = time.time()  # a page is open: it notifies by itself
        session.call("hub_send", to="Owner", text="Merged")
        crewchat.push_round(self.projects(), store, seen, send)
        self.assertEqual(sent, [self.ENDPOINT])
        self.hub.page_seen = 0
        store.set_mode(self.ENDPOINT, self.projects().primary.id, "mentions")
        tid = self.say("all", "Ship it", "task")
        session.call("hub_task", action="take", id=tid)
        session.call("hub_task", action="update", id=tid, status="blocked", note="Which branch?")
        crewchat.push_round(self.projects(), store, seen, send)
        self.assertEqual(sent, [self.ENDPOINT])  # messages only: a blocked task is not pushed

    def test_a_subscription_the_service_dropped_is_forgotten(self):
        store = self.store()
        store.set_mode(self.ENDPOINT, "demo", "all")
        gone = urllib.error.HTTPError(self.ENDPOINT, 410, "Gone", {}, io.BytesIO(b""))
        with mock.patch.object(crewchat.urllib.request, "urlopen", side_effect=gone):
            self.assertEqual(crewchat.send_push(store, self.ENDPOINT), 410)
        self.assertEqual(store.subs, [])

    def test_the_page_subscribes_and_its_service_worker_fetches(self):
        opener, _ = self.owner_browser()
        post = lambda body, extra=None: self.raw("/api/push", json.dumps(body).encode(), dict(  # noqa: E731
            {"Content-Type": "application/json", "Origin": self.base}, **(extra or {})), opener)
        self.assertEqual(self.raw("/api/push", b'{"op":"key"}', {"Content-Type": "application/json"})[0], 401)
        self.assertEqual(post({"op": "key"}, {"Origin": "https://evil.example"})[0], 403)
        status, body, _ = post({"op": "key"})
        self.assertEqual((status, json.loads(body)["key"]), (200, self.store().public_key()))
        self.assertEqual(post({"op": "mode", "endpoint": "https://evil.example/x", "mode": "all"})[0], 400)
        self.assertEqual(post({"op": "mode", "endpoint": self.ENDPOINT, "mode": "all"})[0], 200)
        self.assertEqual(self.store().wanted(self.projects().primary.id), [(self.ENDPOINT, "all")])
        self.store().queue(self.ENDPOINT, {"title": "t", "body": "b", "tag": "x", "url": "/"})
        self.assertEqual(json.loads(post({"op": "notices", "endpoint": self.ENDPOINT})[1])["notices"][0]["title"], "t")
        self.assertEqual(json.loads(post({"op": "notices", "endpoint": self.ENDPOINT})[1])["notices"], [])
        self.assertEqual(post({"op": "nope"})[0], 400)
        _, sw, _ = self.raw("/sw.js")
        self.assertIn(b"addEventListener('push'", sw)
        self.assertIn(b"addEventListener('notificationclick'", sw)


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

        class Coordinated(Link):
            def coordinator(self):
                return {"name": "laptop", "me": False, "term": 3, "preferred": ""}

        hub = lambda sync: type("Hub", (), {"sync": sync})()  # noqa: E731
        self.assertEqual(crewchat.link_info(hub(Link()), shared=False),  # a link that cannot say who coordinates
                         {"via": "cloud", "me": "mac", "online": True, "approved": True, "shared": False,
                          "coordinator": None})
        self.assertEqual(crewchat.link_info(hub(Coordinated()))["coordinator"],
                         {"name": "laptop", "me": False, "term": 3, "preferred": ""})
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
        self.assertIn("mcp__crewchat__hub_code", allow)  # the lead's own tool too: it only reads
        self.assertNotIn("mcp__crewchat", allow)
        cursor_permissions = json.loads((folder / ".cursor" / "cli.json").read_text())["permissions"]
        cursor_allow = cursor_permissions["allow"]
        self.assertEqual(cursor_permissions["deny"], [])  # Cursor's agent refuses a cli.json without one
        self.assertIn("Mcp(crewchat:hub_task)", cursor_allow)
        self.assertIn("Mcp(crewchat:hub_send)", cursor_allow)
        self.assertIn("Mcp(crewchat:hub_code)", cursor_allow)
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
        for name in ("mcp__crewchat-mcp__hub_send", "mcp__chat__hub_inbox", "hub_send", "Mcp(crew:hub_task)"):
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

    def test_a_permission_prompt_is_answered_on_the_page(self):
        session, name = self.linked("asks-first")
        asked = {"session_id": "asks-first", "tool_name": "Bash",
                 "tool_input": {"command": "rm -rf build", "description": "Clean the build"}}

        def ask():  # the hook waits for an answer: run it beside the test, and get the item it puts up
            out = {}
            t = threading.Thread(target=lambda: out.update(text=self.hook("claude", "permission", asked)))
            t.start()
            for _ in range(200):
                item = next((n for n in self.hub.needs_you() if n["kind"] == "permission"), None)
                if item:
                    return t, out, item
                time.sleep(0.05)
            self.fail("the prompt never reached Needs you")

        t, out, item = ask()
        self.assertEqual((item["agent"], item["tool"], item["text"]), (name, "Bash", "rm -rf build\n(Clean the build)"))
        self.assertEqual(self.hub.needs_you()[0]["kind"], "permission")  # before anything else
        answer = lambda pid, a: crewchat.post_json(self.base + "/api/permission", self.owner, {"id": pid, "answer": a})  # noqa: E731
        with self.assertRaises(urllib.error.HTTPError):
            answer(item["id"], "maybe")
        self.assertEqual(answer(item["id"], "allow"), {"agent": name, "answer": "allow"})
        t.join(30)
        self.assertEqual(json.loads(out["text"])["hookSpecificOutput"],
                         {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}})
        self.assertFalse([n for n in self.hub.needs_you() if n["kind"] == "permission"])  # handled: gone
        with self.assertRaises(urllib.error.HTTPError):
            answer(item["id"], "deny")  # answered already

        t, out, item = ask()
        answer(item["id"], "deny")
        t.join(30)
        decision = json.loads(out["text"])["hookSpecificOutput"]["decision"]
        self.assertEqual(decision["behavior"], "deny")
        self.assertIn("denied", decision["message"])

        # Answered in its terminal instead: its next turn (or the end of this one) takes the item away,
        # and the hook says nothing.
        t, out, item = ask()
        self.hook("claude", "prompt", {"session_id": "asks-first"})
        t.join(30)
        self.assertEqual(out["text"], "")
        self.assertFalse([n for n in self.hub.needs_you() if n["kind"] == "permission"])

        # A session that never checked in, or the owner keeping prompts in the terminal: no wait at all.
        self.assertEqual(self.hook("claude", "permission", dict(asked, session_id="never-linked")), "")
        config = crewchat.load_config()
        config["permissions"] = "terminal"
        crewchat.save_config(config)
        self.hub.roster.refresh(force=True)
        try:
            started = time.time()
            self.assertEqual(self.hook("claude", "permission", asked), "")
            self.assertLess(time.time() - started, 15)
        finally:
            config.pop("permissions")
            crewchat.save_config(config)
            self.hub.roster.refresh(force=True)

    def test_nobody_answers_a_permission_prompt_in_time(self):
        session, name = self.linked("asks-nobody")
        place = self.hub.agents[name]["place"]
        key = next(k for k, n in self.hub.links.items() if n == name)
        self.assertEqual(self.hub.ask_permission(place, key, "Write", "notes.md", wait=0.3), "")  # its own prompt shows
        self.assertFalse(self.hub.permissions)
        self.assertEqual(self.hub.ask_permission(place, "unknownkey123", "Write", "x", wait=5), "")  # not linked
        with self.assertRaises(crewchat.HubError):
            self.hub.ask_permission(place, "bad key!", "Write", "x")

    def test_what_a_permission_prompt_shows(self):
        self.assertEqual(crewchat.permission_text({"tool_name": "Write", "tool_input": {"file_path": "/a/b.py", "content": "x"}}),
                         ("Write", "/a/b.py"))
        self.assertEqual(crewchat.permission_text({"tool_name": "WebFetch", "tool_input": {"url": "https://x.dev", "prompt": "p"}}),
                         ("WebFetch", "https://x.dev"))
        self.assertEqual(crewchat.permission_text({"tool_name": "mcp__x__y", "tool_input": {"a": 1}}), ("mcp__x__y", '{"a": 1}'))
        self.assertEqual(len(crewchat.permission_text({"tool_name": "Bash", "tool_input": {"command": "x" * 9000}})[1]),
                         crewchat.MAX_PERMISSION_TEXT)
        self.assertEqual(crewchat.permission_text({}), ("", ""))

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

    def light_hook(self, client, payload, env_extra=None):
        """The tool-call hook as the client runs it now: Python and the light hook script, nothing of crewchat."""
        command = crewchat.hook_command(client, "tool")
        self.assertNotIn("crewchat.py", command)
        env = dict(os.environ, CREWCHAT_PROJECT=str(self.folder), TMPDIR=self.state, TEMP=self.state, TMP=self.state,
                   **(env_extra or {}))
        env.pop("CLAUDE_PROJECT_DIR", None)
        out = subprocess.run(shlex.split(command), input=json.dumps(payload), capture_output=True, text=True,
                             env=env, timeout=60, cwd=str(TMP))
        self.assertEqual((out.returncode, out.stdout, out.stderr), (0, "", ""))

    def test_the_light_hook_script_after_each_tool_call(self):
        text = crewchat.hook_script()
        self.assertEqual(text, crewchat.hook_script())  # the same crewchat writes the same file
        tree = ast.parse(text)
        imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertEqual(imported, {"hashlib", "json", "os", "re", "shlex", "socket", "sys", "tempfile", "time",
                                    "pathlib", "urllib.request"})  # standard library only, crewchat not loaded
        path = crewchat.home() / crewchat.HOOK_SCRIPT_FILE
        self.assertIn(path.as_posix(), crewchat.hook_command("claude", "tool"))
        self.assertEqual(path.read_text(), text)
        self.assertIn(" hook claude stop", crewchat.hook_command("claude", "stop"))  # the other events: crewchat itself
        self.assertTrue(crewchat.is_our_hook(crewchat.hook_command("cursor", "tool")))
        # It does what `crewchat hook claude tool` does: a sign of life, and what the call was.
        session, name = self.linked("light-turn")
        self.hub.agents[name]["seen"] = 0
        self.light_hook("claude", {"session_id": "light-turn", "tool_name": "Bash",
                                   "tool_input": {"command": "FOO=1 npm test -- --watch"}})
        self.assertGreater(self.hub.agents[name]["seen"], time.time() - 30)
        self.assertEqual(self.hub.agents[name]["doing"], "Running npm test")
        self.hub.agents[name]["seen"] = 0
        self.light_hook("claude", {"session_id": "light-turn", "tool_name": "Bash", "tool_input": {"command": "npm test"}})
        self.assertEqual(self.hub.agents[name]["seen"], 0)  # within the minute, nothing new: no call to the server
        self.light_hook("claude", {"session_id": "never-linked", "tool_name": "Read"})  # quiet
        self.assertFalse((Path(self.state) / "crewchat-hooks" / ("claude-" + crewchat.sha("never-linked")[:16])).exists())
        self.light_hook("claude", "not an object")  # a payload it cannot read: quiet too
        # A Cursor agent started from the page links with its start key at its first tool call.
        key = self.hub.new_start()
        cursor = Session(self.base, self.token, "cursor")
        cursor.call("hub_link", key=key)
        self.hub.agents[cursor.me]["seen"] = 0
        self.light_hook("cursor", {"conversation_id": "light-first", "command": "ls"}, env_extra={"CREWCHAT_LINK_KEY": key})
        self.assertGreater(self.hub.agents[cursor.me]["seen"], time.time() - 30)

    def test_an_edit_just_after_a_turn_boundary_is_still_sent(self):
        # The prompt and stop hooks rewrite the state file: they must keep what the tool hook learned there
        # (that edits are kept), or the edits of the next minute go unsent.
        session, name = self.linked("edits-boundary")
        edit = lambda path: self.light_hook("claude", {  # noqa: E731
            "session_id": "edits-boundary", "tool_name": "Edit", "tool_input": {"file_path": path}})
        edit(str(self.folder / "app" / "First.kt"))  # learns that edits are kept here
        self.hook("claude", "prompt", {"session_id": "edits-boundary"})  # the user's next prompt
        before = len(self.hub.events.edits())
        edit(str(self.folder / "app" / "Second.kt"))  # within the minute
        self.assertEqual([r["path"] for r in self.hub.events.edits()[before:]], ["app/Second.kt"])

    def test_edited_files_are_kept_for_a_software_team_paths_only(self):
        # Roadmap C0: the path of each file an agent edits, inside its project, while edit tracking is on.
        session, name = self.linked("edits-turn")
        inside = str(self.folder / "app" / "Login.kt")
        edit = lambda path, sid="edits-turn", tool="Edit": self.light_hook("claude", {  # noqa: E731
            "session_id": sid, "tool_name": tool, "tool_input": {"file_path": path, "old_string": "secret code"}})
        before = len(self.hub.events.edits())
        edit(inside)  # the first call learns that edits are kept here (a software team: auto is on)
        state = json.loads((Path(self.state) / "crewchat-hooks" / ("claude-" + crewchat.sha("edits-turn")[:16])).read_text())
        self.assertTrue(state["edits"])
        edit(str(self.folder / "app" / "Main.kt"))
        edit("/etc/passwd")  # outside the project: never sent
        edit(str(self.folder / "notes.md"), tool="Read")  # not an edit
        rows = self.hub.events.edits()[before:]
        self.assertEqual([r["path"] for r in rows], ["app/Main.kt"])
        self.assertEqual(rows[0]["agent"], name)
        self.assertNotIn("secret code", (self.hub.home / crewchat.EVENTS_FILE).read_bytes().decode("latin-1"))
        # A YouTube team: auto is off, the hook hears it at its next call, and the server keeps nothing anyway.
        crewchat.post_json(self.base + "/api/roles", self.owner, {"op": "pack", "pack": "youtube"})
        try:
            self.hub.agents[name].pop("doing", None)
            edit(str(self.folder / "app" / "Again.kt"))
            self.assertEqual(len(self.hub.events.edits()), before + 1)
            self.assertFalse(self.hub.edits_on())
            out = crewchat.post_json(self.base + "/api/edits", self.owner, {"op": "on"})  # the owner turns it on
            self.assertEqual((out["setting"], out["on"]), ("on", True))
            self.assertIn("app/Main.kt", [f["path"] for f in out["files"]])
            self.assertIn("Edit tracking: on", cli("edits"))
        finally:
            crewchat.post_json(self.base + "/api/edits", self.owner, {"op": "auto"})
            crewchat.post_json(self.base + "/api/roles", self.owner, {"op": "pack", "pack": "software"})
        self.assertTrue(self.hub.edits_on())
        config = crewchat.load_config()
        config["activity"] = "off"  # what agents do kept off the page: edits too, whatever the setting
        crewchat.save_config(config)
        self.hub.roster.refresh(force=True)
        try:
            self.assertFalse(self.hub.edits_on())
        finally:
            config.pop("activity")
            crewchat.save_config(config)
            self.hub.roster.refresh(force=True)

    def test_edits_keep_the_code_map_up_to_date(self):
        # Roadmap C1: the map of the project's code, built while edit tracking is on, read again after an edit.
        code = Path(tempfile.mkdtemp(prefix="mapped-", dir=self.folder)).resolve()  # inside the project
        (code / "shop.py").write_text("def pay():\n    pass\n")
        (code / "cart.py").write_text("import json\n")
        folders = mock.patch.object(crewchat.Hub, "project_folders", lambda hub, place=None: [code])
        folders.start()
        self.addCleanup(folders.stop)
        status = crewchat.post_json(self.base + "/api/map", self.owner, {})
        self.assertTrue(status["on"])
        self.assertEqual(status["maps"][0]["files"], 2)
        session, name = self.linked("map-turn")
        (code / "cart.py").write_text("from shop import pay\npay()\n")
        crewchat.MAP_WAKE.clear()
        self.light_hook("claude", {"session_id": "map-turn", "tool_name": "Edit",
                                   "tool_input": {"file_path": str(code / "cart.py")}})  # learns that edits are kept
        self.light_hook("claude", {"session_id": "map-turn", "tool_name": "Write",
                                   "tool_input": {"file_path": str(code / "cart.py")}})
        self.assertTrue(crewchat.MAP_WAKE.is_set())  # the map thread wakes
        self.assertEqual(self.hub.update_map(), {str(code): ["cart.py"]})
        out = crewchat.post_json(self.base + "/api/map", self.owner, {"op": "imports", "path": "cart.py"})
        self.assertEqual([(f["path"], f["kind"]) for f in out["files"]], [("shop.py", "calls"), ("shop.py", "imports")])
        self.assertIn("commit", out)
        out = crewchat.post_json(self.base + "/api/map", self.owner, {"op": "importers", "path": str(code / "shop.py")})
        self.assertEqual({f["path"] for f in out["files"]}, {"cart.py"})
        self.assertIn("shop.py", cli("map", "cart.py"))
        self.assertIn("pay", cli("map", "shop.py", "--defs"))
        self.assertIn("2 files", cli("map"))
        graph = crewchat.post_json(self.base + "/api/map", self.owner, {"op": "graph"})  # for the analytics view (C8)
        self.assertEqual((sorted(graph["files"]), graph["links"]), (["cart.py", "shop.py"], [[0, 1, 2]]))
        with self.assertRaises(urllib.error.HTTPError):
            crewchat.post_json(self.base + "/api/map", self.token, {})  # the owner's, not an agent's
        crewchat.post_json(self.base + "/api/edits", self.owner, {"op": "off"})  # no edit tracking: no map
        try:
            self.assertEqual(crewchat.post_json(self.base + "/api/map", self.owner, {})["on"], False)
            self.assertEqual(self.hub.update_map(), {})
            self.assertIn("edit tracking", cli("map"))
        finally:
            crewchat.post_json(self.base + "/api/edits", self.owner, {"op": "auto"})

    def test_an_agents_day_is_recorded_for_its_timeline(self):
        # Roadmap X1: working while a turn runs, idle when it ends without listening.
        session, name = self.linked("timeline-turn")
        self.hook("claude", "prompt", {"session_id": "timeline-turn"})
        self.hook("claude", "stop", {"session_id": "timeline-turn"}, listen="0")
        states = [s for ts, s in [(r[0], r[2]) for r in self.hub.events._conn().execute(
            "SELECT ts, agent, state FROM activity WHERE agent = ? ORDER BY ts", (name,))]]
        self.assertEqual(states[-2:], ["working", "idle"])
        line = self.hub.analytics.report(self.hub, 1)["timelines"][name]
        self.assertEqual([piece[2] for piece in line][-2:], ["working", "idle"])
        config = crewchat.load_config()
        config["activity"] = "off"  # kept off the page: nothing recorded either
        crewchat.save_config(config)
        self.hub.roster.refresh(force=True)
        try:
            before = len(states)
            self.hook("claude", "prompt", {"session_id": "timeline-turn"})
            count = self.hub.events._conn().execute("SELECT COUNT(*) FROM activity WHERE agent = ?", (name,)).fetchone()[0]
            self.assertEqual(count, before)
        finally:
            config.pop("activity")
            crewchat.save_config(config)
            self.hub.roster.refresh(force=True)

    def test_the_light_hook_reaches_another_machines_chat_through_urllib(self):
        sent = {}

        class Answer(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(req, timeout):
            sent.update(url=req.full_url, auth=req.get_header("Authorization"), body=json.loads(req.data))
            return Answer(b"{}")
        with mock.patch.object(urllib.request, "urlopen", urlopen):
            crewchat.hook_post("https://computer.tailnet.ts.net/api/hook", "tok", {"event": "tool"})
        self.assertEqual(sent, {"url": "https://computer.tailnet.ts.net/api/hook", "auth": "Bearer tok",
                                "body": {"event": "tool"}})
        with mock.patch.object(urllib.request, "urlopen", side_effect=AssertionError("not for this machine's server")):
            crewchat.hook_post(self.base + "/api/hook", self.token, {"key": "x" * 12, "event": "tool"})  # a plain socket

    def test_tool_hooks_run_crewchat_when_the_script_cannot_be_written(self):
        with mock.patch.object(crewchat, "write_hook_script", return_value=None):
            self.assertTrue(crewchat.hook_command("claude", "tool").endswith(" hook claude tool"))
        folder = Path(tempfile.mkdtemp(prefix="old-tool-hook-", dir=TMP))
        (folder / ".claude").mkdir()
        (folder / ".claude" / "settings.local.json").write_text(json.dumps({"hooks": {"PostToolUse": [
            {"hooks": [{"type": "command", "command": "python3 crewchat.py hook claude tool"}]}]}}))
        crewchat.update_hooks(folder)  # an older crewchat's tool hook gives way to the light script's
        tool = [h["command"] for g in json.loads((folder / ".claude" / "settings.local.json").read_text())["hooks"]
                ["PostToolUse"] for h in g["hooks"]]
        self.assertEqual(len(tool), 1)
        self.assertIn(crewchat.HOOK_SCRIPT_FILE, tool[0])

    @staticmethod
    def transcript(path, turns, secret="the secret plan"):
        """A Claude Code transcript: each assistant message on several lines (one per content block) with the
        same usage, as Claude Code writes them, between user lines that hold text."""
        with open(path, "a", encoding="utf-8") as f:
            for mid, model, usage in turns:
                f.write(json.dumps({"type": "user", "message": {"role": "user", "content": secret + " (usage: none)"}}) + "\n")
                for block in ("thinking", "text", "tool_use"):
                    f.write(json.dumps({"type": "assistant", "requestId": "req_" + mid, "message": {
                        "id": mid, "model": model, "role": "assistant", "usage": usage,
                        "content": [{"type": block, "text": secret}]}}) + "\n")

    def test_token_usage_comes_from_the_transcript_counts_only(self):
        session, name = self.linked("usage-turns")
        path = Path(self.state) / "usage-transcript.jsonl"
        u = lambda i, o, cr=0, cw=0, hour=0: {"input_tokens": i, "output_tokens": o, "cache_read_input_tokens": cr,  # noqa: E731
                                            "cache_creation_input_tokens": cw, "service_tier": "standard",
                                            "cache_creation": {"ephemeral_5m_input_tokens": cw - hour, "ephemeral_1h_input_tokens": hour}}
        self.transcript(path, [("msg_1", "claude-opus-5-5", u(10, 200, 30000, 5000)),
                               ("msg_2", "claude-opus-5-5", u(3, 50, 35000, 400, 100))])
        tid = self.say("all", "count my tokens", "task")
        session.call("hub_task", action="take", id=tid)
        self.hook("claude", "stop", {"session_id": "usage-turns", "transcript_path": str(path)})
        day = time.strftime("%Y-%m-%d", time.gmtime())
        # The transcript's own numbers, each message once: 13 in, 250 out, 65,000 read, 5,400 written (100 of them
        # for an hour).
        self.assertEqual(self.hub.usage[day][name], {"claude-opus-5-5": [13, 250, 65000, 5300, 100]})
        self.assertEqual(self.hub.sheet[str(tid)]["usage"], {"claude-opus-5-5": [13, 250, 65000, 5300, 100]})
        # The next turn sends only what the transcript gained since.
        self.transcript(path, [("msg_3", "claude-haiku-5-5", u(1, 2))])
        self.hook("claude", "stop", {"session_id": "usage-turns", "transcript_path": str(path)})
        self.assertEqual(self.hub.usage[day][name]["claude-haiku-5-5"], [1, 2, 0, 0, 0])
        self.assertEqual(self.hub.usage[day][name]["claude-opus-5-5"], [13, 250, 65000, 5300, 100])  # not again
        self.hook("claude", "stop", {"session_id": "usage-turns", "transcript_path": str(path)})
        self.assertEqual(self.hub.usage[day][name]["claude-haiku-5-5"], [1, 2, 0, 0, 0])  # nothing new: nothing added
        self.hub.flush()
        saved = (self.hub.home / "state.json").read_text()
        self.assertNotIn("secret plan", saved)  # no conversation text reaches the server
        self.assertNotIn("secret plan", json.dumps(self.hub.usage_report()))
        report = crewchat.post_json(self.base + "/api/usage", self.owner, {"op": "view"})
        row = report["agents"][name]
        self.assertEqual(row["all"], 13 + 250 + 65000 + 5300 + 100 + 3)
        cost = (13 * 4 + 250 * 20 + 65000 * 0.20 + 5300 * 4 * 1.25 + 100 * 4 * 2 + 1 * 0.1 + 2 * 0.5) / 1e6
        self.assertAlmostEqual(row["cost_all"], cost)
        self.assertEqual(report["tasks"][0]["id"], str(tid))
        self.assertEqual(report["prices"], crewchat.PRICES_DATE)
        page_row = next(r for r in self.hub.rows() if r["agent"] == name)["usage"]
        self.assertEqual((page_row["today"], page_row["all"]), (row["all"], row["all"]))

    def test_token_usage_is_off_when_the_owner_says_so(self):
        session, name = self.linked("usage-off")
        key = next(k for k, n in self.hub.links.items() if n == name)
        place = self.hub.agents[name]["place"]
        crewchat.post_json(self.base + "/api/usage", self.owner, {"op": "off"})
        try:
            self.assertEqual(self.hub.record_usage(place, key, {"claude-opus-5-5": [1, 1, 1, 1, 0]}), {"kept": False})
            self.assertIn("not kept", cli("usage"))
        finally:
            crewchat.post_json(self.base + "/api/usage", self.owner, {"op": "on"})
        config = crewchat.load_config()
        config["activity"] = "off"  # what agents do kept off the page: their tokens too
        crewchat.save_config(config)
        self.hub.roster.refresh(force=True)
        try:
            self.assertEqual(self.hub.record_usage(place, key, {"claude-opus-5-5": [1, 1, 1, 1, 0]}), {"kept": False})
        finally:
            config.pop("activity")
            crewchat.save_config(config)
            self.hub.roster.refresh(force=True)
        self.assertEqual(self.hub.record_usage(place, key, {"claude-opus-5-5": [1, 1, 1, 1, 0]})["kept"], True)
        self.assertEqual(self.hub.record_usage(place, "unknownkey99", {"claude-opus-5-5": [1, 1, 1, 1, 0]}), {"link": True})
        for bad in ({"m": [1, 2]}, {"m": [-1, 0, 0, 0, 0]}, {"m": ["1", 0, 0, 0, 0]}, ["m"]):
            with self.assertRaises(crewchat.HubError, msg=bad):
                self.hub.record_usage(place, key, bad)
        self.assertIn(name, cli("usage"))

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
        self.assertEqual(sorted(claude), ["PermissionRequest", "PostToolUse", "Stop", "UserPromptSubmit"])
        asks = [h for g in claude["PermissionRequest"] for h in g["hooks"]]
        self.assertEqual(len(asks), 1)
        self.assertTrue(asks[0]["command"].endswith(" hook claude permission"))
        self.assertGreater(asks[0]["timeout"], crewchat.PERMISSION_WAIT)  # the hook outlives its wait
        tool = [h["command"] for g in claude["PostToolUse"] for h in g["hooks"]]
        self.assertEqual((len(tool), tool[0]), (2, "echo mine"))
        self.assertTrue(tool[1].endswith("/%s claude tool" % crewchat.HOOK_SCRIPT_FILE))  # the light hook script
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
        self.assertIn("answer the owner in the chat", session.hello["instructions"])
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


class LogLoading(unittest.TestCase):
    """Startup reads messages.jsonl from its end (roadmap P3): only the messages it keeps."""

    @staticmethod
    def old_load(path):
        """The loader before P3, as the reference: every line, then the last KEEP_MESSAGES."""
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict) or "id" not in msg:
                continue
            if "seq" not in msg:
                msg["seq"] = int(msg["id"])
            msg["id"] = str(msg["id"])
            msg.setdefault("kind", "msg")
            out.append(msg)
        return out[-crewchat.KEEP_MESSAGES:]

    def project(self, lines, raw=b""):
        root = Path(tempfile.mkdtemp(prefix="log-", dir=TMP))
        crewchat.save_config({"project": "logs"}, root)
        (root / "messages.jsonl").write_bytes(raw + b"".join(
            (l if isinstance(l, bytes) else json.dumps(l).encode()) + b"\n" for l in lines))
        return root

    def test_the_same_messages_and_next_number_as_before(self):
        msg = lambda seq, **kw: dict({"id": str(seq), "seq": seq, "ts": 1.0, "from": "Maya", "to": "all", "text": "m%d" % seq}, **kw)  # noqa: E731
        n = crewchat.KEEP_MESSAGES
        fixtures = {
            "empty": [],
            "a few": [msg(1), msg(2, kind="task"), msg(3)],
            "damaged lines": [msg(1), b"not json", b"[1, 2]", b'{"no": "id"}', msg(2), b"", msg(3)],
            "written by 0.2": [{"id": 1, "from": "a", "to": "all", "text": "x", "ts": 1}, {"id": 2, "from": "a", "to": "all", "text": "y", "ts": 2}],
            "more than are kept": [msg(i) for i in range(1, n + 300)],
            "damage among many": [msg(i) if i % 7 else b"{broken" for i in range(1, n + 50)],
            "half a last line": [msg(1), msg(2)] + [b'{"id": "3", "seq": 3, "te'],
        }
        for name, lines in fixtures.items():
            root = self.project(lines)
            hub = crewchat.Hub(crewchat.Roster(root))
            expected = self.old_load(root / "messages.jsonl")
            self.assertEqual(hub.messages, expected, name)
            self.assertEqual(hub.next_seq, expected[-1]["seq"] + 1 if expected else 1, name)
            self.assertEqual({i: m["seq"] for i, m in hub.by_id.items()}, {m["id"]: m["seq"] for m in expected}, name)
        # The last line half written by a crash, with no newline after it: skipped, as before.
        root = self.project([msg(1)], raw=b"")
        with open(root / "messages.jsonl", "ab") as f:
            f.write(b'{"id": "2", "seq"')
        self.assertEqual([m["seq"] for m in crewchat.Hub(crewchat.Roster(root)).messages], [1])

    def test_lines_end_at_newlines_only(self):
        # Windows writes \r\n; a message's text may hold U+2028, which splitlines() took for a line end.
        text = "one\u2028two\u2029three\x85four"
        root = self.project([], raw=b'{"id": "1", "seq": 1, "text": "a"}\r\n' + json.dumps(
            {"id": "2", "seq": 2, "text": text}, ensure_ascii=False).encode() + b"\r\n")
        hub = crewchat.Hub(crewchat.Roster(root))
        self.assertEqual([m["text"] for m in hub.messages], ["a", text])

    def test_only_the_end_of_a_long_log_is_read(self):
        n = crewchat.KEEP_MESSAGES
        lines = [json.dumps({"id": str(i), "seq": i, "text": "x" * 100}).encode() for i in range(1, 3 * n)]
        root = self.project(lines, raw=b"\xff\xfe not UTF-8, never read\n" * 1000)  # the old loader stopped here
        size = (root / "messages.jsonl").stat().st_size
        read = []
        real_open = open

        def counting_open(path, mode="r", *a, **kw):
            f = real_open(path, mode, *a, **kw)
            if str(path).endswith("messages.jsonl"):
                plain = f.read
                f = mock.Mock(wraps=f)
                f.read = lambda *ra: (lambda data: read.append(len(data)) or data)(plain(*ra))
                f.__enter__ = lambda *x: f
                f.__exit__ = lambda *x: False
            return f
        with mock.patch.object(crewchat, "open", counting_open, create=True):
            hub = crewchat.Hub(crewchat.Roster(root))
        self.assertEqual(len(hub.messages), n)
        self.assertEqual(hub.messages[-1]["seq"], 3 * n - 1)
        self.assertLess(sum(read), size * 0.4 + 65536)  # about a third of the file: the messages it keeps

    def test_read_back_across_blocks(self):
        path = Path(tempfile.mkdtemp(prefix="back-", dir=TMP)) / "f"
        for data in (b"", b"a", b"a\n", b"a\nbb\nccc", b"\n\n", b"x" * 50 + b"\n" + b"y" * 3 + b"\n"):
            path.write_bytes(data)
            for block in (1, 2, 3, 7, 1 << 16):
                self.assertEqual(list(crewchat.read_back(path, block)), data.split(b"\n")[::-1], (data, block))


class StateWrites(Base):
    """state.json is rewritten at most once a SAVE_DELAY for read positions, status lines and presence, at
    once for claims, links and the task sheet (roadmap P5)."""

    def writes(self):
        count = [0]
        real = crewchat.write_json

        def counting(path, *a, **kw):
            if Path(path).name == "state.json":
                count[0] += 1
            return real(path, *a, **kw)
        patch = mock.patch.object(crewchat, "write_json", counting)
        patch.start()
        self.addCleanup(patch.stop)
        return count

    def on_disk(self):
        return json.loads((self.hub.home / "state.json").read_text(encoding="utf-8"))

    def test_presence_and_read_positions_wait_and_come_together(self):
        sender, _ = self.agent()
        reader, name = self.agent()
        self.hub.flush()
        count = self.writes()
        for i in range(20):
            sender.call("hub_send", to=name, text="m%d" % i)
            reader.call("hub_inbox")
            reader.call("hub_status", text="reading %d" % i)
        self.assertNotEqual(self.on_disk()["agents"][name]["status"], "reading 19")  # not yet
        time.sleep(crewchat.SAVE_DELAY + 0.5)
        self.assertLessEqual(count[0], 3)  # a handful, not one per call
        disk = self.on_disk()["agents"][name]
        self.assertEqual(disk["status"], "reading 19")  # within SAVE_DELAY
        self.assertEqual(disk["cursor"], self.hub.agents[name]["cursor"])

    def test_claims_links_and_the_sheet_are_written_at_once(self):
        session, name = self.agent()
        self.hub.flush()
        session.call("hub_link", key="key-at-once-01")
        self.assertEqual(self.on_disk()["links"]["key-at-once-01"], name)
        tid = self.say("all", "claim me", "task")
        session.call("hub_task", action="take", id=tid)
        disk = self.on_disk()
        self.assertEqual(disk["taken"][str(tid)], name)  # no flush, no wait: on disk already
        session.call("hub_task", action="update", id=tid, status="blocked", note="need a key")
        self.assertEqual(self.on_disk()["progress"][str(tid)]["status"], "blocked")

    def test_windows_tries_again_when_the_file_is_held(self):
        path = Path(tempfile.mkdtemp(prefix="held-", dir=TMP)) / "state.json"
        real, calls = os.replace, []

        def held_twice(src, dst):
            calls.append(dst)
            if len(calls) <= 2:
                raise PermissionError(32, "The process cannot access the file because it is being used by another process")
            return real(src, dst)
        with mock.patch.object(crewchat.os, "replace", held_twice), mock.patch.object(crewchat.sys, "platform", "win32"), \
                mock.patch.object(crewchat.time, "sleep"):
            crewchat.write_json(path, {"ok": 1})
        self.assertEqual((len(calls), json.loads(path.read_text())), (3, {"ok": 1}))
        with mock.patch.object(crewchat.os, "replace", side_effect=PermissionError(13, "denied")):
            with self.assertRaises(PermissionError):  # elsewhere a refusal is not a moment's: it is an error
                crewchat.write_json(path, {"ok": 2})

    def test_an_immediate_write_takes_what_was_waiting_with_it(self):
        session, name = self.agent()
        session.call("hub_status", text="waiting to be saved")
        self.assertTrue(self.hub._dirty)
        tid = self.say("all", "and a claim", "task")
        session.call("hub_task", action="take", id=tid)
        self.assertFalse(self.hub._dirty)
        self.assertEqual(self.on_disk()["agents"][name]["status"], "waiting to be saved")


class MessageIndex(unittest.TestCase):
    """Hub.by_id finds a message by its id without a scan (roadmap P6), and always matches the messages."""

    def hub(self, lines=()):
        root = Path(tempfile.mkdtemp(prefix="index-", dir=TMP))
        crewchat.save_config({"project": "index"}, root)
        (root / "messages.jsonl").write_text("".join(json.dumps(m) + "\n" for m in lines))
        return crewchat.Hub(crewchat.Roster(root))

    def assertIndexed(self, hub):
        self.assertEqual(hub.by_id, {m["id"]: m for m in hub.messages})
        self.assertTrue(all(hub.by_id[m["id"]] is m for m in hub.messages))  # the same objects, not copies
        self.assertTrue(all(hub.seq_of(m["id"]) == m["seq"] for m in hub.messages))

    def test_loaded_sent_trimmed_and_ingested(self):
        hub = self.hub([{"id": str(i), "seq": i, "ts": 1, "from": "Owner", "to": "all", "text": "x"} for i in range(1, 6)])
        self.assertIndexed(hub)
        with hub.lock:
            for i in range(crewchat.KEEP_MESSAGES + 10):
                hub._append(crewchat.OWNER, "all", "m%d" % i, "msg")
        self.assertEqual(len(hub.messages), crewchat.KEEP_MESSAGES)
        self.assertIndexed(hub)
        self.assertNotIn("1", hub.by_id)  # trimmed out of memory: out of the index too
        self.assertTrue(hub.ingest({"id": "B7", "from": "Teo", "to": "all", "text": "from another machine", "ts": 2}))
        self.assertFalse(hub.ingest({"id": "B7", "from": "Teo", "to": "all", "text": "again", "ts": 2}))
        self.assertEqual(hub.by_id["B7"]["text"], "from another machine")
        self.assertIndexed(hub)

    def test_renamed_tasks_are_found_by_their_new_ids(self):
        hub = self.hub()
        with hub.lock:
            hub._append(crewchat.OWNER, "all", "warm up", "msg")
        old = hub.add_row(crewchat.OWNER, "Ship the release", "", "", "")["id"]
        before = (hub.home / "messages.jsonl").read_bytes()
        renamed = hub.adopt_open_tasks("A", "device-a")  # linking machines: open tasks get the device's tag
        self.assertEqual(renamed, {old: "A" + old})
        self.assertIndexed(hub)
        self.assertNotIn(old, hub.by_id)
        self.assertEqual(hub._task("A" + old)["text"], "Ship the release")
        # A log a crash left with the old number (state saved, log not yet rewritten): put right in memory,
        # and in the index, on load.
        (hub.home / "messages.jsonl").write_bytes(before)
        again = crewchat.Hub(hub.roster)
        self.assertNotIn(old, again.by_id)
        self.assertIndexed(again)
        self.assertEqual(again.by_id["A" + old]["text"], "Ship the release")

    def test_questions_and_answers_are_found_by_id(self):
        hub = self.hub()
        with hub.lock:
            ask = hub._append("Maya", "Wren", "Is this safe?", "ask")
            hub._append(crewchat.OWNER, "all", "unrelated", "msg")
            self.assertIs(hub.by_id[ask["id"]], ask)
        with self.assertRaises(crewchat.HubError):
            hub.answer("Wren", "999", "no such question")


class Persistence(Base):
    def test_everything_survives_a_restart(self):
        session, name = self.agent()
        session.call("hub_link", key="key-persist-001")
        session.call("hub_status", text="busy")
        task = self.say("all", "persist me", "task")
        session.call("hub_task", action="take", id=task)
        session.call("hub_inbox")
        self.hub.flush()  # a server that is stopped writes what waits (its read position) first
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
        self.assertEqual(poll()["update"], "99.0.0")
        self.assertEqual(json.loads(self.raw("/api/boot", opener=opener)[1])["releases"], crewchat.RELEASES)
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

    def test_hooks_under_a_home_with_a_space_are_still_ours(self):
        # C:/Users/John Smith: the light hook script's path is quoted, and crewchat must still know its hook (or each
        # refresh adds another, and uninstalling leaves one behind), in a settings file as in its JSON.
        for script in ("C:/Users/John Smith/.crewchat/crewchat-hook.py", "/Users/js/.crewchat/crewchat-hook.py"):
            with mock.patch.object(crewchat, "hook_program", lambda event: ["C:/Python 3/python.exe", script]):
                command = crewchat.hook_command("claude", "tool")
            self.assertTrue(crewchat.is_our_hook(command), command)
            self.assertTrue(crewchat.is_our_hook(json.dumps({"PostToolUse": [{"command": command}]})), command)
        self.assertFalse(crewchat.is_our_hook('python "/x/my-crewchat-hook.pyc" claude tool'))

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
        found = a.text("hub_task", action="add", title="Fix the crash on rotate", areas="app/main/")
        crash = re.search(r"#(\w+)", found).group(1)
        nxt = a.text("hub_task", action="view")
        self.assertIn("#%s [todo] Build the login screen" % login, nxt)
        self.assertNotIn("Write login tests", nxt)  # waits for the login screen
        self.assertNotIn("Sign the build", nxt)  # needs another machine
        self.assertIn("cannot be taken yet: it waits for #%s" % login, a.text("hub_task", action="take", id=tests))
        self.assertIn("is yours", a.text("hub_task", action="take", id=login))
        # Another task over the same files cannot start while the first is in hand.
        overlap = add("Restyle the login screen", areas="app/login/style/")
        self.assertIn("overlap #%s" % login, b.text("hub_task", action="take", id=overlap))
        a.text("hub_task", action="update", id=login, status="done", note="built")
        self.assertIn("Write login tests", b.text("hub_task", action="view"))
        b.text("hub_task", action="take", id=tests)
        b.text("hub_task", action="update", id=tests, status="todo", note="no time")  # given back
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
            hub._index()  # as trimming leaves it
            self.assertIsNone(hub.by_id.get(login))
        try:
            self.assertEqual({r["id"]: r for r in hub.sheet_rows()}[login]["status"], "done")
        finally:
            with hub.lock:
                hub.messages = kept
                hub._index()
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
        a.text("hub_task", action="take", id=tid)
        a.text("hub_task", action="update", id=tid, status="todo", note="back")
        self.assertEqual(self.hub.claim_key(tid), tid + "@1")
        # Only its holder gives a task back: anyone else's "todo" is refused, and the key stays.
        b, b_name = self.agent()
        a.text("hub_task", action="take", id=tid)
        self.assertIn("you do not hold task #%s" % tid, b.text("hub_task", action="update", id=tid, status="todo", note="not mine"))
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
        a.text("hub_task", action="take", id=tid)
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
                          a.text("hub_task", action="update", id=tid, status="done", note="finished"))
            taken = b.text("hub_task", action="take", id=tid)
            self.assertIn("no sign of life (no tool call, no hook) for 60 min", taken)  # told when it takes it
            self.assertIn("for 60 min", b.text("hub_task", action="view", show="all"))
            self.assertIn("%s holds it now" % b_name, a.text("hub_task", action="update", id=tid, status="in_progress", note="still on it"))
            self.assertIn("already taken by " + b_name, a.text("hub_task", action="take", id=tid))
            row = next(r for r in hub.sheet_rows() if r["id"] == tid)
            self.assertEqual((row["status"], row["holder"], row["reclaimed"]), ("doing", b_name, a_name))
            # Once it has the task again, it reports on it as before.
            b.text("hub_task", action="update", id=tid, status="todo", note="over to you")
            self.assertIn("is yours", a.text("hub_task", action="take", id=tid))
            self.assertIn("marked done", a.text("hub_task", action="update", id=tid, status="done", note="finished"))
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
        a.text("hub_task", action="take", id=long)
        b.text("hub_task", action="take", id=quick)
        c.text("hub_task", action="take", id=blocked)
        c.text("hub_task", action="update", id=blocked, status="blocked", note="which colour?")
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
                self.assertIn("is yours", b.text("hub_task", action="take", id=long))
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

    def test_areas_are_grouped_by_folder(self):
        group = crewchat.group_areas
        self.assertEqual(group(["app/a.kt", "app/b.kt", "README.md"]), ["README.md", "app/"])
        self.assertEqual(group(["app/login/a.kt", "app/a.kt"]), ["app/"])  # a folder inside another counts with it
        many = ["app/f%d/x.kt" % i for i in range(8)] + ["docs/a.md", "docs/b.md"]
        self.assertEqual(group(many), ["app/", "docs/"])  # too many: raised to their parent
        tops = ["t%d/x.py" % i for i in range(7)] + ["t3/y.py", "t3/z.py", "t5/y.py"]
        self.assertEqual(group(tops), ["t0/", "t1/", "t2/", "t3/", "t5/"])  # still too many: the most edited
        self.assertEqual(group([]), [])
        self.assertTrue(crewchat.area_covered("app/login/", ["app/"]))
        self.assertFalse(crewchat.area_covered("app/", ["app/login/"]))

    def test_areas_overlap_by_path_prefix(self):
        self.assertTrue(crewchat.areas_overlap(["app/login/"], ["app/login/tests/"]))
        self.assertTrue(crewchat.areas_overlap(["app"], ["app/"]))
        self.assertFalse(crewchat.areas_overlap(["app/login/"], ["app/logout/"]))
        self.assertFalse(crewchat.areas_overlap([], ["app/"]))
        imports = lambda a, b: {a, b} == {"web/", "lib/"}  # noqa: E731  (the code map's, C1)
        self.assertTrue(crewchat.areas_overlap(["web/"], ["lib/"], imports))
        self.assertFalse(crewchat.areas_overlap(["web/"], ["docs/"], imports))
        self.assertEqual(crewchat.split_list("#A1, A2,-"), ["A1", "A2"])

    def test_update_refuses_a_copy_the_installer_did_not_put_there(self):
        with self.assertRaises(SystemExit):
            cli("update")


if __name__ == "__main__":
    unittest.main()
