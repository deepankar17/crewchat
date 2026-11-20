"""Linked machines on the page, edits and claims, and the browser panel, gate and proxy."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from crewchat_fixture import (  # noqa: E402,F401  (the shared server fixture)
    ProxyHarness,
    Base, Path, ROOT, TMP, cli, contextlib, crewchat, crewchat_sqlite, eventually, http, io, json, mock, old_bridge,
    os, re, shutil, subprocess, sys, tempfile, threading, time, types, unittest, urllib,
)


class MachinesEndpoint(Base):
    """M1: /api/machines, the Machines panel's operations."""

    def ask(self, body, token=None, host=None, headers=None):
        req = urllib.request.Request(self.base + "/api/machines", data=json.dumps(body).encode(), method="POST",
                                     headers=dict({"Content-Type": "application/json"},
                                                  **({"Authorization": "Bearer " + token} if token else {}),
                                                  **({"Host": host} if host else {}), **(headers or {})))
        with urllib.request.urlopen(req, timeout=20) as answer:
            return json.loads(answer.read().decode())

    def test_the_windows_browser_profile_is_chosen_from_settings(self):
        # Settings > General > Browser profile: the profiles of this machine's Chrome, the one chosen, and a name it
        # does not have refused (the same choice as crewchat ui --profile).
        profiles = {"Default": "Work", "Profile 1": "Personal"}
        with mock.patch.object(crewchat, "app_browser", return_value="/opt/chrome"),                 mock.patch.object(crewchat, "browser_profiles", return_value=profiles):
            out = self.ask({"op": "window-profile"}, self.owner)
            self.assertEqual((out["browser"], out["profiles"], out["profile"]), (True, ["Personal", "Work"], ""))
            self.assertEqual(self.ask({"op": "window-profile", "profile": "Work"}, self.owner)["profile"], "Work")
            self.assertEqual(crewchat.load_config().get("profile"), "Work")
            with self.assertRaises(urllib.error.HTTPError):
                self.ask({"op": "window-profile", "profile": "Nobody"}, self.owner)
            self.assertEqual(self.ask({"op": "window-profile", "profile": ""}, self.owner)["profile"], "")
            self.assertNotIn("profile", crewchat.load_config())

    def test_about_shows_the_version_and_checks_for_an_update(self):
        # Settings > About (11 Nov): this copy's version; Check for updates asks the releases now and the page hears.
        import http.server
        tag = {"v": "v99.1.0"}

        class Releases(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"tag_name": tag["v"]}).encode()
                self.send_response(200); self.end_headers(); self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Releases)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        path = crewchat.home() / "update.json"
        self.addCleanup(lambda: path.exists() and path.unlink())
        self.addCleanup(server.shutdown)
        self.addCleanup(self.hub.set_update, "")
        about = self.ask({"op": "about"}, self.owner)
        self.assertEqual((about["version"], about["site"]), (crewchat.__version__, "https://crewchat.dev"))
        self.assertEqual(about["releases"], crewchat.RELEASES)
        with mock.patch.object(crewchat, "LATEST_API", "http://127.0.0.1:%d/latest" % server.server_port):
            out = self.ask({"op": "check-update"}, self.owner)
            self.assertEqual((out["latest"], out["newer"]), ("99.1.0", "99.1.0"))
            self.assertEqual(self.hub.newer, "99.1.0")  # the page's update notice shows it too
            tag["v"] = "v" + crewchat.__version__
            out = self.ask({"op": "check-update"}, self.owner)
            self.assertEqual((out["latest"], out["newer"]), (crewchat.__version__, ""))
        for op in ("about", "check-update"):
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.ask({"op": op}, self.token)
            self.assertEqual(e.exception.code, 403)
        page = crewchat.CHAT_PAGE
        self.assertIn('<section class="card" id="about-card" data-section="about">', page)
        self.assertIn('href="https://crewchat.dev"', page)
        self.assertIn('<img class="logo" src="/icon.svg"', page)  # its own file: the sidebar's drawing is hidden here
        self.assertIn('id="about-check">Check for updates</button>', page)

    def test_machines_operations_are_owner_only(self):
        status = self.ask({"op": "status"}, self.owner)
        self.assertEqual((status["via"], status["machines"], status["local"]), ("none", [], True))
        self.assertEqual((status["me"]["version"], status["me"]["os"]), (crewchat.__version__, crewchat.os_name()))
        for op in ("status", "peer-invite", "peer-join", "peer-remove", "peer-leave", "prefer", "restart",
                   "update", "cloud-setup", "cloud-login", "cloud-approve", "cloud-remove", "cloud-logout", "firebase-agent"):
            for token, code in ((self.token, 403), (None, 401)):
                with self.assertRaises(urllib.error.HTTPError) as e:
                    self.ask({"op": op}, token)
                self.assertEqual(e.exception.code, code, (op, token))

    def test_an_agent_sets_up_firebase_in_chrome(self):
        # W13: Set it up with an agent: Claude Code with the browser on, the setup steps as its first task.
        status = self.ask({"op": "status"}, self.owner)
        self.assertEqual(status["firebase_locations"]["asia-south1"], "Mumbai")
        folder = Path(tempfile.mkdtemp(prefix="firebase-agent-", dir=TMP)).resolve()
        cli("start", str(folder), "--no-open", "--no-service", "--chat", "demo")  # the agent works from a connected folder
        root = self.hub.roster.root
        with mock.patch.object(crewchat.Hub, "launch", return_value={"ok": True}) as launch:
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.ask({"op": "firebase-agent", "location": "mars-1"}, self.owner)
            self.assertIn("cannot be changed later", json.loads(e.exception.read().decode())["error"])
            launch.assert_not_called()
            out = self.ask({"op": "firebase-agent", "location": "asia-south1"}, self.owner)
        self.assertEqual((out["ok"], out["location"]), (True, "Mumbai"))
        (tool, used), kwargs = launch.call_args
        self.assertEqual((tool, kwargs["name"], kwargs["browser"]), ("claude", "firebase-setup", True))
        self.assertIn(used, [f["path"] for f in crewchat.launch_folders(self.hub.machine_config(), root, "")])
        task = kwargs["task"]
        for step in ("location asia-south1", "Build -> Hosting -> Get started", "Desktop app", "crewchat cloud setup",
                     str(Path(root) / "oauth-client.json"), "never put the client secret in the chat"):
            self.assertIn(step, task)
        self.assertLessEqual(len(task), crewchat.MAX_TEXT)
        sites = self.hub.browser_sites()
        self.assertEqual([sites.get(s) for s in crewchat.FIREBASE_SITES], ["allowed", "allowed"])  # changes still ask

    def test_sign_in_with_google_only_at_the_machine(self):
        try:
            import crewchat_cloud
        except ImportError:
            self.skipTest("cloud sync's extra")
        with mock.patch.object(crewchat_cloud.Auth, "login") as login:
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.ask({"op": "cloud-login"}, self.owner, host="deepankars-mac-mini.tailb8516f.ts.net")
            self.assertIn("at the machine itself", json.loads(e.exception.read().decode())["error"])
            login.assert_not_called()
        self.assertFalse(self.ask({"op": "status"}, self.owner, host="phone.ts.net")["local"])
        # Release review (C6): tailscale serve passes the Host on as it came; what it adds tells a served request.
        self.assertTrue(self.ask({"op": "status"}, self.owner)["local"])
        self.assertFalse(self.ask({"op": "status"}, self.owner, host="127.0.0.1",
                                  headers={"X-Forwarded-For": "100.101.102.103"})["local"])
        self.assertFalse(self.ask({"op": "status"}, self.owner, host="localhost",
                                  headers={"Tailscale-User-Login": "owner@example.com"})["local"])

    def test_a_failure_in_an_operation_is_a_message_not_a_dropped_connection(self):
        # Review: an OSError from starting the restart, or a cloud client's own exception, dropped the request.
        with mock.patch.object(crewchat.subprocess, "Popen", side_effect=OSError("access denied")):
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.ask({"op": "restart"}, self.owner)
        self.assertEqual(e.exception.code, 400)
        self.assertIn("access denied", json.loads(e.exception.read().decode())["error"])
        try:
            import crewchat_cloud
        except ImportError:
            return
        with mock.patch.object(crewchat_cloud, "page_op", side_effect=RuntimeError("Firestore is offline")):
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.ask({"op": "cloud-approve", "name": "x"}, self.owner)
        self.assertEqual(e.exception.code, 400)
        self.assertIn("Firestore is offline", json.loads(e.exception.read().decode())["error"])

    def test_restart_on_windows_falls_back_inside_a_job(self):
        # As start_background does: a job that may not be left (Claude Code, CI) refuses CREATE_BREAKAWAY_FROM_JOB.
        calls = []

        def popen(command, **kwargs):
            calls.append(kwargs.get("creationflags", 0))
            if kwargs.get("creationflags", 0) & 0x01000000:
                raise OSError("breakaway refused")
            return mock.Mock()
        windows = lambda python: (python, {"creationflags": 0x00000008 | 0x08000000 | 0x01000000})  # noqa: E731
        with mock.patch.object(crewchat, "detached", windows), mock.patch.object(crewchat.subprocess, "Popen", side_effect=popen):
            crewchat.spawn_detached(["python", "crewchat.py", "restart"])
        self.assertEqual(len(calls), 2)
        self.assertFalse(calls[1] & 0x01000000)

    def test_restart_from_the_page(self):
        with mock.patch.object(crewchat.subprocess, "Popen") as popen:
            self.assertEqual(self.ask({"op": "restart"}, self.owner), {"restarting": True})
        command, kwargs = popen.call_args[0][0], popen.call_args[1]
        self.assertEqual(command[-1], "restart")
        self.assertTrue(kwargs.get("start_new_session") or kwargs.get("creationflags"))

    def test_update_from_the_page(self):
        # The owner's Windows laptop, 6 Nov: the banner said to run `crewchat update`; Update now runs it from the page.
        with mock.patch.object(crewchat, "installed_by_installer", return_value=True), \
                mock.patch.object(crewchat.subprocess, "Popen") as popen:
            self.assertEqual(self.ask({"op": "update"}, self.owner), {"updating": True, "window": os.name == "nt"})
        command, kwargs = popen.call_args[0][0], popen.call_args[1]
        self.assertEqual(command[-1], "update")
        self.assertTrue(kwargs.get("start_new_session") or kwargs.get("creationflags"))

    def test_a_checkout_does_not_update_from_the_page(self):
        with mock.patch.object(crewchat, "installed_by_installer", return_value=False), \
                mock.patch.object(crewchat.subprocess, "Popen") as popen:
            with self.assertRaises(urllib.error.HTTPError) as e:
                self.ask({"op": "update"}, self.owner)
        self.assertEqual(e.exception.code, 400)
        self.assertIn("not from the installer", json.loads(e.exception.read().decode())["error"])
        popen.assert_not_called()


class LinkChecks(unittest.TestCase):
    """M1's "Check everything": each check with its fix, and how projects pair across machines."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="checks-", dir=TMP))
        crewchat.save_config({"project": "Mine", "port": 9}, self.root)
        for name in ("test1.0", "solo"):
            crewchat.create_project(name, self.root)
        self.projects = crewchat.Projects(self.root)
        self.windows = {"id": "b1", "name": "atslap-73", "state": "online", "version": crewchat.__version__, "os": "windows",
                        "projects": [{"name": "test1.0", "pack": "software", "first": True},
                                     {"name": "Other", "pack": "software", "first": False}]}

    def link(self, machines, via="tailscale"):
        link = crewchat.ProjectLink()
        link._init_projects(self.projects.primary.hub, None)
        link.link_status = lambda: {"via": via}
        link.machines_status = lambda: machines
        link.stopping = lambda: False
        self.projects.primary.hub.sync = link
        return link

    def check(self, checks, cid):
        return next(c for c in checks if c["id"] == cid)

    def test_pairing_finds_the_first_project_trap(self):
        # The owner's 1.0 test: test1.0 here (not the first project) stayed apart from test1.0 on the Windows
        # laptop, where it is the first project, which pairs only with first projects.
        rows = {(r["project"], r["machine"]): r for r in crewchat.pairing(self.projects, [self.windows])}
        trap = rows[("test1.0", "atslap-73")]
        self.assertEqual(trap["state"], "first-project")
        self.assertIn("test1.0 2", trap["fix"])
        self.assertEqual(rows[("solo", "atslap-73")]["state"], "missing")
        self.assertEqual(rows[("Mine", "atslap-73")]["state"], "shared")  # first projects pair whatever their names
        self.assertTrue(crewchat.pairs_with("test1.0 2", self.windows))
        self.assertFalse(crewchat.pairs_with("TEST1.0", self.windows))
        older = dict(self.windows, version="", projects=[])
        self.assertEqual({r["state"] for r in crewchat.pairing(self.projects, [older]) if r["project"] != "Mine"}, {"older"})
        self.link([self.windows])
        checks = crewchat.link_checks(self.projects)
        self.assertIs(self.check(checks, "pairing:test1.0:b1")["ok"], False)

    def test_setting_up_a_name_another_machine_has_as_its_first_project_is_refused(self):
        laptop = {"id": "c1", "name": "laptop", "state": "online", "version": crewchat.__version__, "os": "linux",
                  "projects": [{"name": "Laptop first", "pack": "software", "first": True},
                               {"name": "video-app", "pack": "youtube", "first": False}]}
        other = dict(self.windows, projects=[{"name": "video-app", "pack": "software", "first": True}])
        self.link([laptop, other])
        offer = next(o for o in crewchat.offered(self.projects) if o["name"] == "video-app")
        self.assertEqual((offer["machines"], offer["pack"]), (["laptop"], "youtube"))
        with self.assertRaises(crewchat.HubError) as e:
            crewchat.setup_offered(self.projects, "video-app")
        self.assertIn("video-app 2", str(e.exception))
        self.link([laptop])
        made = crewchat.setup_offered(self.projects, "video-app")
        self.assertEqual(made["name"], "video-app")
        self.assertEqual(crewchat.load_config(self.root / crewchat.PROJECTS_DIR / made["id"])["pack"], "youtube")

    def test_tailscale_stopped_gives_its_fix(self):
        import crewchat_peers
        stopped = subprocess.CompletedProcess([], 0, stdout=json.dumps({"BackendState": "Stopped"}), stderr="")
        with mock.patch.object(crewchat, "find_tailscale", return_value="tailscale"), \
                mock.patch.object(crewchat, "run", return_value=stopped):
            checks = crewchat_peers.tailscale_checks(8765)
        self.assertIs(self.check(checks, "tailscale-installed")["ok"], True)
        running = self.check(checks, "tailscale-running")
        self.assertIs(running["ok"], False)
        self.assertIn("Connect", running["fix"])
        with mock.patch.object(crewchat, "find_tailscale", return_value=None):
            missing = self.check(crewchat_peers.tailscale_checks(8765), "tailscale-installed")
        self.assertIs(missing["ok"], False)
        self.assertIn("tailscale.com/download", missing["fix"])

    def test_an_older_machine_is_a_version_check(self):
        self.link([dict(self.windows, version="", projects=[])])
        version = self.check(crewchat.link_checks(self.projects), "version:b1")
        self.assertIs(version["ok"], False)
        self.assertIn("an older version", version["detail"])
        self.link([dict(self.windows, version="0.9.0")])
        self.assertIn("0.9.0", self.check(crewchat.link_checks(self.projects), "version:b1")["detail"])
        self.link([self.windows])
        self.assertIs(self.check(crewchat.link_checks(self.projects), "version:b1")["ok"], True)


class MachinesPage(unittest.TestCase):
    def test_after_a_restart_the_panel_waits_for_the_new_server_and_clears_old_errors(self):
        # Review: the first /health could still be the old server; and an error stayed after the status loaded.
        page = crewchat.CHAT_PAGE
        restart = page[page.index("async function machRestart"):page.index("function machButton")]
        self.assertTrue("down" in restart, "it waits for the old server to stop answering first")
        load = page[page.index("async function loadMachines"):page.index("function watchMachines")]
        self.assertTrue('mach.error = ""' in load, "a status that loads clears the error")

    def test_os_badges_are_logos_not_emoji(self):
        # The owner's 1.0 test: an apple fruit and a window emoji stood for macOS and Windows.
        page = crewchat.CHAT_PAGE
        for emoji in ("\\u{1F34E}", "\\u{1FA9F}", "\\u{1F427}"):
            self.assertFalse(emoji in page, emoji)  # (not assertNotIn: it would print the whole page)
        logos = page[page.index("const OS_LOGOS"):page.index("};", page.index("const OS_LOGOS"))]
        for os_name in ("mac:", "windows:", "linux:"):
            self.assertIn(os_name, logos)
        self.assertGreaterEqual(logos.count('"M'), 3)  # a path for each

    def test_the_update_banner_can_run_the_update(self):
        page = crewchat.CHAT_PAGE
        banner = page[page.index('<div id="update"'):page.index('<div id="offered"')]
        self.assertTrue('id="update-run"' in banner and "Update now" in banner)
        run = page[page.index('$("update-run").addEventListener'):page.index('$("update-close").addEventListener')]
        self.assertTrue('op: "update"' in run and "serverRestarted(" in run and "location.reload()" in run)

    def test_the_page_has_a_machines_panel(self):
        page = crewchat.CHAT_PAGE
        self.assertIn('openPanel("machines")', page)  # Global settings › Machines & devices (no sidebar button since #204)
        self.assertIn('"machines"', page[page.index("const SETTINGS_NAV"):page.index("const MACHINE_TABS")])
        self.assertTrue("function panelMachines" in page)


class UsageAcrossRenames(unittest.TestCase):
    def test_token_usage_follows_tasks_renamed_when_the_chat_is_first_shared(self):
        # Review: adopt_open_tasks renamed the sheet's tasks but left usage.json under the old numbers.
        root = Path(tempfile.mkdtemp(prefix="usage-rename-", dir=TMP))
        crewchat.save_config({"project": "usage-rename", "port": 9}, root)
        hub = crewchat.Hub(crewchat.Roster(root))
        old = hub.add_row(crewchat.OWNER, "counted")["id"]  # (the message itself is renamed in place)
        counts = {"claude-opus-5-5": [1, 2, 3, 4, 0]}
        with hub.lock:
            hub.sheet[old]["usage"] = counts
            hub._save_usage()
        new = hub.adopt_open_tasks("A", "device-1")[old]
        again = crewchat.Hub(crewchat.Roster(root))
        self.assertEqual(again.sheet[new].get("usage"), counts)
        hub.events.close()
        again.events.close()


class AgentsAcrossARestart(unittest.TestCase):
    def test_what_the_owner_chose_for_an_agent_survives_a_restart(self):
        # The e2e test: after a restart an agent had lost its browser and mobile devices, and Start again opened a
        # Cursor agent from the background in a window (no chat to take its turns), on its tool's default model.
        root = Path(tempfile.mkdtemp(prefix="agents-restart-", dir=TMP))
        crewchat.save_config({"project": "agents-restart", "port": 9}, root)
        hub = crewchat.Hub(crewchat.Roster(root))
        name = hub.agent_for(hub.open_session("mac", "cursor"))
        chose = {"browser": True, "mobile": True, "background": True, "stopped": True, "model": "gpt-5", "account": "work"}
        with hub.lock:
            hub.agents[name].update(chose)
            hub._save()
        again = crewchat.Hub(crewchat.Roster(root))
        self.assertEqual({k: again.agents[name].get(k) for k in chose}, chose)
        hub.events.close()
        again.events.close()


class UsageAfterACrashMidRename(unittest.TestCase):
    def test_counts_saved_under_the_old_numbers_are_found_under_the_new(self):
        # Review: a crash between state.json (new numbers) and usage.json (old) dropped the tasks' token counts.
        root = Path(tempfile.mkdtemp(prefix="usage-crash-", dir=TMP))
        crewchat.save_config({"project": "usage-crash", "port": 9}, root)
        hub = crewchat.Hub(crewchat.Roster(root))
        old = hub.add_row(crewchat.OWNER, "counted")["id"]
        counts = {"claude-opus-5-5": [1, 2, 3, 4, 0]}
        with hub.lock:
            hub.sheet[old]["usage"] = counts
            hub._save_usage()
        with mock.patch.object(hub, "_save_usage"):  # the crash: usage.json not written again
            new = hub.adopt_open_tasks("A", "device-1")[old]
        self.assertEqual(crewchat.Hub(crewchat.Roster(root)).sheet[new].get("usage"), counts)
        hub.events.close()


class FlushKeepsGoing(unittest.TestCase):
    def test_one_failed_save_does_not_skip_the_others(self):
        root = Path(tempfile.mkdtemp(prefix="flush-", dir=TMP))
        crewchat.save_config({"project": "flush", "port": 9}, root)
        hub = crewchat.Hub(crewchat.Roster(root))
        hub.events.add_edit("maya", "a.py", ts=1)
        with hub.lock:
            hub._dirty = True
        real = crewchat.write_json

        def failing(path, *a, **kw):
            if str(path).endswith("state.json"):
                raise OSError(28, "No space left on device")
            return real(path, *a, **kw)
        with mock.patch.object(crewchat, "write_json", failing), contextlib.redirect_stderr(io.StringIO()):
            hub.flush()
        self.assertTrue(hub._dirty)  # still to write: the next flush tries again
        self.assertEqual(len(crewchat.EventStore(root / crewchat.EVENTS_FILE).edits()), 1)  # the events were written
        hub.flush()
        self.assertFalse(hub._dirty)
        hub.events.close()


class ReconnectOnThePage(unittest.TestCase):
    def test_asleep_means_idle_or_not_heard_from_for_its_listening_time(self):
        page = crewchat.CHAT_PAGE
        asleep = page[page.index("function asleep(a)"):page.index("function asleep(a)") + 300]
        self.assertTrue('a.activity === "idle"' in asleep)  # never '' alone: unknown after a restart
        self.assertTrue("a.seen" in asleep)

    def test_an_open_but_asleep_session_offers_reconnect(self):
        page = crewchat.CHAT_PAGE
        controls = page[page.index("function sessionControls"):page.index("function sessionControls") + 4000]
        self.assertTrue('"Reconnect"' in controls)
        self.assertTrue('op: "reconnect"' in controls)
        self.assertTrue("asleep(a)" in controls)


class BrowserPanel(Base):
    """B4: the Browser panel's operations (/api/browser), and the owner's answers to browser asks from Needs you."""

    def op(self, op, **body):
        return crewchat.post_json(self.base + "/api/browser", self.owner, dict(body, op=op))

    def test_sites_token_and_status(self):
        out = self.op("site", host="https://Jenkins.MyCorp.com/job/x", state="trusted")
        self.assertEqual(out["sites"]["jenkins.mycorp.com"], "trusted")
        folder = Path(tempfile.mkdtemp(prefix="panel-bridge-", dir=TMP)).resolve()
        crewchat.save_config(dict(crewchat.load_config(), folders=dict(crewchat.load_config().get("folders") or {},
                                                                       **{str(folder): "mac"})))
        crewchat.install_client("cursor", folder, "http://127.0.0.1:9", "tok")
        self.op("token", value="bridge-token-9")
        self.assertEqual(crewchat.browser_token(), "bridge-token-9")
        if os.name != "nt":  # the token's file: this user's only
            self.assertEqual((crewchat.home() / "config.json").stat().st_mode & 0o777, 0o600)
        self.assertIn("bridge-token-9", (folder / ".cursor" / "mcp.json").read_text())  # connected folders get it
        status = self.op("status")
        self.assertEqual((status["token"], status["holder"], status["sites"]["jenkins.mycorp.com"]),
                         (True, None, "trusted"))
        self.assertNotIn("bridge-token-9", json.dumps(status))  # never back to the page
        self.op("token", value="")
        self.assertNotIn("browser", json.loads((folder / ".cursor" / "mcp.json").read_text())["mcpServers"])
        self.op("site", host="jenkins.mycorp.com", state="remove")
        self.assertNotIn("jenkins.mycorp.com", self.op("status")["sites"])
        with self.assertRaises(urllib.error.HTTPError):
            crewchat.post_json(self.base + "/api/browser", self.token, {"op": "status"})  # an agent cannot

    def test_the_token_never_adds_crewchat_to_codex(self):
        # Release review: saving the token ran install_codex whenever ~/.codex/config.toml existed, so an owner who
        # uses Codex only outside crewchat got crewchat's server, hooks and the bridge in every Codex session.
        home = Path(tempfile.mkdtemp(prefix="codex-own-", dir=TMP))
        (home / "config.toml").write_text('model = "gpt-5"\n')
        with mock.patch.object(crewchat, "codex_config", return_value=home / "config.toml"):
            self.op("token", value="bridge-token-9")
            self.assertEqual((home / "config.toml").read_text(), 'model = "gpt-5"\n')
            (home / "config.toml").unlink()
            crewchat.install_codex()  # crewchat set up for Codex: the bridge goes in its block
            self.op("token", value="bridge-token-8")
            self.assertIn("bridge-token-8", (home / "config.toml").read_text())
        self.op("token", value="")

    def test_a_turn_that_ends_drops_its_browser_asks(self):
        # Release review: an ask whose agent was interrupted stayed in Needs you, held the browser for 10 minutes,
        # and an Allow then added the site for a call that no longer existed.
        name = self.hub.agent_for(self.hub.open_session("mac", "claude"))
        key = crewchat.secrets.token_urlsafe(12)
        with self.hub.lock:
            self.hub.links[key] = name
        got = []
        t = threading.Thread(target=lambda: got.append(self.hub._browser_ask(name, "site", "new.example.com", "", 30)))
        t.start()
        for _ in range(200):
            if any(n["kind"] == "browser" for n in self.hub.needs_you()):
                break
            time.sleep(0.02)
        self.hub.hook(self.hub.agents[name]["place"], key, 0, 0, "prompt")  # its user's next prompt: a new turn
        t.join(5)
        self.assertEqual(got, [""])  # no answer: the call is refused, nothing added
        self.assertEqual([n for n in self.hub.needs_you() if n["kind"] == "browser"], [])

    def test_a_browser_ask_is_answered_from_needs_you(self):
        name = self.hub.agent_for(self.hub.open_session("mac", "claude"))
        got = []
        t = threading.Thread(target=lambda: got.append(self.hub._browser_ask(name, "site", "new.example.com", "", 10)))
        t.start()
        ask = None
        for _ in range(200):
            ask = next((n for n in self.hub.needs_you() if n["kind"] == "browser"), None)
            if ask:
                break
            time.sleep(0.02)
        self.assertIn("new.example.com", ask["text"])
        crewchat.post_json(self.base + "/api/needs", self.owner, {"key": ask["key"], "answer": "allow_site"})
        t.join(5)
        self.assertEqual(got, ["allow_site"])
        for bad in ([1], "browser:", "browser:%s" % ask["id"]):  # not a string, no id, answered: a bad request, not a crash
            with self.assertRaises(urllib.error.HTTPError) as e:
                crewchat.post_json(self.base + "/api/needs", self.owner, {"key": bad, "answer": "deny"})
            self.assertEqual(e.exception.code, 400)

    def test_an_answer_to_an_ask_that_is_gone_says_so(self):
        # Release review (C5): an answer was taken for a browser ask only while the ask waited, so one that had expired
        # got "answer is allow or deny"; and Allow the site answered an ask about a change.
        for answer in ("allow_site", "allow_task", "deny"):
            with self.assertRaises(urllib.error.HTTPError) as e:
                crewchat.post_json(self.base + "/api/needs", self.owner,
                                   {"key": "browser:0123456789ab", "answer": answer})
            self.assertIn("not waiting any more", crewchat.http_error_reason(e.exception), answer)
        name = self.hub.agent_for(self.hub.open_session("mac", "claude"))
        got = []
        t = threading.Thread(target=lambda: got.append(self.hub._browser_ask(name, "change", "ci.example.com", "", 10)))
        t.start()
        ask = None
        for _ in range(200):
            ask = next((n for n in self.hub.needs_you() if n["kind"] == "browser"), None)
            if ask:
                break
            time.sleep(0.02)
        with self.assertRaises(urllib.error.HTTPError) as e:
            crewchat.post_json(self.base + "/api/needs", self.owner, {"key": ask["key"], "answer": "allow_site"})
        self.assertIn("allow_task, trust_site or deny", crewchat.http_error_reason(e.exception))
        crewchat.post_json(self.base + "/api/needs", self.owner, {"key": ask["key"], "answer": "allow_task"})
        t.join(5)
        self.assertEqual(got, ["allow_task"])
        # The item's own key (sendNeed adds it to each card's answer).
        self.assertIn('answerNeed(n, "/api/needs", Object.assign({ key: n.key }, body))', crewchat.CHAT_PAGE)
        self.assertIn('sendNeed(n, { answer }, acts, said,', crewchat.CHAT_PAGE)
        self.assertNotIn('"/api/permission"', crewchat.CHAT_PAGE)
        with self.assertRaises(urllib.error.HTTPError) as e:  # R8: one answer route; the old one is gone
            crewchat.post_json(self.base + "/api/permission", self.owner, {"id": ask["id"], "answer": "deny"})
        self.assertEqual(e.exception.code, 404)

    def test_a_token_left_in_a_file_git_tracks_is_reported(self):
        # Release review: removing the token skipped a settings file git tracks, and said nothing of it.
        folder = Path(tempfile.mkdtemp(prefix="panel-tracked-", dir=TMP)).resolve()
        crewchat.save_config(dict(crewchat.load_config(), folders=dict(crewchat.load_config().get("folders") or {},
                                                                       **{str(folder): "mac"})))
        crewchat.install_client("cursor", folder, "http://127.0.0.1:9", "tok")
        self.op("token", value="bridge-token-7")
        with mock.patch.object(crewchat, "tracked_by_git", return_value=True):
            out = self.op("token", value="")
        self.assertEqual(out["kept"], [str(folder / ".cursor" / "mcp.json")])
        self.assertIn("bridge-token-7", (folder / ".cursor" / "mcp.json").read_text())
        self.assertIn("out.kept", crewchat.CHAT_PAGE)  # the panel shows them

    def test_the_page_has_a_browser_panel(self):
        page = crewchat.CHAT_PAGE
        self.assertTrue('["browser", "Agents\' browser"]' in page)  # a tab of Settings › Machines & devices (W16)
        self.assertTrue("function panelBrowser" in page)
        self.assertTrue("function browserItem" in page)
        self.assertTrue('"allow_task"' in page and '"allow_site"' in page)
        # Release review: a browser ask sorts first in Needs you, and the task panel took it for the task's own card.
        self.assertTrue('state.needs.find((n) => n.task === r.id && n.kind !== "browser" && n.kind !== "device")' in page)


class NoConsoleWindows(unittest.TestCase):
    def test_programs_run_for_their_output_open_no_window(self):
        # On Windows the server has no console: a program it runs without CREATE_NO_WINDOW gets a console window of
        # its own, which flashes up and closes (the agents' process check every few seconds, git for the code map).
        import crewchat_map
        flag = {"creationflags": 0x08000000}
        done = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        folder = Path(tempfile.mkdtemp(prefix="hidden-", dir=TMP))
        with mock.patch.object(crewchat, "HIDDEN", flag), mock.patch.object(crewchat_map, "HIDDEN", flag), \
                mock.patch.object(subprocess, "run", return_value=done) as ran:
            crewchat.process_table([os.getpid()])
            crewchat.run(["tailscale", "status", "--json"])
            crewchat.tracked_by_git(folder, "a.py")
            crewchat.git_exclude(folder, ["x"])
            crewchat.tool_help("some-tool")
            crewchat.status_of(["some-tool", "--version"])
            crewchat_map.git(folder, "rev-parse", "HEAD")
        self.assertGreaterEqual(ran.call_count, 7)
        for call in ran.call_args_list:
            self.assertEqual(call.kwargs.get("creationflags"), 0x08000000, call.args[0][:3])


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
        with store.lock:  # the store's connection, as its own reads use it: never beside its writer's commit
            db = store._conn()
            self.assertEqual((db.execute("PRAGMA journal_mode").fetchone()[0], db.execute("PRAGMA synchronous").fetchone()[0]),
                             ("wal", 2))  # 2: FULL, as the records cannot be rebuilt
            plan = db.execute("EXPLAIN QUERY PLAN SELECT ts FROM edits WHERE ts >= 0 AND task = '12'").fetchall()
            self.assertIn("edits_task", " ".join(str(row) for row in plan))
            # The analytics view's "state before the window", every 15 s: found through the index, never a scan of a
            # year of activity.
            plan = " ".join(str(row[3]) for row in db.execute("EXPLAIN QUERY PLAN " + crewchat.BEFORE_STATES, (5,)))
            self.assertNotIn("SCAN activity", plan)
        store.close()
        self.assertEqual(len(crewchat.EventStore(store.path).edits()), 3)  # kept on disk

    def test_a_failed_commit_loses_no_row(self):
        # Review: rows a read had written without committing were rolled back with a failed commit and never queued again.
        store = crewchat.EventStore(Path(tempfile.mkdtemp(prefix="events-", dir=TMP)) / "events.sqlite")
        store.add_edit("maya", "app/a.kt", ts=1)
        store.edits()
        with store.lock:
            store.add_edit("teo", "app/b.kt", ts=2)
            store._read()  # written, not committed
            db = store._conn()
            store.db = mock.Mock(wraps=db)
            store.db.commit = mock.Mock(side_effect=crewchat_sqlite().OperationalError("database is locked"))
            with self.assertRaises(crewchat_sqlite().OperationalError):
                store._write_pending()
            store.db = db
        store.close()
        self.assertEqual([e["agent"] for e in crewchat.EventStore(store.path).edits()], ["maya", "teo"])

    def test_a_failed_rollback_loses_no_row_either(self):
        # Rows lost in a full run: a commit refused ("SQL statements in progress"), then the rollback refused too, and
        # the rows were never queued again.
        store = crewchat.EventStore(Path(tempfile.mkdtemp(prefix="events-", dir=TMP)) / "events.sqlite")
        store.add_edit("maya", "app/a.kt", ts=1)
        store.edits()
        with store.lock:
            store.add_edit("teo", "app/b.kt", ts=2)
            store._read()  # written, not committed
            db = store._conn()
            store.db = mock.Mock(wraps=db)
            store.db.commit = mock.Mock(side_effect=crewchat_sqlite().OperationalError("SQL statements in progress"))
            store.db.rollback = mock.Mock(side_effect=crewchat_sqlite().OperationalError("SQL statements in progress"))
            store.db.close = db.close
            with self.assertRaises(crewchat_sqlite().OperationalError):
                store._write_pending()
        store.close()
        self.assertEqual([e["agent"] for e in crewchat.EventStore(store.path).edits()], ["maya", "teo"])  # once each

    def test_a_failed_write_mid_batch_keeps_the_rest_of_the_batch(self):
        # Review: an error on one row (a full disk) lost it and every row after it in the batch.
        store = crewchat.EventStore(Path(tempfile.mkdtemp(prefix="events-", dir=TMP)) / "events.sqlite")
        store.add_edit("maya", "app/a.kt", ts=1)
        store.edits()
        db = store._conn()
        calls = [0]

        class Flaky:
            def __getattr__(self, name):
                return getattr(db, name)

            def execute(self, sql, row=()):
                if sql.startswith("INSERT"):
                    calls[0] += 1
                    if calls[0] == 2:
                        raise crewchat_sqlite().OperationalError("database or disk is full")
                return db.execute(sql, row)
        with store.lock:
            for i, agent in enumerate(("teo", "ana", "lu")):
                store.add_edit(agent, "app/x.kt", ts=2 + i)
            store.db = Flaky()
            with self.assertRaises(crewchat_sqlite().OperationalError):
                store._write_pending()
            store.db = db
        store.close()
        self.assertEqual([e["agent"] for e in crewchat.EventStore(store.path).edits()], ["maya", "teo", "ana", "lu"])

    def test_a_row_that_cannot_be_written_does_not_block_the_others(self):
        # Review: one bad row was put back at the front every time, and every later row waited behind it.
        store = crewchat.EventStore(Path(tempfile.mkdtemp(prefix="events-", dir=TMP)) / "events.sqlite")
        store.add_edit("maya", "app/a.kt", ts=1)
        store.edits()
        store._add("INSERT INTO edits (ts, agent, path, task) VALUES (?, ?, ?, ?)", (2, None, "bad", ""))  # NOT NULL
        store.add_edit("teo", "app/b.kt", ts=3)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual([e["agent"] for e in store.edits()], ["maya", "teo"])
        self.assertEqual(store.pending, [])
        store.close()

    def test_a_read_does_not_wait_for_the_disk(self):
        # Review: edits() is read under the hub's lock (a task's suggested areas); writing what waits must not commit
        # (synchronous FULL, a wait for the disk) there. The rows are seen at once; the writer commits them.
        store = crewchat.EventStore(Path(tempfile.mkdtemp(prefix="events-", dir=TMP)) / "events.sqlite")
        store.add_edit("maya", "app/a.kt", ts=1)
        store.edits()
        db, committed = store._conn(), []
        real = db.commit
        store.db = mock.Mock(wraps=db)
        store.db.commit = lambda: committed.append(threading.current_thread().name) or real()
        with store.lock:
            store.add_edit("teo", "app/b.kt", ts=2)
        self.assertEqual([e["agent"] for e in store.edits()], ["maya", "teo"])
        self.assertNotIn(threading.current_thread().name, committed)
        for _ in range(200):
            if committed:
                break
            time.sleep(0.01)
        self.assertEqual(committed, ["events-writer"])
        store.close()
        self.assertEqual(len(crewchat.EventStore(store.path).edits()), 2)

    def test_event_rows_are_written_off_the_callers_thread(self):
        # add_edit and add_activity run under the hub's lock: a commit waiting on the disk (synchronous FULL) must
        # not hold it. Here the store's connection is busy (a slow disk): adding a row still returns at once.
        store = crewchat.EventStore(Path(tempfile.mkdtemp(prefix="events-", dir=TMP)) / "events.sqlite")
        store.add_edit("maya", "app/a.kt", ts=1)
        self.assertEqual(len(store.edits()), 1)
        done = threading.Event()
        with store.lock:
            threading.Thread(target=lambda: (store.add_edit("teo", "app/b.kt", ts=2),
                                             store.add_activity("teo", "working", ts=2), done.set()), daemon=True).start()
            self.assertTrue(done.wait(2), "adding a row waited for the store's connection")
        self.assertEqual([e["agent"] for e in store.edits()], ["maya", "teo"])  # a read sees every row added before it
        store.add_edit("lu", "app/d.kt", ts=2.5)
        for _ in range(200):  # and the writer writes them on its own
            if not store.pending:
                break
            time.sleep(0.01)
        self.assertEqual(store.pending, [])
        store.add_edit("ana", "app/c.kt", ts=3)
        store.close()  # what waits is written before the store closes: the rows cannot be rebuilt
        self.assertEqual([e["agent"] for e in crewchat.EventStore(store.path).edits()], ["maya", "teo", "lu", "ana"])


class ConfigWrites(unittest.TestCase):
    """Release review (C2): a change to a config.json reads and writes it whole, one at a time (CONFIG_LOCK), so a
    change made meanwhile (the owner allowing a site) is never written over with an old copy."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="config-writes-", dir=TMP))
        crewchat.save_config({"project": "web", "port": 9}, self.root)

    def interleaved(self, change):
        """Run change; the first time it reads config.json, another thread allows a site, and is given a moment."""
        real, writer = crewchat.load_config, []

        def load(root=None):
            config = real(root)
            if not writer:
                writer.append(threading.Thread(target=lambda: crewchat.Roster(self.root).update(
                    "browser_sites", lambda old: {"ci.example.com": "allowed"})))
                writer[0].start()
                writer[0].join(0.3)
            return config
        with mock.patch.object(crewchat, "load_config", load):
            change()
        writer[0].join(5)
        return crewchat.load_config(self.root)

    def test_a_change_never_drops_one_made_meanwhile(self):
        hub = crewchat.Hub(crewchat.Roster(self.root))
        self.addCleanup(hub.events.close)
        with mock.patch.object(hub, "refresh_guides", return_value=0):
            config = self.interleaved(lambda: hub.set_rules("Tests first."))
        self.assertEqual((config.get("rules"), config.get("browser_sites")), ("Tests first.", {"ci.example.com": "allowed"}))
        crewchat.save_config({"project": "web", "port": 9}, self.root)
        with mock.patch.object(crewchat, "home", return_value=self.root):
            config = self.interleaved(lambda: crewchat.save_roles_change(self.root, {"op": "pack", "pack": "youtube"}))
        self.assertEqual((config.get("pack"), config.get("browser_sites")), ("youtube", {"ci.example.com": "allowed"}))
        crewchat.save_config({"project": "web", "port": 9}, self.root)
        folder = Path(tempfile.mkdtemp(prefix="config-folder-", dir=TMP))
        config = self.interleaved(lambda: crewchat.remember(folder, "mac", self.root))
        self.assertEqual((config.get("folders"), config.get("browser_sites")), ({str(folder): "mac"}, {"ci.example.com": "allowed"}))
        crewchat.save_config(dict(config, browser_sites={}), self.root)
        config = self.interleaved(lambda: crewchat.forget_folders("mac", root=self.root))
        self.assertEqual((config.get("folders"), config.get("browser_sites")), ({}, {"ci.example.com": "allowed"}))

    def test_the_projects_are_listed_under_their_lock(self):
        # Release review (E6): the link's threads list the projects while a refresh may change them.
        projects = crewchat.Projects(self.root)
        done = threading.Event()
        with projects.lock:
            threading.Thread(target=lambda: projects.listed() and done.set(), daemon=True).start()
            self.assertFalse(done.wait(0.3))
        self.assertTrue(done.wait(5))


class ReclaimedHolderTold(unittest.TestCase):
    """An agent back from a silence is told at its first tool call that its task was reclaimed, before a write it
    had queued (a push, an API call) goes further, not only when it next reports on the task."""

    def setUp(self):
        root = Path(tempfile.mkdtemp(prefix="reclaimed-", dir=TMP))
        crewchat.save_config({"project": "web", "port": 9}, root)
        self.hub = crewchat.Hub(crewchat.Roster(root))
        self.addCleanup(self.hub.events.close)

    def agent(self, client="claude"):
        name = self.hub.agent_for(self.hub.open_session("mac", client))
        key = crewchat.secrets.token_urlsafe(12)
        with self.hub.lock:
            self.hub.links[key] = name
        return name, key

    def tool(self, key, name):
        return self.hub.hook(self.hub.agents[name]["place"], key, 0, 0, "tool").get("remind", "")

    def test_the_first_tool_call_after_a_reclaim_says_so_once(self):
        hub = self.hub
        a, a_key = self.agent()
        b, _ = self.agent()
        task = hub.add_row(crewchat.OWNER, "Push the release notes")["id"]
        hub.take(a, task)
        self.assertEqual(self.tool(a_key, a), "")  # its own task: nothing to say
        then = time.time() + 61 * 60
        with mock.patch.object(crewchat.time, "time", lambda: then):
            hub.reclaim_silent()
            remind = self.tool(a_key, a)
            self.assertIn("#%s was reclaimed from you" % task, remind)
            self.assertIn("back on the sheet", remind)
            self.assertIn("Stop", remind)
            self.assertEqual(self.tool(a_key, a), "")  # once, not at every call
            hub.take(b, task)
            self.assertIn("%s holds it now" % b, self.tool(a_key, a))  # a new holder: told again, with its name
            self.assertEqual(self.tool(a_key, a), "")
            hub.update(b, task, "todo", "over to you")
            hub.take(a, task)  # it has the task again: nothing to warn of
            self.assertEqual(self.tool(a_key, a), "")


class ReclaimedHolderRefused(unittest.TestCase):
    """The first shell or file-editing call after a reclaim is refused, once: a write it had queued does not go out
    before it hears of the reclaim. The hook runs before the call, and needs the server only after a silence."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="refused-", dir=TMP))
        crewchat.save_config({"project": "web", "port": 9}, self.root)
        self.hub = crewchat.Hub(crewchat.Roster(self.root))
        self.addCleanup(self.hub.events.close)

    def agent(self, client="claude"):
        name = self.hub.agent_for(self.hub.open_session("mac", client))
        key = crewchat.secrets.token_urlsafe(12)
        with self.hub.lock:
            self.hub.links[key] = name
        return name, key

    def before(self, key, name):
        return self.hub.hook(self.hub.agents[name]["place"], key, 0, 0, "write").get("refuse", "")

    def test_the_first_write_after_a_reclaim_is_refused_once(self):
        hub = self.hub
        a, key = self.agent()
        task = hub.add_row(crewchat.OWNER, "Push the release notes")["id"]
        hub.take(a, task)
        self.assertEqual(self.before(key, a), "")  # its own task: the call goes on
        then = time.time() + 61 * 60
        with mock.patch.object(crewchat.time, "time", lambda: then):
            hub.reclaim_silent()
            refused = self.before(key, a)
            self.assertIn("#%s was reclaimed from you" % task, refused)
            self.assertIn("run it again", refused)  # work on another task is not stopped for good
            self.assertEqual(self.before(key, a), "")  # once
            self.assertEqual(hub.hook(hub.agents[a]["place"], key, 0, 0, "tool").get("remind", ""), "")  # told already

    def test_a_write_counts_as_a_sign_of_life(self):
        hub = self.hub
        a, key = self.agent()
        hub.take(a, hub.add_row(crewchat.OWNER, "Long task")["id"])
        then = time.time() + 59 * 60
        with mock.patch.object(crewchat.time, "time", lambda: then):
            self.assertEqual(self.before(key, a), "")
        with mock.patch.object(crewchat.time, "time", lambda: then + 2 * 60):
            hub.reclaim_silent()
        self.assertIn(a, hub.taken.values())  # still working: not silent

    def state_of(self, client, state):
        session = "s" + crewchat.secrets.token_hex(4)
        path = crewchat.hook_state_file(client, session)
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(state))
        self.addCleanup(path.unlink)
        return session, path

    def run_hook(self, client, state, post):
        return self.run_session(client, self.state_of(client, state)[0], post)

    def run_session(self, client, session, post):
        out = io.StringIO()
        with mock.patch.object(crewchat, "find_project", return_value=self.root), \
                mock.patch.object(crewchat, "client_config", return_value=("http://127.0.0.1:9", "tok")), \
                mock.patch.object(crewchat, "hook_post", post), contextlib.redirect_stdout(out):
            crewchat.write_hook(client, {"session_id": session})
        return out.getvalue()

    def test_the_hook_prints_the_refusal_in_the_tools_own_format(self):
        said = []
        post = lambda url, token, body, timeout=5: said.append(body) or {"refuse": "stop"}  # noqa: E731
        out = self.run_hook("claude", {"key": "k" * 12}, post)
        self.assertEqual(said, [{"key": "k" * 12, "event": "write"}])
        self.assertEqual(json.loads(out), json.loads(crewchat.browser_deny("claude", "stop")))
        self.assertEqual(json.loads(self.run_hook("gemini", {"key": "k" * 12}, post))["decision"], "deny")

    def test_the_hook_asks_the_server_only_after_a_silence(self):
        said = []
        post = lambda url, token, body, timeout=5: said.append(body) or {"refuse": "stop"}  # noqa: E731
        self.assertEqual(self.run_hook("claude", {"key": "k" * 12, "alive": time.time() - 5}, post), "")
        self.assertEqual(self.run_hook("claude", {}, post), "")  # not linked: nothing to ask
        self.assertEqual(said, [])

    def test_a_server_that_cannot_be_reached_stops_nothing(self):
        def post(url, token, body, timeout=5):
            raise OSError("refused")
        self.assertEqual(self.run_hook("claude", {"key": "k" * 12}, post), "")

    def test_every_tool_gets_the_hook_on_its_write_calls(self):
        settings = {"hooks": {"PreToolUse": [{"matcher": "Task", "hooks": [{"type": "command", "command": "mine"}]}]}}
        crewchat.set_claude_hooks(settings)
        crewchat.set_claude_hooks(settings)  # again: its own two replaced, the user's kept
        groups = settings["hooks"]["PreToolUse"]
        self.assertEqual([g["matcher"] for g in groups], ["Task", crewchat.WRITE_MATCHER, crewchat.BROWSER_MATCHER,
                                                         crewchat.QUESTION_MATCHER])
        self.assertEqual(groups[1]["hooks"][0]["command"], crewchat.hook_command("claude", "write"))
        for tool in ("Bash", "Edit", "Write", "run_shell_command", "write_file", "bash"):
            self.assertTrue(re.search(crewchat.WRITE_MATCHER, tool), tool)
        for tool in ("TodoWrite", "mcp__crewchat__hub_task", "WebFetch"):
            self.assertFalse(re.search(crewchat.WRITE_MATCHER, tool), tool)
        # Reads reach it too, only for crewchat's token files (O1): any other read goes on unasked (test_owner_leave).
        for tool in ("Read", "Grep", "read_file"):
            self.assertTrue(re.search(crewchat.WRITE_MATCHER, tool), tool)
        for client, event in (("gemini", "BeforeTool"), ("qwen", "PreToolUse")):
            settings = {}
            crewchat.set_settings_hooks(settings, client)
            self.assertEqual([g["matcher"] for g in settings["hooks"][event]],
                             [crewchat.WRITE_MATCHER, crewchat.BROWSER_MATCHER])
        hooks = {}
        crewchat.set_cursor_hooks(hooks)
        self.assertEqual(hooks["hooks"]["beforeShellExecution"][-1]["command"], crewchat.hook_command("cursor", "write"))

    def reclaimed_from(self, client="claude"):
        a, key = self.agent(client)
        task = self.hub.add_row(crewchat.OWNER, "Push the release notes")["id"]
        self.hub.take(a, task)
        self.hub.update(a, task, "in_progress", "on it")
        return a, key, task

    def test_a_task_finished_or_removed_since_says_nothing(self):
        hub = self.hub
        a, key, task = self.reclaimed_from()
        gone = hub.add_row(crewchat.OWNER, "Dropped")["id"]
        hub.take(a, gone)
        b, _ = self.agent()
        then = time.time() + 61 * 60
        with mock.patch.object(crewchat.time, "time", lambda: then):
            hub.reclaim_silent()
            hub.plan_change(gone, status="removed")
            hub.take(b, task)
            hub.update(b, task, "done", "finished\nDecisions: none\nLeft over: none")
            self.assertEqual(self.before(key, a), "")

    def test_the_writes_queued_with_the_first_are_refused_too(self):
        # A commit and a push in one response: the agent reads the refusal only after both.
        calls, stop = [], {"refuse": "stop"}
        post = lambda url, token, body, timeout=5: calls.append(body) or stop  # noqa: E731
        session, path = self.state_of("claude", {"key": "k" * 12})
        self.assertIn("stop", self.run_session("claude", session, post))
        stop.clear()
        self.assertIn("stop", self.run_session("claude", session, post))  # the push, right after it
        self.assertEqual(calls, [{"key": "k" * 12, "event": "write"}, {"key": "k" * 12, "event": "held"}])
        state = json.loads(path.read_text())
        state["hold_until"] = time.time() - 1
        path.write_text(json.dumps(state))
        self.assertEqual(self.run_session("claude", session, post), "")  # and then it may run it again

    def after_tool(self, client, hold):
        session, path = self.state_of(client, {"key": "k" * 12})
        with mock.patch.object(crewchat, "find_project", return_value=self.root), \
                mock.patch.object(crewchat, "client_config", return_value=("http://127.0.0.1:9", "tok")), \
                mock.patch.object(crewchat, "hook_post", lambda url, token, body, timeout=5: {"hold": hold}), \
                contextlib.redirect_stdout(io.StringIO()):
            crewchat.tool_hook(client, {"session_id": session, "tool_name": "Read", "tool_input": {"file_path": "a"}})
        return session, path

    def test_a_write_right_after_the_tool_call_that_said_so_is_refused(self):
        session, _ = self.after_tool("claude", "stop")
        said = []
        post = lambda url, token, body, timeout=5: said.append(body) or {}  # noqa: E731
        self.assertIn("stop", self.run_session("claude", session, post))
        self.assertEqual(said, [{"key": "k" * 12, "event": "held"}])  # only whether it took the task back

    def test_a_tool_with_no_way_into_the_turn_is_refused_at_its_next_write_however_late(self):
        session, path = self.after_tool("opencode", "stop")
        post = lambda url, token, body, timeout=5: {}  # noqa: E731
        later = time.time() + 20 * 60
        with mock.patch.object(crewchat.time, "time", lambda: later):
            self.assertIn("stop", self.run_session("opencode", session, post))
        self.assertLessEqual(json.loads(path.read_text())["hold_until"], later + crewchat.HOLD_WRITES)

    def test_a_take_after_the_reclaim_ends_the_hold_at_once(self):
        # Owner's Mac retest (11 Nov): Yara was stopped, which put her task back on the sheet. After a Reopen she took
        # it again, and the rest of her turn's Bash calls were still refused with the old notice ("until you hub_task
        # take it again"), as the hook held writes back for a while without asking.
        hub = self.hub
        a, key, task = self.reclaimed_from()
        with hub.lock:
            hub._free_tasks(a, "was stopped")
        place = hub.agents[a]["place"]
        post = lambda url, token, body, timeout=5: hub.hook(place, body["key"], 0, 0, body["event"])  # noqa: E731
        session, path = self.state_of("claude", {"key": key})
        self.assertIn("take it again", self.run_session("claude", session, post))  # what it had queued: held back
        self.assertIn("take it again", self.run_session("claude", session, post))  # still not taken back
        hub.take(a, task)
        self.assertEqual(self.run_session("claude", session, post), "")  # it has the task again: the call runs
        state = json.loads(path.read_text())
        self.assertEqual((state.get("hold_until", 0), state.get("hold")), (0, None))
        self.assertEqual(self.run_session("claude", session, post), "")

    def test_a_long_hold_ends_when_the_agent_has_the_task_again(self):
        hub = self.hub
        a, key, task = self.reclaimed_from("opencode")
        self.assertIs(hub.hook(hub.agents[a]["place"], key, 0, 0, "held")["reclaimed"], False)
        then = time.time() + 61 * 60
        with mock.patch.object(crewchat.time, "time", lambda: then):
            hub.reclaim_silent()
            self.assertIs(hub.hook(hub.agents[a]["place"], key, 0, 0, "held")["reclaimed"], True)
        session, path = self.after_tool("opencode", "stop")
        post = lambda url, token, body, timeout=5: {"reclaimed": False}  # noqa: E731
        self.assertEqual(self.run_session("opencode", session, post), "")  # it took the task back: it may work on it
        self.assertEqual(json.loads(path.read_text()).get("hold_until", 0), 0)

    def test_the_server_says_so_after_a_tool_call_too(self):
        hub = self.hub
        a, key, task = self.reclaimed_from("opencode")
        then = time.time() + 61 * 60
        with mock.patch.object(crewchat.time, "time", lambda: then):
            hub.reclaim_silent()
            self.assertIn("#%s" % task, hub.hook(hub.agents[a]["place"], key, 0, 0, "tool")["hold"])

    def test_the_hook_command_takes_the_write_event(self):
        out = subprocess.run([sys.executable, str(ROOT / "crewchat.py"), "hook", "claude", "write"], input=b"{}",
                             capture_output=True, env=dict(os.environ, CREWCHAT_HOOKS="off"))
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_every_shell_and_edit_tool_crewchat_knows_is_watched(self):
        names = [k for k, v in crewchat.OTHER_TOOL_NAMES.items() if v in ("Bash", "Edit", "Write")]
        self.assertTrue(names)
        for tool in names + list(crewchat.EDIT_TOOLS):
            self.assertTrue(re.search(crewchat.WRITE_MATCHER, tool), tool)

    def test_the_write_hook_runs_in_the_light_script(self):
        with mock.patch.object(crewchat, "write_hook_script", return_value=Path("/x/crewchat-hook.py")):
            self.assertIn("crewchat-hook.py", crewchat.hook_command("claude", "write"))
        text = crewchat.hook_script()
        self.assertIn("def write_hook", text)
        self.assertIn("def browser_deny", text)
        self.assertIn('sys.argv[2:3] == ["write"]', text)


class BrowserGate(unittest.TestCase):
    """B1-B2: every browser call is checked before it runs: one driver per machine, allowed sites, a change asked once
    per task and site, trusted sites free."""

    def setUp(self):
        crewchat.BROWSER.clear()
        root = Path(tempfile.mkdtemp(prefix="browser-", dir=TMP))
        crewchat.save_config({"project": "web", "port": 9}, root)
        self.hub = crewchat.Hub(crewchat.Roster(root))
        self.luca, self.luca_key = self.agent()
        self.jade, self.jade_key = self.agent()
        self.place = self.hub.agents[self.luca]["place"]

    def tearDown(self):
        self.hub.events.close()
        crewchat.BROWSER.clear()

    def agent(self, browser=True):
        name = self.hub.agent_for(self.hub.open_session("mac", "claude"))
        key = crewchat.secrets.token_urlsafe(12)
        with self.hub.lock:
            self.hub.links[key] = name
            self.hub.agents[name]["browser"] = browser
        return name, key

    def check(self, key, tool, args, wait=2):
        return self.hub.browser_check(self.place, key, tool, args, wait=wait)

    def answer_next(self, answer):
        """Answer the next browser ask in Needs you, from another thread, as the owner on the page does."""
        def run():
            for _ in range(200):
                asks = [n for n in self.hub.needs_you() if n["kind"] == "browser"]
                if asks:
                    self.hub.answer_browser(asks[0]["id"], answer)
                    return
                time.sleep(0.01)
        threading.Thread(target=run, daemon=True).start()

    def nav(self, url):
        return "mcp__claude-in-chrome__navigate", {"url": url, "tabId": 1}  # Claude in Chrome's calls name their tab

    def click(self):
        return "mcp__claude-in-chrome__computer", {"action": "left_click", "coordinate": [10, 10], "tabId": 1}

    def test_the_actions_are_classified(self):
        act = crewchat.browser_action
        self.assertEqual(act("mcp__claude-in-chrome__navigate", {"url": "https://ci.example.com/job/x"}),
                         ("navigate", "https://ci.example.com/job/x"))
        for name, args, kind in (
                ("mcp__claude-in-chrome__computer", {"action": "left_click"}, "change"),
                ("mcp__claude-in-chrome__computer", {"action": "type", "text": "x"}, "change"),
                ("mcp__claude-in-chrome__computer", {"action": "key"}, "change"),
                ("mcp__claude-in-chrome__computer", {"action": "screenshot"}, "read"),
                ("mcp__claude-in-chrome__computer", {"action": "scroll"}, "read"),
                ("mcp__claude-in-chrome__form_input", {}, "change"),
                ("mcp__claude-in-chrome__javascript_tool", {}, "change"),
                ("mcp__claude-in-chrome__read_page", {}, "read"),
                ("mcp__claude-in-chrome__get_page_text", {}, "read"),
                ("mcp__browser__browser_click", {}, "change"),
                ("browser_type", {}, "change"),
                ("browser_fill_form", {}, "change"),
                ("browser_evaluate", {}, "change"),
                ("browser_snapshot", {}, "read"),
                ("browser_take_screenshot", {}, "read"),
                ("mcp__browser__browser_navigate", {"url": "https://a.example/"}, "navigate"),
                ("Bash", {"command": "ls"}, "other"),
                ("mcp__crewchat__hub_send", {}, "other")):
            self.assertEqual(act(name, args)[0], kind, (name, args))
        self.assertEqual(crewchat.site_of("https://Jenkins.MyCorp.com:8443/job/release/"), "jenkins.mycorp.com")
        self.assertEqual(crewchat.site_of("not a url"), "")

    def test_an_agent_without_the_browser_is_refused(self):
        name, key = self.agent(browser=False)
        out = self.check(key, *self.nav("https://ci.example.com/"))
        self.assertEqual(out["decision"], "deny")
        self.assertIn("not on for %s" % name, out["reason"])
        self.assertEqual(self.hub.browser_check(self.place, "unknown-key-123", *self.nav("https://x.example/"))["decision"],
                         "allow")  # a session not in the chat: not crewchat's to stop
        self.assertEqual(self.check(key, "Bash", {"command": "ls"})["decision"], "allow")  # not a browser call

    def test_an_allow_the_gate_checked_says_so(self):
        # 11 Nov two-machine test: Claude Code asked again about each call crewchat's gate had passed (the open the
        # owner had just allowed, the tab list, the screenshot). An allow for a chat agent with the browser on is marked
        # checked, so its hook answers Claude Code's prompt; a session not in the chat, or a refusal, is not.
        self.hub.set_browser_site("ci.example.com", "allowed")
        self.assertIs(self.check(self.luca_key, *self.nav("https://ci.example.com/"))["checked"], True)
        tabs = self.check(self.luca_key, "mcp__claude-in-chrome__tabs_context_mcp", {})
        self.assertEqual((tabs["decision"], tabs.get("checked")), ("allow", True))
        shot = self.check(self.luca_key, "mcp__claude-in-chrome__computer", {"action": "screenshot", "tabId": 1})
        self.assertEqual((shot["decision"], shot.get("checked")), ("allow", True))
        self.assertNotIn("checked", self.hub.browser_check(self.place, "unknown-key-123",
                                                           *self.nav("https://x.example/")))
        self.assertNotIn("checked", self.check(self.jade_key, *self.nav("https://ci.example.com/")))  # Luca has it
        _, off = self.agent(browser=False)
        self.assertNotIn("checked", self.check(off, *self.nav("https://ci.example.com/")))
        self.assertNotIn("checked", self.check(self.luca_key, "Bash", {"command": "ls"}))  # not a browser call

    def test_one_driver_at_a_time_and_release_after_idle(self):
        self.hub.set_browser_site("ci.example.com", "allowed")
        now = [1000.0]
        with mock.patch.object(crewchat.time, "time", lambda: now[0]):
            self.assertEqual(self.check(self.luca_key, *self.nav("https://ci.example.com/"))["decision"], "allow")
            out = self.check(self.jade_key, *self.nav("https://ci.example.com/"))
            self.assertEqual(out["decision"], "deny")
            self.assertIn(self.luca, out["reason"])
            now[0] += crewchat.BROWSER_IDLE + 1  # Luca has not used it for ten minutes
            self.assertEqual(self.check(self.jade_key, *self.nav("https://ci.example.com/"))["decision"], "allow")
            self.assertEqual(crewchat.BROWSER["holder"]["agent"], self.jade)
        self.hub.release_browser(self.jade)
        self.assertNotIn("holder", crewchat.BROWSER)

    def test_a_turn_that_ends_lets_the_browser_go(self):
        # 11 Nov two-machine test: Yara was done with the browser, but it stayed hers until the owner turned her browser
        # off, and Anika's calls were refused. The end of the holder's turn (or the start of a new one) lets it go.
        self.hub.set_browser_site("ci.example.com", "allowed")
        for event in ("stop", "prompt"):
            self.assertEqual(self.check(self.luca_key, *self.nav("https://ci.example.com/"))["decision"], "allow")
            self.assertEqual(self.check(self.jade_key, *self.nav("https://ci.example.com/"))["decision"], "deny")
            self.hub.hook(self.place, self.jade_key, 0, 0, event)  # another agent's turn: not Luca's hold
            self.assertEqual(crewchat.BROWSER["holder"]["agent"], self.luca)
            self.hub.hook(self.place, self.luca_key, 0, 0, event)
            self.assertNotIn("holder", crewchat.BROWSER, event)
            self.assertEqual(self.check(self.jade_key, *self.nav("https://ci.example.com/"))["decision"], "allow")
            self.hub.release_browser(self.jade)
        self.assertTrue(any(e["agent"] == self.luca and e["what"] == "let the browser go (its turn ended)"
                            for e in crewchat.BROWSER_LOG))
        # A browser handed over with a task (Q2), not used yet, stays the receiver's when its turn ends; so does one
        # waiting on the owner's answer.
        for kept in ({"until": time.time() + 600}, {"asking": True}):
            crewchat.BROWSER["holder"] = dict({"hub": id(self.hub), "agent": self.luca, "task": "", "since": time.time(),
                                               "last": time.time(), "project": "web"}, **kept)
            self.hub.hook(self.place, self.luca_key, 0, 0, "stop")
            self.assertEqual(crewchat.BROWSER["holder"]["agent"], self.luca, kept)

    def test_closing_its_last_tab_lets_the_browser_go(self):
        # 11 Nov: Yara closed her tab and still held the browser. A close that leaves no tab of hers open lets it go;
        # one that leaves another open keeps it.
        self.hub.set_browser_site("ci.example.com", "allowed")
        self.assertEqual(self.check(self.luca_key, *self.nav("https://ci.example.com/"))["decision"], "allow")
        left = ('Closed tab 1.\n\nTab Context:\n- Available tabs:\n  \u2022 tabId 2: "CI" ("https://ci.example.com/")')
        self.after(self.luca_key, {"tool_name": "mcp__claude-in-chrome__tabs_close_mcp", "tool_input": {"tabId": 1},
                                   "tool_response": [{"type": "text", "text": left}]})
        self.assertEqual(crewchat.BROWSER["holder"]["agent"], self.luca)  # tab 2 is still open
        self.after(self.luca_key, {"tool_name": "mcp__claude-in-chrome__tabs_close_mcp", "tool_input": {"tabId": 2},
                                   "tool_response": [{"type": "text", "text": "Closed tab 2."}]})
        self.assertNotIn("holder", crewchat.BROWSER)
        self.assertEqual(self.check(self.jade_key, *self.nav("https://ci.example.com/"))["decision"], "allow")
        # The Playwright bridge: closing its page, or its tab, lets it go too.
        for tool, args in (("mcp__browser__browser_close", {}), ("mcp__browser__browser_tabs", {"action": "close"})):
            self.assertEqual(crewchat.BROWSER["holder"]["agent"], self.jade)
            self.after(self.jade_key, {"tool_name": tool, "tool_input": args,
                                       "tool_response": [{"type": "text", "text": "Closed."}]})
            self.assertNotIn("holder", crewchat.BROWSER, tool)
            self.assertEqual(self.check(self.jade_key, *self.nav("https://ci.example.com/"))["decision"], "allow")
        self.after(self.jade_key, {"tool_name": "mcp__browser__browser_tabs", "tool_input": {"action": "list"},
                                   "tool_response": [{"type": "text", "text": "### Open tabs\n- 0: (current) [CI](https://ci.example.com/)"}]})
        self.assertEqual(crewchat.BROWSER["holder"]["agent"], self.jade)  # a tab list is not a close

    def test_the_card_of_the_agent_driving_has_let_go(self):
        # The owner freed the browser by turning Yara's browser off: her card now has Let go, which frees it only if
        # she is the one driving.
        self.hub.set_browser_site("ci.example.com", "allowed")
        self.check(self.luca_key, *self.nav("https://ci.example.com/"))
        with self.hub.lock:
            rows = {r["agent"]: r for r in self.hub._rows()}
        self.assertEqual((rows[self.luca]["browser_held"], rows[self.jade]["browser_held"]), (True, False))
        self.assertIn('op: "release", agent: a.agent', crewchat.CHAT_PAGE)
        self.assertIn("a.browser_held", crewchat.CHAT_PAGE)
        handler = mock.Mock(hub=self.hub)
        self.assertEqual(crewchat.Handler._browser(handler, {"op": "release", "agent": self.jade}), {"released": False})
        self.assertEqual(crewchat.BROWSER["holder"]["agent"], self.luca)
        version = self.hub.version
        self.assertEqual(crewchat.Handler._browser(handler, {"op": "release", "agent": self.luca}), {"released": True})
        self.assertNotIn("holder", crewchat.BROWSER)
        self.assertGreater(self.hub.version, version)  # the pages hear of it: the card loses Let go

    def test_the_pages_hear_at_once_who_drives_the_browser(self):
        # Mac retest of #134: Driving it now and Let go only showed after a reload, as taking the browser (and its hold
        # going idle) told no page. Now both change the chat's version, so a waiting poll returns with the new card.
        self.hub.set_browser_site("ci.example.com", "allowed")
        self.hub.check_sessions()
        version = self.hub.version
        self.check(self.luca_key, *self.nav("https://ci.example.com/"))
        self.assertGreater(self.hub.version, version)  # Luca took it: his card says Driving it now
        self.hub.check_sessions()
        self.check(self.luca_key, *self.nav("https://ci.example.com/job/1"))
        version = self.hub.version
        self.hub.check_sessions()
        self.assertEqual(self.hub.version, version)  # still his: nothing new to draw
        crewchat.BROWSER["holder"]["last"] -= crewchat.BROWSER_IDLE + 1  # idle: free for another, with no call
        self.hub.check_sessions()
        self.assertGreater(self.hub.version, version)  # his card loses Let go without a reload
        with self.hub.lock:
            self.assertFalse({r["agent"]: r for r in self.hub._rows()}[self.luca]["browser_held"])

    def test_a_new_site_asks_and_allow_keeps_it(self):
        self.answer_next("allow_site")
        self.assertEqual(self.check(self.luca_key, *self.nav("https://ci.example.com/job/1"))["decision"], "allow")
        self.assertEqual(self.hub.browser_sites()["ci.example.com"], "allowed")
        self.assertEqual(self.check(self.luca_key, *self.nav("https://ci.example.com/job/2"))["decision"], "allow")
        self.answer_next("deny")
        out = self.check(self.luca_key, *self.nav("https://evil.example/"))
        self.assertEqual(out["decision"], "deny")
        self.assertIn("owner", out["reason"])

    def test_always_allow_on_a_site_trusts_it(self):
        # Always allow on a change card trusts the site for the project: changes there never ask again, any task.
        self.hub.set_browser_site("ci.example.com", "allowed")
        self.check(self.luca_key, *self.nav("https://ci.example.com/job/release"))
        self.answer_next("trust_site")
        self.assertEqual(self.check(self.luca_key, *self.click())["decision"], "allow")
        self.assertEqual(self.hub.browser_sites()["ci.example.com"], "trusted")
        # With no task an allow lasts for nothing and the next change asks: trusted, it does not.
        self.assertEqual(self.check(self.luca_key, *self.click(), wait=0)["decision"], "allow")

    def test_a_change_asks_once_per_task_and_site(self):
        self.hub.set_browser_site("ci.example.com", "allowed")
        self.hub.set_browser_site("git.example.com", "allowed")
        task = self.hub.add_row(crewchat.OWNER, "Run the release build")["id"]
        self.hub.take(self.luca, task)
        self.check(self.luca_key, *self.nav("https://ci.example.com/job/release"))
        self.answer_next("allow_task")
        self.assertEqual(self.check(self.luca_key, *self.click())["decision"], "allow")
        asks = []
        with mock.patch.object(self.hub, "_browser_ask", side_effect=lambda *a, **k: asks.append(a) or "deny"):
            self.assertEqual(self.check(self.luca_key, *self.click())["decision"], "allow")  # same task and site: no ask
            self.assertEqual(asks, [])
            self.check(self.luca_key, *self.nav("https://git.example.com/"))
            self.assertEqual(self.check(self.luca_key, *self.click())["decision"], "deny")  # another site asks
            self.assertEqual(len(asks), 1)

    def test_the_browser_is_let_go_when_the_task_goes_to_review(self):
        self.hub.set_browser_site("ci.example.com", "allowed")
        task = self.hub.add_row(crewchat.OWNER, "Run the release build")["id"]
        self.hub.take(self.luca, task)
        self.check(self.luca_key, *self.nav("https://ci.example.com/"))
        self.assertEqual(crewchat.BROWSER["holder"]["task"], task)
        self.hub.update(self.luca, task, "review", "Build 412 is green")
        self.assertNotIn("holder", crewchat.BROWSER)

    def test_the_browser_is_let_go_whoever_moves_the_task_on(self):
        # Release review (C4): only the holder's own review or done let the browser go; the owner settling the task,
        # another agent sending it for review, or the holder giving it back left it held.
        self.hub.set_browser_site("ci.example.com", "allowed")
        for move in (lambda t: self.hub.owner_status(t, "done"), lambda t: self.hub.owner_status(t, "todo"),
                     lambda t: self.hub.update(self.luca, t, "todo", "Giving it back"),
                     lambda t: self.hub.update(self.jade, t, "review", "Luca's build is green")):
            task = self.hub.add_row(crewchat.OWNER, "Run the release build")["id"]
            self.hub.take(self.luca, task)
            self.check(self.luca_key, *self.nav("https://ci.example.com/"))
            self.assertEqual(crewchat.BROWSER["holder"]["task"], task)
            move(task)
            self.assertNotIn("holder", crewchat.BROWSER)
        task = self.hub.add_row(crewchat.OWNER, "Another build")["id"]
        self.hub.take(self.luca, task)
        self.check(self.luca_key, *self.nav("https://ci.example.com/"))
        self.hub.update(self.luca, task, "blocked", "Waiting for a key")  # still its own: it keeps the browser
        self.assertEqual(crewchat.BROWSER["holder"]["agent"], self.luca)

    def test_a_denied_call_does_not_take_the_browser(self):
        # Release review (C3): the caller became the holder before its site was checked, so a refused call kept the
        # browser from everyone else for ten minutes.
        self.answer_next("deny")
        self.assertEqual(self.check(self.luca_key, *self.nav("https://evil.example/"))["decision"], "deny")
        self.assertNotIn("holder", crewchat.BROWSER)
        self.hub.set_browser_site("ci.example.com", "allowed")
        self.assertEqual(self.check(self.jade_key, *self.nav("https://ci.example.com/"))["decision"], "allow")
        self.assertEqual(crewchat.BROWSER["holder"]["agent"], self.jade)
        with mock.patch.object(self.hub, "_browser_ask", return_value="deny"):
            self.assertEqual(self.check(self.jade_key, *self.click())["decision"], "deny")
        self.assertEqual(crewchat.BROWSER["holder"]["agent"], self.jade)  # it held it before: it keeps it

    def test_one_call_waits_on_the_owner_once_in_all(self):
        # Release review (C1): a call could ask about a new site and then about a change, each for the whole wait, so
        # the hook gave up first and the owner could still answer an ask nobody waited for.
        with self.hub.lock:
            self.hub.browser_site[self.luca] = "new.example.com"
            self.hub.browser_tabs[self.luca] = {"1": "new.example.com"}

        def owner():
            for _ in range(200):
                asks = [n for n in self.hub.needs_you() if n["kind"] == "browser"]
                if asks:
                    time.sleep(0.6)
                    self.hub.answer_browser(asks[0]["id"], "allow_site")
                    return
                time.sleep(0.01)
        threading.Thread(target=owner, daemon=True).start()
        started = time.time()
        out = self.check(self.luca_key, *self.click(), wait=1.0)
        self.assertLess(time.time() - started, 1.4)
        self.assertEqual(out["decision"], "deny")
        self.assertIn("did not answer in time", out["reason"])
        self.assertEqual([n for n in self.hub.needs_you() if n["kind"] == "browser"], [])

    def test_crewchats_own_page_is_never_used(self):
        # Release review (S1): sites were allowed by host alone, so allowing 127.0.0.1 for a dev server let an agent
        # open crewchat's own page, signed in as the owner, and act there unasked.
        root = self.hub.roster.root
        crewchat.save_config(dict(crewchat.load_config(root), url="https://mac.tail1234.ts.net"), root)  # tailscale serve's
        self.hub.roster.refresh(force=True)
        for site in ("127.0.0.1", "127.0.0.1:9", "localhost:9", "mac.tail1234.ts.net"):
            self.hub.set_browser_site(site, "trusted")
        with mock.patch.object(self.hub, "_browser_ask", side_effect=AssertionError("asked the owner")):
            for url in ("http://127.0.0.1:9/", "http://localhost:9/api/permission", "http://[::1]:9/",
                        "http://127.1:9/", "http://0x7f.0.0.1:9/", "http://0.0.0.0:9/", "http://LOCALHOST.:9/",
                        "http://app.localhost:9/", "https://mac.tail1234.ts.net/", "http://[::ffff:127.0.0.1]:9/"):
                with self.subTest(url=url):
                    out = self.check(self.luca_key, *self.nav(url))
                    self.assertEqual(out["decision"], "deny")
                    self.assertIn("crewchat's own page", out["reason"])
                    self.assertNotIn("holder", crewchat.BROWSER)
        # A link from a dev server's page to crewchat's: everything there is refused, reads too.
        self.hub.set_browser_site("127.0.0.1:3000", "trusted")
        self.assertEqual(self.check(self.luca_key, *self.nav("http://127.0.0.1:3000/"))["decision"], "allow")
        tab = ('\n\nTab Context:\n- Executed on tabId: 1\n- Available tabs:\n  • tabId 1: "crewchat" '
               '("http://127.0.0.1:9/")')
        self.after(self.luca_key, {"tool_name": "mcp__claude-in-chrome__computer",
                                   "tool_input": {"action": "left_click", "tabId": 1}, "tool_response": tab})
        for call in (self.click(), ("mcp__claude-in-chrome__get_page_text", {"tabId": 1}),
                     ("mcp__claude-in-chrome__computer", {"action": "screenshot", "tabId": 1})):
            out = self.check(self.luca_key, *call)
            self.assertEqual(out["decision"], "deny", call)
            self.assertIn("crewchat's own page", out["reason"])
        self.assertEqual(self.check(self.luca_key, "mcp__claude-in-chrome__tabs_close_mcp", {"tabId": 1})["decision"],
                         "allow")  # it may still close the tab
        # The bridge's page address after a read is not sure; crewchat's own page is believed all the same.
        self.check(self.luca_key, "mcp__browser__browser_navigate", {"url": "http://127.0.0.1:3000/"})
        self.after(self.luca_key, {"tool_name": "mcp__browser__browser_snapshot",
                                   "tool_response": "### Page state\n- Page URL: http://localhost:9/\n"})
        self.assertEqual(self.check(self.luca_key, "mcp__browser__browser_snapshot", {})["decision"], "deny")

    def test_a_local_site_is_its_port(self):
        # Release review (S1): on this machine a site is its host and port, so a dev server allowed on :3000 allows
        # nothing else on 127.0.0.1; elsewhere a site is its host, as before.
        site = crewchat.site_of
        self.assertEqual(site("http://127.0.0.1:3000/x"), "127.0.0.1:3000")
        self.assertEqual(site("http://localhost/"), "localhost:80")
        self.assertEqual(site("https://localhost/"), "localhost:443")
        self.assertEqual(site("https://[::1]:8080/"), "[::1]:8080")
        self.assertEqual(site("http://127.1:3000/"), "127.0.0.1:3000")  # as Chrome reads it
        self.assertEqual(site("localhost:5173"), "localhost:5173")
        self.assertEqual(site("https://Jenkins.MyCorp.com:8443/"), "jenkins.mycorp.com")
        self.assertEqual(site("http://10.0.0.5:8080/"), "10.0.0.5")
        self.assertEqual(site("http://1.2.3.4.5/"), "")  # not an address a browser opens
        self.hub.set_browser_site("http://127.0.0.1:3000", "allowed")
        self.assertIn("127.0.0.1:3000", self.hub.browser_sites())
        self.assertEqual(self.check(self.luca_key, *self.nav("http://127.0.0.1:3000/app"))["decision"], "allow")
        asked = []
        with mock.patch.object(self.hub, "_browser_ask_owner", side_effect=lambda *a: asked.append(a) or "deny"):
            self.assertEqual(self.check(self.luca_key, *self.nav("http://127.0.0.1:4000/"))["decision"], "deny")
        self.assertEqual(asked[0][2], "127.0.0.1:4000")  # the owner is asked about that port

    def test_a_session_not_in_the_chat_cannot_use_the_bridge(self):
        # Release review (S2): the bridge's token skips the extension's own approval, and a session not in the chat
        # was let through unchecked. Claude in Chrome asks in Chrome itself: it is left as it was.
        out = self.hub.browser_check(self.place, "unknown-key-123", "mcp__browser__browser_navigate",
                                     {"url": "https://x.example/"})
        self.assertEqual(out["decision"], "deny")
        self.assertIn("only an agent in the chat", out["reason"])
        self.assertEqual(self.hub.browser_check("elsewhere", self.luca_key, "browser_click", {})["decision"], "deny")
        self.assertEqual(self.hub.browser_check(self.place, "unknown-key-123", *self.nav("https://x.example/"))["decision"],
                         "allow")

    def test_a_browser_task_is_taken_only_with_the_browser_on(self):
        task = self.hub.add_row(crewchat.OWNER, "Make the video in Google Vids")["id"]
        self.hub.set_task_browser(task, True)
        plain, _ = self.agent(browser=False)
        with self.assertRaises(crewchat.HubError) as e:
            self.hub.take(plain, task)
        self.assertIn("needs the browser", str(e.exception))
        self.hub.take(self.luca, task)
        self.assertEqual(self.hub.taken[task], self.luca)
        self.assertTrue(next(r for r in self.hub.sheet_rows() if r["id"] == task)["browser"])

    def test_a_tasks_need_for_the_browser_reaches_every_machine(self):
        # Owner's decision (23 Oct): "Needs the browser" was kept on one machine; it is a plan message now, so a
        # linked machine's sheet says so, and messages.jsonl can rebuild it.
        task = self.hub.add_row(crewchat.OWNER, "Make the song in Suno")["id"]
        self.hub.set_task_browser(task, True)
        plan = self.hub.messages[-1]
        self.assertEqual((plan["kind"], plan["task"], plan["browser"]), ("plan", task, "on"))
        root = Path(tempfile.mkdtemp(prefix="browser-other-", dir=TMP))
        crewchat.save_config({"project": "web", "port": 9}, root)
        other = crewchat.Hub(crewchat.Roster(root))
        self.addCleanup(other.events.close)
        for m in self.hub.messages:
            other.ingest(dict(m, origin="mac-1"))
        self.assertTrue(other.sheet[task]["browser"])
        self.hub.set_task_browser(task, False)
        other.ingest(dict(self.hub.messages[-1], origin="mac-1"))
        self.assertFalse(other.sheet[task]["browser"])

    def test_the_holder_is_reminded_to_post_progress(self):
        self.hub.set_browser_site("ci.example.com", "allowed")
        task = self.hub.add_row(crewchat.OWNER, "Run the release build")["id"]
        self.hub.take(self.luca, task)
        now = [1000.0]
        with mock.patch.object(crewchat.time, "time", lambda: now[0]):
            self.check(self.luca_key, *self.nav("https://ci.example.com/"))
            self.assertNotIn("remind", self.hub.hook(self.place, self.luca_key, 0, 0, "tool"))
            now[0] += crewchat.BROWSER_NOTE_EVERY + 1
            self.check(self.luca_key, *self.nav("https://ci.example.com/job/412"))  # still using it
            remind = self.hub.hook(self.place, self.luca_key, 0, 0, "tool").get("remind", "")
            self.assertIn("#%s" % task, remind)
            self.assertIn("hub_task update", remind)
            self.assertNotIn("remind", self.hub.hook(self.place, self.luca_key, 0, 0, "tool"))  # once, not every call
            self.hub.update(self.luca, task, "in_progress", "Build 412 started")  # a note: the clock starts again
            now[0] += crewchat.BROWSER_NOTE_EVERY - 10
            self.assertNotIn("remind", self.hub.hook(self.place, self.luca_key, 0, 0, "tool"))
        self.assertNotIn("remind", self.hub.hook(self.place, self.jade_key, 0, 0, "tool"))  # not the holder

    def test_an_address_it_cannot_name_is_never_opened_unasked(self):
        # Final review C1: an address with no scheme, a host without a dot or an IPv6 one gave no site, and was opened
        # without asking; another scheme (file:, javascript:, chrome:) too.
        for url, host in (("evil.example.com/?d=secret", "evil.example.com"), ("http://intranet/admin", "intranet"),
                          ("http://[::1]:3000/", "::1"), ("localhost:3000", "localhost")):
            self.answer_next("deny")
            out = self.check(self.luca_key, *self.nav(url))
            self.assertEqual(out["decision"], "deny", url)
            self.assertIn(host, out["reason"], url)
        for url in ("file:///etc/passwd", "javascript:alert(1)", "chrome://settings"):
            out = self.check(self.luca_key, *self.nav(url))
            self.assertEqual(out["decision"], "deny", url)
        for url in ("", "back", "forward"):  # the previous page: no new site
            out = self.check(self.luca_key, *self.nav(url))
            self.assertEqual(out["decision"], "allow", (url, out))

    def test_an_address_a_browser_reads_otherwise_is_refused(self):
        # Release review: Python read the host after the last @, past a backslash, where Chrome stops at the backslash
        # (https://evil.com\@github.com opens evil.com): an allowed or trusted site let the agent open another unasked.
        self.hub.set_browser_site("github.com", "trusted")
        for url in ("https://evil.com\\@github.com", "evil.com\\@github.com/x", "https://evil.com\\SECRET@github.com/",
                    "https://evil.com@github.com/", "https://x:y@github.com/", "https://evil.com%2f@github.com",
                    "https://evil.com\n@github.com", "https://git\thub.com/", "https://github.com\\evil"):
            with self.subTest(url=url):
                self.assertEqual(crewchat.site_of(url), "")
                with mock.patch.object(self.hub, "_browser_ask", side_effect=AssertionError("asked about %r" % url)):
                    out = self.check(self.luca_key, *self.nav(url))
                self.assertEqual(out["decision"], "deny")
                self.assertIn("plain http or https address", out["reason"])
        for url in ("https://GitHub.com:8443/x", "github.com/a@b", "https://github.com/a\\b", "https://github.com?q=a@b"):
            self.assertEqual(crewchat.site_of(url), "github.com", url)  # after the host, anything goes
            self.assertEqual(self.check(self.luca_key, *self.nav(url))["decision"], "allow", url)

    def test_a_tool_it_does_not_know_is_a_change(self):
        # Final review C2: the bridge is @playwright/mcp@latest; a tool added there (browser_run_code, a mouse click
        # by position) was taken for a read and never asked about.
        for name in ("mcp__browser__browser_run_code", "browser_mouse_click_xy", "browser_press_sequentially",
                     "browser_something_new", "mcp__claude-in-chrome__browser_batch"):
            self.assertEqual(crewchat.browser_action(name, {})[0], "change", name)
        self.assertEqual(crewchat.browser_action("browser_navigate_forward", {}), ("navigate", ""))
        for name in ("browser_snapshot", "browser_tabs", "browser_wait_for", "mcp__claude-in-chrome__tabs_context_mcp",
                     "mcp__claude-in-chrome__switch_browser"):
            self.assertEqual(crewchat.browser_action(name, {})[0], "read", name)

    def test_the_reminder_counts_from_the_last_note_and_stops_away_from_the_browser(self):
        # Final review I1: a browser call after a note forgot the note (reminded again at once), and an agent that
        # had left the browser long ago was told it had been working in it all along.
        self.hub.set_browser_site("ci.example.com", "allowed")
        task = self.hub.add_row(crewchat.OWNER, "Run the release build")["id"]
        self.hub.take(self.luca, task)
        now = [1000.0]
        with mock.patch.object(crewchat.time, "time", lambda: now[0]):
            self.check(self.luca_key, *self.nav("https://ci.example.com/"))
            now[0] += crewchat.BROWSER_NOTE_EVERY + 1
            self.hub.update(self.luca, task, "in_progress", "Build 412 started")
            self.check(self.luca_key, *self.nav("https://ci.example.com/job/412"))
            self.assertNotIn("remind", self.hub.hook(self.place, self.luca_key, 0, 0, "tool"))
            now[0] += crewchat.BROWSER_IDLE + 1  # no browser call since: it is not working in the browser
            self.assertNotIn("remind", self.hub.hook(self.place, self.luca_key, 0, 0, "tool"))

    def test_one_look_at_the_browser_is_no_reason_to_remind(self):
        # Two-machine test (11 Nov): an agent that looked once and went on to other work was told it had been working
        # in the browser for 5 minutes. The reminder goes by its calls since the last note, not the time since.
        self.hub.set_browser_site("ci.example.com", "allowed")
        task = self.hub.add_row(crewchat.OWNER, "Run the release build")["id"]
        self.hub.take(self.luca, task)
        now = [1000.0]
        with mock.patch.object(crewchat.time, "time", lambda: now[0]):
            self.check(self.luca_key, *self.nav("https://ci.example.com/"))
            now[0] += crewchat.BROWSER_NOTE_EVERY + 60
            self.assertNotIn("remind", self.hub.hook(self.place, self.luca_key, 0, 0, "tool"))

    def test_an_allow_with_no_task_is_not_kept(self):
        # Final review I4: "Allow for this task" for an agent with no task was kept until the server stopped.
        self.hub.set_browser_site("github.com", "allowed")
        self.check(self.luca_key, *self.nav("https://github.com/"))
        self.answer_next("allow_task")
        self.assertEqual(self.check(self.luca_key, *self.click())["decision"], "allow")
        out = self.check(self.luca_key, *self.click(), wait=0.2)  # asks again: nobody answers
        self.assertEqual(out["decision"], "deny")

    def test_the_tool_hook_hands_the_reminder_to_the_agent(self):
        for client, event in (("claude", "PostToolUse"), ("gemini", "AfterTool"), ("qwen", "PostToolUse"),
                              ("codex", "PostToolUse")):
            self.assertEqual(json.loads(crewchat.remind_output(client, "post a note")),
                             {"hookSpecificOutput": {"hookEventName": event, "additionalContext": "post a note"}})
        self.assertEqual(crewchat.remind_output("cursor", "post a note"), "")  # no context: the chat tells it

    def test_the_guide_says_progress_and_no_passwords(self):
        guide = crewchat.project_guide(self.hub.roster.root)
        self.assertIn("browser", guide)
        self.assertIn("progress", guide)
        self.assertIn("password", guide)

    def test_a_trusted_site_never_asks(self):
        self.hub.set_browser_site("suno.com", "trusted")
        with mock.patch.object(self.hub, "_browser_ask", side_effect=AssertionError("asked on a trusted site")):
            self.check(self.luca_key, *self.nav("https://suno.com/create"))
            self.assertEqual(self.check(self.luca_key, *self.click())["decision"], "allow")

    def test_no_answer_in_time_is_a_deny(self):
        out = self.check(self.luca_key, *self.nav("https://slow.example/"), wait=0.1)
        self.assertEqual(out["decision"], "deny")
        self.assertIn("did not answer", out["reason"])
        self.assertEqual([n for n in self.hub.needs_you() if n["kind"] == "browser"], [])  # gone from Needs you

    def test_a_change_with_no_known_site_asks(self):
        with mock.patch.object(self.hub, "_browser_ask", return_value="deny") as ask:
            self.assertEqual(self.check(self.luca_key, *self.click())["decision"], "deny")
        self.assertEqual(ask.call_args[0][2], "")  # no site known

    def after(self, key, data):
        """A browser call's after-tool hook, as the agent's tool runs it: what it learned, to the server."""
        self.hub.hook(self.place, key, 0, 0, "tool", browser=crewchat.browser_seen(data))

    def test_a_link_to_another_site_asks_before_changes_there(self):
        # Final review I2: a link, a form or a redirect from a trusted site to another one was still counted as the
        # first site, so changes there went unasked. The page's address in each call's result is the site now.
        self.hub.set_browser_site("suno.com", "trusted")
        tab = ('\n\nTab Context:\n- Executed on tabId: 7\n- Available tabs:\n  \u2022 tabId 3: "Docs" ("https://suno.com/")'
               '\n  \u2022 tabId 7: "Sign in" ("https://accounts.google.com/signin")')
        claude = {"session_id": "s1", "tool_name": "mcp__claude-in-chrome__computer",
                  "tool_input": {"action": "left_click"},
                  "tool_response": [{"type": "text", "text": 'Clicked. Tab Context: tabId 3: "x" ("https://suno.com/")'},
                                    {"type": "text", "text": tab}]}
        self.assertEqual(crewchat.browser_seen(claude)["url"], "https://accounts.google.com/signin")
        bridge = {"session_id": "s2", "tool_name": "mcp__browser__browser_click", "tool_input": {"ref": "e4"},
                  "tool_response": [{"type": "text", "text": "### Page state\n- Page URL: https://accounts.google.com/o\n"
                                                             "- Page Snapshot:\n```yaml\n- text: Page URL: https://suno.com/\n```"}]}
        self.assertEqual(crewchat.browser_seen(bridge)["url"], "https://accounts.google.com/o")  # not the page's own text
        self.hub.set_browser_site("accounts.google.com", "allowed")  # allowed to open: changes there still ask
        for data, click in ((claude, ("mcp__claude-in-chrome__computer", {"action": "left_click", "tabId": 7})),
                            (bridge, ("mcp__browser__browser_click", {"ref": "e4"}))):
            self.check(self.luca_key, "mcp__claude-in-chrome__navigate", {"url": "https://suno.com/create", "tabId": 7})
            self.assertEqual(self.check(self.luca_key, *click)["decision"], "allow")  # trusted: no ask
            self.after(self.luca_key, data)  # the click signed in with Google
            with mock.patch.object(self.hub, "_browser_ask", return_value="deny") as ask:
                self.assertEqual(self.check(self.luca_key, *click)["decision"], "deny")
            self.assertEqual(ask.call_args[0][1:3], ("change", "accounts.google.com"))

    def test_a_link_to_a_site_never_allowed_asks_before_any_call_there(self):
        # Owner's decision (23 Oct): a link, a form or a redirect to a site not allowed asks before the agent reads
        # there too, as opening it would; reads on a site crewchat cannot tell go on.
        self.hub.set_browser_site("ci.example.com", "allowed")
        self.check(self.luca_key, *self.nav("https://ci.example.com/"))
        to_mail = {"tool_name": "mcp__claude-in-chrome__computer", "tool_input": {"action": "left_click"},
                   "tool_response": 'Tab Context:\n- Executed on tabId: 1\n- Available tabs:\n  '
                                    '\u2022 tabId 1: "Inbox" ("https://mail.google.com/mail/u/0/")'}  # a link to Gmail
        self.after(self.luca_key, to_mail)
        read = ("mcp__claude-in-chrome__get_page_text", {})
        with mock.patch.object(self.hub, "_browser_ask", return_value="deny") as ask:
            out = self.check(self.luca_key, *read)
        self.assertEqual(out["decision"], "deny")
        self.assertEqual(ask.call_args[0][1:3], ("site", "mail.google.com"))
        self.assertEqual(self.check(self.luca_key, *self.nav("https://ci.example.com/"))["decision"], "allow")  # back
        self.after(self.luca_key, to_mail)
        self.answer_next("allow_site")
        self.assertEqual(self.check(self.luca_key, *read)["decision"], "allow")
        self.assertEqual(self.hub.browser_sites()["mail.google.com"], "allowed")  # asked once: allowed from now on
        with mock.patch.object(self.hub, "_browser_ask", side_effect=AssertionError("asked again")):
            self.assertEqual(self.check(self.luca_key, *read)["decision"], "allow")
        with self.hub.lock:
            self.hub.browser_site[self.luca] = None  # a page it cannot name
        with mock.patch.object(self.hub, "_browser_ask", side_effect=AssertionError("a read asked")):
            self.assertEqual(self.check(self.luca_key, *read)["decision"], "allow")

    def test_a_refused_site_is_not_asked_about_again(self):
        # Review of the owner's decisions: after a Deny on a site a link reached, every call there asked again (a tab
        # switch too), each waiting up to 10 minutes; parallel calls asked twice; a click asked twice (site, change).
        self.hub.set_browser_site("ci.example.com", "allowed")
        to_mail = {"tool_name": "mcp__claude-in-chrome__computer", "tool_input": {"action": "left_click"},
                   "tool_response": 'Tab Context:\n- Executed on tabId: 1\n- Available tabs:\n  '
                                    '\u2022 tabId 1: "Inbox" ("https://mail.google.com/mail/u/0/")'}
        self.check(self.luca_key, *self.nav("https://ci.example.com/"))
        self.after(self.luca_key, to_mail)
        with mock.patch.object(self.hub, "_browser_ask_owner", return_value="deny") as ask:
            self.assertEqual(self.check(self.luca_key, "mcp__claude-in-chrome__get_page_text", {})["decision"], "deny")
            self.assertEqual(self.check(self.luca_key, "mcp__claude-in-chrome__read_page", {})["decision"], "deny")
            self.assertEqual(self.check(self.luca_key, *self.click())["decision"], "deny")
            self.assertEqual(self.check(self.luca_key, "mcp__claude-in-chrome__tabs_context_mcp", {})["decision"],
                             "allow")  # it may still look at its tabs and get away
        self.assertEqual(ask.call_count, 1)  # asked once
        self.assertEqual(self.check(self.luca_key, *self.nav("https://ci.example.com/"))["decision"], "allow")
        self.assertNotIn((self.luca, "mail.google.com"), self.hub.browser_refused)
        # A click on a new site reached by a link asks about the site, then the change (security review: one "Allow for
        # this task" quietly allowed the site for the whole project).
        self.after(self.luca_key, dict(to_mail, tool_response=to_mail["tool_response"].replace("mail.google", "docs.google")))
        task = self.hub.add_row(crewchat.OWNER, "Write the notes")["id"]
        self.hub.take(self.luca, task)
        with mock.patch.object(self.hub, "_browser_ask_owner", return_value="allow_task") as ask:
            self.assertEqual(self.check(self.luca_key, *self.click())["decision"], "allow")
            self.assertEqual(self.check(self.luca_key, "mcp__claude-in-chrome__get_page_text", {})["decision"], "allow")
        self.assertEqual([c[0][1] for c in ask.call_args_list], ["site", "change"])
        self.assertEqual(self.hub.browser_sites()["docs.google.com"], "allowed")

    def test_another_tab_does_not_get_around_the_gate(self):
        # Security review: the gate kept one site per agent, so after a Deny on tab 1 an agent opened an allowed site
        # in tab 2 and read or clicked in tab 1 unasked. Claude in Chrome's calls name their tab: each is checked as
        # it is. The bridge works in its current tab, which its tab list marks.
        self.hub.set_browser_site("ci.example.com", "trusted")
        tabs = ('Tab Context:\n- Executed on tabId: %s\n- Available tabs:\n  \u2022 tabId 1: "Inbox" '
                '("https://mail.google.com/mail/u/0/")\n  \u2022 tabId 2: "CI" ("https://ci.example.com/")')
        self.check(self.luca_key, "mcp__claude-in-chrome__navigate", {"url": "https://ci.example.com/", "tabId": 1})
        self.after(self.luca_key, {"tool_name": "mcp__claude-in-chrome__computer",
                                   "tool_input": {"action": "left_click", "tabId": 1}, "tool_response": tabs % 1})
        with mock.patch.object(self.hub, "_browser_ask_owner", return_value="deny"):
            self.assertEqual(self.check(self.luca_key, "mcp__claude-in-chrome__get_page_text", {"tabId": 1})["decision"],
                             "deny")
        self.check(self.luca_key, "mcp__claude-in-chrome__navigate", {"url": "https://ci.example.com/", "tabId": 2})
        self.after(self.luca_key, {"tool_name": "mcp__claude-in-chrome__navigate", "tool_input": {"tabId": 2},
                                   "tool_response": tabs % 2})
        with mock.patch.object(self.hub, "_browser_ask_owner", return_value="deny") as ask:
            for call in (("mcp__claude-in-chrome__get_page_text", {"tabId": 1}),
                         ("mcp__claude-in-chrome__computer", {"action": "left_click", "tabId": 1}),
                         ("mcp__claude-in-chrome__form_input", {"tabId": 1, "ref": "r", "value": "x"})):
                self.assertEqual(self.check(self.luca_key, *call)["decision"], "deny", call)
            self.assertEqual(self.check(self.luca_key, "mcp__claude-in-chrome__get_page_text", {"tabId": 2})["decision"],
                             "allow")  # its own tab, on the trusted site
        self.assertTrue(all(c[0][2] == "mail.google.com" for c in ask.call_args_list))
        # The bridge: a switch to another tab moves its page to that tab's site, read from its tab list.
        listed = {"tool_name": "mcp__browser__browser_tabs", "tool_input": {"action": "select", "index": 0},
                  "tool_response": "### Open tabs\n- 0: (current) [Inbox (current) [x](https://ci.example.com/)]"
                                   "(https://mail.google.com/)\n- 1: [CI](https://ci.example.com/)\n"}
        self.assertEqual(crewchat.browser_seen(listed)["url"], "https://mail.google.com/")  # the title cannot say
        self.check(self.luca_key, *self.nav("https://ci.example.com/"))
        self.after(self.luca_key, listed)
        self.assertEqual(self.hub.browser_site[self.luca], "mail.google.com")
        with mock.patch.object(self.hub, "_browser_ask_owner", return_value="deny"):
            self.assertEqual(self.check(self.luca_key, "mcp__browser__browser_snapshot", {})["decision"], "deny")

    def test_a_browser_of_the_users_own_does_not_move_the_agents_site(self):
        # Review of the owner's decisions: the after-tool hook still reported the pages of the user's own Playwright
        # browser (a folder without the bridge), and they set the agent's site in the owner's Chrome.
        name, key = self.agent()
        folder = Path(tempfile.mkdtemp(prefix="own-pages-", dir=TMP)).resolve()
        crewchat.install_client("gemini", folder, "http://127.0.0.1:9", "tok")
        with self.hub.lock:
            self.hub.agents[name].update(client="gemini", session={"folder": str(folder)})
            self.hub.browser_site[name] = "ci.example.com"
        own = {"tool_name": "browser_click", "tool_response": "### Page state\n- Page URL: https://example.org/\n"}
        self.after(key, own)
        self.assertEqual(self.hub.browser_site[name], "ci.example.com")
        old_bridge(folder, "gemini")  # with the bridge from before the proxy there, the same names are its
        self.after(key, own)
        self.assertEqual(self.hub.browser_site[name], "example.org")
        crewchat.sync_browser_server(folder, "bridge-secret-123")  # R8: the proxy says where the browser is
        self.after(key, dict(own, tool_response="- Page URL: https://other.example.net/\n"))
        self.assertEqual(self.hub.browser_site[name], "example.org")

    def test_every_tab_listed_is_known_and_a_refusal_stays_while_its_tab_is_open(self):
        # Engineering review of the decisions: a tab list with no "Executed on" line (tabs_context_mcp) was dropped,
        # so a tab on a site never allowed was read unasked; a navigation in another tab cleared a refusal for a site
        # still open; a page title could pose as the "Executed on" line; a console line could pose as the bridge's
        # tab list.
        self.hub.set_browser_site("ci.example.com", "allowed")
        self.check(self.luca_key, "mcp__claude-in-chrome__navigate", {"url": "https://ci.example.com/", "tabId": 1})
        listing = ('Tab Context:\n- Available tabs:\n  \u2022 tabId 1: "CI" ("https://ci.example.com/")\n'
                   '  \u2022 tabId 2: "Executed on tabId: 1" ("https://evil.example/")')
        seen = crewchat.browser_seen({"tool_name": "mcp__claude-in-chrome__tabs_context_mcp", "tool_response": listing})
        self.assertEqual((seen["tab"], seen["url"]), ("", ""))  # the title does not say which tab acted
        self.after(self.luca_key, {"tool_name": "mcp__claude-in-chrome__tabs_context_mcp", "tool_response": listing})
        read2 = ("mcp__claude-in-chrome__get_page_text", {"tabId": 2})
        with mock.patch.object(self.hub, "_browser_ask_owner", return_value="deny") as ask:
            self.assertEqual(self.check(self.luca_key, *read2)["decision"], "deny")
        self.assertEqual(ask.call_args[0][2], "evil.example")
        self.check(self.luca_key, "mcp__claude-in-chrome__navigate", {"url": "https://ci.example.com/x", "tabId": 1})
        with mock.patch.object(self.hub, "_browser_ask_owner", side_effect=AssertionError("asked again")):
            self.assertEqual(self.check(self.luca_key, *read2)["decision"], "deny")  # tab 2 is still on it
        console = {"tool_name": "mcp__browser__browser_tabs", "tool_input": {"action": "list"},
                   "tool_response": "### Result\n- [LOG] x\n- 0: (current) [x](https://ci.example.com/)\n"}
        self.assertEqual(crewchat.browser_seen(console)["sure"], False)  # no "### Open tabs": no tab list

    def test_calls_made_at_once_wait_on_one_question(self):
        out = []
        calls = [threading.Thread(target=lambda: out.append(self.check(self.luca_key, *self.nav("https://new.example/"),
                                                                       wait=5))) for _ in range(2)]
        for t in calls:
            t.start()
        for _ in range(200):
            asks = [n for n in self.hub.needs_you() if n["kind"] == "browser"]
            if asks:
                break
            time.sleep(0.01)
        time.sleep(0.2)  # both calls are waiting now
        self.assertEqual(len([n for n in self.hub.needs_you() if n["kind"] == "browser"]), 1)
        self.hub.answer_browser(asks[0]["id"], "allow_site")
        for t in calls:
            t.join(5)
        self.assertEqual([o["decision"] for o in out], ["allow", "allow"])

    def test_a_change_whose_page_cannot_be_told_forgets_the_site(self):
        self.hub.set_browser_site("suno.com", "trusted")
        self.check(self.luca_key, *self.nav("https://suno.com/create"))
        self.after(self.luca_key, {"tool_name": "mcp__claude-in-chrome__get_page_text", "tool_response": "text"})
        self.assertEqual(self.hub.browser_site[self.luca], "suno.com")  # a read that says nothing keeps it
        # A click on tab 1 whose result names no page (engineering review: the tab kept its old site).
        self.after(self.luca_key, {"tool_name": "mcp__claude-in-chrome__computer",
                                   "tool_input": {"action": "left_click", "tabId": 1}, "tool_response": "Clicked"})
        with mock.patch.object(self.hub, "_browser_ask", return_value="deny") as ask:
            self.assertEqual(self.check(self.luca_key, *self.click())["decision"], "deny")
        self.assertEqual(ask.call_args[0][2], "")
        self.check(self.luca_key, "browser_navigate", {"url": "https://suno.com/"})  # OpenCode's bridge: the same
        self.after(self.luca_key, {"tool_name": "browser_click", "tool_response": "Clicked"})
        with mock.patch.object(self.hub, "_browser_ask", return_value="deny") as ask:
            self.assertEqual(self.check(self.luca_key, "browser_click", {})["decision"], "deny")
        self.assertEqual(ask.call_args[0][2], "")
        self.after(self.luca_key, {"tool_name": "mcp__claude-in-chrome__tabs_context_mcp", "tool_response":
                                   'Tab Context:\n- Available tabs:\n  \u2022 tabId 1: "Suno" ("https://suno.com/x")'})
        self.assertEqual(self.check(self.luca_key, *self.click())["decision"], "allow")  # back on a known site
        self.assertIsNone(crewchat.browser_seen({"tool_name": "Bash", "tool_response": "Page URL: https://x.example/"}))

    def test_a_pages_own_text_cannot_name_its_site(self):
        # Release review: a page that logs "- Page URL: https://trusted..." made a read's result name a trusted site,
        # and the next change there went unasked.
        self.hub.set_browser_site("suno.com", "trusted")
        self.hub.set_browser_site("evil.example", "allowed")
        self.check(self.luca_key, *self.nav("https://evil.example/"))
        console = {"tool_name": "mcp__browser__browser_console_messages",
                   "tool_response": "### Result\n- [LOG] hello\n- Page URL: https://suno.com/\n"}
        self.after(self.luca_key, console)
        self.assertIsNone(self.hub.browser_site[self.luca])  # a read only takes it off its site
        self.check(self.luca_key, *self.nav("https://evil.example/"))
        click = {"tool_name": "mcp__browser__browser_click", "tool_response": "### New console messages\n- [LOG] x\n"
                 "- Page URL: https://suno.com/\n### Page state\n- Page URL: https://evil.example/\n"}
        self.assertEqual(crewchat.browser_seen(click)["url"], "about:unclear")  # two addresses: none
        self.after(self.luca_key, click)
        self.assertIsNone(self.hub.browser_site[self.luca])
        with mock.patch.object(self.hub, "_browser_ask", return_value="deny") as ask:
            self.assertEqual(self.check(self.luca_key, "mcp__browser__browser_click", {})["decision"], "deny")
        self.assertEqual(ask.call_args[0][2], "")

    def test_an_agent_waiting_for_the_owner_keeps_the_browser(self):
        # Release review: the holder's last call aged while it waited for the owner, so another agent took the
        # browser and both drove it once the owner answered.
        now = [1000.0]
        started = threading.Event()

        def slow_owner(*args):
            started.set()
            now[0] += crewchat.BROWSER_IDLE + 60  # the owner answers late
            time.sleep(0.2)
            return "allow_site"
        with mock.patch.object(crewchat.time, "time", lambda: now[0]), \
                mock.patch.object(self.hub, "_browser_ask_owner", side_effect=slow_owner):
            got = []
            t = threading.Thread(target=lambda: got.append(self.check(self.luca_key, *self.nav("https://new.example/"))))
            t.start()
            started.wait(5)
            time.sleep(0.05)
            out = self.check(self.jade_key, *self.nav("https://suno.com/"))
            t.join(5)
        self.assertEqual(out["decision"], "deny")
        self.assertIn("%s is using the browser" % self.luca, out["reason"])
        self.assertEqual(got[0]["decision"], "allow")

    def test_the_tool_hook_tells_the_server_every_browser_call(self):
        # Even within a minute of its last sign of life: where the page went is news every time.
        session = "browser-" + crewchat.secrets.token_hex(4)
        state = Path(tempfile.gettempdir()) / "crewchat-hooks" / ("claude-%s" % crewchat.sha(session)[:16])
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"key": self.luca_key, "alive": time.time(), "doing": "Reading", "doing_at": time.time()}))
        self.addCleanup(lambda: state.unlink(missing_ok=True))
        sent = []
        with mock.patch.object(crewchat, "find_project", return_value=Path(TMP)), \
                mock.patch.object(crewchat, "client_config", return_value=("http://127.0.0.1:9", "tok")), \
                mock.patch.object(crewchat, "hooks_off", return_value=False), \
                mock.patch.object(crewchat, "hook_post", side_effect=lambda url, token, body: sent.append(body) or {}):
            crewchat.tool_hook("claude", {"session_id": session, "tool_name": "Read", "tool_input": {}})
            self.assertEqual(sent, [])
            crewchat.tool_hook("claude", {"session_id": session, "tool_name": "mcp__browser__browser_navigate",
                                          "tool_input": {"url": "https://suno.com/"},
                                          "tool_response": "- Page URL: https://accounts.google.com/"})
        self.assertEqual(sent[0]["browser"], {"tool": "mcp__browser__browser_navigate", "action": "",
                                              "url": "https://accounts.google.com/", "sure": False, "tab": "",
                                              "tabs": {}})


class BinaryOnWindows(unittest.TestCase):
    """Release review: os.open on Windows opens in text mode unless asked, so Python's newline translation doubled
    every line end (messages.jsonl, hub.log: \\r\\r\\n) and a shared file's byte 10 became 13 10. Every file crewchat
    writes through os.open asks for binary mode (O_BINARY, nothing elsewhere)."""

    def test_every_writer_asks_for_binary_mode(self):
        folder = Path(tempfile.mkdtemp(prefix="binary-", dir=TMP))
        flags = []
        real = os.open

        def opened(path, flag, *mode):
            flags.append(flag)
            return real(path, flag & ~0x8000, *mode)
        with mock.patch.object(crewchat, "O_BINARY", 0x8000), mock.patch.object(crewchat.os, "open", opened):
            crewchat.write_text(folder / "a.json", "{}\n")
            with crewchat.open_private(folder / "b.jsonl") as f:
                f.write("x\n")
            crewchat.ensure_token(crewchat.OWNER, folder)
        self.assertEqual(len(flags), 3)
        self.assertTrue(all(f & 0x8000 for f in flags), flags)
        self.assertEqual(crewchat.O_BINARY, getattr(os, "O_BINARY", 0))
        self.assertNotIn("os.O_WRONLY |", "".join(line for line in Path(crewchat.__file__).read_text(encoding="utf-8").splitlines()
                                                  if "os.open(" in line and "O_BINARY" not in line))


class NewTokens(unittest.TestCase):
    def test_start_says_to_use_new_agent_where_only_crewchats_agents_join(self):
        own = "\n".join(crewchat.start_advice("own", ["claude", "cursor"], "this folder", "with a first name"))
        self.assertIn("+ New agent on the chat page", own)
        self.assertNotIn("joins the chat by itself", own)
        anyone = "\n".join(crewchat.start_advice("anyone", ["claude"], "this folder", "with a first name"))
        self.assertIn("Each new session joins the chat by itself", anyone)

    def test_a_token_never_starts_with_a_dash(self):
        # `crewchat stdio --url URL --token TOKEN` takes a token that starts with "-" for an option (1 in 64 do): it
        # exits with "expected one argument" and the client sees a server that never answers.
        drawn = iter(["-nd2gVp8Ge9wauDGHjahoOo964hovCbcu3roS5oGXB0", "-x", "Qd2gVp8Ge9wauDGHjahoOo964hovCbcu3roS5oGXB0"])
        with mock.patch.object(crewchat.secrets, "token_urlsafe", side_effect=lambda n: next(drawn)):
            with tempfile.TemporaryDirectory() as tmp:
                crewchat.ensure_token(crewchat.OWNER, Path(tmp))
                token = crewchat.read_token(crewchat.OWNER, Path(tmp))
        self.assertEqual(token, "Qd2gVp8Ge9wauDGHjahoOo964hovCbcu3roS5oGXB0")
        parsed = crewchat.build_parser().parse_args(["stdio", "--url", "http://127.0.0.1:1", "--token", token])
        self.assertEqual(parsed.token, token)


class RememberedMachines(unittest.TestCase):
    """Owner's decision (23 Oct): a linked machine that is off is judged by what it last said, kept on disk
    (older_machines). If that included the `for` mark, it is not older. keep_info(id, None) stores an empty mark
    record (back on an older crewchat, which says nothing) and that machine stays older. A damaged machines file
    stays older, the safe side. A machine never heard from has no record and is not older, so it does not turn the
    advisor excerpt off."""

    class Link(crewchat.ProjectLink):
        def __init__(self, root, chat=None):
            self._init_projects(types.SimpleNamespace(roster=types.SimpleNamespace(root=root)), chat)

        def linked_ids(self):
            return ["laptop", "desktop"]

    def test_a_machine_that_is_off_is_judged_by_what_it_last_said(self):
        root = Path(tempfile.mkdtemp(prefix="remembered-", dir=TMP))
        link = self.Link(root)
        self.assertEqual(link.older_machines(), [])  # nothing written yet: not older
        link.keep_info("laptop", {"version": "1.1.0", "marks": ["for"], "projects": [{"name": "web"}]})
        self.assertEqual(link.older_machines(), [])  # desktop has no record; laptop keeps the mark
        again = self.Link(root)  # this server restarted; the laptop is off
        self.assertEqual(again.older_machines(), [])
        self.assertIn("for", again.info_of("laptop")["marks"])
        self.assertEqual((again.info_of("laptop")["version"], again.info_of("laptop")["projects"]), ("1.1.0", []))
        (root / crewchat.MACHINES_FILE).write_text('{"laptop": {"version": 3, "marks": 5}, "desktop": [1]}')
        self.assertEqual(self.Link(root).older_machines(), ["laptop", "desktop"])  # a damaged file: no crash, unknown
        self.Link(root).keep_info("laptop", {"version": "1.1.0", "marks": ["for"]})  # it reports again
        again = self.Link(root)
        self.assertEqual(again.older_machines(), [])  # sound file again; desktop's bad entry was not kept
        again.keep_info("laptop", None)  # back on an older crewchat, which says nothing of itself
        saved = json.loads((root / crewchat.MACHINES_FILE).read_text())
        self.assertEqual(saved.get("laptop"), {"version": "", "marks": []})  # empty record, not a missing key
        self.assertNotIn("desktop", saved)
        self.assertEqual(self.Link(root).older_machines(), ["laptop"])
        self.Link(root, chat="other").keep_info("desktop", {"version": "1.1.0", "marks": ["for"]})
        many = self.Link(root)  # each peer's puller reports on its own thread
        ids = ["m%d" % i for i in range(40)]
        threads = [threading.Thread(target=many.keep_info, args=(i, {"version": "1.1.0", "marks": ["for"]})) for i in ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        saved = json.loads((root / crewchat.MACHINES_FILE).read_text())
        self.assertEqual(sorted(saved), sorted(ids + ["laptop"]))
        self.assertNotIn("desktop", saved)  # only the machine's link keeps them

    def test_a_fresh_link_with_nothing_written_is_not_older(self):
        """Two linked machines and no machines file: never heard from, so not older. A file that is not JSON, or
        not an object, is damaged and both are older."""
        root = Path(tempfile.mkdtemp(prefix="unheard-", dir=TMP))
        self.assertFalse((root / crewchat.MACHINES_FILE).exists())
        self.assertEqual(self.Link(root).older_machines(), [])
        (root / crewchat.MACHINES_FILE).write_text("{")
        self.assertEqual(self.Link(root).older_machines(), ["laptop", "desktop"])
        (root / crewchat.MACHINES_FILE).write_text("[]")
        self.assertEqual(self.Link(root).older_machines(), ["laptop", "desktop"])


class HotPathCaches(unittest.TestCase):
    """bridge_here / bridge_set_up and claude_accounts are read on hot paths. The answer stays until the file that
    was read changes (mtime, inode, size), so a settings change or a new account is seen on the next call and an
    unchanged file is not opened again."""

    def _reads(self, path):
        seen = []
        real = Path.read_text

        def counting(self, *args, **kwargs):
            if Path(self) == Path(path):
                seen.append(1)
            return real(self, *args, **kwargs)

        return seen, counting

    def test_the_bridge_is_reread_when_its_settings_change(self):
        folder = Path(tempfile.mkdtemp(prefix="bridge-cache-", dir=TMP))
        rel, servers = crewchat.BRIDGE_FILES["cursor"]
        path = folder / rel
        path.parent.mkdir(parents=True)
        bare = {servers: {}}
        bridged = {servers: {"browser": {"command": "npx", "env": {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": "t"}}}}
        path.write_text(json.dumps(bare))
        seen, counting = self._reads(path)
        with mock.patch.object(Path, "read_text", counting):
            self.assertFalse(crewchat.bridge_here(folder, "cursor", "browser_click"))
            self.assertEqual(len(seen), 1)
            self.assertFalse(crewchat.bridge_here(folder, "cursor", "browser_click"))
            self.assertEqual(len(seen), 1)  # unchanged: not opened again
            path.write_text(json.dumps(bridged))
            self.assertTrue(crewchat.bridge_here(folder, "cursor", "browser_click"))  # the gate must see it
            self.assertEqual(len(seen), 2)
            path.write_text(json.dumps(bare))
            self.assertFalse(crewchat.bridge_here(folder, "cursor", "browser_click"))
            self.assertEqual(len(seen), 3)

    def test_codexs_bridge_is_reread_when_its_settings_change(self):
        home = Path(tempfile.mkdtemp(prefix="codex-cache-", dir=TMP))
        path = home / "config.toml"
        path.write_text("# no bridge\n")
        block = "\n%s\n%s\n%s\n" % (crewchat.CODEX_BEGIN, crewchat.CODEX_BRIDGE, crewchat.CODEX_END)
        seen, counting = self._reads(path)
        with mock.patch.object(crewchat, "codex_config", return_value=path), \
                mock.patch.object(Path, "read_text", counting):
            self.assertFalse(crewchat.bridge_set_up(None, "codex"))
            self.assertEqual(len(seen), 1)
            self.assertFalse(crewchat.bridge_here(None, "codex", "browser_click"))
            self.assertEqual(len(seen), 1)
            path.write_text(block)
            self.assertTrue(crewchat.bridge_here(None, "codex", "browser_click"))
            self.assertEqual(len(seen), 2)

    @unittest.skipIf(os.name == "nt", "a folder's stamp moves when a folder is made in it")
    def test_a_new_claude_account_is_seen_and_an_unchanged_home_is_not_listed_again(self):
        home = Path(tempfile.mkdtemp(prefix="acct-cache-", dir=TMP))
        listed = []
        real = Path.iterdir

        def counting(self):
            if Path(self) == home:
                listed.append(1)
            return real(self)

        with mock.patch.object(Path, "iterdir", counting):
            self.assertEqual([a["id"] for a in crewchat.claude_accounts({}, home)], [""])
            self.assertEqual(len(listed), 1)
            self.assertEqual([a["id"] for a in crewchat.claude_accounts({}, home)], [""])
            self.assertEqual(len(listed), 1)  # unchanged: the home is not listed again
            work = home / ".claude-work"
            work.mkdir()
            self.assertEqual([a["id"] for a in crewchat.claude_accounts({}, home)], [""])  # no sign-in yet
            self.assertEqual(len(listed), 2)
            (work / ".claude.json").write_text("{}")
            self.assertEqual([a["id"] for a in crewchat.claude_accounts({}, home)], ["", "work"])
            self.assertEqual(len(listed), 3)
            self.assertEqual([a["id"] for a in crewchat.claude_accounts({}, home)], ["", "work"])
            self.assertEqual(len(listed), 3)
            added = crewchat.claude_accounts({"claude_accounts": {"extra": str(work)}}, home)
            self.assertEqual([a["id"] for a in added], ["", "work", "extra"])  # config is applied without listing
            self.assertEqual(len(listed), 3)

    def test_on_windows_a_new_claude_account_is_seen_within_seconds(self):
        # 1.1.0 review: on Windows a folder's stamp does not move when a folder is made in it, so the home listing
        # was kept for good and a new ~/.claude-work was never seen. There the listing lasts a few seconds.
        home = Path(tempfile.mkdtemp(prefix="acct-win-", dir=TMP))
        real_stamp, now = crewchat._file_stamp, [1000.0]

        def windows_stamp(path):  # what Windows reports: the home's stamp never changes
            return ("home",) if Path(path) == home else real_stamp(path)

        with mock.patch.object(crewchat, "_file_stamp", windows_stamp), \
                mock.patch.object(crewchat, "HOME_STAMP_MOVES", False), \
                mock.patch.object(crewchat.time, "monotonic", lambda: now[0]):
            self.assertEqual([a["id"] for a in crewchat.claude_accounts({}, home)], [""])
            work = home / ".claude-work"
            work.mkdir()
            (work / ".claude.json").write_text("{}")
            self.assertEqual([a["id"] for a in crewchat.claude_accounts({}, home)], [""])  # the listing still holds
            now[0] += crewchat.ACCOUNT_LIST_TTL + 0.1
            self.assertEqual([a["id"] for a in crewchat.claude_accounts({}, home)], ["", "work"])


class DevAndQaShare(unittest.TestCase):
    """Q1-Q3 (owner, 11 Nov 2026): QA sends a task back to its developer with what failed and screenshots, and the
    developer sends it back fixed; with live, QA's browser page or emulator goes with it, as it is, and nobody takes it
    in between. Across linked machines the address goes, and the browser there is kept for the receiver."""

    def setUp(self):
        crewchat.BROWSER.clear()
        crewchat.DEVICES.clear()
        root = Path(tempfile.mkdtemp(prefix="devqa-", dir=TMP))
        crewchat.save_config({"project": "web", "port": 9}, root)
        self.hub = crewchat.Hub(crewchat.Roster(root))
        self.dev, self.dev_sid, self.dev_key = self.agent()
        self.qa, self.qa_sid, self.qa_key = self.agent()
        self.other, _, self.other_key = self.agent()
        self.place = self.hub.agents[self.dev]["place"]
        self.hub.set_browser_site("shop.example.com", "allowed")
        self.task = self.hub.add_row(crewchat.OWNER, "Fix the checkout button")["id"]
        self.hub.take(self.dev, self.task)

    def tearDown(self):
        self.hub.events.close()
        crewchat.BROWSER.clear()
        crewchat.DEVICES.clear()

    def agent(self):
        sid = self.hub.open_session("mac", "claude")
        name = self.hub.agent_for(sid)
        key = crewchat.secrets.token_urlsafe(12)
        with self.hub.lock:
            self.hub.links[key] = name
            self.hub.agents[name]["browser"] = True
            self.hub.agents[name]["mobile"] = True
        return name, sid, key

    def nav(self, key, url):
        return self.hub.browser_check(self.place, key, "mcp__claude-in-chrome__navigate", {"url": url, "tabId": 1},
                                      wait=1)

    def inbox_line(self, name):
        return "\n".join(crewchat.fmt(m, name) for m in self.hub.inbox(name, 0, True))

    def test_a_new_holder_or_done_starts_the_rounds_again(self):
        # Review: rounds and returned_by were never reset, so a task given to someone else (or done and reopened)
        # went to the lead as blocked after fewer rounds than MAX_RETURNS.
        for _ in range(crewchat.MAX_RETURNS):
            self.hub.return_task(self.qa, self.task, "Still broken")
        self.hub.update(self.dev, self.task, "todo", "Giving it back")
        self.hub.take(self.other, self.task)
        row = self.hub.sheet[self.task]
        self.assertEqual((row.get("rounds"), row.get("returned_by")), (None, None))
        msg = self.hub.return_task(self.qa, self.task, "Broken for you too")
        self.assertEqual((msg["to"], msg["status"], msg["back"]), (self.other, "in_progress", "1"))
        self.hub.update(self.other, self.task, "done", "Fixed\nDecisions: none\nLeft over: none")
        self.assertNotIn("rounds", self.hub.sheet[self.task])

    def test_qa_sends_it_back_and_the_developer_sends_it_back_fixed(self):
        shot = {"id": "f1", "name": "checkout.png", "type": "image/png", "size": 2048}
        msg = self.hub.return_task(self.qa, self.task, "Tap Pay: nothing happens. Expected the receipt.", [shot])
        self.assertEqual((msg["to"], msg["status"], msg["back"]), (self.dev, "in_progress", "1"))
        row = self.hub.sheet[self.task]
        self.assertEqual((row["rounds"], row["returned_by"]), (1, self.qa))
        said = self.inbox_line(self.dev)
        self.assertIn("[SENT BACK #%s, round 1 of 3]" % self.task, said)
        self.assertIn("checkout.png", said)
        self.assertIn("hub_task action=return id=%s" % self.task, said)
        self.assertEqual(self.hub.taken[self.task], self.dev)  # still the developer's
        # Fixed: back to whoever sent it, without naming them, ready to test again.
        back = self.hub.return_task(self.dev, self.task, "The button calls pay() now.")
        self.assertEqual((back["to"], back["status"]), (self.qa, "review"))
        self.assertIn("[READY TO TEST AGAIN #%s]" % self.task, self.inbox_line(self.qa))
        self.assertEqual(self.hub.sheet[self.task]["rounds"], 1)  # only QA's sends count
        rows = {r["id"]: r for r in self.hub.sheet_rows()}
        self.assertEqual(rows[self.task]["rounds"], 1)  # the page shows it

    def test_who_may_send_it_back_and_to_whom(self):
        with self.assertRaises(crewchat.HubError) as said:
            self.hub.return_task(self.dev, self.task, "Ready")  # nobody tested it yet: name the tester
        self.assertIn("who tests it next", str(said.exception))
        self.assertEqual(self.hub.return_task(self.dev, self.task, "Ready", to=self.qa.upper())["to"], self.qa)
        with self.assertRaises(crewchat.HubError):
            self.hub.return_task(self.qa, self.task, "Broken", to=self.other)  # it goes to its holder only
        free = self.hub.add_row(crewchat.OWNER, "Nobody holds this")["id"]
        with self.assertRaises(crewchat.HubError):
            self.hub.return_task(self.qa, free, "Broken")

    def test_after_three_rounds_it_goes_to_the_owner_as_stuck(self):
        for n in range(crewchat.MAX_RETURNS):
            self.assertEqual(self.hub.return_task(self.qa, self.task, "Still broken")["to"], self.dev)
            self.hub.return_task(self.dev, self.task, "Fixed again")
        stuck = self.hub.return_task(self.qa, self.task, "Still broken", live=True)
        self.assertEqual((stuck["to"], stuck["status"]), (crewchat.OWNER, "blocked"))
        self.assertIn("after 3 rounds", stuck["text"])
        self.assertTrue(any(n["kind"] == "blocked" and n["task"] == self.task for n in self.hub.needs_you()))

    def test_after_three_rounds_it_goes_to_the_lead_when_there_is_one(self):
        self.hub.set_role(crewchat.OWNER, self.other, "lead")
        with self.hub.lock:
            self.hub.sheet[self.task]["rounds"] = crewchat.MAX_RETURNS
        self.assertEqual(self.hub.return_task(self.qa, self.task, "Still broken")["to"], self.other)

    def test_live_hands_qa_s_browser_page_to_the_developer(self):
        self.assertEqual(self.nav(self.qa_key, "https://shop.example.com/cart?item=7")["decision"], "allow")
        now = [time.time()]
        with mock.patch.object(crewchat.time, "time", lambda: now[0]):
            msg = self.hub.return_task(self.qa, self.task, "Pay does nothing", live=True)
            self.assertEqual(msg["page"], "https://shop.example.com/cart?item=7")
            holder = crewchat.BROWSER["holder"]
            self.assertEqual((holder["agent"], holder["task"], holder["handed_by"]), (self.dev, self.task, self.qa))
            self.assertIn("handed you the browser on https://shop.example.com/cart?item=7", self.inbox_line(self.dev))
            now[0] += crewchat.BROWSER_IDLE + 60  # the developer has not called yet: kept for it all the same
            refused = self.nav(self.other_key, "https://shop.example.com/")
            self.assertEqual(refused["decision"], "deny")
            self.assertIn(self.dev, refused["reason"])
            self.assertEqual(self.nav(self.qa_key, "https://shop.example.com/")["decision"], "deny")  # QA let it go
            self.assertEqual(self.nav(self.dev_key, msg["page"])["decision"], "allow")
            self.assertNotIn("until", crewchat.BROWSER["holder"])  # from its first call, the usual rule
            # Fixed, and handed back to QA the same way; the developer's hold for the task ends with its review.
            back = self.hub.return_task(self.dev, self.task, "Fixed", live=True)
            self.assertEqual(back["page"], "https://shop.example.com/cart?item=7")
            self.assertEqual(crewchat.BROWSER["holder"]["agent"], self.qa)
            now[0] += crewchat.HAND_HOLD + 1
            self.assertEqual(self.nav(self.other_key, "https://shop.example.com/")["decision"], "allow")

    def test_qa_sending_a_task_back_is_asked_for_a_fact_and_the_developer_is_not(self):
        out = crewchat.call_tool(self.hub, self.qa_sid, "hub_task", {"action": "return", "id": self.task,
                                                                     "note": "Login fails while the VPN is on"})
        self.assertIn("record it as a fact: hub_process propose with note kind fact", out)  # L8
        back = crewchat.call_tool(self.hub, self.dev_sid, "hub_task", {"action": "return", "id": self.task,
                                                                       "note": "Fixed"})
        self.assertNotIn("as a fact", back)

    def test_live_with_nothing_held_sends_the_note_only(self):
        out = crewchat.call_tool(self.hub, self.qa_sid, "hub_task", {"action": "return", "id": self.task,
                                                                     "note": "Broken", "live": True})
        self.assertIn("only the note went", out)
        self.assertNotIn("holder", crewchat.BROWSER)
        self.assertIn("round 1 of 3", out)

    def test_live_hands_qa_s_emulator_to_the_developer(self):
        now = time.time()
        crewchat.DEVICES["emulator-5554"] = {"hub": id(self.hub), "agent": self.qa, "task": "", "since": now,
                                             "last": now, "project": "web"}
        with self.hub.lock:
            self.hub.device_app[(self.qa, "emulator-5554")] = "com.example.shop"
        msg = self.hub.return_task(self.qa, self.task, "The cart is empty after login", live=True)
        self.assertEqual((msg["device"], msg["app"]), ("emulator-5554", "com.example.shop"))
        held = crewchat.DEVICES["emulator-5554"]
        self.assertEqual((held["agent"], held["handed_by"]), (self.dev, self.qa))
        self.assertIn("the device emulator-5554, com.example.shop in front", self.inbox_line(self.dev))

    def test_across_machines_the_address_goes_and_the_browser_there_is_kept(self):
        # Received from QA on another machine: this machine's free browser is kept for the developer, and a device
        # id, which names another device here, keeps nothing.
        self.hub.set_remote("m-far", "laptop", [{"agent": "far-qa", "client": "claude", "place": "x"}], time.time())
        self.assertTrue(self.hub.ingest({
            "id": "far-1", "ts": time.time(), "from": "far-qa", "to": self.dev, "kind": "update",
            "text": "Pay does nothing", "task": self.task, "status": "in_progress", "back": "1",
            "page": "https://shop.example.com/cart", "device": "emulator-5554", "app": "com.example.shop",
            "machine": "laptop"}))
        self.assertEqual((crewchat.BROWSER["holder"]["agent"], crewchat.BROWSER["holder"]["handed_by"]),
                         (self.dev, "far-qa"))
        self.assertNotIn("emulator-5554", crewchat.DEVICES)
        self.assertIn("on laptop", self.inbox_line(self.dev))
        self.assertEqual(self.hub.sheet[self.task]["returned_by"], "far-qa")
        # And sent to an agent on another machine: the hold here ends, the address goes with the message.
        crewchat.BROWSER.clear()
        self.assertEqual(self.nav(self.dev_key, "https://shop.example.com/cart")["decision"], "allow")
        msg = self.hub.return_task(self.dev, self.task, "Fixed", live=True)
        self.assertEqual((msg["to"], msg["page"]), ("far-qa", "https://shop.example.com/cart"))
        self.assertNotIn("holder", crewchat.BROWSER)

    def test_an_old_hand_over_keeps_nothing(self):
        self.assertTrue(self.hub.ingest({
            "id": "far-2", "ts": time.time() - crewchat.HAND_HOLD - 5, "from": self.qa, "to": self.dev,
            "kind": "update", "text": "Old", "task": self.task, "status": "in_progress", "back": "1",
            "page": "https://shop.example.com/", "machine": "laptop"}))
        self.assertNotIn("holder", crewchat.BROWSER)

    def test_the_roles_say_how(self):
        roles = crewchat.ROLES
        self.assertIn("hub_task action=return", roles["qa"][1])
        self.assertIn("live=true", roles["qa"][1])
        self.assertIn("hub_task action=return", roles["developer"][1])
        self.assertIn("open the site or app yourself", roles["developer"][1])
        self.assertIn("browser or mobile devices on", roles["lead"][1])
        self.assertIn("has browser and mobile devices on", crewchat.roster_text(self.hub.rows(), self.dev))


class BrowserHooks(unittest.TestCase):
    """B2: a before-hook for browser calls in every tool, printing a deny in that tool's own format."""

    def test_every_tool_gets_a_before_hook(self):
        settings = {}
        crewchat.set_claude_hooks(settings)
        group = next(g for g in settings["hooks"]["PreToolUse"] if g["matcher"] == crewchat.BROWSER_MATCHER)
        self.assertIn("claude-in-chrome", group["matcher"])
        self.assertIn("mcp__browser__", group["matcher"])
        self.assertEqual(group["hooks"][0]["command"], crewchat.hook_command("claude", "browser"))
        self.assertGreater(group["hooks"][0]["timeout"], crewchat.PERMISSION_WAIT)  # it waits for the owner
        hooks = {}
        crewchat.set_cursor_hooks(hooks)
        self.assertEqual(hooks["hooks"]["beforeMCPExecution"][-1]["command"], crewchat.hook_command("cursor", "browser"))
        for client, event in (("gemini", "BeforeTool"), ("qwen", "PreToolUse")):
            settings = {}
            crewchat.set_settings_hooks(settings, client)
            group = settings["hooks"][event][-1]
            self.assertIn("browser", group["matcher"])
            self.assertEqual(group["hooks"][0]["command"], crewchat.hook_command(client, "browser"))
        home = Path(tempfile.mkdtemp(prefix="codex-browser-", dir=TMP))
        with mock.patch.object(crewchat, "codex_config", return_value=home / "config.toml"):
            crewchat.install_codex()
            text = (home / "config.toml").read_text()
        self.assertEqual(text.count("[[hooks.PreToolUse]]"), 2)  # write calls, and the browser's
        self.assertIn(crewchat.hook_command("codex", "browser").split()[-1], text)
        plugin = crewchat.OPENCODE_PLUGIN % {"hook": "[]", "tool_hook": "[]", "after": "[]", "reopen": '""'}
        self.assertIn('"tool.execute.before"', plugin)
        self.assertIn('hook("browser"', plugin)
        self.assertIn("tool_response: (output && output.output)", plugin)  # where the page went (I2)
        if shutil.which("node"):
            path = Path(tempfile.mkdtemp(prefix="plugin-", dir=TMP)) / "crewchat.mjs"
            path.write_text(plugin)
            self.assertEqual(subprocess.run(["node", "--check", str(path)], capture_output=True).returncode, 0)

    def test_a_browser_of_the_users_own_is_not_checked(self):
        # Owner's decision (23 Oct): Gemini CLI, Qwen Code and Cursor name an MCP server's tools bare, so a Playwright
        # server of the user's own (browser_click) was taken for the bridge and refused. Without the bridge in the
        # folder it is the user's own browser; with it, it is checked (here the server is down: a change is refused).
        call = {"session_id": "s", "tool_name": "browser_click", "tool_input": {"ref": "e1"}}
        for client in ("gemini", "qwen", "cursor"):
            folder = Path(tempfile.mkdtemp(prefix="own-browser-%s-" % client, dir=TMP)).resolve()
            crewchat.install_client(client, folder, "http://127.0.0.1:9", "tok")
            self.assertEqual(crewchat.browser_hook(client, call, "http://127.0.0.1:9", "tok", "k" * 12, folder), "", client)
            old_bridge(folder, client)  # the bridge from before the proxy: the same names are its, checked
            out = crewchat.browser_hook(client, call, "http://127.0.0.1:9", "tok", "k" * 12, folder)
            self.assertIn("crewchat is not running here", out, client)
            crewchat.sync_browser_server(folder, "bridge-secret-123")  # R8: through the proxy, which checks it
            self.assertEqual(crewchat.browser_hook(client, call, "http://127.0.0.1:9", "tok", "k" * 12, folder), "")
        folder = Path(tempfile.mkdtemp(prefix="own-browser-claude-", dir=TMP)).resolve()
        out = crewchat.browser_hook("claude", {"tool_name": "mcp__claude-in-chrome__computer",
                                               "tool_input": {"action": "left_click"}}, "http://127.0.0.1:9", "tok",
                                    "k" * 12, folder)
        self.assertIn("crewchat is not running here", out)  # Claude in Chrome: always crewchat's to check
        # The bridge under another name (a server of the owner's own with the extension's token) is checked too.
        folder = Path(tempfile.mkdtemp(prefix="own-browser-renamed-", dir=TMP)).resolve()
        crewchat.install_client("gemini", folder, "http://127.0.0.1:9", "tok")
        settings = json.loads((folder / ".gemini/settings.json").read_text())
        settings["mcpServers"]["chrome"] = {"command": "npx", "args": ["@playwright/mcp", "--extension"],
                                            "env": {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": "x" * 30}}
        (folder / ".gemini/settings.json").write_text(json.dumps(settings))
        self.assertIn("crewchat is not running here",
                      crewchat.browser_hook("gemini", call, "http://127.0.0.1:9", "tok", "k" * 12, folder))

    def test_a_deny_is_printed_in_each_tools_format(self):
        reason = "the owner denied this"
        out = {c: json.loads(crewchat.browser_deny(c, reason)) for c in crewchat.HOOK_CLIENTS}
        self.assertEqual(out["claude"]["hookSpecificOutput"],
                         {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason})
        self.assertEqual(out["codex"], out["claude"])
        for client in ("gemini", "qwen", "opencode"):
            self.assertEqual((out[client]["decision"], out[client]["reason"]), ("deny", reason), client)
        self.assertEqual((out["cursor"]["permission"], out["cursor"]["agent_message"]), ("deny", reason))

    def test_the_hook_asks_the_server_and_prints_its_deny(self):
        said = []
        with mock.patch.object(crewchat, "post_json", lambda url, token, body, timeout=60: said.append(body) or
                               {"decision": "deny", "reason": "Jade is using the browser"}):
            text = crewchat.browser_hook("claude", {"tool_name": "mcp__claude-in-chrome__navigate",
                                                    "tool_input": {"url": "https://ci.example.com/"}},
                                         "http://127.0.0.1:9", "token", "key-12345678")
            self.assertIn("Jade is using the browser", text)
            self.assertEqual(said[-1]["event"], "browser")
            self.assertEqual(crewchat.browser_hook("claude", {"tool_name": "Bash", "tool_input": {}}, "u", "t", "k"), "")
            self.assertEqual(len(said), 1)  # not a browser call: the server is not asked
            # Cursor gives its tool's input as JSON text.
            crewchat.browser_hook("cursor", {"tool_name": "browser_click", "tool_input": "{\"element\": \"Build\"}"},
                                  "u", "t", "key-12345678")
            self.assertEqual(said[-1]["args"], {"element": "Build"})
        with mock.patch.object(crewchat, "post_json", side_effect=urllib.error.URLError("refused")):
            change = crewchat.browser_hook("claude", {"tool_name": "browser_click", "tool_input": {}}, "u", "t", "key-12345678")
            self.assertIn("not running", change)
            read = crewchat.browser_hook("claude", {"tool_name": "browser_snapshot", "tool_input": {}}, "u", "t", "key-12345678")
            self.assertEqual(read, "")  # a read goes on without the server
            # Release review (E3): opening a site is not checked without the server either.
            opened = crewchat.browser_hook("claude", {"tool_name": "browser_navigate", "tool_input": {"url": "https://x.example/"}},
                                           "u", "t", "key-12345678")
            self.assertIn("not running", opened)

    def test_a_checked_claude_in_chrome_call_needs_no_second_approval(self):
        # 11 Nov two-machine test: one screenshot of example.com asked five times. The gate's checked allow becomes
        # Claude Code's own allow, for Claude in Chrome only: an allow the server did not check leaves Claude Code's
        # prompt as it is, and other tools' hooks are never told to allow.
        call = {"tool_name": "mcp__claude-in-chrome__navigate", "tool_input": {"url": "https://example.com/", "tabId": 1}}
        with mock.patch.object(crewchat, "post_json", lambda *a, **k: {"decision": "allow", "reason": "", "checked": True}):
            out = json.loads(crewchat.browser_hook("claude", call, "u", "t", "key-12345678"))["hookSpecificOutput"]
            self.assertEqual((out["hookEventName"], out["permissionDecision"]), ("PreToolUse", "allow"))
            self.assertIn("crewchat", out["permissionDecisionReason"])
            shot = dict(call, tool_name="mcp__claude-in-chrome__computer", tool_input={"action": "screenshot"})
            self.assertIn('"allow"', crewchat.browser_hook("claude", shot, "u", "t", "key-12345678"))
            for client in ("codex", "cursor", "gemini"):
                self.assertEqual(crewchat.browser_hook(client, call, "u", "t", "key-12345678"), "", client)
            bridge = {"tool_name": "mcp__browser__browser_navigate", "tool_input": {"url": "https://example.com/"}}
            self.assertEqual(crewchat.browser_hook("claude", bridge, "u", "t", "key-12345678"), "")
        for said in ({"decision": "allow", "reason": ""}, {"decision": "allow", "checked": "yes"}, {}):
            with mock.patch.object(crewchat, "post_json", lambda *a, **k: said):
                self.assertEqual(crewchat.browser_hook("claude", call, "u", "t", "key-12345678"), "", said)
        with mock.patch.object(crewchat, "post_json", side_effect=urllib.error.URLError("refused")):
            read = dict(call, tool_name="mcp__claude-in-chrome__read_page", tool_input={})
            self.assertEqual(crewchat.browser_hook("claude", read, "u", "t", "key-12345678"), "")  # nobody checked it

    def test_a_session_not_in_the_chat_cannot_use_the_bridge(self):
        # Release review (S2): the bridge is in every connected folder and in Codex's settings for every folder, with
        # the token that skips the extension's own approval, and only sessions in the chat were checked.
        def hook(client, call, project):
            out = io.StringIO()
            env = {k: v for k, v in os.environ.items() if k != "CREWCHAT_LINK_KEY"}
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(crewchat, "find_project", return_value=project), \
                    mock.patch.object(crewchat, "post_json", side_effect=AssertionError("asked the server")), \
                    mock.patch("sys.stdin", io.StringIO(json.dumps(dict(call, session_id=secrets_hex())))), \
                    mock.patch("sys.stdout", out):
                crewchat.run_hook(client, "browser")
            return out.getvalue()
        secrets_hex = lambda: crewchat.secrets.token_hex(8)  # noqa: E731  (a session the hooks never saw)
        folder = Path(tempfile.mkdtemp(prefix="unlinked-bridge-", dir=TMP)).resolve()
        crewchat.install_client("gemini", folder, "http://127.0.0.1:9", "tok")
        click = {"tool_name": "browser_click", "tool_input": {"ref": "e1"}}
        self.assertEqual(hook("gemini", click, folder), "")  # no bridge here: the user's own browser
        old_bridge(folder, "gemini")  # a bridge from before the proxy: the hook refuses it
        self.assertIn("only an agent in the chat", json.loads(hook("gemini", click, folder))["reason"])
        crewchat.sync_browser_server(folder, "bridge-secret-123")  # R8: the proxy refuses it instead
        self.assertEqual(hook("gemini", click, folder), "")
        home = Path(tempfile.mkdtemp(prefix="codex-unlinked-", dir=TMP))
        (home / "config.toml").write_text('%s\n%s\ncommand = "npx"\nargs = ["@playwright/mcp@latest"]\n%s\n' % (
            crewchat.CODEX_BEGIN, crewchat.CODEX_BRIDGE, crewchat.CODEX_END))
        with mock.patch.object(crewchat, "codex_config", return_value=home / "config.toml"), \
                mock.patch.object(crewchat, "browser_token", return_value="bridge-secret-123"):
            out = hook("codex", {"tool_name": "mcp__browser__browser_navigate", "tool_input": {"url": "https://x.example/"}},
                       None)  # a Codex session in a folder with no chat at all
        self.assertIn("only an agent in the chat",
                      json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"])
        chrome = {"tool_name": "mcp__claude-in-chrome__navigate", "tool_input": {"url": "https://x.example/"}}
        self.assertEqual(hook("claude", chrome, folder), "")  # Claude in Chrome asks in Chrome itself, as before

    def test_opencode_names_its_mcp_tools_server_first(self):
        self.assertEqual(crewchat.browser_action("browser_browser_click", {})[0], "change")
        self.assertEqual(crewchat.browser_action("browser_browser_navigate", {"url": "https://a.example/"}),
                         ("navigate", "https://a.example/"))


class BrowserServers(unittest.TestCase):
    """B1: the Playwright bridge in the folder's MCP settings of every tool but Claude Code, only with a token."""

    def folder(self, client):
        folder = Path(tempfile.mkdtemp(prefix="bridge-%s-" % client, dir=TMP)).resolve()
        crewchat.install_client(client, folder, "http://127.0.0.1:9", "tok")
        return folder

    def test_the_folder_gets_the_bridge_only_with_a_token(self):
        token = "bridge-secret-123"
        for client, rel, servers in (("cursor", ".cursor/mcp.json", "mcpServers"),
                                     ("gemini", ".gemini/settings.json", "mcpServers"),
                                     ("qwen", ".qwen/settings.json", "mcpServers"),
                                     ("opencode", "opencode.json", "mcp")):
            folder = self.folder(client)
            self.assertNotIn("browser", json.loads((folder / rel).read_text())[servers], client)
            self.assertTrue(crewchat.sync_browser_server(folder, token))
            entry = json.loads((folder / rel).read_text())[servers]["browser"]
            self.assertIn('"browser-proxy", "%s"' % client, json.dumps(entry), client)  # R8: through crewchat's proxy
            self.assertIn(token, json.dumps(entry), client)
            self.assertIn(crewchat.SERVER_NAME, json.loads((folder / rel).read_text())[servers])  # crewchat's stays
            self.assertFalse(crewchat.sync_browser_server(folder, token))  # up to date
            crewchat.sync_browser_server(folder, "")  # the token taken away: so is the bridge
            self.assertNotIn("browser", json.loads((folder / rel).read_text())[servers], client)
            if client == "cursor":  # its tools allowed without asking, as crewchat's are (the proxy asks)
                self.assertNotIn("Mcp(browser:*)", (folder / ".cursor/cli.json").read_text())
                crewchat.sync_browser_server(folder, token)
                self.assertIn("Mcp(browser:*)", json.loads((folder / ".cursor/cli.json").read_text())["permissions"]["allow"])
                crewchat.sync_browser_server(folder, "")
                self.assertNotIn("Mcp(browser:*)", (folder / ".cursor/cli.json").read_text())
        home = Path(tempfile.mkdtemp(prefix="codex-bridge-", dir=TMP))
        with mock.patch.object(crewchat, "codex_config", return_value=home / "config.toml"), \
                mock.patch.object(crewchat, "browser_token", return_value=token):
            crewchat.install_codex()
            text = (home / "config.toml").read_text()
        self.assertIn("[mcp_servers.browser]", text)
        self.assertIn(token, text)
        self.assertIn('"browser-proxy", "codex"', text)

    def test_refresh_moves_an_old_npx_bridge_to_the_proxy(self):
        # R8: a bridge written before the proxy (npx @playwright/mcp@latest, with the token) is rewritten.
        folder = self.folder("gemini")
        path = folder / ".gemini/settings.json"
        data = json.loads(path.read_text())
        data["mcpServers"]["browser"] = {"command": "npx", "args": ["@playwright/mcp@latest", "--extension"],
                                         "env": {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": "tok-1"}}
        path.write_text(json.dumps(data))
        self.assertEqual(crewchat.bridge_set_up(folder, "gemini"), "npx")
        self.assertTrue(crewchat.sync_browser_server(folder, "tok-1"))
        entry = json.loads(path.read_text())["mcpServers"]["browser"]
        self.assertEqual(entry["args"][-2:], ["browser-proxy", "gemini"])
        self.assertEqual(crewchat.bridge_set_up(folder, "gemini"), "proxy")

    def test_the_bridge_waits_as_long_as_the_owner_may_take(self):
        # Final review: the owner's answer can take PERMISSION_WAIT inside the call; a client giving up first retried.
        for client, rel, servers in (("gemini", ".gemini/settings.json", "mcpServers"), ("qwen", ".qwen/settings.json", "mcpServers"),
                                     ("opencode", "opencode.json", "mcp")):
            folder = self.folder(client)
            crewchat.sync_browser_server(folder, "tok-3")
            entry = json.loads((folder / rel).read_text())[servers]["browser"]
            self.assertGreaterEqual(entry["timeout"], (crewchat.PERMISSION_WAIT + 30) * 1000, client)
        home = Path(tempfile.mkdtemp(prefix="codex-timeout-", dir=TMP))
        with mock.patch.object(crewchat, "codex_config", return_value=home / "config.toml"), \
                mock.patch.object(crewchat, "browser_token", return_value="tok-3"):
            crewchat.install_codex()
        self.assertIn("tool_timeout_sec = %d" % (crewchat.PERMISSION_WAIT + 30), (home / "config.toml").read_text())

    def test_only_the_proxy_command_counts_as_the_proxy(self):
        folder = self.folder("gemini")
        path = folder / ".gemini/settings.json"
        data = json.loads(path.read_text())
        data["mcpServers"]["browser"] = {"command": "node", "args": ["/srv/browser-proxy/server.js"],
                                         "env": {"PLAYWRIGHT_MCP_EXTENSION_TOKEN": "tok-9"}}
        path.write_text(json.dumps(data))
        self.assertEqual(crewchat.bridge_set_up(folder, "gemini"), "npx")  # a token'd bridge of its own: the hooks gate it

    def test_the_playwright_version_is_pinned(self):
        self.assertEqual(crewchat.PLAYWRIGHT_MCP, "@playwright/mcp@0.0.83")
        self.assertNotIn("@latest", " ".join(crewchat.BROWSER_BRIDGE))
        self.assertEqual(crewchat.BROWSER_BRIDGE, ["@playwright/mcp@0.0.83", "--extension"])

    def test_bare_tools_skip_the_hook_where_the_proxy_runs(self):
        folder = self.folder("gemini")
        crewchat.sync_browser_server(folder, "tok-2")
        self.assertFalse(crewchat.bridge_here(folder, "gemini", "browser_click"))  # the proxy gates it
        self.assertFalse(crewchat.bridge_here(folder, "gemini", "mcp__browser__browser_click"))
        self.assertTrue(crewchat.bridge_here(folder, "claude", "mcp__claude-in-chrome__navigate"))
        # A session in no chat, in that folder: the proxy refuses it, not the hook.
        self.assertEqual(crewchat.bridge_refusal("gemini", {"tool_name": "browser_click"}, folder), "")

    def test_the_docs_name_the_proxy(self):
        self.assertIn("crewchat browser-proxy", (ROOT / "docs" / "teams.md").read_text(encoding="utf-8"))

    def test_a_browser_server_of_the_users_own_is_left_alone(self):
        # Release review: a "browser" MCP server the user set up was taken for the bridge, removed with no token and
        # replaced with one; in Codex's config.toml a second [mcp_servers.browser] table made the file invalid.
        theirs = {"command": "node", "args": ["my-browser.js"]}
        for client, rel, servers in (("gemini", ".gemini/settings.json", "mcpServers"), ("opencode", "opencode.json", "mcp")):
            folder = self.folder(client)
            data = json.loads((folder / rel).read_text())
            data[servers]["browser"] = theirs
            (folder / rel).write_text(json.dumps(data))
            for token in ("", "bridge-secret-123"):
                self.assertEqual(crewchat.sync_browser_server(folder, token), [], (client, token))
                self.assertEqual(json.loads((folder / rel).read_text())[servers]["browser"], theirs, client)
        home = Path(tempfile.mkdtemp(prefix="codex-theirs-", dir=TMP))
        (home / "config.toml").write_text('[mcp_servers.browser]\ncommand = "node"\n')
        with mock.patch.object(crewchat, "codex_config", return_value=home / "config.toml"), \
                mock.patch.object(crewchat, "browser_token", return_value="bridge-secret-123"):
            crewchat.install_codex()
            text = (home / "config.toml").read_text()
        self.assertEqual(text.count("[mcp_servers.browser]"), 1)
        self.assertNotIn("bridge-secret-123", text)
        self.assertIn("[mcp_servers.%s]" % crewchat.SERVER_NAME, text)  # crewchat itself is still set up


class BrowserOnThePage(unittest.TestCase):
    def test_new_agent_and_the_card_turn_the_browser_on(self):
        page = crewchat.CHAT_PAGE
        self.assertTrue('id="l-use-browser"' in page)
        self.assertTrue('browser: $("l-use-browser").checked' in page)
        ids = re.findall(r'\bid="([^"]+)"', page)  # one element per id: $() finds the first (l-browser was two)
        self.assertEqual([i for i in set(ids) if ids.count(i) > 1], [])
        self.assertTrue('&browser=' in page)  # the readiness check asks what the browser needs
        self.assertTrue('op: "browser"' in page)  # the card's switch

    def test_the_task_panel_says_a_task_needs_the_browser(self):
        page = crewchat.CHAT_PAGE
        self.assertTrue('op: "browser", task: r.id' in page)
        self.assertTrue('"Needs the browser"' in page)

    def test_hooks_refresh_brings_the_bridge_to_a_connected_folder(self):
        folder = Path(tempfile.mkdtemp(prefix="refresh-bridge-", dir=TMP)).resolve()
        crewchat.install_client("cursor", folder, "http://127.0.0.1:9", "tok")
        with mock.patch.object(crewchat, "browser_token", return_value="bridge-token-1"):
            changed = crewchat.update_hooks(folder)
        self.assertIn(".cursor/mcp.json", changed)
        self.assertIn("browser", json.loads((folder / ".cursor" / "mcp.json").read_text())["mcpServers"])


class PrivateSettings(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "file modes")
    def test_refreshing_hooks_keeps_a_settings_file_with_a_token_private(self):
        # A Gemini CLI or Qwen Code folder's settings hold crewchat's token: written readable by this user only. An
        # upgrade's hooks refresh (edit_json) wrote it back readable by everyone.
        for client, rel in (("gemini", ".gemini/settings.json"), ("qwen", ".qwen/settings.json")):
            folder = Path(tempfile.mkdtemp(prefix="private-%s-" % client, dir=TMP)).resolve()
            crewchat.install_client(client, folder, "http://127.0.0.1:9", "secret-token")
            path = folder / rel
            self.assertEqual(path.stat().st_mode & 0o777, 0o600, client)
            data = json.loads(path.read_text())
            del data["hooks"]  # connected before crewchat had them: the refresh adds them
            path.write_text(json.dumps(data))
            os.chmod(path, 0o600)
            self.assertIn(rel, crewchat.update_hooks(folder))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600, client)

    @unittest.skipIf(os.name == "nt", "file modes")
    def test_a_token_file_an_older_version_left_readable_is_made_private(self):
        # 1.0.0 and 1.0.1 left such a file readable by everyone (or a person made it group-readable): the next
        # refresh writes it back for this user only, as it holds a token.
        for mode in (0o644, 0o640):
            folder = Path(tempfile.mkdtemp(prefix="widened-", dir=TMP)).resolve()
            crewchat.install_client("gemini", folder, "http://127.0.0.1:9", "secret-token")
            path = folder / ".gemini/settings.json"
            data = json.loads(path.read_text())
            del data["hooks"]
            path.write_text(json.dumps(data))
            os.chmod(path, mode)
            self.assertIn(".gemini/settings.json", crewchat.update_hooks(folder))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600, oct(mode))

    @unittest.skipIf(os.name == "nt", "file modes")
    def test_a_private_write_is_private_even_over_a_leftover_temporary_file(self):
        # A write cut short leaves settings.json.tmp behind, readable by everyone: the next private write reuses
        # it and must not carry that mode into place.
        folder = Path(tempfile.mkdtemp(prefix="leftover-", dir=TMP)).resolve()
        tmp = folder / "settings.json.tmp"
        tmp.write_text("{}")
        os.chmod(tmp, 0o644)
        crewchat.write_json(folder / "settings.json", {"token": "x"}, private=True)
        self.assertEqual((folder / "settings.json").stat().st_mode & 0o777, 0o600)


    @unittest.skipIf(os.name == "nt", "file modes")
    def test_codexs_settings_with_the_bridge_token_are_private(self):
        # Final review I3: ~/.codex/config.toml holds the bridge token once there is one; it was written readable by all.
        home = Path(tempfile.mkdtemp(prefix="codex-private-", dir=TMP))
        path = home / "config.toml"
        path.write_text("")
        os.chmod(path, 0o644)
        with mock.patch.object(crewchat, "codex_config", return_value=path), \
                mock.patch.object(crewchat, "browser_token", return_value="bridge-token-1"):
            crewchat.install_codex()
        self.assertIn("bridge-token-1", path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class PrivateChatData(unittest.TestCase):
    """Argus scan: the chat's own files (its settings, messages, logs, databases, the hooks' state) are this user's
    only, and one an older version left readable by others is tightened when next written."""

    def mode(self, path):
        return Path(path).stat().st_mode & 0o777

    @unittest.skipIf(os.name == "nt", "file modes")
    def test_a_projects_settings_are_private_with_no_token_in_them(self):
        root = Path(tempfile.mkdtemp(prefix="private-config-", dir=TMP))
        crewchat.save_config({"project": "private-config", "port": 9}, root)
        self.assertEqual(self.mode(root / "config.json"), 0o600)
        os.chmod(root / "config.json", 0o644)
        crewchat.save_config({"project": "private-config", "port": 10}, root)
        self.assertEqual(self.mode(root / "config.json"), 0o600)

    @unittest.skipIf(os.name == "nt", "file modes")
    def test_the_messages_logs_and_databases_are_private(self):
        root = Path(tempfile.mkdtemp(prefix="private-chat-", dir=TMP))
        crewchat.save_config({"project": "private-chat", "port": 9}, root)
        (root / "messages.jsonl").write_text("")
        os.chmod(root / "messages.jsonl", 0o644)  # as 1.0.2 left it
        hub = crewchat.Hub(crewchat.Roster(root))
        self.addCleanup(hub.events.close)
        hub.add_row(crewchat.OWNER, "private")
        hub.chat_index.about([], "private")
        hub.events.edits()
        self.assertEqual(self.mode(root / "messages.jsonl"), 0o600)
        self.assertEqual(self.mode(root / crewchat.HISTORY_FILE), 0o600)
        self.assertEqual(self.mode(root / crewchat.EVENTS_FILE), 0o600)
        log = root / "hub.log"
        log.write_text("before\n")
        os.chmod(log, 0o644)
        with crewchat.open_private(log) as f:
            f.write("after\n")
        self.assertEqual((self.mode(log), log.read_text()), (0o600, "before\nafter\n"))

    @unittest.skipIf(os.name == "nt", "file modes")
    def test_the_hooks_state_in_the_shared_temporary_folder_is_private(self):
        folder = Path(tempfile.mkdtemp(prefix="private-hooks-", dir=TMP)) / "crewchat-hooks"
        crewchat.save_hook_state(folder / "claude-session", {"seen": 1})
        self.assertEqual((self.mode(folder), self.mode(folder / "claude-session")), (0o700, 0o600))
        other = folder.parent / "claims"
        self.assertTrue(crewchat.claim_launch_key(other, "key-1", "session-1"))
        self.assertEqual(self.mode(other), 0o700)


class NoFormattedSql(unittest.TestCase):
    def test_no_table_name_is_formatted_into_a_statement(self):
        # Argus scan: values always go in as ? parameters; table names are written out, never formatted in.
        root = Path(crewchat.__file__).parent
        names = ["crewchat.py", "crewchat_map.py", "crewchat_peers.py"]
        source = root / "crewchat_src"
        if source.is_dir():  # a public checkout has the built file and not the modules
            names.extend("crewchat_src/" + path.name for path in sorted(source.glob("*.py")))
        for name in names:
            lines = (root / name).read_text(encoding="utf-8").splitlines()
            for n, line in enumerate(lines, 1):
                self.assertNotRegex(line, r"\b(FROM|INTO|UPDATE|JOIN|TABLE)\s+(%s|\{)", "%s:%d" % (name, n))



class ProxyGate(Base):
    """R8: the server's side of the browser proxy: which agent a proxy serves, and its calls gated on the page the
    browser reports."""

    def gemini(self, folder="/work/app", pids=None):
        name = self.hub.agent_for(self.hub.open_session(self.place, "gemini"))
        key = crewchat.secrets.token_urlsafe(12)
        with self.hub.lock:
            self.hub.links[key] = name
            self.hub.agents[name]["session"] = {"folder": folder}
            self.hub.agents[name]["browser"] = True
        if pids:
            self.hub.note_pids(self.place, key, pids)
        self.addCleanup(self.forget, name)
        return name

    def forget(self, name):
        with self.hub.lock:
            self.hub.links = {k: n for k, n in self.hub.links.items() if n != name}
        with crewchat.BROWSER_LOCK:
            crewchat.BROWSER.pop("holder", None)

    def test_a_proxy_finds_its_agent_by_a_shared_parent(self):
        name = self.gemini(pids=[500, 400])
        self.gemini(pids=[600, 400])  # another gemini in the same folder: the folder alone could not tell
        proxy = self.hub.register_proxy(self.place, "gemini", "/work/app", [500, 1])
        self.assertEqual(self.hub.proxy_agent(proxy), name)

    def test_a_far_shared_ancestor_does_not_match(self):
        # Final review: any shared ancestor matched (a terminal app both tabs run under), so one agent's proxy could
        # drive the browser as another. Only the nearest process of each counts: the agent tool itself.
        self.gemini(folder="/work/far", pids=[700, 77, 400])  # gemini, login, iTerm2
        self.gemini(folder="/work/far")
        proxy = self.hub.register_proxy(self.place, "gemini", "/work/far", [800, 78, 400])
        self.assertIsNone(self.hub.proxy_agent(proxy))
        self.assertEqual(self.hub.note_pids(self.place, "not-a-linked-key", [1, 2]), False)  # not kept: says so

    def test_a_proxy_unknown_here_is_told_so_and_belongs_to_its_machine(self):
        # Final review minors: "unknown proxy" and "cannot tell which agent" were one answer, so an unmatched proxy
        # registered again on every call; and any place's token could use another place's proxy id.
        self.assertEqual(self.hub.proxy_call("feedfeedfeed", "browser_snapshot", {})["reason"], crewchat.PROXY_GONE)
        self.gemini(folder="/work/mine")
        proxy = self.hub.register_proxy(self.place, "gemini", "/work/mine", [9020])
        self.assertEqual(self.hub.proxy_call(proxy, "browser_snapshot", {}, place="elsewhere")["reason"], crewchat.PROXY_GONE)
        self.assertEqual(self.hub.proxy_call(proxy, "browser_snapshot", {}, place=self.place)["decision"], "allow")
        self.assertIsNone(self.hub.proxy_seen(proxy, [{"url": "https://a.example.com/", "current": True}], place="elsewhere"))

    def test_a_tool_without_the_browser_prefix_is_still_gated(self):
        # Final review: a Playwright tool named otherwise was "not a browser call" and allowed at once.
        name = self.gemini(folder="/work/bare")
        with self.hub.lock:
            self.hub.agents[name]["browser"] = False
        proxy = self.hub.register_proxy(self.place, "gemini", "/work/bare", [9010])
        self.assertEqual(self.hub.proxy_call(proxy, "run_code", {"code": "1"})["decision"], "deny")

    def test_the_folder_fallback_needs_exactly_one(self):
        name = self.gemini(folder="/work/one")
        self.assertEqual(self.hub.proxy_agent(self.hub.register_proxy(self.place, "gemini", "/work/one", [9001])), name)
        self.gemini(folder="/work/two")
        self.gemini(folder="/work/two")
        proxy = self.hub.register_proxy(self.place, "gemini", "/work/two", [9002])
        self.assertIsNone(self.hub.proxy_agent(proxy))
        said = self.hub.proxy_call(proxy, "browser_snapshot", {})
        self.assertEqual(said["decision"], "deny")
        self.assertTrue(said["reason"].startswith(crewchat.PROXY_WHO), said)
        self.assertIsNone(self.hub.proxy_agent("nonexistent"))

    def test_a_proxy_started_with_its_agents_link_key_finds_that_agent(self):
        # 11 Nov e2e test: a background Cursor agent runs each turn as a new process, so the proxy's processes match
        # no hook's, and other agents' sessions in its folder made the folder fallback fail: every call was refused.
        others = [self.gemini(folder="/work/keyed", pids=[9100 + i]) for i in range(2)]
        name = self.gemini(folder="/work/keyed", pids=[9150])
        with self.hub.lock:
            key = next(k for k, n in self.hub.links.items() if n == name)
        proxy = self.hub.register_proxy(self.place, "gemini", "/work/keyed", [9199, 1], key)
        self.assertEqual(self.hub.proxy_agent(proxy), name)
        self.assertEqual(self.hub.proxy_call(proxy, "browser_snapshot", {})["decision"], "allow")
        self.assertIsNone(self.hub.proxy_agent(self.hub.register_proxy(self.place, "gemini", "/work/keyed", [9199], "")))
        self.assertIsNone(self.hub.proxy_agent(self.hub.register_proxy(self.place, "gemini", "/work/keyed", [9199],
                                                                       "not-a-key")))
        # A key is no way into another place's agents, nor into another tool's.
        self.assertIsNone(self.hub.proxy_agent(self.hub.register_proxy("elsewhere", "gemini", "/work/keyed", [], key)))
        self.assertIsNone(self.hub.proxy_agent(self.hub.register_proxy(self.place, "cursor", "/work/keyed", [], key)))
        self.assertNotIn(name, others)

    def test_a_proxy_under_an_agents_session_is_that_agents_whatever_its_key_says(self):
        # 12 Nov Windows test: Claude Code runs a background session from a service started with the environment of the
        # first `claude --bg` (Bea's reopen), so Mo's mobile proxy carried Bea's link key and was refused as Bea's.
        bea = self.gemini(folder="/work/svc")
        mo = self.gemini(folder="/work/svc")
        with self.hub.lock:
            bea_key = next(k for k, n in self.hub.links.items() if n == bea)
            self.hub.agents[mo]["session"]["pid"] = 4242  # Mo's recorded session process
        proxy = self.hub.register_proxy(self.place, "gemini", "/work/svc", [4300, 4242, 4000], bea_key)
        self.assertEqual(self.hub.proxy_agent(proxy), mo)
        # With no recorded session above the proxy, its key still names its agent.
        self.assertEqual(self.hub.proxy_agent(self.hub.register_proxy(self.place, "gemini", "/work/svc", [4301, 4000],
                                                                      bea_key)), bea)

    def test_on_windows_a_proxy_walks_the_process_table_up(self):
        # Windows has no `ps`: a process knows only its parent, which for a proxy started through a shell or launcher
        # is not the agent tool. The table walked up from it reaches the agent's session process.
        table = {300: (200, "", "python crewchat.py device-proxy claude"),
                 200: (100, "", "claude.exe --session-id 5e5"), 100: (4, "", "claude.exe daemon run")}
        with mock.patch.object(crewchat, "PS_TREE", False), mock.patch.object(crewchat.os, "getppid", return_value=300):
            self.assertEqual(crewchat.proxy_ancestors(table=table), [300, 200, 100])
        with mock.patch.object(crewchat, "PS_TREE", False), mock.patch.object(crewchat.os, "getppid", return_value=999):
            self.assertEqual(crewchat.proxy_ancestors(table=table), [999])  # not in the table: its parent, as before

    def test_a_browser_refusal_says_never_to_go_around_crewchat(self):
        name = self.gemini(folder="/work/around")
        with self.hub.lock:
            self.hub.agents[name]["browser"] = False
        proxy = self.hub.register_proxy(self.place, "gemini", "/work/around", [9030])
        said = self.hub.proxy_call(proxy, "browser_navigate", {"url": "https://a.example.com/"})
        self.assertEqual(said["decision"], "deny")
        self.assertIn(crewchat.BROWSER_NO_BYPASS, said["reason"])
        # Gone is left exactly as it is: the proxy registers again on that text.
        self.assertEqual(self.hub.proxy_call("feedfeedfeed", "browser_snapshot", {}),
                         {"decision": "deny", "reason": crewchat.PROXY_GONE})

    def test_a_proxy_for_an_agent_not_in_the_chat_is_refused(self):
        name = self.gemini(folder="/work/gone")
        proxy = self.hub.register_proxy(self.place, "gemini", "/work/gone", [9003])
        self.forget(name)  # it left the chat
        self.assertTrue(self.hub.proxy_call(proxy, "browser_snapshot", {})["reason"].startswith(crewchat.PROXY_WHO))

    def test_navigate_is_not_recorded_before_it_runs(self):
        name = self.gemini(folder="/work/nav")
        self.hub.set_browser_site("a.example.com", "allowed")
        proxy = self.hub.register_proxy(self.place, "gemini", "/work/nav", [9004])
        self.assertEqual(self.hub.proxy_call(proxy, "browser_navigate", {"url": "https://a.example.com/"})["decision"],
                         "allow")
        self.assertIsNone(self.hub.browser_site.get(name))  # not until the browser says where it is
        self.hub.proxy_seen(proxy, [{"url": "https://a.example.com/x", "current": True}])
        self.assertEqual(self.hub.browser_site[name], "a.example.com")

    def test_seen_from_the_proxy_moves_even_a_read_and_says_when_off_the_allowed_sites(self):
        name = self.gemini(folder="/work/seen")
        self.hub.set_browser_site("a.example.com", "allowed")
        proxy = self.hub.register_proxy(self.place, "gemini", "/work/seen", [9005])
        self.assertIsNone(self.hub.proxy_seen(proxy, [{"url": "https://a.example.com/", "current": True}]))
        note = self.hub.proxy_seen(proxy, [{"url": "https://other.example.net/", "current": False},
                                           {"url": "https://evil.example.org/", "current": True}])
        self.assertEqual(self.hub.browser_site[name], "evil.example.org")
        self.assertIn("evil.example.org", note)
        self.hub.browser_refused.add((name, "evil.example.org"))
        self.hub.proxy_seen(proxy, [])  # the browser could not say (a page's dialog stops it): nothing known
        self.assertIsNone(self.hub.browser_site[name])
        self.assertIn((name, "evil.example.org"), self.hub.browser_refused)  # a denied site is not forgotten for that

    def test_only_the_argument_itself_marks_the_bridge_as_the_proxy(self):
        for entry, want in (({"args": ["browser-proxy", "gemini"]}, "proxy"),
                            ({"command": ["crewchat", "browser-proxy", "gemini"]}, "proxy"),
                            ({"args": "/srv/browser-proxy/server.js"}, "npx"),
                            ({"command": "/opt/browser-proxy/bin/run", "args": []}, "npx"), ({"args": 5}, "npx")):
            folder = Path(tempfile.mkdtemp(prefix="bridge-", dir=TMP))
            (folder / ".gemini").mkdir()
            (folder / ".gemini" / "settings.json").write_text(json.dumps({"mcpServers": {"browser": dict(
                entry, env={"PLAYWRIGHT_MCP_EXTENSION_TOKEN": "tok"})}}))
            with mock.patch.object(crewchat, "browser_token", return_value="tok"):
                self.assertEqual(crewchat.bridge_set_up(folder, "gemini"), want, entry)

    def test_the_proxy_routes_take_a_place_token(self):
        self.gemini(folder="/work/http")
        out = crewchat.post_json(self.base + "/api/hook", self.token,
                                 {"event": "proxy", "client": "gemini", "folder": "/work/http", "pids": [9006]})
        said = crewchat.post_json(self.base + "/api/hook", self.token,
                                  {"event": "proxy_call", "proxy": out["proxy"], "tool": "browser_snapshot", "args": {}})
        self.assertEqual(said["decision"], "allow")
        seen = crewchat.post_json(self.base + "/api/hook", self.token,
                                  {"event": "proxy_seen", "proxy": out["proxy"],
                                   "tabs": [{"url": "https://a.example.com/", "current": True}]})
        self.assertIn("note", seen)
        with self.assertRaises(urllib.error.HTTPError) as e:
            crewchat.post_json(self.base + "/api/hook", self.owner, {"event": "proxy", "client": "gemini"})
        self.assertEqual(e.exception.code, 403)

    def test_a_new_site_through_the_proxy_asks_in_needs_you(self):
        # The proxy, a fake Playwright child and this server, end to end: a new site waits for the owner's Allow.
        name = self.gemini(folder="/work/e2e")
        server = crewchat.ProxyServer(self.base, self.token, "gemini", "/work/e2e")
        to_r, to_w = os.pipe()
        from_r, from_w = os.pipe()
        inp, out = os.fdopen(to_w, "w", buffering=1), os.fdopen(from_r, "r")
        proxy = crewchat.BrowserProxy("gemini", [sys.executable, BrowserProxy.FAKE], server, os.fdopen(to_r, "r"),
                                      os.fdopen(from_w, "w", buffering=1))
        thread = threading.Thread(target=proxy.run, daemon=True)
        thread.start()
        self.addCleanup(out.close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(inp.close)
        inp.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "browser_navigate", "arguments": {"url": "https://new.example.com/"}}}) + "\n")
        item = eventually(lambda: next((n for n in self.hub.needs_you() if n["kind"] == "browser"), None),
                          what="the browser ask in Needs you")
        self.assertEqual((item["agent"], item["host"]), (name, "new.example.com"))
        crewchat.post_json(self.base + "/api/needs", self.owner, {"key": item["key"], "answer": "allow_site"})
        answer = json.loads(out.readline())
        self.assertEqual(answer["id"], 1)
        self.assertFalse(answer["result"].get("isError"))
        self.assertEqual(self.hub.browser_site[name], "new.example.com")  # from the browser, after the call

    def test_the_hook_sends_its_pids_only_when_the_server_wants_them(self):
        # E2E benchmark (29 Oct): the prompt hook walked `ps` and told the server its pids at every prompt, 26 ms more
        # than 1.1.0. The server says when it has none (a new agent, or after a restart); only then are they sent.
        name = self.gemini(folder="/work/pids")
        key = next(k for k, n in self.hub.links.items() if n == name)
        self.assertEqual(self.hub.hook(self.place, key, 0, 0, "prompt").get("pids"), "want")
        walked = []

        def pids():
            walked.append(1)
            return [4242, 4000]
        crewchat.hook_check(self.base, self.token, key, 0, 0, "prompt", pids=pids)
        self.assertEqual((walked, self.hub.hook_pids[name]), ([1], [4242, 4000]))
        self.assertNotIn("pids", self.hub.hook(self.place, key, 0, 0, "prompt"))
        crewchat.hook_check(self.base, self.token, key, 0, 0, "prompt", pids=pids)
        self.assertEqual(walked, [1])  # known: not walked again
        with self.hub.lock:
            self.hub.hook_pids.clear()  # what a restarted server knows
        crewchat.hook_check(self.base, self.token, key, 0, 0, "stop", pids=pids)
        self.assertEqual(walked, [1, 1])

    def test_a_reopened_agent_gives_its_new_pids_and_still_hears_of_its_reclaim(self):
        # A Reopen keeps the agent's name and key, but its process is another: the old pids would not find its proxy.
        # The session report that says so is a "tool" event too, and nobody reads its answer: it must not use up the
        # notice of a task reclaimed while the agent was gone.
        name = self.gemini(folder="/work/pids2")
        key = next(k for k, n in self.hub.links.items() if n == name)
        task = self.hub.add_row(crewchat.OWNER, "Push the release notes")["id"]
        session = {"id": "abc12345", "pid": 4242, "started": "x", "tool": "gemini", "folder": "/work/pids2"}
        walked = []

        def pids():
            walked.append(1)
            return [5151, 4000]
        with mock.patch.object(self.hub, "agent_folders", return_value=["/work/pids2"]), \
                mock.patch.object(self.hub, "_wants_session", return_value=True):
            self.hub.hook(self.place, key, 0, 0, "tool", session=session)
            self.hub.note_pids(self.place, key, [4242, 4000])
            with self.hub.lock:
                self.hub.sheet[task]["reclaimed"] = name
            crewchat.hook_check(self.base, self.token, key, 0, 0, "prompt", pids=pids,
                                report=lambda: dict(session, pid=5151))
        self.assertEqual((walked, self.hub.hook_pids[name]), ([1], [5151, 4000]))
        self.assertIn("#%s was reclaimed from you" % task, self.hub.hook(self.place, key, 0, 0, "write")["refuse"])

    def test_ancestor_pids_skips_shells(self):
        table = {100: (300, "python3"), 300: (200, "/bin/zsh"), 200: (1, "gemini")}

        def ps(cmd, **kw):
            pid = int(cmd[-1])
            parent, comm = table[pid]
            return types.SimpleNamespace(returncode=0, stdout="%d %s\n" % (parent, comm))
        with mock.patch.object(crewchat.os, "getppid", return_value=100), \
                mock.patch.object(crewchat.subprocess, "run", side_effect=ps), mock.patch.object(crewchat, "PS_TREE", True):
            self.assertEqual(crewchat.ancestor_pids(), [100, 200])
        with mock.patch.object(crewchat.os, "getppid", return_value=100), mock.patch.object(crewchat, "PS_TREE", False):
            self.assertEqual(crewchat.ancestor_pids(), [100])

    def test_a_proxy_registers_with_the_link_key_its_agent_tool_gave_it(self):
        sent = []
        server = crewchat.ProxyServer("http://127.0.0.1:1", "token", "cursor", "/work/app")
        with mock.patch.object(server, "_post", side_effect=lambda body, timeout=10: sent.append(body) or {"proxy": "p"}), \
                mock.patch.object(crewchat, "ancestor_pids", return_value=[100]), \
                mock.patch.dict(crewchat.os.environ, {"CREWCHAT_LINK_KEY": "the-agents-key"}):
            server._register()
        self.assertEqual(sent[0]["key"], "the-agents-key")
        self.assertEqual(server.proxy, "p")


class BrowserProxy(ProxyHarness, unittest.TestCase):
    """R8: crewchat's bridge runs behind `crewchat browser-proxy`, which gates each call and reads the page from the
    browser (a fake Playwright MCP child here)."""
    FAKE = str(Path(__file__).resolve().parent / "fake_playwright_mcp.py")
    PROXY = "BrowserProxy"
    NAME = "fake"

    def test_calls_pass_through_when_allowed(self):
        self.start()
        result = self.result("browser_navigate", url="https://a.com/")
        self.assertIn("https://a.com/", result["content"][0]["text"])
        self.assertEqual(self.server.calls[0][:2], ("browser_navigate", {"url": "https://a.com/"}))
        self.assertEqual(self.server.seen_tabs, [[{"url": "https://a.com/", "current": True}]])

    def test_a_refused_call_is_not_forwarded(self):
        self.start()
        self.server.deny = "the owner denied this: do not open b.com"
        result = self.result("browser_navigate", url="https://b.com/")
        self.assertTrue(result["isError"])
        self.assertIn("the owner denied this", result["content"][0]["text"])
        self.assertEqual(self.child_calls(), [])
        self.assertEqual(self.server.seen_tabs, [])

    def test_the_site_comes_from_the_browser_not_the_page(self):
        self.start()
        self.result("browser_navigate", url="https://b.com/")
        self.result("browser_snapshot", text="- Page URL: https://trusted.com/")
        self.assertEqual(self.server.seen_tabs[-1], [{"url": "https://b.com/", "current": True}])

    def test_an_address_that_names_another_site_is_not_taken_for_it(self):
        # Seen against the real Playwright MCP 0.0.83 (27 Oct): its tab list prints "[title](url)" unescaped, so a page
        # at evil.example/a(https://trusted.example) read as trusted.example. The address comes from page.url(), not from the page.
        self.start()
        self.result("browser_navigate", url="https://evil.example/a(https://trusted.example)")
        self.assertEqual(self.server.seen_tabs[-1], [{"url": "https://evil.example/a(https://trusted.example)", "current": True}])

    def test_an_agents_own_ids_are_never_taken_for_the_proxys(self):
        self.start()
        self.send({"jsonrpc": "2.0", "id": "crewchat-1", "method": "tools/list", "params": {}})
        self.assertEqual(self.read()["id"], "crewchat-1")

    def test_one_child_is_started_however_many_calls_find_it_gone(self):
        proxy = crewchat.BrowserProxy("gemini", [sys.executable, self.FAKE], self.Server(), io.StringIO(), io.StringIO())
        started = []

        def start():
            started.append(1)
            time.sleep(0.2)
            proxy.child = types.SimpleNamespace(poll=lambda: None, stdin=io.StringIO())
            return True
        with mock.patch.object(proxy, "_start", side_effect=start):
            threads = [threading.Thread(target=proxy._to_child, args=({"jsonrpc": "2.0", "method": "x"},)) for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5)
        self.assertEqual(len(started), 1)

    def test_a_click_that_leaves_is_seen(self):
        self.start()
        self.result("browser_navigate", url="https://a.com/")
        self.result("browser_click", goes_to="https://evil.com/")
        self.assertEqual(self.server.seen_tabs[-1], [{"url": "https://evil.com/", "current": True}])

    def test_calls_are_gated_one_at_a_time(self):
        self.start()
        self.call("browser_navigate", url="https://a.com/")
        self.call("browser_snapshot", text="x")
        self.assertEqual({self.read()["id"], self.read()["id"]}, {1, 2})
        # The second call was asked about only after the first had run and its page was seen.
        self.assertEqual([(c[0], c[2]) for c in self.server.calls], [("browser_navigate", 0), ("browser_snapshot", 1)])

    def test_notifications_pass_during_a_call(self):
        self.start()
        self.server.gate = threading.Event()
        self.call("browser_snapshot", text="x")
        eventually(lambda: self.server.calls, what="the call to reach the server")
        self.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 99}})
        eventually(lambda: "cancelled" in self.child_calls(), what="the notification to reach the child")
        self.server.gate.set()
        self.assertEqual(self.read()["id"], 1)

    def test_a_dead_child_gives_an_error_and_restarts(self):
        self.start(env={"FAKE_DIE": str(Path(tempfile.mkdtemp(prefix="proxy-die-", dir=TMP)) / "died")})
        result = self.result("browser_snapshot", text="x")
        self.assertTrue(result["isError"])
        self.assertIn("browser", result["content"][0]["text"])
        result = self.result("browser_navigate", url="https://a.com/")
        self.assertFalse(result.get("isError"))
        self.assertEqual(self.server.seen_tabs[-1], [{"url": "https://a.com/", "current": True}])

    def test_no_current_tab_reports_nothing_known(self):
        self.start(env={"FAKE_TABS": "none"})
        self.result("browser_navigate", url="https://a.com/")
        self.assertEqual(self.server.seen_tabs, [[]])

    def test_a_batch_is_refused_not_passed_on(self):
        # Final review: a JSON-RPC batch (a list) went to the browser as it was, tools/call and all, ungated.
        self.start()
        self.send([{"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                    "params": {"name": "browser_navigate", "arguments": {"url": "https://evil.example/"}}}])
        answer = self.read()
        self.assertEqual(answer["error"]["code"], -32600)
        self.assertEqual(self.child_calls(), [])
        self.assertEqual(self.server.calls, [])

    def test_a_call_cancelled_while_it_waits_is_not_run(self):
        # Final review: a client that gave up (its timeout) and retried got the first call run too, after the owner's Allow.
        self.start()
        self.server.gate = threading.Event()
        self.call("browser_click", goes_to="https://a.example/")
        eventually(lambda: self.server.calls, what="the call to reach the server")
        self.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}})
        eventually(lambda: "cancelled" in self.child_calls(), what="the cancellation to pass")
        self.server.gate.set()
        self.server.gate = None
        self.assertEqual(self.result("browser_snapshot", text="next")["content"][0]["text"], "next")
        self.assertNotIn("browser_click", self.child_calls())

    def test_an_unexpected_error_still_answers_the_call(self):
        self.start()
        self.server.broken = True
        result = self.result("browser_snapshot", text="x")
        self.assertTrue(result["isError"])
        self.assertEqual(self.child_calls(), [])

    def test_a_browser_that_cannot_start_answers_with_an_error(self):
        # Final review: on Windows npx is npx.cmd, which a plain Popen cannot find: the proxy died, the agent waited.
        self.start(child=["/nonexistent/npx-%s" % os.getpid()])
        result = self.result("browser_snapshot", text="x")
        self.assertTrue(result["isError"])
        self.assertIn("could not start", result["content"][0]["text"])

    def test_the_real_proxy_finds_npx_and_reads_utf8(self):
        made = {}

        class Fake:
            def __init__(self, client, child, server, stdin, stdout, env=None):
                made.update(child=child, stdin=stdin, stdout=stdout)

            def run(self):
                return 0
        # Its own stand-ins for stdin and stdout: a wrapper around the real ones closes them when it goes, and the
        # tests after this one in a full run then find sys.stdin closed.
        pipes = types.SimpleNamespace(buffer=io.BytesIO())
        with mock.patch.object(crewchat, "BrowserProxy", Fake), mock.patch.object(crewchat, "find_project", return_value=None), \
                mock.patch.object(crewchat.shutil, "which", return_value="C:/node/npx.cmd"), \
                mock.patch.object(crewchat.sys, "stdin", pipes), mock.patch.object(crewchat.sys, "stdout", pipes):
            self.assertEqual(crewchat.proxy_main("gemini"), 0)
        self.assertEqual(made["child"][0], "C:/node/npx.cmd")
        self.assertEqual((made["stdin"].encoding.lower(), made["stdout"].encoding.lower()), ("utf-8", "utf-8"))

    def test_server_down_refuses_changes_allows_reads(self):
        self.start()
        self.server.down = True
        result = self.result("browser_click", goes_to="https://x.com/")
        self.assertTrue(result["isError"])
        self.assertIn(crewchat.BROWSER_DOWN, result["content"][0]["text"])
        result = self.result("browser_snapshot", text="hello")
        self.assertEqual(result["content"][0]["text"], "hello")
        self.assertEqual(self.child_calls(), ["browser_snapshot"])


if __name__ == "__main__":
    unittest.main()
