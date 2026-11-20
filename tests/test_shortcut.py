"""The "crewchat" shortcut (D1) and the app window, split from test_project_folders.py to keep each file short."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from crewchat_fixture import (  # noqa: E402,F401  (the shared server fixture)
    Base, Path, REAL_WRITE_SHORTCUT, ROOT, Session, TMP, argparse, cli, cli_in, contextlib, crewchat, http, io,
    json, mock, os, re, shutil, socket, stat, subprocess, sys, tempfile, threading, time, types, unittest, urllib,
)


class Shortcut(unittest.TestCase):
    """D1: a "crewchat" shortcut that runs crewchat through Python, with no console window and no new program
    (Smart App Control blocks unsigned ones). Every test works in a temporary home: none writes a real shortcut."""

    def setUp(self):
        self.user = Path(tempfile.mkdtemp(prefix="shortcut-user-", dir=TMP)).resolve()
        self.home = Path(tempfile.mkdtemp(prefix="shortcut-home-", dir=TMP)).resolve()
        (self.home / "config.json").write_text("{}", encoding="utf-8")
        for patch in (mock.patch.dict(os.environ, {"CREWCHAT_HOME": str(self.home)}),
                      mock.patch.object(crewchat, "user_home", lambda: self.user),
                      mock.patch.object(crewchat, "write_shortcut", REAL_WRITE_SHORTCUT)):
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.dict(os.environ)  # the guard is off for these tests: each writes into its own temp home
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("CREWCHAT_NO_SHORTCUT", None)

    def test_the_guard_makes_no_shortcut(self):
        """CREWCHAT_NO_SHORTCUT (set by every test module and by the e2e harness, so by their child processes too):
        neither write_shortcut nor `start`'s add_shortcut_once makes anything, and neither says so."""
        self.assertEqual(crewchat.write_shortcut("linux", self.user), [crewchat.shortcut_files("linux", self.user)[0]])
        crewchat.remove_shortcut("linux", self.user)
        with mock.patch.dict(os.environ, {"CREWCHAT_NO_SHORTCUT": "1"}):
            self.assertEqual(crewchat.write_shortcut("linux", self.user), [])
            self.assertEqual(crewchat.write_shortcut("windows", self.user), [])
            with mock.patch.object(crewchat, "write_shortcut", mock.Mock(return_value=[])) as written:
                crewchat.add_shortcut_once()
            written.assert_not_called()
        self.assertFalse(any(p.exists() for p in crewchat.shortcut_files("linux", self.user)))
        self.assertNotIn("shortcut", json.loads((self.home / "config.json").read_text(encoding="utf-8")))

    def test_the_files_for_each_system(self):
        start = self.user / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs/crewchat.lnk"
        self.assertEqual(crewchat.shortcut_files("windows", self.user), [start])  # no Desktop folder: none for it
        (self.user / "Desktop").mkdir()
        self.assertEqual(crewchat.shortcut_files("windows", self.user), [start, self.user / "Desktop/crewchat.lnk"])
        self.assertEqual(crewchat.shortcut_files("darwin", self.user), [self.user / "Applications/crewchat.app"])
        self.assertEqual(crewchat.shortcut_files("linux", self.user),
                         [self.user / ".local/share/applications/crewchat.desktop"])

    def test_the_command_runs_python_not_a_new_program(self):
        command = crewchat.shortcut_command()
        self.assertEqual(command[1:][-1], "start")
        self.assertTrue(command[1].endswith("crewchat.py"))
        self.assertEqual(len(command), 3)
        self.assertNotIn(".exe", "".join(command[1:]).lower())
        with mock.patch.object(crewchat.sys, "executable", "C:/Py/python.exe"), \
                mock.patch.object(crewchat, "self_command", lambda: ["C:/Py/python.exe", "C:/x/crewchat.py"]), \
                mock.patch.object(crewchat.os, "name", "nt"), \
                mock.patch.object(crewchat.os.path, "exists", lambda p: str(p).endswith("pythonw.exe")):
            self.assertEqual(crewchat.shortcut_command()[0].replace("\\", "/"), "C:/Py/pythonw.exe")  # no Path: os.name "nt" makes it WindowsPath

    def test_the_mac_app_runs_the_command(self):
        with mock.patch.object(crewchat, "shortcut_command", lambda: ["/py/python3", "/x y/crewchat.py", "start"]):
            made = crewchat.write_shortcut("darwin", self.user)
        app = self.user / "Applications/crewchat.app"
        self.assertEqual(made, [app])
        script = app / "Contents/MacOS/crewchat"
        lines = script.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "#!/bin/sh")
        self.assertIn("exec '/py/python3' '/x y/crewchat.py' 'start'", lines)
        if os.name != "nt":
            self.assertEqual(script.stat().st_mode & 0o777, 0o755)
        plist = (app / "Contents/Info.plist").read_text(encoding="utf-8")
        self.assertIn("<key>CFBundleExecutable</key><string>crewchat</string>", plist)

    def test_the_linux_entry(self):
        with mock.patch.object(crewchat, "shortcut_command", lambda: ["/py/python3", "/x y/crewchat.py", "start"]):
            made = crewchat.write_shortcut("linux", self.user)
        entry = self.user / ".local/share/applications/crewchat.desktop"
        self.assertEqual(made, [entry])
        text = entry.read_text(encoding="utf-8")
        self.assertIn("[Desktop Entry]", text)
        self.assertIn("Name=crewchat", text)
        self.assertIn('Exec="/py/python3" "/x y/crewchat.py" "start"', text)
        self.assertIn("Terminal=false", text)

    def test_the_windows_shortcut_is_made_with_the_values_in_the_environment(self):
        # a home folder whose name has a space, quotes, ; $( a backtick and # in it: the argv stays the same four items
        user = self.user / ("my home %s'x'; $(a) `b` #c" % ("" if os.name == "nt" else '"q" '))
        (user / "Desktop").mkdir(parents=True)
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            for path in kwargs["env"]["CREWCHAT_LNK_PATHS"].split("|"):
                Path(path).write_text("link")  # the script made them
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(crewchat, "shortcut_command", lambda: ["C:/Py/pythonw.exe", "C:/a b/crewchat.py", "start"]), \
                mock.patch.object(crewchat.subprocess, "run", run):
            made = crewchat.write_shortcut("windows", user)
        links = crewchat.shortcut_files("windows", user)
        self.assertEqual(made, links)
        self.assertEqual(len(calls), 1)  # one PowerShell call for both links
        command, kwargs = calls[0]
        self.assertEqual(command[:4], ["powershell", "-NoProfile", "-NonInteractive", "-Command"])
        self.assertEqual(len(command), 5)  # nothing after the fixed script
        self.assertEqual(command, crewchat.SHORTCUT_POWERSHELL)
        env = kwargs["env"]
        values = {"CREWCHAT_LNK_TARGET": "C:/Py/pythonw.exe", "CREWCHAT_LNK_ARGS": '"C:/a b/crewchat.py" start',
                  "CREWCHAT_LNK_DIR": "C:/a b", "CREWCHAT_LNK_PATHS": "|".join(str(p) for p in links)}
        for name, value in values.items():
            self.assertEqual(env[name], value)
            self.assertNotIn(value, command[4])  # never built into the script
        for part in ("my home", "$(a)", "`b`", "#c", "C:/a b"):
            self.assertNotIn(part, command[4])
        for name in values:
            self.assertIn("$env:" + name, command[4])
        self.assertIn("WScript.Shell", command[4])
        self.assertEqual(kwargs["timeout"], 10)
        if os.name == "nt":
            self.assertEqual(kwargs["creationflags"], crewchat.CREATE_NO_WINDOW)
        self.assertNotIn("CREWCHAT_LNK_TARGET", os.environ)  # set for the child only

    def test_a_missing_or_failing_powershell_makes_no_noise(self):
        for failure in (FileNotFoundError("powershell"), subprocess.TimeoutExpired("powershell", 1),
                        types.SimpleNamespace(returncode=1, stdout="", stderr="no")):
            def run(command, **kwargs):
                if isinstance(failure, Exception):
                    raise failure
                return failure
            with mock.patch.object(crewchat.subprocess, "run", run):
                self.assertEqual(crewchat.write_shortcut("windows", self.user), [])

    def test_a_failure_is_remembered_so_start_does_not_retry(self):
        written = mock.Mock(return_value=[])
        with mock.patch.object(crewchat, "write_shortcut", written):
            crewchat.add_shortcut_once()
            self.assertEqual(crewchat.load_config().get("shortcut"), "failed")
            crewchat.add_shortcut_once()
            written.assert_called_once()  # not again
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                crewchat.cmd_shortcut(argparse.Namespace(action="status"))
            self.assertIn("could not be made", out.getvalue())
            self.assertIn("crewchat shortcut on", out.getvalue())
            written.return_value = [self.user / "x"]
            with contextlib.redirect_stdout(io.StringIO()):
                crewchat.cmd_shortcut(argparse.Namespace(action="on"))  # tries again, and clears it
            self.assertEqual(written.call_count, 2)
            self.assertEqual(crewchat.load_config().get("shortcut"), "on")

    def test_a_shortcut_whose_python_moved_is_made_again(self):
        """Path-to-10 review: uv rebuilds the tool, perhaps on another Python; the old shortcut then fails silently."""
        made = self.user / "made"
        written = mock.Mock(side_effect=lambda: made.write_text("x") and [made])
        runs = ["/old/python", "/old/crewchat.py", "start"]
        with mock.patch.object(crewchat, "write_shortcut", written), \
                mock.patch.object(crewchat, "shortcut_files", lambda *a: [made]), \
                mock.patch.object(crewchat, "shortcut_command", lambda: list(runs)):
            crewchat.add_shortcut_once()  # not there: made
            self.assertEqual(written.call_count, 1)
            self.assertEqual(crewchat.load_config()["shortcut_runs"], runs)
            crewchat.add_shortcut_once()  # there, the same: left
            self.assertEqual(written.call_count, 1)
            runs[0] = "/new/python"
            crewchat.add_shortcut_once()  # there, but Python moved: made again
            self.assertEqual(written.call_count, 2)
            self.assertEqual(crewchat.load_config()["shortcut_runs"][0], "/new/python")
            crewchat.change_config(lambda c: c.pop("shortcut_runs", None))  # made before 2.1.0, which noted nothing
            crewchat.add_shortcut_once()  # made again once, then noted
            self.assertEqual(written.call_count, 3)
            self.assertEqual(crewchat.load_config()["shortcut_runs"], runs)

    def test_the_mac_app_has_crewchat_s_icon(self):
        with mock.patch.object(crewchat, "shortcut_command", lambda: ["/py/python3", "/x/crewchat.py", "start"]):
            crewchat.write_shortcut("darwin", self.user)
        app = self.user / "Applications/crewchat.app"
        self.assertIn("<key>CFBundleIconFile</key><string>crewchat</string>",
                      (app / "Contents/Info.plist").read_text(encoding="utf-8"))
        icns = (app / "Contents/Resources/crewchat.icns").read_bytes()
        self.assertEqual((icns[:4], int.from_bytes(icns[4:8], "big"), icns[8:12]), (b"icns", len(icns), b"ic08"))
        self.assertEqual(icns[16:24], b"\x89PNG\r\n\x1a\n")

    def test_a_change_made_while_it_was_written_is_kept(self):
        def slow(*args):
            config = crewchat.load_config()
            config["other"] = "kept"
            crewchat.save_config(config)  # another command changes the config meanwhile
            return [self.user / "x"]
        with mock.patch.object(crewchat, "write_shortcut", slow):
            crewchat.add_shortcut_once()
        config = crewchat.load_config()
        self.assertEqual((config.get("other"), config.get("shortcut")), ("kept", "on"))

    def test_status_and_off_work_on_a_machine_not_set_up(self):
        (self.home / "config.json").unlink()
        for action in ("status", "off"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                crewchat.cmd_shortcut(argparse.Namespace(action=action))
            self.assertNotIn("not set up", out.getvalue())

    def test_off_on_a_machine_not_set_up_says_nothing_was_changed(self):
        (self.home / "config.json").unlink()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            crewchat.cmd_shortcut(argparse.Namespace(action="off"))
        self.assertIn("There is no shortcut, and nothing was changed.", out.getvalue())
        self.assertNotIn("will not add it again", out.getvalue())  # untrue here: start would set up and add one
        self.assertFalse((self.home / "config.json").exists())

    def agent_env(self, **extra):
        clean = {k: v for k, v in os.environ.items() if k not in crewchat.AGENT_SESSION_ENV}
        return mock.patch.dict(os.environ, dict(clean, **extra), clear=True)

    def test_in_an_agent_session_on_and_off_ask_the_owner_first(self):
        """O1: an agent cannot add or take away the owner's Desktop and Start menu shortcut by itself."""
        crewchat.OWNER_LEAVE.clear()
        self.addCleanup(crewchat.OWNER_LEAVE.clear)
        written, removed = mock.Mock(return_value=[self.user / "x"]), mock.Mock(return_value=[self.user / "x"])
        denied = mock.Mock(side_effect=SystemExit("the owner did not allow it"))
        with self.agent_env(CLAUDECODE="1"), mock.patch.object(crewchat, "write_shortcut", written), \
                mock.patch.object(crewchat, "remove_shortcut", removed), mock.patch.object(crewchat, "owner_token", denied):
            for action in ("on", "off"):
                with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
                    crewchat.cmd_shortcut(argparse.Namespace(action=action))
            written.assert_not_called()
            removed.assert_not_called()
            self.assertNotIn("shortcut", crewchat.load_config())
            self.assertEqual(denied.call_count, 2)
            with contextlib.redirect_stdout(io.StringIO()):
                crewchat.cmd_shortcut(argparse.Namespace(action="status"))  # reading needs no leave
            self.assertEqual(denied.call_count, 2)
        allowed = mock.Mock(return_value="owner-secret")
        with self.agent_env(CLAUDECODE="1"), mock.patch.object(crewchat, "write_shortcut", written), \
                mock.patch.object(crewchat, "remove_shortcut", removed), mock.patch.object(crewchat, "owner_token", allowed), \
                contextlib.redirect_stdout(io.StringIO()):
            crewchat.cmd_shortcut(argparse.Namespace(action="on"))
            crewchat.cmd_shortcut(argparse.Namespace(action="off"))
        written.assert_called_once()
        removed.assert_called_once()
        self.assertEqual(allowed.call_count, 2)
        asked = mock.Mock(return_value="owner-secret")
        with self.agent_env(), mock.patch.object(crewchat, "write_shortcut", written), \
                mock.patch.object(crewchat, "owner_token", asked), contextlib.redirect_stdout(io.StringIO()):
            crewchat.cmd_shortcut(argparse.Namespace(action="on"))  # the owner in a terminal: as before
        asked.assert_not_called()

    def test_start_in_an_agent_session_adds_a_shortcut_only_with_the_owners_leave(self):
        crewchat.OWNER_LEAVE.clear()
        self.addCleanup(crewchat.OWNER_LEAVE.clear)
        written = mock.Mock(return_value=[self.user / "x"])
        with self.agent_env(CURSOR_AGENT="1"), mock.patch.object(crewchat, "write_shortcut", written), \
                contextlib.redirect_stdout(io.StringIO()):
            crewchat.add_shortcut_once()
            written.assert_not_called()
            self.assertNotIn("shortcut", crewchat.load_config())  # nothing remembered: a later start may add it
            crewchat.OWNER_LEAVE["allowed"] = True  # the owner allowed this run of `crewchat start`
            crewchat.add_shortcut_once()
        written.assert_called_once()

    def test_a_percent_sign_in_the_linux_entry_is_escaped(self):
        with mock.patch.object(crewchat, "shortcut_command", lambda: ["/py/python3", "/100%/crewchat.py", "start"]):
            crewchat.write_shortcut("linux", self.user)
        text = (self.user / ".local/share/applications/crewchat.desktop").read_text(encoding="utf-8")
        self.assertIn('"/100%%/crewchat.py"', text)

    def test_off_is_remembered_and_start_adds_none(self):
        written = mock.Mock(return_value=[self.user / "x"])
        with mock.patch.object(crewchat, "write_shortcut", written), mock.patch.object(crewchat, "shortcut_files",
                                                                                         lambda *args: [self.user / "x"]):
            crewchat.add_shortcut_once()
            written.assert_called_once()  # the file is missing, the choice is not "off"
            self.assertEqual(crewchat.load_config().get("shortcut"), "on")
            written.reset_mock()
            with contextlib.redirect_stdout(io.StringIO()):
                crewchat.cmd_shortcut(argparse.Namespace(action="off"))
            self.assertEqual(crewchat.load_config().get("shortcut"), "off")
            crewchat.add_shortcut_once()
            written.assert_not_called()
            with contextlib.redirect_stdout(io.StringIO()):
                crewchat.cmd_shortcut(argparse.Namespace(action="on"))
            written.assert_called_once()
            self.assertEqual(crewchat.load_config().get("shortcut"), "on")
        (self.user / "x").write_text("there")
        with mock.patch.object(crewchat, "write_shortcut", written), mock.patch.object(crewchat, "shortcut_files",
                                                                                         lambda *args: [self.user / "x"]):
            written.reset_mock()
            crewchat.add_shortcut_once()
            written.assert_not_called()  # it is there: not written again

    def test_start_adds_it_after_the_page_opens_unless_it_is_off(self):
        order = []
        written = mock.Mock(side_effect=lambda: order.append("shortcut") or [])
        with mock.patch.object(crewchat, "write_shortcut", written), \
                mock.patch.object(crewchat, "ensure_server", mock.Mock()), \
                mock.patch.object(crewchat, "cmd_ui", lambda args: order.append("page")):
            with contextlib.redirect_stdout(io.StringIO()):
                crewchat.start_page_only(argparse.Namespace(no_open=False, pack=None, no_service=True))
            self.assertEqual(order, ["page", "shortcut"])
            written.reset_mock()
            config = crewchat.load_config()
            config["shortcut"] = "off"
            crewchat.save_config(config)
            with contextlib.redirect_stdout(io.StringIO()):
                crewchat.start_page_only(argparse.Namespace(no_open=True, pack=None, no_service=True))
            written.assert_not_called()

    def test_uninstall_removes_it(self):
        with mock.patch.object(crewchat, "write_shortcut", REAL_WRITE_SHORTCUT):
            made = crewchat.write_shortcut("linux", self.user)
        self.assertTrue(made[0].exists())
        self.assertEqual(crewchat.remove_shortcut("linux", self.user), made)
        self.assertFalse(made[0].exists())
        self.assertEqual(crewchat.remove_shortcut("linux", self.user), [])  # nothing left: nothing reported
        again = crewchat.write_shortcut("darwin", self.user)
        self.assertTrue(again[0].is_dir())
        crewchat.remove_shortcut("darwin", self.user)
        self.assertFalse(again[0].exists())
        removed = mock.Mock(return_value=[])
        with mock.patch.object(crewchat, "remove_shortcut", removed), \
                mock.patch.object(crewchat, "stop_server"), mock.patch.object(crewchat, "service_installed", lambda: False), \
                mock.patch.object(crewchat, "installed_by_installer", lambda: False), \
                mock.patch.object(crewchat, "sync_codex_mcp", lambda servers: False), \
                mock.patch.object(crewchat, "uninstall_codex", lambda: False), \
                mock.patch.object(crewchat, "claude_plugin_on", lambda config: False), \
                mock.patch.object(crewchat, "remove_user_level", lambda client: False):
            cli("uninstall", "--yes")
        removed.assert_called_once()


class AppWindow(unittest.TestCase):
    """D0: `crewchat ui` opens the page in a window of its own (Edge or Chrome started with --app=URL), and in a
    tab of the default browser when there is no such browser, it does not start, or the owner asks for a tab."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="app-home-", dir=TMP)).resolve()
        self.popen, self.tab = mock.Mock(), mock.Mock()
        self.real_browser = crewchat.app_browser
        (self.home / "config.json").write_text("{}", encoding="utf-8")
        self.browser = mock.Mock(return_value="/opt/browser")
        for patch in (mock.patch.dict(os.environ, {"CREWCHAT_HOME": str(self.home)}),
                      mock.patch.object(crewchat, "CURRENT_CHAT", ""),
                      mock.patch.object(crewchat, "owner_call", lambda *a, **k: {"code": "ABCD2345"}),
                      mock.patch.object(crewchat, "local_url", lambda: "http://127.0.0.1:1234"),
                      mock.patch.object(crewchat, "app_browser", self.browser),
                      mock.patch.object(crewchat.subprocess, "Popen", self.popen),
                      mock.patch.object(crewchat.webbrowser, "open", self.tab)):
            patch.start()
            self.addCleanup(patch.stop)

    def ui(self, tab=False):
        with contextlib.redirect_stdout(io.StringIO()):
            crewchat.cmd_ui(argparse.Namespace(print=False, tab=tab))

    def test_the_command_is_the_browser_and_one_app_option(self):
        self.assertEqual(crewchat.app_command("/b/msedge.exe", "http://x/y?a=1&b=2"),
                         ["/b/msedge.exe", "--app=http://x/y?a=1&b=2"])
        self.ui()
        self.popen.assert_called_once()
        self.assertEqual(self.popen.call_args[0][0],
                         ["/opt/browser", "--app=http://127.0.0.1:1234/login?code=ABCD2345"])
        self.assertNotIn("shell", self.popen.call_args[1])
        self.tab.assert_not_called()

    def test_the_default_browser_is_found_first_on_windows(self):
        """The app window, and so the sign-in pages in it, opens in the owner's default browser when that is Edge or
        Chrome. Without the fix the window always chose Edge, so a Chrome user's sign-in opened in Edge."""
        root = Path(tempfile.mkdtemp(prefix="app-default-", dir=TMP)).resolve()
        edge = root / "pf86" / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        chrome = root / "pf" / "Google" / "Chrome" / "Application" / "chrome.exe"
        for exe in (edge, chrome):
            exe.parent.mkdir(parents=True)
            exe.write_text("")
        env = {"ProgramFiles(x86)": str(root / "pf86"), "ProgramFiles": str(root / "pf"),
               "LocalAppData": str(root / "local")}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(self.real_browser("win32", default="chrome"), str(chrome))
            self.assertEqual(self.real_browser("win32", default="edge"), str(edge))
            self.assertEqual(self.real_browser("win32", default=""), str(edge))  # no default: Edge, as before
        with mock.patch.object(crewchat, "windows_default_browser", return_value="chrome"), \
                mock.patch.dict(os.environ, env):
            self.assertEqual(self.real_browser("win32"), str(chrome))  # the registry's answer, when none is given

    def test_the_window_opens_in_the_chosen_profile(self):
        """Chrome opens an app window in its last-used profile, which may be the personal one. With a profile chosen
        (its name, from Chrome's own list of profiles), the window opens in that one. Without the fix there was no way to
        choose it."""
        root = Path(tempfile.mkdtemp(prefix="app-profile-", dir=TMP)).resolve()
        user_data = root / "local" / "Google" / "Chrome" / "User Data"
        user_data.mkdir(parents=True)
        (user_data / "Local State").write_text(json.dumps({"profile": {"info_cache": {
            "Default": {"name": "Personal"}, "Profile 1": {"name": "Work"}}}}), encoding="utf-8")
        chrome = "C:/Program Files/Google/Chrome/Application/chrome.exe"
        with mock.patch.dict(os.environ, {"LocalAppData": str(root / "local")}), \
                mock.patch.object(crewchat.sys, "platform", "win32"):
            self.assertEqual(crewchat.browser_profiles(chrome), {"Default": "Personal", "Profile 1": "Work"})
            self.assertEqual(crewchat.resolve_profile(chrome, "work"), "Profile 1")  # by its name, any case
            self.assertEqual(crewchat.resolve_profile(chrome, "Profile 1"), "Profile 1")  # or its folder
            self.assertEqual(crewchat.resolve_profile(chrome, "Nobody"), "")
        self.assertEqual(crewchat.app_command(chrome, "http://x/y", "Profile 1"),
                         [chrome, "--app=http://x/y", "--profile-directory=Profile 1"])
        self.assertEqual(crewchat.app_command(chrome, "http://x/y"), [chrome, "--app=http://x/y"])

    def test_with_several_profiles_and_none_chosen_it_says_how_to_choose(self):
        # Owner, 17 Nov: the window (and Jira from its links) opened in the personal profile; nothing said the window's
        # profile can be chosen, so other users would not find --profile.
        with mock.patch.object(crewchat, "browser_profiles", return_value={"Default": "Work", "Profile 1": "Personal"}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                crewchat.cmd_ui(argparse.Namespace(print=False, tab=False))
            self.assertIn("more than one profile (Personal, Work)", out.getvalue())
            self.assertIn("Settings > General > Browser profile", out.getvalue())
            (self.home / "config.json").write_text(json.dumps({"profile": "Work"}), encoding="utf-8")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                crewchat.cmd_ui(argparse.Namespace(print=False, tab=False))
            self.assertNotIn("more than one profile", out.getvalue())  # chosen: nothing to say
            self.assertEqual(self.popen.call_args[0][0][-1], "--profile-directory=Default")

    def test_edge_is_found_before_chrome_on_windows(self):
        root = Path(tempfile.mkdtemp(prefix="app-pf-", dir=TMP)).resolve()
        edge = root / "pf86" / "Microsoft" / "Edge" / "Application" / "msedge.exe"
        chrome = root / "pf" / "Google" / "Chrome" / "Application" / "chrome.exe"
        for exe in (edge, chrome):
            exe.parent.mkdir(parents=True)
            exe.write_text("")
        env = {"ProgramFiles(x86)": str(root / "pf86"), "ProgramFiles": str(root / "pf"),
               "LocalAppData": str(root / "local")}
        with mock.patch.dict(os.environ, env), mock.patch.object(crewchat, "windows_default_browser", return_value=""):
            self.assertEqual(self.real_browser("win32"), str(edge))
            edge.unlink()
            self.assertEqual(self.real_browser("win32"), str(chrome))
            chrome.unlink()
            self.assertEqual(self.real_browser("win32"), "")

    def test_start_passes_no_tab_option_and_still_opens_the_window(self):
        with contextlib.redirect_stdout(io.StringIO()):
            crewchat.cmd_ui(argparse.Namespace(print=False))  # what `crewchat start` passes
        self.popen.assert_called_once()
        self.tab.assert_not_called()

    def test_the_browser_is_started_detached_and_not_waited_for(self):
        self.ui()
        kwargs = self.popen.call_args[1]
        for name in ("stdin", "stdout", "stderr"):
            self.assertIs(kwargs[name], subprocess.DEVNULL)
        self.assertTrue(kwargs["close_fds"])
        self.assertEqual(kwargs["start_new_session"], os.name != "nt")
        for key, value in crewchat.HIDDEN.items():  # no console window on Windows
            self.assertEqual(kwargs[key], value)

    def test_the_chat_is_part_of_the_one_app_argument(self):
        with mock.patch.object(crewchat, "CURRENT_CHAT", "c1"):
            self.ui()
        self.assertEqual(self.popen.call_args[0][0],
                         ["/opt/browser", "--app=http://127.0.0.1:1234/login?code=ABCD2345&chat=c1"])

    def test_macos_finds_chrome_before_edge(self):
        chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        edge = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
        with mock.patch.object(crewchat.os.path, "isfile", lambda p: p in (chrome, edge)):
            self.assertEqual(self.real_browser("darwin"), chrome)
        with mock.patch.object(crewchat.os.path, "isfile", lambda p: p == edge):
            self.assertEqual(self.real_browser("darwin"), edge)
        with mock.patch.object(crewchat.os.path, "isfile", lambda p: False):
            self.assertEqual(self.real_browser("darwin"), "")
        mine = os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        with mock.patch.object(crewchat.os.path, "isfile", lambda p: p == mine):  # installed without admin rights
            self.assertEqual(self.real_browser("darwin"), mine)

    def test_linux_tries_the_names_in_order(self):
        asked = []

        def which(name):
            asked.append(name)
            return "/usr/bin/" + name if name == "microsoft-edge" else None
        with mock.patch.object(crewchat.shutil, "which", which):
            self.assertEqual(self.real_browser("linux"), "/usr/bin/microsoft-edge")
        self.assertEqual(asked, ["google-chrome", "chromium", "chromium-browser", "microsoft-edge"])
        with mock.patch.object(crewchat.shutil, "which", lambda name: None):
            self.assertEqual(self.real_browser("linux"), "")

    def test_no_browser_opens_a_tab(self):
        self.browser.return_value = ""
        self.ui()
        self.popen.assert_not_called()
        self.tab.assert_called_once_with("http://127.0.0.1:1234/login?code=ABCD2345")

    def test_a_browser_that_does_not_start_opens_a_tab(self):
        self.popen.side_effect = OSError("no such file")
        self.ui()
        self.tab.assert_called_once_with("http://127.0.0.1:1234/login?code=ABCD2345")

    def test_the_ui_command_without_a_print_option_still_opens_the_page(self):
        with contextlib.redirect_stdout(io.StringIO()):
            crewchat.cmd_ui(argparse.Namespace())  # a caller that builds its own arguments: no print, no tab
        self.popen.assert_called_once()

    def test_tab_is_kept_when_asked(self):
        self.ui(tab=True)
        self.popen.assert_not_called()
        self.tab.assert_called_once()
        self.tab.reset_mock()
        config = crewchat.load_config()
        config["window"] = "tab"
        crewchat.save_config(config)
        self.ui()
        self.popen.assert_not_called()
        self.tab.assert_called_once()
