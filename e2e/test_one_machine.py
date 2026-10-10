"""One machine: setup, agents joining, messages, tasks, roles, files, listening, restarts."""
import json
import re
import threading
import time
import unittest

import harness as H
from harness import ToolError, eventually


class OneMachine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = H.Machine("alpha")
        cls.m.start()

    @classmethod
    def tearDownClass(cls):
        cls.m.stop()

    def linked(self, client="claude"):
        agent = self.m.agent(client=client)
        agent.link()
        return agent

    # Setup -----------------------------------------------------------------------------------
    def test_start_again_keeps_the_same_server_and_connection(self):
        before = (self.m.folder / ".mcp.json").read_text()
        pids = self.m.server_pids()
        out = self.m.cli("start", "--no-service", "--no-open")
        self.assertEqual((self.m.folder / ".mcp.json").read_text(), before)
        self.assertEqual(self.m.server_pids(), pids, out)
        self.assertIn("Demo", self.m.cli("status"))

    def test_the_folder_is_set_up_for_both_clients_and_git_ignores_the_secrets(self):
        for f in (".mcp.json", ".claude/settings.local.json", ".cursor/mcp.json", ".cursor/hooks.json"):
            self.assertTrue((self.m.folder / f).exists(), f)
        exclude = (self.m.folder / ".git" / "info" / "exclude").read_text()
        for f in (".mcp.json", ".cursor/"):
            self.assertIn(f.rstrip("/"), exclude)
        status = H.subprocess.run(["git", "status", "--porcelain"], cwd=str(self.m.folder), capture_output=True,
                                  text=True).stdout
        self.assertNotIn(".mcp.json", status)
        self.assertNotIn("mcp.json", status)

    def test_a_second_folder_with_chat_joins_the_project_under_its_own_name(self):
        site = self.m.dir / "website"
        site.mkdir()
        H.subprocess.run(["git", "init", "-q", str(site)], check=True)
        out = self.m.cli("start", "--no-service", "--no-open", "--chat", "Demo", cwd=site)
        self.assertIn('Added %s to the project "Demo", as "website".' % site.resolve(), out)
        agent = self.m.agent(folder=site)
        self.assertRegex(agent.link(), r"^claude-website(-\d+)?$")
        self.assertTrue(self.m.owner.sees(agent.name))
        self.assertIn("website", self.m.cli("places"))

    # Joining -----------------------------------------------------------------------------------
    def test_sessions_join_with_their_own_names(self):
        a, b = self.linked(), self.linked()
        c = self.linked("cursor")
        self.assertRegex(a.name, r"^claude-alpha(-\d+)?$")
        self.assertNotEqual(a.name, b.name)
        self.assertRegex(c.name, r"^cursor-alpha(-\d+)?$")
        listing = a.call("hub_agents")
        for agent in (a, b, c):
            self.assertIn(agent.name, listing)
        for agent in (a, b, c):
            self.assertTrue(self.m.owner.sees(agent.name), agent.name)
        self.m.owner.wait_for("%s joined" % c.name)

    def test_a_resumed_session_gets_its_name_back(self):
        a = self.linked()
        key = a.key
        again = self.m.agent(session=a.session)  # claude --resume: a new MCP connection, same conversation
        self.assertIn('key "%s"' % key, again.hook("prompt"))  # asked once, with the key it had
        self.assertIn("is %s again" % a.name, again.call("hub_link", key=key))
        self.assertEqual(again.whoami(), a.name)
        self.assertEqual(again.hook("prompt"), "")
        self.assertEqual(len([r for r in self.m.owner.agents() if r["agent"] == a.name]), 1)

    def test_a_session_that_never_links_is_asked_three_prompts_then_left_alone(self):
        a = self.m.agent()
        for _ in range(3):
            self.assertIn("hub_link", a.hook("prompt"))
            self.assertEqual(a.stop_hook(), "")  # only the prompt asks: a stop-hook ask costs a whole turn
        self.assertEqual(a.hook("prompt"), "")
        self.assertEqual(a.stop_hook(), "")
        # It can still link later with the key it was given.
        a.call("hub_link", key=a.key)
        self.assertTrue(a.whoami())

    def test_a_cursor_session_is_asked_to_link_by_its_stop_hook(self):
        c = self.m.agent(client="cursor")
        out = json.loads(c.hook("stop"))
        self.assertIn("followup_message", out)
        self.assertNotIn("decision", out)
        c.call("hub_link", key=re.search(r'key "([^"]+)"', out["followup_message"]).group(1))
        self.assertTrue(c.whoami().startswith("cursor-"))

    def test_rename_from_the_agent_and_from_the_terminal(self):
        a = self.linked()
        a.call("hub_rename", name="builder")
        self.assertEqual(a.whoami(), "builder")
        self.m.cli("agent", "rename", "builder", "builder2")
        self.assertEqual(a.whoami(), "builder2")
        with self.assertRaises(ToolError):
            self.linked().call("hub_rename", name="builder2")  # taken
        self.m.cli("agent", "remove", "builder2")
        eventually(lambda: not self.m.owner.sees("builder2", online=False), what="the agent to be removed")

    # Messages ----------------------------------------------------------------------------------
    def test_owner_message_reaches_the_agent_at_the_end_of_its_turn_once(self):
        a = self.linked()
        a.stop_hook()
        self.m.owner.send(a.name, "please check the build")
        reason = a.stop_hook()
        self.assertIn("please check the build", reason)
        self.assertIn("first send hub_update in_progress", reason)
        self.assertEqual(a.stop_hook(), "")  # handled: not shown again
        self.assertEqual(a.hook("prompt"), "")

    def test_cursor_gets_messages_as_a_followup(self):
        c = self.linked("cursor")
        c.stop_hook()
        self.m.owner.send(c.name, "cursor, look at the tests")
        out = json.loads(c.hook("stop"))
        self.assertIn("cursor, look at the tests", out["followup_message"])
        # A turn that did not complete does not get messages.
        self.m.owner.send(c.name, "second")
        self.assertEqual(c.hook("stop", extra={"status": "aborted"}).strip(), "")
        self.assertIn("second", c.stop_hook())

    def test_an_interrupted_turn_sees_its_messages_again(self):
        a = self.linked()
        a.stop_hook()
        self.m.owner.send(a.name, "interrupt me")
        self.assertIn("interrupt me", a.hook("prompt"))  # shown at the user's prompt
        self.assertIn("interrupt me", a.hook("prompt"))  # that turn was interrupted: shown again
        self.assertEqual(a.stop_hook(), "")  # a turn finished: confirmed, not shown a third time

    def test_a_listening_agent_wakes_up_when_a_message_arrives(self):
        a = self.linked()
        a.stop_hook()
        got = {}
        t = threading.Thread(target=lambda: got.update(r=a.stop_hook(listen=20), t=time.time()))
        start = time.time()
        t.start()
        eventually(lambda: self.m.owner.row(a.name)["activity"] == "listening", what="the agent to listen")
        self.m.owner.send(a.name, "wake up")
        t.join(30)
        self.assertIn("wake up", got["r"])
        self.assertLess(got["t"] - start, 15)

    def test_listen_settings_of_the_project(self):
        a = self.linked()
        a.stop_hook()
        self.m.cli("listen", "off")
        start = time.time()
        self.assertEqual(a.stop_hook(listen=None), "")
        self.assertLess(time.time() - start, 5)
        self.assertIn("off", self.m.cli("listen", "status"))
        self.m.cli("listen", "on", "--minutes", "1")
        self.assertIn("1 minutes", self.m.cli("listen", "status"))
        t = threading.Thread(target=lambda: setattr(self, "_got", a.stop_hook(listen=None)))
        t.start()
        time.sleep(2)
        self.m.owner.send(a.name, "during listen")
        t.join(70)
        self.assertIn("during listen", self._got)
        self.m.cli("listen", "default")
        self.assertIn("by default", self.m.cli("listen", "status"))
        self.assertIn(".crewchat-listen", (self.m.folder / ".git" / "info" / "exclude").read_text())

    def test_a_burst_of_messages_arrives_in_one_turn(self):
        a = self.linked()
        a.stop_hook()
        got = {}
        t = threading.Thread(target=lambda: got.update(r=a.stop_hook(listen=30)))
        t.start()
        eventually(lambda: self.m.owner.row(a.name)["activity"] == "listening", what="the agent to listen")
        time.sleep(2)  # well into its wait (a message already there is handed over at once, unbatched)
        self.m.owner.send(a.name, "burst one")
        time.sleep(2)
        self.m.owner.send(a.name, "burst two")
        t.join(40)
        self.assertIn("burst one", got["r"])
        self.assertIn("burst two", got["r"])
        self.assertEqual(a.stop_hook(), "")

    def test_hooks_can_be_switched_off_for_a_folder_and_a_session(self):
        folder = self.m.dir / "quiet-folder"
        folder.mkdir()
        H.subprocess.run(["git", "init", "-q", str(folder)], check=True)
        self.m.cli("start", "--no-service", "--no-open", cwd=folder)
        self.assertIn("off", self.m.cli("hooks", "off", cwd=folder))
        quiet = self.m.agent(folder=folder)
        self.assertEqual(quiet.hook("prompt"), "")
        self.assertEqual(quiet.stop_hook(), "")
        self.assertIn(".crewchat-hooks", (folder / ".git" / "info" / "exclude").read_text())
        self.assertIn("off", self.m.cli("hooks", "status", cwd=folder))
        self.m.cli("hooks", "on", cwd=folder)
        self.assertIn("hub_link", quiet.hook("prompt"))
        one = self.m.agent(folder=folder)
        one.machine.env["CREWCHAT_HOOKS"] = "off"
        try:
            self.assertEqual(one.hook("prompt"), "")
        finally:
            del one.machine.env["CREWCHAT_HOOKS"]

    def test_broadcast_direct_and_replies_to_owner(self):
        a, b = self.linked(), self.linked()
        for x in (a, b):
            x.stop_hook()
        self.m.owner.send("all", "standup in five")
        self.assertIn("standup in five", a.stop_hook())
        self.assertIn("standup in five", b.stop_hook())
        a.call("hub_send", to=b.name, text="can you review my branch")
        self.assertIn("can you review my branch", b.stop_hook())
        self.assertEqual(a.stop_hook(), "")  # own message is not delivered back
        b.call("hub_send", to="Owner", text="review done")
        self.m.owner.wait_for("review done", **{"from": b.name, "to": "Owner"})
        inbox = a.call("hub_inbox")
        self.assertNotIn("review done", inbox)
        with self.assertRaises(ToolError):
            a.call("hub_send", to="nobody-here", text="x")

    def test_inbox_peek_and_wait(self):
        a = self.linked()
        a.call("hub_inbox")
        self.m.owner.send(a.name, "peek at me")
        self.assertIn("peek at me", a.call("hub_inbox", peek=True))
        self.assertIn("peek at me", a.call("hub_inbox"))
        self.assertNotIn("peek at me", a.call("hub_inbox"))
        threading.Timer(1.5, lambda: self.m.owner.send(a.name, "late one")).start()
        start = time.time()
        self.assertIn("late one", a.call("hub_inbox", wait_seconds=10))
        self.assertLess(time.time() - start, 8)

    def test_the_terminal_commands_for_the_owner(self):
        a = self.linked()
        a.call("hub_inbox")
        self.m.cli("say", "--to", a.name, "from", "the", "terminal")
        self.assertIn("from the terminal", a.call("hub_inbox"))
        note = self.m.dir / "notes.md"
        note.write_text("# Plan\nship it\n")
        self.m.cli("say", "--task", "--file", note, "write", "the", "plan")
        task = self.m.owner.wait_for("write the plan", kind="task")
        self.assertEqual(task["files"][0]["name"], "notes.md")
        self.assertIn(a.name, self.m.cli("agents"))
        self.assertIn("alpha", self.m.cli("places"))

    # Tasks -------------------------------------------------------------------------------------
    def test_only_one_agent_gets_an_open_task(self):
        agents = [self.linked() for _ in range(5)]
        tid = self.m.owner.task("all", "fix the login crash")
        results = {}

        def take(agent):
            try:
                results[agent.name] = agent.call("hub_take", id=tid)
            except ToolError as e:
                results[agent.name] = e

        threads = [threading.Thread(target=take, args=(x,)) for x in agents]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        winners = [n for n, r in results.items() if isinstance(r, str)]
        self.assertEqual(len(winners), 1, results)
        self.assertIn("hub_update id=%s status=in_progress" % tid, results[winners[0]])
        for n, r in results.items():
            if n not in winners:
                self.assertIn("already taken by %s" % winners[0], str(r))
        self.assertEqual(self.m.owner.poll()["taken"][tid], winners[0])
        self.m.owner.wait_for("I am taking task #%s" % tid, **{"from": winners[0]})

    def test_a_task_for_one_agent_cannot_be_taken_by_another(self):
        a, b = self.linked(), self.linked()
        tid = self.m.owner.task(a.name, "only for a")
        with self.assertRaises(ToolError) as e:
            b.call("hub_take", id=tid)
        self.assertIn("was given to %s" % a.name, str(e.exception))
        a.call("hub_take", id=tid)
        with self.assertRaises(ToolError):
            a.call("hub_take", id="999999")

    def test_status_updates_go_to_owner_without_a_lead(self):
        a = self.linked()
        tid = self.m.owner.task("all", "update the README")
        a.call("hub_take", id=tid)
        for status in ("in_progress", "blocked", "review", "done"):
            a.call("hub_update", id=tid, status=status, note="now " + status)
            self.m.owner.wait_for("now " + status, kind="update", to="Owner", status=status)
        self.assertEqual(self.m.owner.poll()["progress"][tid]["status"], "done")
        with self.assertRaises(ToolError):
            a.call("hub_update", id=tid, status="finished", note="x")


class Team(unittest.TestCase):
    """Roles: a lead coordinating developers, on one machine."""

    def setUp(self):
        self.m = H.Machine("team")
        self.m.start()
        self.lead, self.dev, self.dev2 = (self.m.agent() for _ in range(3))
        for a in (self.lead, self.dev, self.dev2):
            a.link()
            a.stop_hook()
        self.owner = self.m.owner
        self.owner.role(self.lead.name, "lead")
        self.owner.role(self.dev.name, "developer")
        self.owner.role(self.dev2.name, "developer")

    def tearDown(self):
        self.m.stop()

    def test_roles_reach_the_agents_with_their_instructions(self):
        lead_view = self.lead.stop_hook()
        self.assertIn("Your role in this chat is now: Lead", lead_view)
        self.assertIn("give that agent its next task with hub_assign", lead_view)
        dev_view = self.dev.stop_hook()
        self.assertIn("Developer", dev_view)
        self.assertIn("first send hub_update on it with in_progress", dev_view)
        self.assertEqual(self.owner.row(self.lead.name)["role"], "lead")
        self.assertIn("lead", self.lead.call("hub_agents"))

    def test_lead_assigns_developer_confirms_finishes_and_lead_is_told_to_hand_out_more(self):
        for a in (self.lead, self.dev, self.dev2):
            a.stop_hook()
        job = self.owner.task("all", "build the settings screen")
        self.assertIn("build the settings screen", self.lead.stop_hook())
        with self.assertRaises(ToolError):
            self.dev.call("hub_assign", to=self.dev2.name, text="not a lead")
        out = self.lead.call("hub_assign", to=self.dev.name, text="make the toggle", task=job)
        piece = re.search(r"#(\d+)", out).group(1)
        got = self.dev.stop_hook()
        self.assertIn("[TASK, assigned to you, part of #%s]" % job, got)
        # The developer confirms first, and the lead hears it.
        self.dev.call("hub_update", id=piece, status="in_progress", note="adding a Switch to SettingsScreen")
        self.assertIn("adding a Switch", self.lead.stop_hook())
        self.dev.call("hub_update", id=piece, status="done", note="toggle works")
        lead_sees = self.lead.stop_hook()
        self.assertIn("toggle works", lead_sees)
        self.assertIn("%s is free" % self.dev.name, lead_sees)
        self.assertIn("give it its next task with hub_assign", lead_sees)
        # The owner sees all of it on the page, and the hint is not shown to the owner.
        update = self.owner.wait_for("toggle works", kind="update")
        self.assertEqual(update["to"], self.lead.name)

    def test_a_task_the_owner_gives_one_agent_is_reported_to_the_owner_even_with_a_lead(self):
        for a in (self.lead, self.dev):
            a.stop_hook()
        tid = self.owner.task(self.dev.name, "fix the crash on Windows")
        self.assertIn("fix the crash on Windows", self.dev.stop_hook())
        self.dev.call("hub_update", id=tid, status="in_progress", note="reading the stack trace")
        self.owner.wait_for("reading the stack trace", kind="update", to="Owner")
        self.assertNotIn("reading the stack trace", self.lead.stop_hook())

    def test_an_open_task_taken_by_a_developer_is_reported_to_the_lead(self):
        tid = self.owner.task("all", "write release notes")
        self.dev2.call("hub_take", id=tid)
        self.dev2.call("hub_update", id=tid, status="in_progress", note="starting the notes")
        self.owner.wait_for("starting the notes", kind="update", to=self.lead.name)

    def test_one_lead_at_a_time_and_roles_can_be_removed(self):
        self.lead.stop_hook()
        self.owner.role(self.dev.name, "lead")
        self.assertIn("You are no longer the lead: %s is" % self.dev.name, self.lead.stop_hook())
        roles = {a["agent"]: a["role"] for a in self.owner.agents()}
        self.assertEqual(roles[self.dev.name], "lead")
        self.assertEqual(roles[self.lead.name], "")
        self.owner.role(self.dev.name, "none")
        self.assertEqual(self.owner.row(self.dev.name)["role"], "")
        self.m.cli("role", self.dev2.name, "qa")
        self.assertEqual(self.owner.row(self.dev2.name)["role"], "qa")
        with self.assertRaises(AssertionError):
            self.owner.role(self.dev2.name, "astronaut")

    def test_custom_roles(self):
        self.m.cli("roles", "add", "designer", "--prompt", "You design screens.")
        self.assertIn("designer", self.m.cli("roles"))
        self.owner.role(self.dev2.name, "designer")
        self.assertIn("You design screens.", self.dev2.stop_hook())
        self.m.cli("roles", "remove", "designer")
        self.assertNotIn("designer", self.m.cli("roles"))

    def test_the_chain_limit_stops_agents_waking_each_other_but_not_the_owner(self):
        self.m.cli("setup", "--max-chain", "2")
        self.m.restart()
        a, b = self.dev, self.dev2
        for x in (a, b):
            x.hook("prompt")  # the user typed: the count of turns in a row starts at 0
            x.stop_hook()
        # Two agents keep answering each other: after the limit only the owner wakes them.
        woke = 0
        for i in range(6):
            a.call("hub_send", to=b.name, text="ping %d" % i)
            if b.stop_hook():
                woke += 1
        self.assertEqual(woke, 2)  # max_chain: 2 turns in a row on agent messages alone
        self.owner.send(b.name, "owner says stop")
        self.assertIn("owner says stop", b.stop_hook())
        b.hook("prompt")  # the user typed: the count starts again
        a.call("hub_send", to=b.name, text="after prompt")
        self.assertIn("after prompt", b.stop_hook())


class Files(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = H.Machine("files")
        cls.m.start()
        cls.a = cls.m.agent()
        cls.a.link()

    @classmethod
    def tearDownClass(cls):
        cls.m.stop()

    def test_owner_shares_text_and_image_files_and_the_agent_opens_them(self):
        text = self.m.owner.upload("spec.md", b"# Spec\nThe button is blue.\n", "text/markdown")
        png = self.m.owner.upload("shot.png", H.PNG, "image/png")
        self.m.owner.send(self.a.name, "see attached", files=[text["id"], png["id"]])
        inbox = self.a.call("hub_inbox")
        self.assertIn("[file %s: spec.md, text/markdown" % text["id"], inbox)
        self.assertIn("The button is blue.", self.a.call("hub_file", id=text["id"]))
        result = self.a.call_result("hub_file", id=png["id"])
        self.assertTrue(any(c["type"] == "image" for c in result["content"]), result)
        status, body, headers = H.request(self.m.url + "/files/%s/shot.png" % png["id"], opener=self.m.owner.opener)
        self.assertEqual((status, body), (200, H.PNG))

    def test_an_agent_shares_a_file_from_the_project(self):
        report = self.m.folder / "report.txt"
        report.write_text("all green\n")
        self.a.call("hub_send", to="Owner", text="results", files=[str(report)])
        msg = self.m.owner.wait_for("results", **{"from": self.a.name})
        self.assertEqual(msg["files"][0]["name"], "report.txt")
        status, body, _ = H.request(self.m.url + "/files/%s/report.txt" % msg["files"][0]["id"], opener=self.m.owner.opener)
        self.assertEqual((status, body), (200, b"all green\n"))
        for bad in ("report.txt", str(self.m.folder / "missing.txt")):
            with self.assertRaises(ToolError):
                self.a.call("hub_send", to="Owner", text="bad", files=[bad])

    def test_file_names_cannot_escape_the_files_folder(self):
        meta = self.m.owner.upload("../../../../etc/passwd", b"x", "text/plain")
        self.assertEqual(meta["name"], "passwd")
        meta = self.m.owner.upload('..\\..\\evil<>:"|?*.txt', b"x", "text/plain")
        self.assertNotIn("\\", meta["name"])
        self.assertNotIn("..", meta["name"].strip("."))
        for path in ("/files/../config.json", "/files/%2e%2e/config.json", "/files/zz/../../config.json"):
            status, body, _ = H.request(self.m.url + path, opener=self.m.owner.opener)
            self.assertNotEqual(status, 200, path)
            self.assertNotIn(b"owner_token", body)

    def test_upload_limit(self):
        try:  # refused before it is read: a 413, or the connection closed under the sender
            status = H.request(self.m.url + "/api/upload", b"x" * (20 * 1024 * 1024 + 1), {
                "Origin": self.m.url, "Content-Type": "text/plain", "X-File-Name": "big.txt"}, self.m.owner.opener)[0]
            self.assertEqual(status, 413)
        except H.urllib.error.URLError:
            pass
        self.assertTrue(self.m.up())
        self.assertEqual(self.m.owner.upload("ok.bin", b"y" * (20 * 1024 * 1024), "application/octet-stream")["size"],
                         20 * 1024 * 1024)
        self.assertFalse(list((self.m.home / "files").glob("*/big.txt")))


class Restarts(unittest.TestCase):
    def test_everything_survives_a_restart_and_a_crash(self):
        m = H.Machine("persist")
        try:
            m.start()
            a, b = m.agent(), m.agent()
            a.link(), b.link()
            a.stop_hook()
            m.owner.role(a.name, "lead")
            tid = m.owner.task("all", "persist me")
            b.call("hub_take", id=tid)
            b.call("hub_status", text="on the persist task")
            for crash in (False, True):
                if crash:
                    m.kill(hard=True)
                    m.cli("start", "--no-service", "--no-open")
                    m.wait_up()
                else:
                    m.restart()
                poll = m.owner.poll()
                self.assertEqual(poll["taken"][tid], b.name)
                self.assertTrue(any("persist me" in x["text"] for x in poll["messages"]))
                self.assertEqual(m.owner.row(a.name)["role"], "lead")
                self.assertEqual(m.owner.row(b.name)["status"], "on the persist task")
                # The same sessions carry on: their hooks still reach them under their names.
                m.owner.send(a.name, "after restart %s" % crash)
                self.assertIn("after restart %s" % crash, a.stop_hook())
                again = m.agent(session=b.session)
                again.call("hub_link", key=b.key)
                self.assertEqual(again.whoami(), b.name)
                self.assertNotIn("error", m.log().lower().replace("no error", ""))
        finally:
            m.stop()


if __name__ == "__main__":
    unittest.main()
