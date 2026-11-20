"""`crewchat doctor` (path to 10, Solution architect): a health report read on this machine only, with what to do for
each problem, that holds no token, key or message, so it can be pasted into an issue as it is."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from crewchat_fixture import (  # noqa: E402,F401  (the shared server fixture)
    Path, TMP, crewchat, io, json, mock, os, tempfile, unittest,
)


class Doctor(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="doctor-", dir=TMP))
        (self.home / "config.json").write_text('{"port": 8799, "project": "app"}', encoding="utf-8")
        crewchat.ensure_token(crewchat.OWNER, self.home)
        patch = mock.patch.object(crewchat, "service_installed", lambda: False)
        patch.start()
        self.addCleanup(patch.stop)

    def checks(self, up=False, about=None):
        return {what: (mark, said, fix) for mark, what, said, fix in
                crewchat.doctor_checks(self.home, up=lambda: up, about=about)}

    def test_a_healthy_home(self):
        found = self.checks(up=True, about=lambda port: {"version": crewchat.__version__})
        self.assertEqual({w for w, c in found.items() if c[0] != "ok"}, set(), found)
        self.assertIn("port 8799", found["Settings"][1])
        self.assertIn("running crewchat " + crewchat.__version__, found["Server"][1])

    def test_each_problem_says_what_to_do(self):
        (self.home / "config.json").write_text("{not json", encoding="utf-8")
        (self.home / "messages.jsonl").write_text('{"id": "a1"}\nbroken line\n{"id": "a2"}\n', encoding="utf-8")
        (self.home / "state.json").write_text("{", encoding="utf-8")
        found = self.checks(up=False)
        self.assertEqual(found["Settings"][0], "bad")
        self.assertIn("config.json.bak", found["Settings"][2])
        self.assertEqual(found["Messages"][:2], ("warn", "2, 1 lines cannot be read"))
        self.assertEqual(found["State"][0], "warn")
        self.assertEqual(found["Server"], ("warn", "not running", "start it with `crewchat start`"))

    def test_a_server_on_an_older_version_is_told_to_restart(self):
        found = self.checks(up=True, about=lambda port: {"version": "1.0.0"})
        self.assertEqual(found["Server"][0], "warn")
        self.assertIn("crewchat restart", found["Server"][2])

    @unittest.skipIf(os.name == "nt", "file modes are POSIX")
    def test_a_token_others_can_read(self):
        crewchat.token_path(crewchat.OWNER, self.home).chmod(0o644)
        self.assertEqual(self.checks()["Tokens"][0], "warn")

    def test_a_missing_home_or_owner_token(self):
        crewchat.token_path(crewchat.OWNER, self.home).unlink()
        self.assertEqual(self.checks()["Tokens"][0], "bad")
        self.home = self.home / "gone"
        self.assertEqual(self.checks()["Folder"][0], "bad")

    def test_no_project_or_folder_name_is_in_it(self):
        """Round 10 (Security): it goes into a public issue as it is."""
        place = Path(tempfile.mkdtemp(prefix="acme-secret-client-", dir=TMP))
        crewchat.ensure_token("acme-secret-client", self.home)
        crewchat.token_path("acme-secret-client", self.home).chmod(0o644)
        with mock.patch.object(crewchat, "other_workspaces", lambda base=None: [{"path": str(place), "name": "acme"}]):
            text = repr(crewchat.doctor_checks(self.home, up=lambda: False))
        self.assertNotIn("acme", text)
        with mock.patch.object(crewchat, "other_workspaces", side_effect=OSError(13, "Permission denied",
                                                                                  "/home/acme/secret/projects.json")):
            text = repr(crewchat.doctor_checks(self.home, up=lambda: False))
        self.assertNotIn("secret", text)
        self.assertNotIn(self.home.name, text)

    def test_the_report_holds_no_secret_and_fails_on_a_problem(self):
        token = crewchat.read_token(crewchat.OWNER, self.home)
        (self.home / "messages.jsonl").write_text(json.dumps({"text": "my secret plan"}) + "\n", encoding="utf-8")
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"CREWCHAT_HOME": str(self.home)}), mock.patch("sys.stdout", out), \
                mock.patch.object(crewchat, "server_up", lambda config=None: False):
            crewchat.cmd_doctor(mock.Mock(json=False))
        self.assertNotIn(token, out.getvalue())
        self.assertNotIn("secret plan", out.getvalue())
        self.assertIn("Nothing above was sent anywhere", out.getvalue())
        (self.home / "config.json").unlink()
        with mock.patch.dict(os.environ, {"CREWCHAT_HOME": str(self.home)}), mock.patch("sys.stdout", io.StringIO()), \
                mock.patch.object(crewchat, "server_up", lambda config=None: False), self.assertRaises(SystemExit):
            crewchat.cmd_doctor(mock.Mock(json=True))

    def test_each_project_folder_is_checked(self):
        """Round 10 (Solution): 2.1.0's project folders fail in their own ways: not found, held, open on another
        machine, on a network drive, messages that cannot be read."""
        gone, held, shared, ok = (Path(tempfile.mkdtemp(prefix="doctor-ws-", dir=TMP)) for _ in range(4))
        for folder in (held, shared, ok):
            (folder / "config.json").write_text("{}", encoding="utf-8")
        (ok / "messages.jsonl").write_text('{"id": "a1"}\nbad\n', encoding="utf-8")
        rows = [{"path": str(gone), "name": "gone"}, {"path": str(held), "name": "held", "held": True},
                {"path": str(shared), "name": "shared"}, {"path": str(ok), "name": "ok"}]
        with mock.patch.object(crewchat, "other_workspaces", lambda base=None: rows), \
                mock.patch.object(crewchat, "open_elsewhere", lambda f, base=None: "other-mac" if f == shared else ""), \
                mock.patch.object(crewchat, "network_drive", lambda f: "smbfs" if f == held else ""):
            found = [(m, w, said) for m, w, said, _ in crewchat.doctor_checks(self.home, up=lambda: False)]
        self.assertIn(("warn", "Project 2", "not found where it was"), found)
        self.assertIn(("warn", "Project 3", "held: its agents get nothing until you trust it"), found)
        self.assertIn(("warn", "Project 3", "on smbfs"), found)
        self.assertIn(("bad", "Project 4", "open in crewchat on other-mac too: two machines writing one project "
                                                "lose messages"), found)
        self.assertIn(("warn", "Project 5", "1 lines of messages.jsonl cannot be read"), found)

    def test_the_command_is_offered(self):
        self.assertTrue(crewchat.build_parser().parse_args(["doctor", "--json"]).json)


if __name__ == "__main__":
    unittest.main()
