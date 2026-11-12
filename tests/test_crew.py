"""C10: a lead whose role has the crew power adds agents to the crew (hub_crew add), sees them (list) and removes them
(remove), within the owner's limit and without asking; an added agent with no work is pointed out to its lead."""
import json
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_crewchat import TMP, Base, crewchat  # noqa: E402  (the shared server fixture)

LEAD_POWERS = ["assign", "areas", "notes", "code", "crew"]


class LeadAddsAgents(unittest.TestCase):

    def setUp(self, powers=LEAD_POWERS):
        self.root = Path(tempfile.mkdtemp(prefix="crew-", dir=TMP))
        crewchat.save_config({"project": "web", "port": 9,
                              "roles": {"lead": {"description": "You lead.", "powers": list(powers)}}}, self.root)
        self.hub = crewchat.Hub(crewchat.Roster(self.root))
        self.folder = str(Path(tempfile.mkdtemp(prefix="crew-folder-", dir=TMP)).resolve())
        self.launched = []
        self.saved = crewchat.launch_agent, crewchat.first_run_questions
        crewchat.launch_agent = lambda tool, folder, key, permissions="ask", root=None, model="", env=None, \
            chrome=False, background=False, fork="": self.launched.append(
                {"tool": tool, "folder": folder, "key": key, "permissions": permissions, "model": model,
                 "background": background}) or ""
        crewchat.first_run_questions = lambda tool, folder, env=None: []
        connected = mock.patch.object(self.hub, "_connected_folder", side_effect=lambda f: f == self.folder)
        connected.start()
        self.addCleanup(connected.stop)
        self.lead = self.agent("Lead")
        self.hub.set_role(crewchat.OWNER, self.lead, "lead")
        with self.hub.lock:
            self.hub.agents[self.lead].update(
                session={"tool": "claude", "folder": self.folder, "id": "s-lead", "pid": 1},
                permissions="edits", model="opus")

    def tearDown(self):
        crewchat.launch_agent, crewchat.first_run_questions = self.saved
        self.hub.events.close()

    def agent(self, name):
        sid = self.hub.open_session("mac", "claude")
        made = self.hub.agent_for(sid)
        self.hub.rename(made, name)
        return name

    def check_in(self, n=-1):
        """The launched agent checks in with its start key: (its name, what it is told)."""
        sid = self.hub.open_session("mac", "claude")
        return self.hub.start_link(sid, self.launched[n]["key"])

    def call(self, who, **args):
        sid = next(s for s, r in self.hub.sessions.items() if r["agent"] == who)
        return crewchat.call_tool(self.hub, sid, "hub_crew", args)

    def events(self, word):
        return [m["text"] for m in self.hub.messages if m["kind"] == "event" and word in m["text"]]

    def test_the_lead_adds_a_developer_who_gets_its_task_from_the_lead(self):
        out = self.call(self.lead, action="add", role="developer", name="Dev1", task="Fix the login form",
                        jira="PROJ-7")
        self.assertIn("Starting a new developer agent called Dev1", out)
        start = self.launched[0]
        # The lead's own tool, folder, model and permissions; in the background, nobody asked.
        self.assertEqual((start["tool"], start["folder"], start["model"], start["permissions"], start["background"]),
                         ("claude", self.folder, "opus", "edits", True))
        self.assertTrue(self.events("Lead started a new Claude Code agent"))
        self.assertFalse(self.hub.first_runs)
        name, note = self.check_in()
        self.assertEqual(name, "Dev1")
        self.assertIn("started from the crewchat by Lead, the lead", note)
        self.assertEqual(self.hub.agents["Dev1"]["role"], "developer")
        self.assertEqual(self.hub.agents["Dev1"]["added_by"], "Lead")
        task = [m for m in self.hub.messages if m["kind"] == "task" and m["to"] == "Dev1"][-1]
        self.assertEqual((task["from"], task["text"]), ("Lead", "Fix the login form"))  # it reports to the lead
        self.assertEqual(self.hub.sheet[task["id"]].get("jira"), "PROJ-7")
        self.assertEqual(next(r for r in self.hub.rows() if r["agent"] == "Dev1")["added_by"], "Lead")
        self.assertIn("Dev1 (developer)", self.call(self.lead, action="list"))

    def test_the_limit_counts_agents_still_starting_and_the_owner_sets_it(self):
        self.assertEqual(self.hub.crew_settings()["limit"], 4)  # the owner's default (11 Nov 2026)
        self.hub.set_crew(limit=2)
        self.call(self.lead, action="add", role="developer")
        self.call(self.lead, action="add", role="qa")  # neither has checked in yet: both count
        with self.assertRaises(crewchat.HubError) as e:
            self.call(self.lead, action="add", role="developer")
        self.assertIn("the most the owner allows at a time (2)", str(e.exception))
        self.assertEqual(len(self.launched), 2)
        name, _ = self.check_in(0)
        self.call(self.lead, action="remove", name=name)  # one removed: room for one more
        self.call(self.lead, action="add", role="developer")
        self.assertEqual(len(self.launched), 3)
        self.hub.set_crew(limit=0)
        with self.assertRaises(crewchat.HubError):
            self.call(self.lead, action="add", role="developer")
        for bad in (-1, 11, "lots", True):
            with self.assertRaises(crewchat.HubError):
                self.hub.set_crew(limit=bad)

    def test_two_adds_at_once_cannot_both_pass_the_limit(self):
        # Review: the limit was checked under the lock, then launch ran outside it: two adds at once both passed.
        self.hub.set_crew(limit=1)
        real, refused = self.hub.launch, []

        def launch(*args, **kwargs):
            if not refused:
                refused.append("")
                try:
                    self.call(self.lead, action="add", role="qa")  # the other add, while this one starts
                except crewchat.HubError as e:
                    refused.append(str(e))
            return real(*args, **kwargs)
        with mock.patch.object(self.hub, "launch", side_effect=launch):
            self.call(self.lead, action="add", role="developer")
        self.assertEqual(len(self.launched), 1)
        self.assertIn("the most the owner allows at a time (1)", refused[-1])
        self.assertFalse([k for k in self.hub.starts if k.startswith("crew-hold-")])  # the hold is let go

    def test_do_anything_needs_the_owner_say_so(self):
        with self.hub.lock:
            self.hub.agents[self.lead]["permissions"] = "all"
        self.call(self.lead, action="add", role="developer")
        self.assertEqual(self.launched[-1]["permissions"], "auto")  # the strongest short of "Do anything"
        self.hub.set_crew(anything=True)
        self.call(self.lead, action="add", role="developer")
        self.assertEqual(self.launched[-1]["permissions"], "all")

    def test_the_lead_gives_chrome_or_the_emulators_as_the_work_needs(self):
        synced = []
        with mock.patch.object(crewchat, "sync_device_server", side_effect=lambda f, on: synced.append((f, on))), \
                mock.patch.object(self.hub, "_mark_mobile_used"):
            out = self.call(self.lead, action="add", role="qa", name="Qa1", browser=True, mobile=True)
            self.assertIn("using the browser and the mobile devices", out)
            self.assertEqual(synced, [(self.folder, True)])  # its "mobile" server is there before it starts
            name, _ = self.check_in()
            self.assertTrue(self.hub.agents[name]["browser"])
            self.assertTrue(self.hub.agents[name]["mobile"])
            self.call(self.lead, action="add", role="developer", name="Dev1")  # left out: neither (the lead has no browser)
            name, _ = self.check_in()
            self.assertFalse(self.hub.agents[name].get("browser"))
            self.assertFalse(self.hub.agents[name].get("mobile"))
            with self.hub.lock:
                self.hub.agents[self.lead]["browser"] = True
            self.call(self.lead, action="add", role="developer", name="Dev2")  # the browser as the lead has it
            self.assertTrue(self.hub.agents["Dev2"]["browser"] if "Dev2" in self.hub.agents
                            else self.hub.starts[self.launched[-1]["key"]]["browser"])
            self.call(self.lead, action="add", role="developer", name="Dev3", browser=False)
            self.assertFalse(self.hub.starts[self.launched[-1]["key"]]["browser"])
        with self.assertRaises(crewchat.HubError):
            self.call(self.lead, action="add", role="qa", mobile="yes")

    def test_which_roles_a_lead_may_give(self):
        for role in ("lead", "advisor", "nope"):  # never another top lead, nor the advisor (only the owner gives it)
            with self.assertRaises(crewchat.HubError):
                self.call(self.lead, action="add", role=role)
        self.assertEqual(self.launched, [])

    def test_a_folder_not_connected_here_is_refused(self):
        with self.assertRaises(crewchat.HubError):
            self.call(self.lead, action="add", role="developer", folder="/somewhere/else")
        with self.hub.lock:
            self.hub.agents[self.lead]["session"] = {}  # no session recorded: it must name a folder
        with self.assertRaises(crewchat.HubError) as e:
            self.call(self.lead, action="add", role="developer")
        self.assertIn("no folder recorded", str(e.exception))
        self.call(self.lead, action="add", role="developer", folder=self.folder)
        self.assertEqual(len(self.launched), 1)

    def test_remove_stops_its_session_and_puts_its_task_back(self):
        self.call(self.lead, action="add", role="developer", name="Dev1", task="Fix the login form")
        self.check_in()
        mid = next(m["id"] for m in self.hub.messages if m["kind"] == "task" and m["to"] == "Dev1")
        self.hub.take("Dev1", mid)
        with self.hub.lock:
            self.hub.agents["Dev1"]["session"] = {"tool": "claude", "folder": self.folder, "id": "s-dev", "pid": 2}
        stopped = []
        with mock.patch.object(self.hub, "_running_now", return_value=True), \
                mock.patch.object(self.hub, "_end_session", side_effect=lambda n, s: stopped.append(n)):
            out = self.call(self.lead, action="remove", name="Dev1")
        self.assertIn("its session is stopped", out)
        self.assertEqual(stopped, ["Dev1"])
        self.assertNotIn("Dev1", self.hub.agents)
        self.assertTrue(self.events("Dev1 was removed by Lead"))
        self.assertTrue(any(m.get("reclaimed") == "Dev1" and m.get("task") == mid for m in self.hub.messages))

    def test_a_lead_removes_only_agents_it_added(self):
        other = self.agent("Owners-dev")
        with self.assertRaises(crewchat.HubError) as e:
            self.call(self.lead, action="remove", name=other)
        self.assertIn("only agents you added", str(e.exception))
        self.assertIn(other, self.hub.agents)

    def test_without_the_power_hub_crew_is_refused(self):
        dev = self.agent("Dev")
        self.hub.set_role(crewchat.OWNER, dev, "developer")
        for action in ("list", "add", "remove"):
            with self.assertRaises(crewchat.HubError) as e:
                self.call(dev, action=action, role="qa", name="x")
            self.assertIn("Adds and removes agents", str(e.exception))
        told = [m for m in self.hub.messages if m["to"] == crewchat.OWNER and "tried to add or remove" in m["text"]]
        self.assertEqual(len(told), 1)  # once, however often it tries
        self.assertIn("on the developer role in Crew & roles", told[0]["text"])

    def test_an_idle_added_agent_is_pointed_out_to_its_lead_once(self):
        self.call(self.lead, action="add", role="developer", name="Dev1")
        self.check_in()
        now = time.time()
        self.assertEqual(self.hub.crew_tick(now), [])  # free from now
        self.assertEqual(self.hub.crew_tick(now + crewchat.CREW_IDLE - 1), [])
        self.assertEqual(self.hub.crew_tick(now + crewchat.CREW_IDLE), ["Dev1"])
        told = [m for m in self.hub.messages if m["to"] == "Lead" and "has had no work" in m["text"]]
        self.assertEqual(len(told), 1)
        self.assertIn("hub_crew action=remove name=Dev1", told[0]["text"])
        self.assertEqual(self.hub.crew_tick(now + 2 * crewchat.CREW_IDLE), [])  # once
        # Work again, then free again: told again after another stretch.
        mid = self.hub.assign("Lead", "Dev1", "Add a test")["id"]
        self.hub.take("Dev1", mid)
        self.assertEqual(self.hub.crew_tick(now + 3 * crewchat.CREW_IDLE), [])
        self.hub.owner_status(mid, "done", "")
        later = now + 4 * crewchat.CREW_IDLE
        self.assertEqual(self.hub.crew_tick(later), [])
        self.assertEqual(self.hub.crew_tick(later + crewchat.CREW_IDLE), ["Dev1"])

    def test_after_a_restart_the_lead_still_removes_counts_and_hears_of_its_agents(self):
        # Review: _load restored only the fields it listed, so added_by (and restart_due) were lost on a restart, and
        # its permissions "plan" too.
        self.call(self.lead, action="add", role="developer", name="Dev1")
        self.check_in()
        with self.hub.lock:
            self.hub.agents["Dev1"].update(permissions="plan", restart_due=["browser"])
            self.hub._save()
        self.hub.events.close()
        self.hub = crewchat.Hub(crewchat.Roster(self.root))
        dev = self.hub.agents["Dev1"]
        self.assertEqual((dev.get("added_by"), dev.get("permissions"), dev.get("restart_due")),
                         ("Lead", "plan", ["browser"]))
        self.assertIn("Agents you added (1 of 4)", self.hub.crew_list(self.lead))
        self.assertEqual(self.hub.crew_tick(time.time() + crewchat.CREW_IDLE + 1), [])
        self.assertEqual(self.hub.crew_tick(time.time() + 2 * crewchat.CREW_IDLE + 2), ["Dev1"])
        self.assertIn("is removed", self.hub.crew_remove(self.lead, "Dev1"))

    def test_a_rename_keeps_who_added_whom(self):
        # Review: rename missed other agents' added_by and _crew_free.
        self.call(self.lead, action="add", role="developer", name="Dev1")
        self.check_in()
        now = time.time()
        self.hub.crew_tick(now)
        self.hub.rename("Dev1", "Dev9")
        self.assertEqual(self.hub._crew_free.get("Dev9"), now)
        self.assertNotIn("Dev1", self.hub._crew_free)
        self.hub.rename(self.lead, "Boss")
        self.assertEqual(self.hub.agents["Dev9"]["added_by"], "Boss")
        self.assertIn("Agents you added (1 of 4)", self.hub.crew_list("Boss"))
        self.assertIn("is removed", self.hub.crew_remove("Boss", "Dev9"))

    def test_agents_the_owner_started_are_never_pointed_out(self):
        self.agent("Owners-dev")
        self.assertEqual(self.hub.crew_tick(time.time() + 10 * crewchat.CREW_IDLE), [])

    def test_the_power_gives_the_tool_and_says_how_to_use_it(self):
        sid = next(s for s, r in self.hub.sessions.items() if r["agent"] == self.lead)
        self.assertIn("hub_crew", [t["name"] for t in crewchat.tools_for(self.hub, sid)])
        role_text = [m for m in self.hub.messages if m["kind"] == "role" and m["to"] == self.lead][-1]["text"]
        self.assertIn("hub_crew action=add", role_text)
        self.assertIn("at most 4 at a time", role_text)
        self.assertIn("not the crewchat command line", role_text)
        self.assertNotIn("hub_crew", self.hub._crew_note("developer"))
        self.assertLessEqual(len(json.dumps(crewchat.CREW_TOOL)), 1000)  # only a role with the power loads it
        self.hub.set_role(crewchat.OWNER, self.lead, "developer")
        self.assertNotIn("hub_crew", [t["name"] for t in crewchat.tools_for(self.hub, sid)])
        with self.assertRaises(crewchat.HubError):
            self.call(self.lead, action="list")


class CommandLineRunByAnAgent(unittest.TestCase):
    """`crewchat agent add|remove` run by an agent crewchat started (CREWCHAT_LINK_KEY set) asks for hub_crew as that
    agent, not /api/launch as the owner (a live test, 11 Nov 2026: a lead ran it and the agent showed as the owner's)."""

    def run_cli(self, *argv, key="key-of-leo"):
        calls = []
        args = crewchat.build_parser().parse_args(["agent", *argv])
        with mock.patch.dict(crewchat.os.environ, {"CREWCHAT_LINK_KEY": key} if key else {}, clear=False), \
                mock.patch.object(crewchat, "owner_call", side_effect=lambda path, body, **k: calls.append(
                    (path, body)) or {"text": "ok"}):
            if not key:
                crewchat.os.environ.pop("CREWCHAT_LINK_KEY", None)
            args.fn(args)
        return calls

    def test_an_agent_adds_and_removes_as_itself(self):
        (path, body), = self.run_cli("add", "Quill", "--role", "docs", "--task", "Write the guide")
        self.assertEqual(path, "/api/launch")
        self.assertEqual((body["agent_key"], body["name"], body["role"], body["task"]),
                         ("key-of-leo", "Quill", "docs", "Write the guide"))
        (path, body), = self.run_cli("remove", "Quill")
        self.assertEqual((path, body["agent_key"], body["op"], body["name"]), ("/api/agent", "key-of-leo", "remove", "Quill"))

    def test_the_owner_in_a_terminal_still_starts_one_as_before(self):
        (path, body), = self.run_cli("add", "Quill", "--role", "docs", key="")
        self.assertEqual(path, "/api/launch")
        self.assertNotIn("agent_key", body)


class AgentKeyOverHttp(Base):
    """/api/launch and /api/agent with an agent's link key go through hub_crew's rules, never the owner's launch."""

    def post(self, path, body):
        try:
            return crewchat.post_json(self.base + path, self.owner, body)
        except urllib.error.HTTPError as e:
            return dict(json.loads(e.read()), status=e.code)

    def test_an_unknown_key_or_an_agent_without_the_power_is_refused(self):
        launched = []
        with mock.patch.object(self.hub, "launch", side_effect=lambda *a, **k: launched.append(a) or {"ok": True}):
            out = self.post("/api/launch", {"agent_key": "nobody", "role": "developer", "tool": "claude"})
            self.assertEqual(out["status"], 400)
            self.assertIn("hub_crew", out["error"])
            _, name = self.agent()
            with self.hub.lock:
                self.hub.links["key-of-" + name] = name
            out = self.post("/api/launch", {"agent_key": "key-of-" + name, "role": "developer", "tool": "claude"})
            self.assertEqual(out["status"], 400)
            self.assertIn("Adds and removes agents", out["error"])
            out = self.post("/api/agent", {"agent_key": "key-of-" + name, "op": "remove", "name": "someone"})
            self.assertEqual(out["status"], 400)
            self.assertIn(name, self.hub.agents)
        self.assertEqual(launched, [])


class PackLead(unittest.TestCase):
    """The lead of a pack, and of a project whose Lead has no powers saved, has the crew power (the owner's decision,
    11 Nov 2026); a lead whose powers the owner saved without it is told how to get it, not left to guess."""

    def test_a_pack_lead_has_the_crew_power(self):
        root = Path(tempfile.mkdtemp(prefix="crew-pack-", dir=TMP))
        crewchat.save_config({"project": "web", "port": 9, "roles": {"lead": {"description": "You lead."}}}, root)
        hub = crewchat.Hub(crewchat.Roster(root))
        try:
            self.assertIn("crew", hub._powers("lead"))
            self.assertIn("hub_crew action=add", hub._crew_note("lead"))
            self.assertNotIn("crew", hub._powers("developer"))
        finally:
            hub.events.close()

    def test_a_lead_without_it_is_told_how_to_get_it(self):
        root = Path(tempfile.mkdtemp(prefix="crew-pack-", dir=TMP))
        crewchat.save_config({"project": "web", "port": 9,
                              "roles": {"lead": {"description": "You lead.", "powers": ["assign"]}}}, root)
        hub = crewchat.Hub(crewchat.Roster(root))
        try:
            note = hub._crew_note("lead")
            self.assertIn("ask Owner with hub_send to turn it on for your role in Crew & roles", note)
            self.assertIn("do not use the crewchat command line", note)
            self.assertEqual(hub._crew_note("developer"), "")  # no word to those who do not hand out work
        finally:
            hub.events.close()


class CrewSettingsOverHttp(Base):
    """The owner sets the limit and "Do anything" on the page (/api/roles op crew); the role list carries them."""

    def op(self, **body):
        try:
            return crewchat.post_json(self.base + "/api/roles", self.owner, body)
        except urllib.error.HTTPError as e:
            return dict(json.loads(e.read()), status=e.code)

    def test_the_owner_sets_them(self):
        self.addCleanup(lambda: self.hub.set_crew(limit=crewchat.CREW_LIMIT, anything=False, park=crewchat.PARK_AFTER))
        self.assertEqual(self.op(op="list")["crew"],
                         {"limit": 4, "anything": False, "most": 10, "park": 30, "park_most": 1440})
        self.assertEqual(self.op(op="crew", limit=6)["limit"], 6)
        self.assertTrue(self.op(op="crew", anything=True)["anything"])
        self.assertEqual(self.op(op="crew", park=45)["park"], 45)  # how long before a lead may park a task (C11)
        self.assertEqual(self.op(op="list")["crew"],
                         {"limit": 6, "anything": True, "most": 10, "park": 45, "park_most": 1440})
        self.assertEqual(self.op(op="crew", limit=99)["status"], 400)
        self.assertEqual(self.op(op="crew", park=99999)["status"], 400)


if __name__ == "__main__":
    unittest.main()
