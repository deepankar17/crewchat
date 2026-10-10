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
        cls.mac = H.Machine("mac")
        cls.win = H.Machine("win")
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
        self.assertIn("first send hub_update in_progress", got)
        self.dev.call("hub_update", id=tid, status="in_progress", note="stopping running processes first")
        for m in (self.mac, self.win):
            m.owner.wait_for("stopping running processes first", kind="update", to="Owner")
        self.dev.call("hub_update", id=tid, status="done", note="installer fixed")
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
        out = self.lead.call("hub_assign", to=self.dev.name, text="build the download page", task=job)
        piece = re.search(r"#(\w+)", out).group(1)
        got = eventually(self.dev.stop_hook, what="the piece to reach the dev")
        self.assertIn("[TASK, assigned to you, part of #%s]" % job, got)
        self.dev.call("hub_update", id=piece, status="in_progress", note="starting the download page")
        self.assertIn("starting the download page", eventually(self.lead.stop_hook, what="the lead to hear"))
        self.dev.call("hub_update", id=piece, status="done", note="download page live")
        heard = eventually(self.lead.stop_hook, what="the lead to hear it is done")
        self.assertIn("download page live", heard)
        self.assertIn("%s is free" % self.dev.name, heard)
        with self.assertRaises(ToolError):
            self.dev.call("hub_assign", to=self.lead.name, text="not the lead")
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
                    results[agent.name] = agent.call("hub_take", id=tid)
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
        self.dev.call("hub_take", id=tid)
        self.dev.call("hub_update", id=tid, status="todo", note="giving it back")
        eventually(lambda: tid not in self.mac.owner.poll()["taken"], what="the give-back on the Mac")
        self.lead.call("hub_take", id=tid)  # was refused before: the first claim never released
        for m in (self.mac, self.win):
            eventually(lambda: m.owner.poll()["taken"].get(tid) == self.lead.name, what="both to agree")
        # The owner puts it back too, and it can be taken afresh, from the other machine.
        self.win.owner.post("/api/task", {"op": "status", "task": tid, "status": "todo"})
        eventually(lambda: tid not in self.win.owner.poll()["taken"], what="the owner's give-back")
        eventually(lambda: tid not in self.mac.owner.poll()["taken"], what="the owner's give-back on the Mac")
        self.dev.call("hub_take", id=tid)
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


class Outages(unittest.TestCase):
    """A machine going down, coming back, and leaving."""

    def setUp(self):
        self.a, self.b = H.Machine("desk"), H.Machine("laptop")
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
        tid = self.a.owner.task("all", "a task from the desk")
        eventually(lambda: tid in self.y.call("hub_history"), what="the task to reach the laptop")
        self.a.kill(hard=True)
        with self.assertRaises(ToolError) as e:
            self.y.call("hub_take", id=tid)
        self.assertIn("Try again when it is back", str(e.exception))
        self.a.cli("start", "--no-service", "--no-open")
        self.a.wait_up()
        self.y.call("hub_take", id=tid)
        eventually(lambda: self.a.owner.poll()["taken"].get(tid) == self.y.name, what="the take to travel")

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
        a, b, c = H.Machine("one"), H.Machine("two"), H.Machine("three")
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


if __name__ == "__main__":
    unittest.main()
