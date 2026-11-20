"""Review of 14 Nov, roadmap V3: the server log grew for ever, nothing checked the built crewchat.py for names its
modules lost, and background agents rest on Claude Code's own commands with no test of what they print."""
import ast
import builtins
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp(prefix="crewchat-v3-test-")
os.environ.setdefault("CREWCHAT_HOME", str(Path(TMP) / "home"))

import crewchat  # noqa: E402

RECORDED = ROOT / "tests" / "fixtures" / "claude-2.1.325"  # `claude --help` (the parts crewchat reads) and `agents`


class ServerLog(unittest.TestCase):
    def test_the_log_moves_aside_when_it_is_big(self):
        folder = Path(tempfile.mkdtemp(dir=TMP))
        log = crewchat.RotatingLog(folder / "hub.log", limit=200)
        for i in range(40):
            log.write("2026-11-14 line %d of the server log\n" % i)
        log.close()
        self.assertTrue((folder / "hub.log.1").is_file())
        self.assertLess((folder / "hub.log").stat().st_size, 250)
        self.assertIn("line 39", (folder / "hub.log").read_text())
        self.assertFalse((folder / "hub.log.2").exists())  # one older log, no more

    def test_a_big_log_left_from_before_moves_aside_at_start(self):
        folder = Path(tempfile.mkdtemp(dir=TMP))
        (folder / "hub.log").write_text("x" * 300)
        log = crewchat.RotatingLog(folder / "hub.log", limit=200)
        log.write("started\n")
        log.close()
        self.assertEqual((folder / "hub.log.1").read_text(), "x" * 300)

    def test_the_processs_own_output_follows_the_log_when_it_moves(self):
        """Audit 14 Nov: launchd points descriptor 2 at hub.log. Writes straight to it went on into hub.log.1."""
        folder = Path(tempfile.mkdtemp(dir=TMP))
        spare = os.open(str(folder / "start"), os.O_WRONLY | os.O_CREAT)  # stands in for descriptor 2
        try:
            log = crewchat.RotatingLog(folder / "hub.log", limit=100, fds=[spare])
            for i in range(10):
                log.write("line %d of the server log, long enough\n" % i)
            os.write(spare, b"written straight to the descriptor\n")
            log.close()
        finally:
            os.close(spare)
        self.assertIn("written straight", (folder / "hub.log").read_text())

    def test_a_move_that_fails_is_not_tried_at_every_line(self):
        folder = Path(tempfile.mkdtemp(dir=TMP))
        log = crewchat.RotatingLog(folder / "hub.log", limit=50)
        with mock.patch.object(crewchat.os, "replace", side_effect=OSError("in use")) as moved:
            for i in range(20):
                log.write("line %d of the server log\n" % i)
        log.close()
        self.assertEqual(moved.call_count, 1)

    def test_serve_rotates_hub_log_when_launchd_writes_to_it(self):
        home = Path(tempfile.mkdtemp(dir=TMP))
        with mock.patch.object(crewchat, "home", lambda: home):
            with open(home / "hub.log", "a") as stream:
                self.assertTrue(crewchat.log_file(stream))
            with open(home / "other.log", "a") as stream:  # someone's own file: left as it is
                self.assertFalse(crewchat.log_file(stream))


def bound_names(tree):
    """Every name the file binds anywhere, in any scope."""
    names = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__", "__builtins__", "__loader__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
        elif hasattr(ast, "MatchAs") and isinstance(node, getattr(ast, "MatchAs")) and node.name:
            names.add(node.name)
    return names


class BuiltFile(unittest.TestCase):
    def test_every_name_the_built_file_uses_is_defined_in_it(self):
        """crewchat.py is built from crewchat_src/ and must run alone: a helper moved to another module and not
        built in shows up only when that line runs. Names used but bound nowhere in the file are such slips."""
        for name in ("crewchat.py", "crewchat_peers.py", "crewchat_map.py", "crewchat_cloud.py"):
            tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
            known = bound_names(tree)
            missing = sorted({n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                              and n.id not in known})
            self.assertEqual(missing, [], name)

    def test_the_check_finds_a_name_bound_nowhere(self):
        tree = ast.parse("def f():\n    return helper_that_was_not_built(1)\n")
        self.assertNotIn("helper_that_was_not_built", bound_names(tree))


class Structure(unittest.TestCase):
    """Review 14 Nov: an 8,100-line Hub and a 19,000-line test file. One area moved out, the tests split by area."""

    @unittest.skipUnless((ROOT / "crewchat_src").is_dir(), "the source modules are not in the public copy")
    def test_no_two_modules_define_the_same_name(self):
        """The modules share one namespace in crewchat.py: a second SECRET_FILES once replaced the first (round 2)."""
        import ast
        import collections
        where = collections.defaultdict(set)
        for path in sorted((ROOT / "crewchat_src").glob("*.py")):
            for node in ast.parse(path.read_text(encoding="utf-8")).body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    names = [node.name]
                elif isinstance(node, ast.Assign):
                    names = [t.id for t in node.targets if isinstance(t, ast.Name)]
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    names = [node.target.id]
                else:
                    names = []
                for name in names:
                    where[name].add(path.name)
        self.assertEqual({n: sorted(m) for n, m in where.items() if len(m) > 1}, {})

    @unittest.skipUnless((ROOT / "crewchat_src").is_dir(), "the source modules are not in the public copy")
    def test_process_notes_live_in_their_own_module(self):
        hub = (ROOT / "crewchat_src" / "hub.py").read_text(encoding="utf-8")
        self.assertNotIn("def _replay_processes", hub)
        self.assertIn("def _replay_processes", (ROOT / "crewchat_src" / "processes.py").read_text(encoding="utf-8"))
        self.assertIn(crewchat.Processes, crewchat.Hub.__mro__)

    def test_no_test_file_is_a_whole_book(self):
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            with self.subTest(path.name):
                self.assertLess(len(path.read_text(encoding="utf-8").splitlines()), 5000)


class WindowsUpdatePython(unittest.TestCase):
    """Audit 14 Nov: the installer used python.org's signed Python under Smart App Control, and `crewchat update`
    went back to uv's own, which Smart App Control blocks."""

    def test_an_update_keeps_the_signed_python_when_smart_app_control_is_on(self):
        args = crewchat.uv_install_args(windows=True, state=1, base="C:\\Python312\\python.exe")
        self.assertEqual(args[-4:], ["--python-preference", "only-system", "--python", "C:\\Python312\\python.exe"])
        self.assertNotIn("3.12", args)

    def test_elsewhere_an_update_uses_uvs_python(self):
        self.assertEqual(crewchat.uv_install_args(windows=False, state=1), crewchat.UV_INSTALL)
        with mock.patch.dict(os.environ, {"CREWCHAT_SYSTEM_PYTHON": ""}):
            self.assertEqual(crewchat.uv_install_args(windows=True, state=0), crewchat.UV_INSTALL)

    def test_while_smart_app_control_evaluates_an_update_keeps_the_installers_choice(self):
        """Re-review 14 Nov: evaluation mode (2) can turn itself on later; then uv's own Python stops working."""
        signed, uvs = "C:\\Python312\\python.exe", "C:\\Users\\d\\AppData\\Roaming\\uv\\python\\cpython-3.12\\python.exe"
        with mock.patch.dict(os.environ, {"CREWCHAT_SYSTEM_PYTHON": ""}):
            self.assertEqual(crewchat.uv_install_args(windows=True, state=2, base=signed)[-1], signed)
            self.assertEqual(crewchat.uv_install_args(windows=True, state=2, base=uvs), crewchat.UV_INSTALL)

    def test_the_windows_update_window_uses_the_same_choice(self):
        signed = crewchat.UV_INSTALL[:-2] + ["--python-preference", "only-system", "--python", "C:\\Py 3\\python.exe"]
        with mock.patch.object(crewchat, "uv_install_args", return_value=signed):
            script = crewchat.windows_update_script("C:\\uv.exe", ["crewchat==9.9.9"], ["crewchat"], "C:\\t", 1)
        self.assertIn("--python-preference only-system --python 'C:\\Py 3\\python.exe'", script)


class ClaudeCodeContract(unittest.TestCase):
    """What crewchat reads from Claude Code's own commands, checked against their recorded output (2.1.325) and,
    where Claude Code is installed, against the real thing."""

    def check_help(self, text):
        self.assertIn("--bg", text)  # background_start looks for it before it starts one
        for command in ("agents", "logs <id", "stop|kill <id"):
            self.assertIn(command, text)
        modes = crewchat.permission_modes(text)
        for option in crewchat.LAUNCH_PERMISSIONS["claude"].values():
            for mode in option[1][1:]:  # ["--permission-mode", mode]
                self.assertIn(mode, modes)

    def check_agents(self, out):
        with mock.patch.object(crewchat, "background_run", lambda *a, **k: (0, out)):
            found = crewchat.background_sessions("claude")
        self.assertIsInstance(found, dict)  # None would mean crewchat could not read the list
        rows = json.loads(out[out.index("["):])
        for row in rows:
            self.assertIn("kind", row)
            self.assertTrue("status" in row or "state" in row, row)  # Claude Code 17 Nov: a done row has state only
            if row["kind"] == "background":
                self.assertIn(str(row.get("id")), found)
                self.assertIn(crewchat.background_state(row), ("working", "idle", "waiting", "blocked", "done"))

    def test_recorded_help(self):
        self.check_help((RECORDED / "help.txt").read_text(encoding="utf-8"))

    def test_recorded_agents_list(self):
        self.check_agents((RECORDED / "agents.json").read_text(encoding="utf-8"))

    @unittest.skipUnless(shutil.which("claude"), "Claude Code is not installed here")
    def test_the_installed_claude_code(self):
        exe = shutil.which("claude")
        self.check_help(crewchat.tool_help(exe))
        out = subprocess.run([exe, "agents", "--json", "--all"], capture_output=True, text=True, timeout=30)
        if out.returncode == 0 and "[" in out.stdout:
            self.check_agents(out.stdout)


if __name__ == "__main__":
    unittest.main()
