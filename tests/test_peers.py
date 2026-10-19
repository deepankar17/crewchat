"""Linking machines over a private network: three real servers in one process, talking HTTP."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
import urllib.request
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = tempfile.mkdtemp(prefix="crewchat-peers-")
os.environ.setdefault("CREWCHAT_HOME", str(Path(TMP) / "unused-home"))

import crewchat  # noqa: E402
import crewchat_peers as peers  # noqa: E402

crewchat.Handler.log_message = lambda *args: None
crewchat.user_home = lambda: Path(TMP) / "user"  # never the real ~/.claude.json or ~/.cursor
os.environ.pop("CLAUDE_CONFIG_DIR", None)
peers.PULL_WAIT = 2
peers.BACKOFF = (0.2, 0.3, 0.5)
peers.log = lambda text: None


def eventually(check, timeout=10.0, what="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError("timed out waiting for " + what)


class Machine:
    count = 0

    def __init__(self, name, port=0, root=None):
        Machine.count += 1
        self.name = name
        self.root = root or Path(TMP) / ("m%d-%s" % (Machine.count, name))
        if root is None:
            self.root.mkdir(parents=True)
            # Agents named after tool and machine (claude-alpha): linked machines that each pick the same first
            # name at once settle the clash with a rename, which these tests should not have to wait for.
            crewchat.save_config({"project": "Demo", "port": 1, "url": "", "forget_hours": 24,
                                  "agent_names": "place"}, self.root)
            crewchat.ensure_token(crewchat.OWNER, self.root)
        self.server = crewchat.make_server(port, self.root)
        self.server.RequestHandlerClass.log_message = lambda *args: None
        self.hub = self.server.RequestHandlerClass.hub
        self.port = self.server.server_address[1]
        self.url = "http://127.0.0.1:%d" % self.port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.sync = None

    def found(self):
        peers.Mesh(self.root).found(self.name, self.url)
        self.sync = peers.start(self.hub, self.root)
        return self

    def invite(self):
        return peers.admin(self.hub, self.root, "peer-invite", {})["code"]

    def join(self, other):
        code = other.invite()
        out = post(other.url + "/peer/join", {"code": code, "device": {"name": self.name, "url": self.url}})
        you = out["you"]
        peers.Mesh(self.root).adopt(out, {"id": you["id"], "name": you["name"], "tag": you["tag"], "url": self.url})
        self.sync = peers.start(self.hub, self.root)
        return self

    def agent(self, client="claude"):
        sid = self.hub.open_session(self.name, client)
        return self.hub.agent_for(sid)

    def say(self, to, text, kind="msg"):
        return self.hub.send(crewchat.OWNER, to, text, kind)

    def has(self, text):
        with self.hub.lock:
            return next((m for m in self.hub.messages if m["text"] == text), None)

    def sees(self, agent):
        return any(r["agent"] == agent and r["online"] for r in self.hub.rows())

    def stop(self):
        if self.sync:
            self.sync.stop()
        self.server.shutdown()
        self.server.server_close()


def post(url, body, token=None, device=""):
    headers = {"Content-Type": "application/json", "X-Crewchat-Device": device}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as answer:
        return json.loads(answer.read().decode())


def status_of(url, body, token=None, device=""):
    try:
        post(url, body, token, device)
        return 200
    except urllib.error.HTTPError as e:
        return e.code


class Linked(unittest.TestCase):
    """Three machines: alpha founds, beta joins through alpha, gamma joins through beta."""

    @classmethod
    def setUpClass(cls):
        cls.a = Machine("alpha").found()
        cls.b = Machine("beta").join(cls.a)
        cls.c = Machine("gamma").join(cls.b)
        cls.machines = [cls.a, cls.b, cls.c]

    @classmethod
    def tearDownClass(cls):
        for m in cls.machines:
            m.stop()

    def test_everyone_learns_about_everyone(self):
        self.assertEqual([m.hub.prefix for m in self.machines], ["A", "B", "C"])
        for m in self.machines:
            eventually(lambda: len(peers.Mesh(m.root).members()) == 2, what="%s to know both others" % m.name)
        agent = self.c.agent()
        for m in (self.a, self.b):
            eventually(lambda: m.sees(agent), what="%s to see %s" % (m.name, agent))
        row = next(r for r in self.a.hub.rows() if r["agent"] == agent)
        self.assertEqual((row["remote"], row["device"]), (True, "gamma"))

    def test_messages_reach_every_machine_once(self):
        to = self.c.agent()
        eventually(lambda: self.a.sees(to), what="alpha to see gamma's agent")
        sender = self.a.agent()
        msg = self.a.hub.send(sender, to, "hello across machines")
        self.assertTrue(msg["id"].startswith("A"))
        got = eventually(lambda: self.c.has("hello across machines"), what="gamma to get it")
        self.assertEqual(got["id"], msg["id"])
        unread = self.c.hub.inbox(to, 0, True)
        self.assertIn("hello across machines", [m["text"] for m in unread])
        eventually(lambda: self.b.has("hello across machines"), what="beta to get it")
        time.sleep(0.5)
        for m in self.machines:
            with m.hub.lock:
                self.assertEqual(sum(x["text"] == "hello across machines" for x in m.hub.messages), 1, m.name)

    def test_one_agent_takes_a_task_across_machines(self):
        agents = [(m, m.agent()) for m in self.machines]
        task = self.b.say("all", "race for this", "task")
        for m in self.machines:
            eventually(lambda: m.has("race for this"), what="%s to get the task" % m.name)
        results = {}

        def take(machine, agent):
            try:
                machine.hub.take(agent, task["id"])
                results[agent] = "won"
            except crewchat.HubError as e:
                results[agent] = str(e)
        threads = [threading.Thread(target=take, args=pair) for pair in agents]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        winners = [a for a, r in results.items() if r == "won"]
        self.assertEqual(len(winners), 1, results)
        for m in self.machines:
            eventually(lambda: m.hub.taken.get(task["id"]) == winners[0], what="%s to know the taker" % m.name)

    def test_the_machine_that_posted_a_task_decides_its_claim_key(self):
        x, y, z = self.a.agent(), self.a.agent(), self.c.agent()
        task = self.b.say("all", "handed around", "task")
        for m in self.machines:
            eventually(lambda: m.has("handed around"), what="%s to get the task" % m.name)
        tid = task["id"]
        self.a.hub.take(x, tid)
        self.a.hub.update(x, tid, "todo", "giving it back")
        for m in self.machines:
            eventually(lambda: m.hub.claim_gen.get(tid) == 1, what="%s to count the give-back" % m.name)
        self.a.hub.take(y, tid)
        # gamma lags (it joined later, or lost old messages): it counts no give-back.
        with self.c.hub.lock:
            self.c.hub.claim_gen.pop(tid, None)
            self.c.hub.taken.pop(tid, None)
        with self.assertRaises(crewchat.HubError) as e:
            self.c.hub.take(z, tid)
        self.assertIn("already taken by %s" % y, str(e.exception))  # the live holder, not the first one

    def test_activity_travels(self):
        agent = self.a.agent()
        self.a.hub.touch(agent, active=True)
        eventually(lambda: any(r["agent"] == agent and r["activity"] == "working" for r in self.c.hub.rows()),
                   what="gamma to see the agent working")

    def test_roles_and_progress_cross_machines(self):
        lead, dev = self.a.agent(), self.c.agent()
        eventually(lambda: self.a.sees(dev) and self.c.sees(lead), what="rosters")
        self.a.hub.set_role(crewchat.OWNER, lead, "lead")
        self.a.hub.set_role(crewchat.OWNER, dev, "developer")
        eventually(lambda: self.c.hub.agents[dev]["role"] == "developer", what="the role to arrive")
        eventually(lambda: any(r["agent"] == lead and r["role"] == "lead" for r in self.c.hub.rows()),
                   what="gamma to see the lead")
        piece = self.a.hub.assign(lead, dev, "Build it")
        eventually(lambda: self.c.hub.taken.get(piece["id"]) == dev, what="the assignment")
        _, to = self.c.hub.update(dev, piece["id"], "review", "Ready to test")
        self.assertEqual(to, lead)
        eventually(lambda: self.a.hub.progress.get(piece["id"], {}).get("status") == "review", what="the update")
        self.a.hub.set_role(crewchat.OWNER, lead, "none")

    def test_files_are_fetched_from_the_machine_they_were_shared_on(self):
        meta = self.a.hub.add_file("shot.png", b"PNGDATA", "image/png")
        self.a.hub.send(crewchat.OWNER, "all", "", files=[meta])
        eventually(lambda: self.c.has(""), what="the message to arrive")
        got, path = self.c.hub.open_file(meta["id"])
        self.assertEqual((got["name"], path.read_bytes()), ("shot.png", b"PNGDATA"))
        # A member never relays a file it did not share itself.
        mesh = peers.Mesh(self.c.root)
        self.assertEqual(status_of(self.c.url + "/peer/file", {"id": meta["id"]}, mesh.data["secret"],
                                   mesh.me["id"]), 404)

    def test_the_key_is_required(self):
        self.assertEqual(status_of(self.a.url + "/peer/pull", {"wait": 0}, token="wrong"), 401)
        self.assertEqual(status_of(self.a.url + "/peer/pull", {"wait": 0}), 401)

    def test_a_join_code_works_once(self):
        code = self.a.invite()
        body = {"code": code, "device": {"name": "x", "url": "http://127.0.0.1:9"}}
        post(self.a.url + "/peer/join", body)
        self.assertEqual(status_of(self.a.url + "/peer/join", body), 401)
        # Leave things as they were: drop the machine that never really joined.
        mid = peers.Mesh(self.a.root).find("x")
        peers.admin(self.a.hub, self.a.root, "peer-remove", {"id": mid})


class Projects(unittest.TestCase):
    """Projects besides the first are shared by name: t2 on alpha and t2 on beta are one chat."""

    @classmethod
    def setUpClass(cls):
        cls.a = Machine("alpha").found()
        cls.b = Machine("beta").join(cls.a)
        for m, names in ((cls.a, ("t2", "solo")), (cls.b, ("t2", "other"))):
            for name in names:
                crewchat.create_project(name, m.root)
            m.projects = m.server.RequestHandlerClass.projects
            m.projects.refresh()
            crewchat.share_projects(m.projects)
            m.projects.on_change = crewchat.share_projects

    @classmethod
    def tearDownClass(cls):
        for m in (cls.a, cls.b):
            m.stop()

    def hub(self, machine, name):
        return machine.projects.get(name).hub

    def test_a_project_with_the_same_name_is_one_chat(self):
        a2, b2 = self.hub(self.a, "t2"), self.hub(self.b, "t2")
        writer = a2.agent_for(a2.open_session("alpha", "claude"))
        reader = b2.agent_for(b2.open_session("beta", "claude"))
        eventually(lambda: any(r["agent"] == writer for r in b2.rows()), what="beta's t2 to see alpha's agent")
        msg = a2.send(writer, "all", "hello t2")
        self.assertTrue(msg["id"].startswith("A"))
        eventually(lambda: any(m["text"] == "hello t2" for m in b2.messages), what="beta's t2 to get it")
        self.assertIn("hello t2", [m["text"] for m in b2.inbox(reader, 0, True)])
        # Nothing crosses into another project, either way.
        self.a.say("all", "first project only")
        eventually(lambda: any(m["text"] == "first project only" for m in self.b.hub.messages), what="the first projects")
        time.sleep(0.5)
        self.assertFalse(any(m["text"] == "first project only" for m in b2.messages))
        self.assertFalse(any(m["text"] == "hello t2" for m in self.b.hub.messages))

    def test_one_task_one_taker_in_a_shared_project(self):
        a2, b2 = self.hub(self.a, "t2"), self.hub(self.b, "t2")
        x = a2.agent_for(a2.open_session("alpha", "claude"))
        y = b2.agent_for(b2.open_session("beta", "claude"))
        task = a2.add_row(crewchat.OWNER, "one owner for this")
        eventually(lambda: task["id"] in b2.sheet, what="the task on beta's sheet")
        results = {}

        def take(hub, agent):
            try:
                hub.take(agent, task["id"])
                results[agent] = "won"
            except crewchat.HubError as e:
                results[agent] = str(e)
        threads = [threading.Thread(target=take, args=pair) for pair in ((a2, x), (b2, y))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        winners = [k for k, v in results.items() if v == "won"]
        self.assertEqual(len(winners), 1, results)
        for hub in (a2, b2):
            eventually(lambda: hub.taken.get(task["id"]) == winners[0], what="both to agree")
        # Claims are kept per project: the same id in the first project is a separate task.
        self.assertNotIn(task["id"], self.a.hub.taken)

    def test_a_project_only_one_machine_has_stays_there_and_is_not_marked_shared(self):
        solo = self.hub(self.a, "solo")
        solo.send(crewchat.OWNER, "all", "only on alpha")
        time.sleep(1)
        self.assertFalse(solo.sync.shared)
        self.assertTrue(self.hub(self.a, "t2").sync.shared or eventually(lambda: self.hub(self.a, "t2").sync.shared,
                                                                        what="t2 shared"))
        for name in ("t2", "other"):
            self.assertFalse(any(m["text"] == "only on alpha" for m in self.hub(self.b, name).messages))

    def test_the_chat_page_shows_the_other_machine_reachable(self):
        eventually(lambda: [m["online"] for m in crewchat.link_info(self.a.hub)["machines"]] == [True],
                   what="the chat page to show beta reachable")
        self.assertEqual(crewchat.link_info(self.a.hub)["via"], "tailscale")

    def test_a_project_made_later_is_linked_at_once(self):
        for m in (self.a, self.b):
            crewchat.create_project("late", m.root)
            m.projects.all()  # the server notices the new folder on its next request
        a3, b3 = self.hub(self.a, "late"), self.hub(self.b, "late")
        a3.send(crewchat.OWNER, "all", "late but shared")
        eventually(lambda: any(m["text"] == "late but shared" for m in b3.messages), what="the late project shared")

    def test_one_link_per_project_however_many_ask_at_once(self):
        entry = crewchat.create_project("busy", self.a.root)
        link = self.a.hub.sync
        hub = crewchat.Projects(self.a.root).get(entry["id"]).hub  # a hub not linked yet
        started = []
        real_start = peers.PeerSync.start

        def counted(sync):
            started.append(sync)
            time.sleep(0.05)  # widen the window two callers could slip through
            return real_start(sync)
        peers.PeerSync.start = counted
        try:
            threads = [threading.Thread(target=link.share, args=(hub, "busy")) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            peers.PeerSync.start = real_start
        self.assertEqual(len(started), 1, "one link started, not %d" % len(started))
        self.assertIs(link.projects["busy"], hub.sync)
        self.assertEqual(len(link.projects["busy"].threads), len(peers.Mesh(self.a.root).members()))
        # Unsharing lets it go entirely: it can be linked again (as after a rename).
        link.unshare("busy")
        self.assertIsNone(hub.sync)
        self.assertIsNotNone(link.share(hub, "busy-renamed"))
        link.unshare("busy-renamed")

    def test_an_older_crewchat_that_knows_no_projects_is_not_mixed_in(self):
        b_other = self.hub(self.b, "other")
        crewchat.create_project("other", self.a.root)
        self.a.projects.all()
        a_other = self.hub(self.a, "other")
        # beta answers like an older crewchat: with its first project, whatever is asked.
        sync = b_other.sync
        saved = sync._pull
        sync._pull = lambda body: {k: v for k, v in saved(body).items() if k != "chat"}
        try:
            b_other.send(crewchat.OWNER, "all", "from an old machine")
            time.sleep(1.5)
            self.assertFalse(any(m["text"] == "from an old machine" for m in a_other.messages))
        finally:
            sync._pull = saved


class SharingEdges(unittest.TestCase):
    """What sharing projects must get right at its edges (review of 0.11.0)."""

    def setUp(self):
        self.a = Machine("alpha").found()
        self.b = Machine("beta").join(self.a)
        self.machines = [self.a, self.b]
        for m in self.machines:
            m.projects = m.server.RequestHandlerClass.projects

    def tearDown(self):
        for m in self.machines:
            m.stop()

    def follow(self, machine):
        machine.projects.refresh()
        crewchat.share_projects(machine.projects)
        machine.projects.on_change = crewchat.share_projects

    def test_open_tasks_from_before_the_link_are_shared_as_history(self):
        for m in self.machines:
            crewchat.create_project("prelink", m.root)
            m.projects.refresh()  # not linked yet: nothing follows changes so far
        a2, b2 = self.a.projects.get("prelink").hub, self.b.projects.get("prelink").hub
        x = a2.agent_for(a2.open_session("alpha", "claude"))
        old = a2.add_row(crewchat.OWNER, "written before the link")["id"]
        done = a2.add_row(crewchat.OWNER, "finished before the link")["id"]
        a2.update(crewchat.OWNER, done, "done", "")
        a2.send(crewchat.OWNER, "all", "chatter before the link")
        self.assertFalse(old.startswith("A"))
        for m in self.machines:
            self.follow(m)
        new = "A" + old
        y = b2.agent_for(b2.open_session("beta", "claude"))
        eventually(lambda: new in b2.sheet, what="the open task on beta's sheet")
        self.assertNotIn("A" + done, b2.sheet)  # a finished one stays behind
        self.assertFalse(any(m["text"] == "chatter before the link" for m in b2.messages))
        # History: on the sheet and the page, but no one is woken for it.
        self.assertFalse(any(m["id"] == new for m in b2.inbox(y, 0, True)))
        self.assertFalse(any(m["id"] == new for m in a2.inbox(x, 0, True)))
        self.assertTrue(any("now shared" in m["text"] for m in a2.messages if m["kind"] == "event"))
        # The old number still works where it was written, and the take holds on both machines.
        a2.take(x, old)
        eventually(lambda: b2.taken.get(new) == x, what="beta to see who took it")
        with self.assertRaises(crewchat.HubError):
            b2.take(y, new)
        # It survives a restart: the log on disk carries the new number at its new place.
        again = crewchat.Hub(a2.roster)
        self.assertIn(new, again.sheet)
        self.assertEqual(again._id(old), new)
        self.assertEqual(again.messages[-1]["seq"] < again.next_seq, True)
        # A crash between saving the state and rewriting the log: the old log is put right on load.
        log = a2.home / "messages.jsonl"
        lines = log.read_text(encoding="utf-8").splitlines()
        stale = [json.loads(line) for line in lines]
        for msg in stale:
            if msg["id"] == new:
                msg["id"] = old
                msg.pop("origin", None)
                msg.pop("history", None)
        log.write_text("".join(json.dumps(m) + "\n" for m in stale), encoding="utf-8")
        recovered = crewchat.Hub(a2.roster)
        row = next(m for m in recovered.messages if m["id"] == new)
        self.assertEqual((row["origin"], row["history"]), (a2.sync.device_id, "1"))
        self.assertNotIn(old, recovered.by_id)

    def test_a_machine_that_joins_later_is_followed_by_every_shared_project(self):
        for m in self.machines:
            crewchat.create_project("t9", m.root)
            self.follow(m)
        c = Machine("gamma").join(self.a)  # alpha learns of gamma through its join request
        self.machines.append(c)
        c.projects = c.server.RequestHandlerClass.projects
        crewchat.create_project("t9", c.root)
        self.follow(c)
        c9, a9 = c.projects.get("t9").hub, self.a.projects.get("t9").hub
        c9.send(crewchat.OWNER, "all", "from gamma's t9")
        eventually(lambda: any(m["text"] == "from gamma's t9" for m in a9.messages), timeout=15,
                   what="alpha's t9 to pull from gamma")

    def test_a_link_let_go_does_not_bring_back_the_other_machines_agents(self):
        crewchat.create_project("ghostly", self.a.root)
        self.follow(self.a)
        link = self.a.hub.sync
        sync = link.projects["ghostly"]
        hub = sync.hub
        link.unshare("ghostly")
        self.assertIsNone(hub.sync)
        mid = next(iter(self.a.hub.sync.mesh.members()))
        member = self.a.hub.sync.mesh.members()[mid]
        sync._apply(mid, member, {"messages": [], "agents": [{"agent": "claude-beta", "client": "claude",
                                                               "seen": time.time()}]})
        self.assertFalse(hub.remote)

    def test_share_projects_acts_on_the_projects_as_they_are_when_it_runs(self):
        class Fake:
            pass

        class FakeLink:
            def __init__(self):
                self.projects = {}

            def share(self, hub, name):
                if name == "x":
                    time.sleep(0.3)  # the other run starts meanwhile
                self.projects[name] = hub

            def unshare(self, name):
                self.projects.pop(name, None)

        projects = Fake()
        projects.primary = Fake()
        projects.primary.name = "first"
        projects.primary.hub = Fake()
        projects.primary.hub.sync = link = FakeLink()

        def project(name):
            one = Fake()
            one.name, one.hub = name, Fake()
            return one
        projects.others = {"x": project("x")}
        first = threading.Thread(target=crewchat.share_projects, args=(projects,))
        first.start()
        time.sleep(0.1)
        projects.others["y"] = project("y")  # a second project, and a second run for it
        second = threading.Thread(target=crewchat.share_projects, args=(projects,))
        second.start()
        first.join()
        second.join()
        self.assertEqual(sorted(link.projects), ["x", "y"])

    def test_a_new_project_is_found_even_if_the_folder_time_does_not_move(self):
        root = Path(TMP) / "coarse-clock"
        root.mkdir()
        crewchat.save_config({"project": "Demo", "port": 1, "url": "", "forget_hours": 24}, root)
        crewchat.create_project("one", root)
        found = crewchat.Projects(root)
        folder = root / crewchat.PROJECTS_DIR
        before = folder.stat().st_mtime_ns
        crewchat.create_project("two", root)
        os.utime(str(folder), ns=(before, before))  # as a one-second clock would leave it
        self.assertIsNotNone(found.get("two"))


class OfflineAndRemoval(unittest.TestCase):
    def test_a_machine_that_was_off_catches_up(self):
        a = Machine("one").found()
        b = Machine("two").join(a)
        try:
            agent = b.agent()
            eventually(lambda: a.sees(agent), what="one to see two's agent")
            port, root = b.port, b.root
            b.stop()
            a.say("all", "while you were away")
            eventually(lambda: not a.sees(agent), timeout=15, what="one to see two go offline")
            b = Machine("two", port=port, root=root)
            b.sync = peers.start(b.hub, b.root)
            eventually(lambda: b.has("while you were away"), what="two to catch up")
            eventually(lambda: a.sees(agent), what="one to see two back")
        finally:
            a.stop()
            b.stop()

    def test_a_machine_off_for_days_gets_every_message_from_whoever_is_on(self):
        # Roadmap Y3, done when: a machine that was off gets every message once it is back, in order, from the
        # machines that were on: more than they hold in memory, and those of a machine that is off by then too.
        def logged(m):
            return [json.loads(line)["text"] for line in (m.hub.home / "messages.jsonl").read_text().splitlines()]
        with mock.patch.object(crewchat, "KEEP_MESSAGES", 20):
            a = Machine("first").found()
            b = Machine("second").join(a)
            c = Machine("third").join(b)
            machines = [a, b, c]
            try:
                for m in machines:
                    eventually(lambda: len(peers.Mesh(m.root).members()) == 2, what="%s to know both others" % m.name)
                a.say("all", "first: hello")
                c.say("all", "third: hello")
                eventually(lambda: b.has("first: hello") and b.has("third: hello"), what="second to be in step")
                port, root = b.port, b.root
                b.stop()  # second is off for days
                firsts = ["first %d" % i for i in range(45)]  # more than first keeps in memory
                thirds = ["third %d" % i for i in range(30)]
                for text in firsts:
                    a.say("all", text)
                for text in thirds:
                    c.say("all", text)
                eventually(lambda: "third 29" in logged(a), what="first to have third's messages")
                c.stop()  # and third is off by the time second is back: first has its messages
                b = machines[1] = Machine("second", port=port, root=root)
                b.sync = peers.start(b.hub, b.root)
                eventually(lambda: set(firsts + thirds) <= set(logged(b)), timeout=60, what="second to catch up")
                got = logged(b)
                self.assertEqual([t for t in got if t.startswith("first ")], firsts)  # all, in order
                self.assertEqual([t for t in got if t.startswith("third ")], thirds)
                self.assertEqual(len(got), len(set(got)))  # each once
            finally:
                for m in machines:
                    m.stop()

    def test_a_burst_bigger_than_memory_reaches_the_others_whole(self):
        # More messages than memory holds, written while another machine's pull waits: the first of them are in
        # the log only by the time the pull answers. They used to be skipped; they come from the log now (Y3).
        def logged(m):
            return [json.loads(line)["text"] for line in (m.hub.home / "messages.jsonl").read_text().splitlines()]
        with mock.patch.object(crewchat, "KEEP_MESSAGES", 20):
            a = Machine("quiet").found()
            b = Machine("busy").join(a)
            try:
                b.say("all", "busy: hello")
                eventually(lambda: a.has("busy: hello"), what="quiet to be in step")
                burst = ["burst %d" % i for i in range(60)]
                for text in burst:
                    b.say("all", text)
                eventually(lambda: "burst 59" in logged(a), what="the burst to arrive")
                self.assertEqual([t for t in logged(a) if t.startswith("burst ")], burst)
            finally:
                a.stop()
                b.stop()

    def test_the_log_is_read_from_the_point_asked(self):
        # Y3: a catch-up's page is found by halving messages.jsonl, past damaged lines, however long the log.
        m = Machine("logger")
        try:
            with mock.patch.object(crewchat, "KEEP_MESSAGES", 5):
                sent = [m.say("all", "line %d %s" % (i, "x" * 200)) for i in range(400)]
            log = m.hub.home / "messages.jsonl"
            with open(log, "ab") as f:
                f.write(b'{"half written')  # a crash mid-line, at the end
            seqs = [msg["seq"] for msg in sent]
            for after in (0, seqs[0], seqs[137], seqs[-2], seqs[-1]):
                self.assertEqual([x["seq"] for x in crewchat.log_from(log, after)], [s for s in seqs if s > after], after)
            self.assertEqual(list(crewchat.log_from(m.root / "no-such-log", 0)), [])
            m.hub.messages[:] = m.hub.messages[-5:]  # memory holds the last five: the rest comes from the log
            page, more = m.hub.messages_after(m.hub.messages[0].get("origin"), seqs[100], 50)
            self.assertEqual(([x["seq"] for x in page], more), (seqs[101:151], True))
        finally:
            m.stop()

    def test_a_long_relay_reads_the_log_about_once(self):
        # Y3: /peer/relay pages through another machine's messages for a member away a long time. Each page read
        # the log back from its end to the point asked: a catch-up of n messages read about n^2 / page lines.
        m = Machine("relayer")
        try:
            log, n, page = m.hub.home / "messages.jsonl", 6000, 100
            with open(log, "w") as f:
                seq = 0
                for i in range(1, n + 1):
                    number = i + 1 if i % 50 == 1 else i - 1 if i % 50 == 2 else i  # a few arrived out of order
                    for origin, mid in (("other", "B%d" % number), ("third", "C%d" % i)):
                        seq += 1
                        f.write(json.dumps({"id": mid, "seq": seq, "origin": origin, "from": "x", "to": "all",
                                            "ts": 1e9 + seq, "text": "m"}) + "\n")
            m.hub.messages[:] = [dict(seq=seq, id="Z1", origin="z")]  # memory does not hold them
            reads, real = [0], crewchat.log_message

            def counting(line):
                reads[0] += 1
                return real(line)
            got, since, more = [], 10, True
            with mock.patch.object(crewchat, "log_message", counting):
                while more:
                    out, more = m.hub.messages_after("other", since, page, by_id=True)
                    got += [crewchat.origin_number(x["id"]) for x in out]
                    since = got[-1]
            self.assertEqual(got, list(range(11, n + 1)))
            self.assertLess(reads[0], 5 * 2 * n)  # the log has 2n lines; reading back each page, over 25 times that
        finally:
            m.stop()

    def test_one_bad_message_does_not_stop_the_link(self):
        a = Machine("sender").found()
        b = Machine("receiver").join(a)
        ingest = b.hub.ingest

        def picky(msg):
            if msg.get("text") == "poison":
                raise ValueError("cannot store this one")
            return ingest(msg)

        b.hub.ingest = picky
        try:
            a.say("all", "poison")
            a.say("all", "after the bad one")
            eventually(lambda: b.has("after the bad one"), what="the next message to arrive anyway")
            self.assertIsNone(b.has("poison"))
            a.say("all", "and the one after")
            eventually(lambda: b.has("and the one after"), what="the link to keep going")
        finally:
            a.stop()
            b.stop()

    def test_whichever_machine_is_on_coordinates(self):
        # Roadmap Y1: the preferred machine when reachable, else the reachable one with the lowest tag.
        a = Machine("first").found()
        b = Machine("second").join(a)
        c = Machine("third").join(b)
        machines = [a, b, c]
        try:
            who = lambda m: m.sync.coordinator()["name"]  # noqa: E731
            eventually(lambda: all(who(m) == "first" for m in machines), timeout=30, what="the lowest tag to lead")
            self.assertTrue(a.sync.coordinator()["me"])
            self.assertFalse(b.sync.coordinator()["me"])
            # The owner prefers the second machine (on the third): every machine hears of it with its pulls.
            self.assertEqual(c.sync.set_preferred("second")["preferred"], "second")
            eventually(lambda: all(who(m) == "second" for m in machines), timeout=30, what="the preferred machine")
            with self.assertRaises(peers.PeerError):
                a.sync.set_preferred("nowhere")
            # It goes off: the others fall back to the lowest tag they can reach, and the term goes up.
            term = a.sync.coordinator()["term"]
            port, root = b.port, b.root
            b.stop()
            eventually(lambda: who(a) == "first" and who(c) == "first", timeout=60, what="a fallback without it")
            self.assertGreater(a.sync.coordinator()["term"], term)
            # Back on, it takes the role back.
            b = machines[1] = Machine("second", port, root)
            b.sync = peers.start(b.hub, root)
            eventually(lambda: all(who(m) == "second" for m in machines), timeout=90, what="the preferred one back")
            a.sync.set_preferred("none")
            eventually(lambda: all(who(m) == "first" for m in machines), timeout=30, what="no preference again")
            info = crewchat.link_info(a.hub)
            self.assertEqual(info["coordinator"]["name"], "first")
        finally:
            for m in machines:
                m.stop()

    def test_a_task_from_a_machine_that_is_off_is_settled_by_the_coordinator(self):
        # Roadmap Y2: with the posting machine off, the coordinator settles claims; exactly one taker.
        a = Machine("poster").found()
        b = Machine("second").join(a)
        c = Machine("third").join(b)
        machines = [b, c]
        try:
            task = a.say("all", "posted, then switched off", "task")
            eventually(lambda: b.has("posted, then switched off") and c.has("posted, then switched off"),
                       what="the task everywhere")
            port, root = a.port, a.root
            a.stop()
            eventually(lambda: not b.sync.reachable(a.sync.device_id) and not c.sync.reachable(a.sync.device_id),
                       timeout=30, what="the poster to be seen off")
            self.assertEqual(b.sync.coordinator(exclude={a.sync.device_id})["name"], "second")
            agents = [b.agent(), c.agent()]
            results = {}

            def take(machine, agent):
                try:
                    machine.hub.take(agent, task["id"])
                    results[agent] = "took"
                except crewchat.HubError as e:
                    results[agent] = str(e)
            threads = [threading.Thread(target=take, args=(m, ag)) for m, ag in zip(machines, agents)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
            took = [ag for ag, r in results.items() if r == "took"]
            self.assertEqual(len(took), 1, results)  # exactly one taker, and no "cannot reach" for the other
            self.assertNotIn("cannot reach", " ".join(results.values()))
            winner = took[0]
            # The poster comes back and adopts what the coordinator decided.
            a = Machine("poster", port, root)
            a.sync = peers.start(a.hub, root)
            machines.append(a)
            eventually(lambda: a.sync.mesh.data.get("claims", {}).get(task["id"]) == winner, timeout=60,
                       what="the poster to adopt the decision")
            self.assertEqual(a.sync.mesh.claim(task["id"], "someone-else"), winner)  # settled: stays settled
        finally:
            for m in machines:
                m.stop()

    def test_after_a_split_the_earlier_claim_wins(self):
        root = Path(tempfile.mkdtemp(prefix="split-", dir=TMP))
        mesh = peers.Mesh(root)
        mesh.found("here", "http://127.0.0.1:1")
        self.assertEqual(mesh.claim("12", "maya"), "maya")
        clock = mesh.decisions()["12"]["clock"]
        # Another machine, cut off from this one, gave #12 to teo: earlier on its clock, so teo holds it now.
        changed = mesh.merge_decisions({"12": {"agent": "teo", "clock": clock - 1, "tag": "B", "origin": "x"}})
        self.assertEqual(changed, [("12", "teo", "maya")])
        self.assertEqual(mesh.claim("12", "anyone"), "teo")
        # A later decision for someone else loses, and changes nothing here.
        self.assertEqual(mesh.merge_decisions({"12": {"agent": "wren", "clock": clock + 5, "tag": "C"}}), [])
        # Equal clocks: the lower tag wins. One decided only there is adopted.
        mesh.claim("13", "maya")
        same = mesh.decisions()["13"]["clock"]
        self.assertEqual(mesh.merge_decisions({"13": {"agent": "teo", "clock": same, "tag": "A"},
                                               "14": {"agent": "wren", "clock": 1, "tag": "C"}}), [])
        self.assertEqual((mesh.claim("13", "x"), mesh.claim("14", "x")), ("maya", "wren"))
        # Three machines: #14 was decided on C (adopted here), and B's earlier decision overturns it. This machine
        # switches too, but C, which made the decision, is the one that says so: the news is given once.
        self.assertEqual(mesh.merge_decisions({"14": {"agent": "teo", "clock": 0, "tag": "B"}}), [])
        self.assertEqual(mesh.claim("14", "x"), "teo")
        mesh.claim("17", "maya")
        self.assertGreater(mesh.decisions()["17"]["clock"], clock + 5)  # Lamport: after every clock it has seen
        self.assertEqual(mesh.merge_decisions({"15": {"agent": "not a name!", "clock": 1, "tag": "C"}, "16": "x"}), [])

    def test_peers_json_is_read_again_while_windows_holds_it(self):
        root = Path(tempfile.mkdtemp(prefix="held-", dir=TMP))
        peers.Mesh(root).found("here", "http://127.0.0.1:1")
        real, calls = Path.read_text, []

        def held_once(path, *a, **kw):
            calls.append(path)
            if len(calls) == 1:
                raise PermissionError(13, "Permission denied")
            return real(path, *a, **kw)
        with mock.patch.object(Path, "read_text", held_once), mock.patch.object(peers.sys, "platform", "win32"), \
                mock.patch.object(peers.time, "sleep"):
            self.assertEqual(peers.Mesh(root).me["name"], "here")
        self.assertEqual(len(calls), 2)
        # And a pull that brings nothing new does not rewrite it.
        mesh = peers.Mesh(root)
        mesh.claim("1", "maya")
        with mock.patch.object(mesh, "save") as save:
            mesh.merge_decisions(mesh.decisions())
            save.assert_not_called()

    def test_the_agent_whose_claim_lost_is_told(self):
        a = Machine("teller").found()
        try:
            loser = a.agent()
            task = a.say("all", "split task", "task")
            a.hub.take(loser, task["id"])
            a.sync._settle({task["id"]: {"agent": "claude-elsewhere", "clock": 0, "tag": "B", "origin": a.sync.device_id}})
            self.assertEqual(a.hub.taken[task["id"]], "claude-elsewhere")
            told = [m for m in a.hub.messages if m["to"] == loser and m["from"] == crewchat.SERVER_NAME]
            self.assertIn("Stop work on it", told[-1]["text"])
            settled = [m for m in a.hub.messages if m.get("settled")]
            self.assertEqual((settled[-1]["kind"], settled[-1]["from"]), ("take", "claude-elsewhere"))
        finally:
            a.stop()

    def test_removing_a_machine_shuts_it_out(self):
        a = Machine("keep").found()
        b = Machine("stay").join(a)
        c = Machine("go").join(a)
        try:
            agent = c.agent()
            eventually(lambda: b.sees(agent), what="stay to see go's agent")
            mesh_c = peers.Mesh(c.root)
            old_key = mesh_c.data["secret"]
            out = peers.admin(a.hub, a.root, "peer-remove", {"id": peers.Mesh(a.root).find("go")})
            self.assertEqual(out["told"], ["stay"])
            eventually(lambda: not b.sees(agent), what="stay to drop go's agent")
            self.assertNotIn(mesh_c.me["id"], peers.Mesh(b.root).members())
            self.assertNotEqual(peers.Mesh(b.root).data["secret"], old_key)
            # go is refused by device, and its old key stops working after the grace period.
            self.assertEqual(status_of(b.url + "/peer/pull", {"wait": 0}, old_key, mesh_c.me["id"]), 403)
            # go notices it was removed and stops syncing; its own chat keeps working.
            eventually(lambda: c.hub.sync is None, what="go to notice it was removed")
            self.assertEqual(c.hub.rows() and [r for r in c.hub.rows() if r["remote"]], [])
            mesh_b = peers.Mesh(b.root)
            mesh_b.data["old_until"] = 0
            self.assertFalse(mesh_b.key_ok(old_key))
        finally:
            for m in (a, b, c):
                m.stop()

    def test_leaving(self):
        a = Machine("host").found()
        b = Machine("leaver").join(a)
        try:
            agent = b.agent()
            eventually(lambda: a.sees(agent), what="host to see leaver's agent")
            out = peers.admin(b.hub, b.root, "peer-leave", {})
            self.assertEqual(out["told"], ["host"])
            self.assertIsNone(b.hub.sync)
            eventually(lambda: not peers.Mesh(a.root).members(), what="host to drop the leaver")
            self.assertFalse(a.sees(agent))
        finally:
            a.stop()
            b.stop()


class Setup(unittest.TestCase):
    def test_tailscale_address_reuses_an_existing_serve(self):
        calls = []
        serve = {"TCP": {"443": {"HTTPS": True}},
                 "Web": {"box.tail.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8765"}}}}}

        def fake_run(command):
            calls.append(command)
            out = {"status": {"BackendState": "Running", "Self": {"DNSName": "box.tail.ts.net."}}, "serve": serve}
            kind = "serve" if command[1] == "serve" and command[2] == "status" else "status"
            if command[1] == "serve" and command[2] == "--bg":
                return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            return type("R", (), {"returncode": 0, "stdout": json.dumps(out[kind]), "stderr": ""})()
        old = crewchat.run, crewchat.find_tailscale
        crewchat.run, crewchat.find_tailscale = fake_run, lambda: "tailscale"
        try:
            self.assertEqual(peers.tailscale_address(8765), ("https://box.tail.ts.net", None))
            self.assertFalse(any("--bg" in c for c in calls))
            # Another port: 443 is taken, so it serves on the next free https port.
            self.assertEqual(peers.tailscale_address(8766), ("https://box.tail.ts.net:8443", None))
            self.assertIn(["tailscale", "serve", "--bg", "--https=8443", "http://127.0.0.1:8766"], calls)
        finally:
            crewchat.run, crewchat.find_tailscale = old


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
