"""Projects: each folder its own chat on one machine, apart from the others."""
import os
import subprocess
import unittest

import harness as H
from harness import request


class Projects(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = H.Machine("mac", project="test")
        cls.m.start()
        cls.test2 = cls.m.dir / "test2"
        cls.test2.mkdir()
        subprocess.run(["git", "init", "-q", str(cls.test2)], check=True)
        cls.out = cls.m.cli("start", "--no-service", "--no-open", cwd=cls.test2)
        cls.a = cls.m.agent()
        cls.a.link()
        cls.b = cls.m.agent(folder=cls.test2)
        cls.b.link()
        for x in (cls.a, cls.b):
            x.stop_hook()

    @classmethod
    def tearDownClass(cls):
        cls.m.stop()

    def test_a_new_folder_starts_its_own_project(self):
        self.assertIn('Created the project "test2"', self.out)
        listing = self.m.cli("projects")
        self.assertRegex(listing, r"(?m)^test( \(here\))?  \[--chat test\]$")
        self.assertRegex(listing, r"(?m)^test2  \[--chat test2\]$")
        self.assertIn("test2 (here)", self.m.cli("projects", cwd=self.test2))
        self.assertTrue((self.m.home / "projects" / "test2" / "config.json").exists())

    def test_agents_and_messages_stay_in_their_project(self):
        self.assertNotIn(self.b.name, self.a.call("hub_agents"))
        self.assertNotIn(self.a.name, self.b.call("hub_agents"))
        self.m.cli("say", "only for test2", cwd=self.test2)
        self.assertIn("only for test2", self.b.stop_hook())
        self.assertEqual(self.a.stop_hook(), "")
        self.m.cli("say", "only for test")
        self.assertIn("only for test", self.a.stop_hook())
        self.assertEqual(self.b.stop_hook(), "")
        self.assertIn(self.b.name, self.m.cli("agents", "--chat", "test2"))
        self.assertNotIn(self.b.name, self.m.cli("agents", "--chat", "test").replace(self.a.name, ""))

    def test_the_page_shows_one_project_at_a_time(self):
        o = self.m.owner
        test2 = request(self.m.url + "/api/poll?after=-1&v=-1&wait=0&chat=test2", opener=o.opener)
        data = H.json.loads(test2[1])
        self.assertEqual(data["chat"], "test2")
        self.assertEqual(sorted(p["id"] for p in data["projects"]), ["test", "test2"])
        self.assertIn(self.b.name, [r["agent"] for r in data["agents"]])
        self.assertNotIn(self.a.name, [r["agent"] for r in data["agents"]])
        status, raw, _ = request(self.m.url + "/api/send?chat=test2", {"to": "all", "text": "from the page"},
                                 {"Origin": self.m.url}, o.opener)
        self.assertEqual(status, 200)
        self.assertIn("from the page", self.b.stop_hook())
        self.assertEqual(request(self.m.url + "/api/poll?after=-1&chat=nope", opener=o.opener)[0], 404)

    def test_an_agent_cannot_reach_another_project(self):
        ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        status = request(self.b.mcp, ping, {"Authorization": "Bearer " + self.b.token, "X-Crewchat-Chat": "test"})[0]
        self.assertEqual(status, 403)
        status = request(self.b.mcp, ping, {"Authorization": "Bearer " + self.b.token, "X-Crewchat-Chat": "test2"})[0]
        self.assertEqual(status, 200)
        hook = request(self.m.url + "/api/hook?chat=test", {"key": self.b.key}, {"Authorization": "Bearer " + self.b.token})
        self.assertEqual(hook[0], 403)
        # Asking for another project is refused, not counted as a bad token: the agent still works.
        for _ in range(12):
            request(self.b.mcp, ping, {"Authorization": "Bearer " + self.b.token, "X-Crewchat-Chat": "test"})
        self.assertTrue(self.b.call("hub_agents"))

    def test_files_shared_by_path_only_from_the_agents_own_project_folders(self):
        mine = self.test2 / "notes.txt"
        mine.write_text("test2 notes")
        self.b.call("hub_send", to="Owner", text="mine", files=[str(mine)])
        self.m.owner.chat = "test2"
        try:
            msg = self.m.owner.wait_for("mine")
        finally:
            self.m.owner.chat = ""
        status, body, _ = request(self.m.url + "/files/%s/notes.txt" % msg["files"][0]["id"], opener=self.m.owner.opener)
        self.assertEqual((status, body), (200, b"test2 notes"))


@unittest.skipUnless(H.shutil.which("uv"), "needs uv")
class UpgradeToProjects(unittest.TestCase):
    """A machine set up before projects keeps its chat, tokens and agents; new folders get projects."""

    def test_an_existing_chat_becomes_the_first_project(self):
        from test_install import Install
        inst = Install("test_a_fresh_install_runs_a_chat")
        inst.setUp()
        old = inst.checkout(os.environ.get("CREWCHAT_E2E_FROM_PROJECTS", "v0.9.10"))
        inst.install(old, lean=True)
        m = inst.installed_machine("upgrader")
        m.start()
        a = m.agent()
        a.link()
        a.stop_hook()
        tid = m.owner.task("all", "made before projects")
        a.call("hub_take", id=tid)
        mcp_before = (m.folder / ".mcp.json").read_text()
        m.kill(hard=False)
        inst.install(H.ROOT, lean=True)
        m.cli("start", "--no-service", "--no-open")
        m.wait_up()
        m._owner = None
        self.assertEqual((m.folder / ".mcp.json").read_text(), mcp_before)  # same address and token
        self.assertEqual(m.owner.poll()["taken"][tid], a.name)
        m.owner.send(a.name, "after the upgrade")
        self.assertIn("after the upgrade", a.stop_hook())
        listing = m.cli("projects")
        self.assertRegex(listing, r"(?m)^Demo( \(here\))?  \[--chat demo\]$")
        other = m.dir / "other"
        other.mkdir()
        self.assertIn('Created the project "other"', m.cli("start", "--no-service", "--no-open", cwd=other))
        self.assertIn(a.name, m.cli("agents"))


if __name__ == "__main__":
    unittest.main()
