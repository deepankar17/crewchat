"""Machines linked with `crewchat peers`: each runs its own server, and they share one chat."""
import re
import threading
import time
import unittest

import harness as H
from harness import ToolError, eventually


def same_chat(*machines, timeout=30):
    """Wait until every machine's page has the same messages, each once, and each sender's in the
    order they were sent. (Messages sent at the same moment on two machines may be listed in either
    order.) Returns them."""
    def view(machine):
        return [(m["id"], m["from"], m["to"], m["text"]) for m in machine.owner.messages() if m["kind"] != "event"]

    def check():
        views = [view(x) for x in machines]
        if any(sorted(v) != sorted(views[0]) for v in views):
            return None
        for v in views:
            if len(v) != len(set(v)):
                raise AssertionError("a message is shown twice: %s" % v)
            for sender in {m[1] for m in v}:
                if [m for m in v if m[1] == sender] != [m for m in views[0] if m[1] == sender]:
                    raise AssertionError("%s's messages are out of order" % sender)
        return views[0]
    return eventually(check, timeout, "the machines to show the same chat")


class TwoMachines(unittest.TestCase):
    """The owner's set-up: a Mac with the lead, a Windows laptop with a developer."""

    @classmethod
    def setUpClass(cls):
        cls.mac = H.Machine("mac", agent_names="place")
        cls.win = H.Machine("win", agent_names="place")
        cls.mac.start()
        cls.win.start()
        cls.mac.cli("peers", "invite", "--url", cls.mac.url, "--name", "mac")  # as the guide does it
        cls.win.link_to(cls.mac)
        cls.lead = cls.mac.agent()
        cls.lead.link()
        cls.dev = cls.win.agent()
        cls.dev.link()
        for m, other in ((cls.mac, cls.dev), (cls.win, cls.lead)):
            eventually(lambda: m.owner.sees(other.name), what="%s to see %s" % (m.name, other.name))

    @classmethod
    def tearDownClass(cls):
        cls.mac.stop()
        cls.win.stop()

    def setUp(self):
        for a in (self.lead, self.dev):
            a.stop_hook()

    def test_both_machines_list_everyone_and_the_links(self):
        for m in (self.mac, self.win):
            names = {r["agent"]: r for r in m.owner.agents()}
            self.assertIn(self.lead.name, names)
            self.assertIn(self.dev.name, names)
        self.assertTrue(self.mac.owner.row(self.dev.name)["remote"])
        self.assertFalse(self.mac.owner.row(self.lead.name)["remote"])
        for agent in (self.lead, self.dev):
            listing = agent.call("hub_agents")
            self.assertIn(self.lead.name, listing)
            self.assertIn(self.dev.name, listing)
        status = self.mac.peers_status()
        self.assertIn("win", status)
        self.assertNotIn("offline", status.lower())

    def test_messages_in_every_direction(self):
        self.mac.owner.send(self.dev.name, "from the mac page")
        self.assertIn("from the mac page", eventually(self.dev.stop_hook, what="the dev to get it"))
        self.win.owner.send(self.lead.name, "from the windows page")
        self.assertIn("from the windows page", eventually(self.lead.stop_hook, what="the lead to get it"))
        self.dev.call("hub_send", to=self.lead.name, text="dev to lead")
        self.assertIn("dev to lead", eventually(self.lead.stop_hook, what="the lead to get it"))
        self.lead.call("hub_send", to="all", text="lead to all")
        self.assertIn("lead to all", eventually(self.dev.stop_hook, what="the dev to get it"))
        self.dev.call("hub_send", to="Owner", text="dev to owner")
        for m in (self.mac, self.win):
            m.owner.wait_for("dev to owner", **{"from": self.dev.name, "to": "Owner"})
        same_chat(self.mac, self.win)

    def test_a_listening_agent_on_the_other_machine_wakes_up(self):
        got = {}
        t = threading.Thread(target=lambda: got.update(r=self.dev.stop_hook(listen=30)))
        t.start()
        eventually(lambda: self.mac.owner.row(self.dev.name)["activity"] == "listening",
                   what="the mac page to show the dev listening")
        start = time.time()
        self.mac.owner.send(self.dev.name, "wake up over there")
        t.join(40)
        self.assertIn("wake up over there", got["r"])
        self.assertLess(time.time() - start, 10)

    def test_the_owners_exact_case_lead_on_mac_and_a_direct_task_for_the_windows_developer(self):
        self.mac.owner.role(self.lead.name, "lead")
        self.win.owner.role(self.dev.name, "developer")  # roles can be set from either page
        eventually(lambda: self.win.owner.row(self.lead.name)["role"] == "lead", what="the role to travel")
        eventually(lambda: self.mac.owner.row(self.dev.name)["role"] == "developer", what="the role to travel")
        self.lead.stop_hook()
        self.dev.stop_hook()
        tid = self.mac.owner.task(self.dev.name, "fix the Windows installer")
        got = eventually(self.dev.stop_hook, what="the task to reach the dev")
        self.assertIn("fix the Windows installer", got)
        self.assertIn("first send hub_task update in_progress", got)
        self.dev.call("hub_task", action="update", id=tid, status="in_progress", note="stopping running processes first")
        for m in (self.mac, self.win):
            m.owner.wait_for("stopping running processes first", kind="update", to="Owner")
        self.dev.call("hub_task", action="update", id=tid, status="done", note="installer fixed")
        self.mac.owner.wait_for("installer fixed", kind="update", to="Owner")
        self.assertNotIn("installer fixed", self.lead.stop_hook())
        self.mac.owner.role(self.lead.name, "none")
        self.mac.owner.role(self.dev.name, "none")

    def test_the_lead_on_one_machine_runs_a_developer_on_the_other(self):
        self.mac.owner.role(self.lead.name, "lead")
        eventually(lambda: self.win.owner.row(self.lead.name)["role"] == "lead", what="the role to travel")
        self.lead.stop_hook()
        job = self.mac.owner.task("all", "ship the website")
        self.assertIn("ship the website", self.lead.stop_hook())
        out = self.lead.call("hub_task", action="assign", to=self.dev.name, title="build the download page", parent=job)
        piece = re.search(r"#(\w+)", out).group(1)
        got = eventually(self.dev.stop_hook, what="the piece to reach the dev")
        self.assertIn("[TASK, assigned to you, part of #%s]" % job, got)
        self.dev.call("hub_task", action="update", id=piece, status="in_progress", note="starting the download page")
        self.assertIn("starting the download page", eventually(self.lead.stop_hook, what="the lead to hear"))
        self.dev.call("hub_task", action="update", id=piece, status="done", note="download page live")
        heard = eventually(self.lead.stop_hook, what="the lead to hear it is done")
        self.assertIn("download page live", heard)
        self.assertIn("%s is free" % self.dev.name, heard)
        with self.assertRaises(ToolError):
            self.dev.call("hub_task", action="assign", to=self.lead.name, title="not the lead")
        self.mac.owner.role(self.lead.name, "none")

    def test_one_task_one_taker_across_machines(self):
        extra = [self.mac.agent(), self.win.agent(), self.win.agent()]
        for a in extra:
            a.link()
        racers = [self.lead, self.dev] + extra
        for origin in (self.mac, self.win):
            tid = origin.owner.task("all", "race from %s" % origin.name)
            for a in racers:
                eventually(lambda: tid in a.call("hub_history", limit=50), what="the task everywhere")
            results = {}

            def take(agent):
                try:
                    results[agent.name] = agent.call("hub_task", action="take", id=tid)
                except ToolError as e:
                    results[agent.name] = e

            threads = [threading.Thread(target=take, args=(a,)) for a in racers]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            winners = [n for n, r in results.items() if isinstance(r, str)]
            self.assertEqual(len(winners), 1, results)
            for m in (self.mac, self.win):
                eventually(lambda: m.owner.poll()["taken"].get(tid) == winners[0], what="both to agree")

    def test_a_task_given_back_can_be_taken_on_the_other_machine(self):
        tid = self.mac.owner.task("all", "a task that changes hands")
        for agent in (self.lead, self.dev):
            eventually(lambda: tid in agent.call("hub_history", limit=50), what="the task on both machines")
        self.dev.call("hub_task", action="take", id=tid)
        self.dev.call("hub_task", action="update", id=tid, status="todo", note="giving it back")
        eventually(lambda: tid not in self.mac.owner.poll()["taken"], what="the give-back on the Mac")
        self.lead.call("hub_task", action="take", id=tid)  # was refused before: the first claim never released
        for m in (self.mac, self.win):
            eventually(lambda: m.owner.poll()["taken"].get(tid) == self.lead.name, what="both to agree")
        # The owner puts it back too, and it can be taken afresh, from the other machine.
        self.win.owner.post("/api/task", {"op": "status", "task": tid, "status": "todo"})
        eventually(lambda: tid not in self.win.owner.poll()["taken"], what="the owner's give-back")
        eventually(lambda: tid not in self.mac.owner.poll()["taken"], what="the owner's give-back on the Mac")
        self.dev.call("hub_task", action="take", id=tid)
        for m in (self.mac, self.win):
            eventually(lambda: m.owner.poll()["taken"].get(tid) == self.dev.name, what="both to agree again")

    def test_files_travel_between_machines(self):
        report = self.win.folder / "crash.txt"
        report.write_text("stack trace here\n")
        self.dev.call("hub_send", to=self.lead.name, text="the crash", files=[str(report)])
        got = eventually(self.lead.stop_hook, what="the lead to get the file")
        fid = re.search(r"\[file (\w+): crash.txt", got).group(1)
        self.assertIn("stack trace here", self.lead.call("hub_file", id=fid))
        shot = self.mac.owner.upload("screen.png", H.PNG, "image/png")
        self.mac.owner.send(self.dev.name, "the screen", files=[shot["id"]])
        got = eventually(self.dev.stop_hook, what="the dev to get the image")
        fid = re.search(r"\[file (\w+): screen.png", got).group(1)
        result = self.dev.call_result("hub_file", id=fid)
        self.assertTrue(any(c["type"] == "image" for c in result["content"]))
        status, body, _ = H.request(self.win.url + "/files/%s/screen.png" % fid, opener=self.win.owner.opener)
        self.assertEqual(body, H.PNG, status)

    def test_status_and_renames_travel(self):
        self.dev.call("hub_status", text="compiling")
        eventually(lambda: self.mac.owner.row(self.dev.name)["status"] == "compiling", what="the status")


class SharedProjects(unittest.TestCase):
    """The owner's case: a folder called t2 on both machines is one project, t2, on both."""

    def test_projects_with_the_same_name_are_one_chat(self):
        mac, win = H.Machine("mac", project="test", agent_names="place"), H.Machine("win", project="android", agent_names="place")
        try:
            mac.start()
            win.start()
            win.link_to(mac)
            folders = {}
            for m in (mac, win):
                folder = m.dir / "t2"
                folder.mkdir()
                H.subprocess.run(["git", "init", "-q", str(folder)], check=True)
                self.assertIn('Created the project "t2"', m.cli("start", "--no-service", "--no-open", cwd=folder))
                folders[m.name] = folder
            x = mac.agent(folder=folders["mac"])
            y = win.agent(folder=folders["win"])
            x.link()
            y.link()
            mac.owner.chat = win.owner.chat = "t2"
            for m, other in ((mac, y), (win, x)):
                eventually(lambda: m.owner.sees(other.name), timeout=60, what="%s's t2 to show %s" % (m.name, other.name))
            y.stop_hook()
            x.call("hub_send", to=y.name, text="hello from the Mac's t2")
            self.assertIn("hello from the Mac's t2", eventually(y.stop_hook, timeout=60, what="the laptop's t2 agent"))
            # The first projects (test and android) stay linked with each other, and apart from t2.
            a, b = mac.agent(), win.agent()
            a.link()
            b.link()
            mac.owner.chat = win.owner.chat = ""
            eventually(lambda: win.owner.sees(a.name), timeout=60, what="the first projects linked")
            self.assertNotIn(y.name, [r["agent"] for r in mac.owner.agents()])
            projects = {p["id"]: p for p in mac.owner.poll()["projects"]}
            eventually(lambda: {p["id"]: p for p in mac.owner.poll()["projects"]}["t2"]["linked"], timeout=60,
                       what="t2 marked shared")
            self.assertTrue(projects["test"]["linked"])
            # A task on the shared sheet is taken once, from either machine.
            win.owner.chat = "t2"
            tid = win.owner.post("/api/task?chat=t2", {"op": "add", "title": "shared sheet task"})
            self.assertEqual(tid[0], 200)
            task = H.json.loads(tid[1])["id"]
            eventually(lambda: task in x.call("hub_task", action="view", show="all"), timeout=60, what="the task on the Mac's sheet")
            x.call("hub_task", action="take", id=task)
            eventually(lambda: win.owner.poll()["taken"].get(task) == x.name, timeout=60, what="the laptop to know")
        finally:
            mac.stop()
            win.stop()


class Outages(unittest.TestCase):
    """A machine going down, coming back, and leaving."""

    def setUp(self):
        self.a, self.b = H.Machine("desk", agent_names="place"), H.Machine("laptop", agent_names="place")
        self.a.start()
        self.b.start()
        self.b.link_to(self.a)
        self.x, self.y = self.a.agent(), self.b.agent()
        self.x.link(), self.y.link()
        eventually(lambda: self.a.owner.sees(self.y.name), what="the link")
        eventually(lambda: self.b.owner.sees(self.x.name), what="the link")
        self.x.stop_hook(), self.y.stop_hook()

    def tearDown(self):
        self.a.stop()
        self.b.stop()

    def test_a_machine_that_crashes_catches_up_without_losing_or_repeating_anything(self):
        self.b.kill(hard=True)
        eventually(lambda: "offline" in self.a.peers_status().lower(), timeout=60, what="desk to see laptop offline")
        eventually(lambda: not self.a.owner.row(self.y.name)["online"], timeout=60, what="laptop's agent offline")
        for i in range(5):
            self.a.owner.send(self.y.name, "while you were down %d" % i)
        self.x.call("hub_send", to="all", text="broadcast while down")
        self.b.cli("start", "--no-service", "--no-open")
        self.b.wait_up()
        got = eventually(lambda: self.y.stop_hook() if "broadcast while down" in str(self.b.owner.messages()) else "",
                         timeout=60, what="laptop to catch up")
        for i in range(5):
            self.assertEqual(got.count("while you were down %d" % i), 1)
        self.assertIn("broadcast while down", got)
        self.assertEqual(self.y.stop_hook(), "")
        eventually(lambda: self.a.owner.row(self.y.name)["online"], timeout=60, what="laptop's agent back online")
        same_chat(self.a, self.b)
        ids = [m["id"] for m in self.b.owner.messages()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_the_first_machine_restarting_does_not_break_the_link(self):
        self.a.restart()
        self.y.call("hub_send", to=self.x.name, text="after the desk restarted")
        self.assertIn("after the desk restarted", eventually(self.x.stop_hook, timeout=60, what="the message"))
        self.a.owner.send(self.y.name, "and back")
        self.assertIn("and back", eventually(self.y.stop_hook, timeout=60, what="the message"))

    def test_a_task_from_a_machine_that_is_down_cannot_be_taken_twice(self):
        # Roadmap Y2: the desk is down, so the coordinator (the laptop) settles the claim; back, the desk keeps it.
        tid = self.a.owner.task("all", "a task from the desk")
        eventually(lambda: tid in self.y.call("hub_history"), what="the task to reach the laptop")
        self.a.kill(hard=True)
        self.assertIn("is yours", self.y.call("hub_task", action="take", id=tid))  # no "try again when it is back"
        self.a.cli("start", "--no-service", "--no-open")
        self.a.wait_up()
        eventually(lambda: self.a.owner.poll()["taken"].get(tid) == self.y.name, timeout=60, what="the take to travel")
        x = self.a.agent()  # (the desk's agent before the restart was not linked again)
        x.link()
        with self.assertRaises(ToolError) as e:
            x.call("hub_task", action="take", id=tid)
        self.assertIn("taken by %s" % self.y.name, str(e.exception))

    def test_leaving_and_removing(self):
        self.b.cli("peers", "leave")
        eventually(lambda: "laptop" not in self.a.peers_status(), timeout=30, what="desk to forget laptop")
        self.a.owner.send("all", "after leaving")
        time.sleep(3)
        self.assertNotIn("after leaving", str(self.b.owner.messages()))
        # Link again, then remove from the other side.
        self.b.link_to(self.a)
        eventually(lambda: self.a.owner.sees(self.y.name), timeout=30, what="the new link")
        self.a.cli("peers", "remove", "laptop")
        eventually(lambda: "laptop" not in self.a.peers_status(), timeout=30, what="laptop removed")
        self.a.owner.send("all", "after removing")
        time.sleep(3)
        self.assertNotIn("after removing", str(self.b.owner.messages()))


class ThreeMachines(unittest.TestCase):
    def test_a_third_machine_joins_through_the_second_and_everything_arrives_once(self):
        a, b, c = H.Machine("one", agent_names="place"), H.Machine("two", agent_names="place"), H.Machine("three", agent_names="place")
        try:
            for m in (a, b, c):
                m.start()
            b.link_to(a)
            c.link_to(b)  # invited by the second machine, not the first
            agents = {m.name: m.agent() for m in (a, b, c)}
            for agent in agents.values():
                agent.link()
            for m in (a, b, c):
                for agent in agents.values():
                    eventually(lambda: m.owner.sees(agent.name), timeout=30, what="%s to see %s" % (m.name, agent.name))
            for agent in agents.values():
                agent.stop_hook()
            agents["three"].call("hub_send", to=agents["one"].name, text="three to one")
            agents["one"].call("hub_send", to="all", text="one to all")
            got = eventually(agents["one"].stop_hook, timeout=30, what="one to hear from three")
            self.assertEqual(got.count("three to one"), 1)
            for name in ("two", "three"):
                got = eventually(agents[name].stop_hook, timeout=30, what="%s to hear one" % name)
                self.assertEqual(got.count("one to all"), 1)
            same_chat(a, b, c)
            # The second machine goes away; the others still have each other.
            b.kill(hard=True)
            agents["three"].call("hub_send", to=agents["one"].name, text="without two")
            self.assertIn("without two", eventually(agents["one"].stop_hook, timeout=60, what="direct link 3->1"))
        finally:
            for m in (a, b, c):
                m.stop()



def race(agents, tid):
    """Every agent tries to take the task at once: {agent name: its answer, or the ToolError it got}."""
    results = {}

    def take(agent):
        try:
            results[agent.name] = agent.call("hub_task", action="take", id=tid)
        except ToolError as e:
            results[agent.name] = e
    threads = [threading.Thread(target=take, args=(a,)) for a in agents]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def coordinating(machine):
    """Who the machine says coordinates now (`crewchat coordinator`), by name."""
    line = machine.cli("coordinator").splitlines()[0]
    return line.split(": ", 1)[1].split(" (")[0].rstrip(".")


def link_three(cleanup, names=("one", "two", "three")):
    """Three machines, the second and third joining the first; an agent on each, seen by all three. cleanup: the
    test's (or its class's) addCleanup, which stops them."""
    machines = [H.Machine(name, agent_names="place") for name in names]
    for m in machines:
        cleanup(m.stop)
        m.start()
    for m in machines[1:]:
        m.link_to(machines[0])
    agents = [m.agent() for m in machines]
    for agent in agents:
        agent.link()
    for m in machines:
        for agent in agents:
            eventually(lambda: m.owner.sees(agent.name), timeout=60, what="%s to see %s" % (m.name, agent.name))
    for agent in agents:
        agent.stop_hook()
    return machines, agents


class ThreeMachinesLinked(unittest.TestCase):
    """Three machines over Tailscale, all on: tasks, files and the coordinator agree everywhere."""

    @classmethod
    def setUpClass(cls):
        cls.machines, cls.agents = link_three(cls.addClassCleanup)

    def setUp(self):
        for a in self.agents:
            a.stop_hook()

    def test_a_task_has_one_taker_among_agents_on_three_machines(self):
        extra = [m.agent() for m in self.machines]
        for a in extra:
            a.link()
        racers = self.agents + extra
        for origin in self.machines:
            tid = origin.owner.task("all", "race from %s" % origin.name)
            for a in racers:
                eventually(lambda: tid in a.call("hub_history", limit=50), timeout=60, what="the task on every machine")
            results = race(racers, tid)
            winners = [n for n, r in results.items() if isinstance(r, str)]
            self.assertEqual(len(winners), 1, results)
            for m in self.machines:
                eventually(lambda: m.owner.poll()["taken"].get(tid) == winners[0], timeout=60, what="all three to agree")
        same_chat(*self.machines)

    def test_a_file_shared_on_one_opens_on_both_others(self):
        one, two, three = self.agents
        report = self.machines[0].folder / "trace.txt"
        report.write_text("stack trace from one\n")
        one.call("hub_send", to="all", text="the trace", files=[str(report)])
        for agent in (two, three):
            got = eventually(agent.stop_hook, timeout=60, what="%s to get the file" % agent.name)
            fid = re.search(r"\[file (\w+): trace.txt", got).group(1)
            self.assertIn("stack trace from one", agent.call("hub_file", id=fid))

    def test_all_three_name_the_same_coordinator_and_the_owners_choice(self):
        for m in self.machines:
            eventually(lambda: coordinating(m) == "one", timeout=60, what="%s to name the first machine" % m.name)
        self.machines[1].cli("coordinator", "three")  # chosen on the second, about the third
        try:
            for m in self.machines:
                eventually(lambda: coordinating(m) == "three", timeout=60, what="%s to follow the choice" % m.name)
        finally:
            self.machines[1].cli("coordinator", "none")
        for m in self.machines:
            eventually(lambda: coordinating(m) == "one", timeout=60, what="%s back to the first" % m.name)


class ThreeMachineOutages(unittest.TestCase):
    """One of three machines going down, coming back, or being removed: the other two carry on together."""

    def setUp(self):
        self.machines, self.agents = link_three(self.addCleanup)

    def test_when_the_coordinator_goes_down_the_other_two_agree_on_the_next(self):
        one, two, three = self.machines
        one.kill(hard=True)
        for m in (two, three):
            eventually(lambda: coordinating(m) == "two", timeout=90, what="%s to name the second machine" % m.name)

    def test_a_task_from_a_machine_that_is_down_has_one_taker_on_the_other_two(self):
        one, two, three = self.machines
        tid = one.owner.task("all", "a task from the first machine")
        for agent in self.agents[1:]:
            eventually(lambda: tid in agent.call("hub_history", limit=50), timeout=60, what="the task everywhere")
        one.kill(hard=True)
        results = race(self.agents[1:], tid)  # no "try again when it is back": the coordinator settles it
        winners = [n for n, r in results.items() if isinstance(r, str)]
        self.assertEqual(len(winners), 1, results)
        for m in (two, three):
            eventually(lambda: m.owner.poll()["taken"].get(tid) == winners[0], timeout=60, what="the two to agree")
        one.cli("start", "--no-service", "--no-open")
        one.wait_up()
        eventually(lambda: one.owner.poll()["taken"].get(tid) == winners[0], timeout=90, what="the first to adopt it")
        late = one.agent()
        late.link()
        with self.assertRaises(ToolError) as e:
            late.call("hub_task", action="take", id=tid)
        self.assertIn("taken by %s" % winners[0], str(e.exception))

    def test_a_machine_back_after_a_crash_catches_up_from_both_others(self):
        one, two, three = self.machines
        a, b, c = self.agents
        three.kill(hard=True)
        eventually(lambda: not one.owner.row(c.name)["online"], timeout=90, what="the third machine's agent offline")
        for i in range(3):
            a.call("hub_send", to=c.name, text="from one %d" % i)
            b.call("hub_send", to=c.name, text="from two %d" % i)
        three.cli("start", "--no-service", "--no-open")
        three.wait_up()
        def caught_up():
            text = str(three.owner.messages())
            return all("from %s %d" % (w, i) in text for w in ("one", "two") for i in range(3))
        eventually(caught_up, timeout=90, what="the third machine to catch up")
        got = c.stop_hook()
        for w in ("one", "two"):
            for i in range(3):
                self.assertEqual(got.count("from %s %d" % (w, i)), 1, got)
        same_chat(one, two, three, timeout=60)

    def test_removing_one_keeps_the_other_two_together(self):
        one, two, three = self.machines
        a, b, c = self.agents
        one.cli("peers", "remove", "three")
        for m in (one, two):
            eventually(lambda: "three" not in m.peers_status(), timeout=60, what="%s to forget the third" % m.name)
        b.call("hub_send", to=a.name, text="two to one after the removal")
        self.assertIn("two to one after the removal", eventually(a.stop_hook, timeout=60, what="one to hear two"))
        a.call("hub_send", to="all", text="one to all after the removal")
        self.assertIn("one to all after the removal", eventually(b.stop_hook, timeout=60, what="two to hear one"))
        time.sleep(3)
        self.assertNotIn("after the removal", str(three.owner.messages()))


if __name__ == "__main__":
    unittest.main()
