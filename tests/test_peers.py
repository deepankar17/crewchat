"""Linking machines over a private network: three real servers in one process, talking HTTP."""
import json
import os
import shutil
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

TMP = tempfile.mkdtemp(prefix="crewchat-peers-")
os.environ.setdefault("CREWCHAT_HOME", str(Path(TMP) / "unused-home"))

import crewchat  # noqa: E402
import crewchat_peers as peers  # noqa: E402

crewchat.Handler.log_message = lambda *args: None
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
            crewchat.save_config({"project": "Demo", "port": 1, "url": "", "forget_hours": 24}, self.root)
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

    def test_a_project_made_later_is_linked_at_once(self):
        for m in (self.a, self.b):
            crewchat.create_project("late", m.root)
            m.projects.all()  # the server notices the new folder on its next request
        a3, b3 = self.hub(self.a, "late"), self.hub(self.b, "late")
        a3.send(crewchat.OWNER, "all", "late but shared")
        eventually(lambda: any(m["text"] == "late but shared" for m in b3.messages), what="the late project shared")

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

    def test_taking_a_task_needs_the_machine_that_posted_it(self):
        a = Machine("poster").found()
        b = Machine("taker").join(a)
        try:
            task = a.say("all", "posted on one", "task")
            eventually(lambda: b.has("posted on one"), what="taker to get the task")
            agent = b.agent()
            a.stop()
            with self.assertRaises(crewchat.HubError) as caught:
                b.hub.take(agent, task["id"])
            self.assertIn("cannot reach poster", str(caught.exception))
        finally:
            b.stop()

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
