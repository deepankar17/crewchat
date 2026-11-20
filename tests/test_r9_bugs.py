"""The known bugs left by the 2.0 review (roadmap R9, review of 14 Nov action 3), fixed for 2.0.1: hook input read in
Windows' code page, the message log's one rewrite, git that cannot answer, and writers sharing one temporary file."""
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp(prefix="crewchat-r9-test-")
os.environ.setdefault("CREWCHAT_HOME", str(Path(TMP) / "home"))

import crewchat  # noqa: E402

HINDI = "फ़ाइल ठीक करो: एक झलक"  # ए is E0 A4 8F and झ E0 A4 9D in UTF-8: 0x8F and 0x9D are not in cp1252


def windows_stdin(payload):
    """stdin as Python opens it on a Windows machine whose code page is cp1252, holding the tool's UTF-8 bytes."""
    return io.TextIOWrapper(io.BytesIO(json.dumps(payload, ensure_ascii=False).encode("utf-8")), encoding="cp1252")


class HookInputOnWindows(unittest.TestCase):
    def setUp(self):
        with self.assertRaises(UnicodeDecodeError):  # the text really cannot be read in that code page
            HINDI.encode("utf-8").decode("cp1252")
        self.payload = {"session_id": "s1", "tool_name": "Edit", "tool_input": {"file_path": "a.py", "old_string": HINDI}}

    def test_crewchats_hook_reads_hindi_text(self):
        seen = []
        with mock.patch("sys.stdin", windows_stdin(self.payload)), \
                mock.patch.object(crewchat, "tool_hook", lambda client, data: seen.append(data)):
            crewchat.run_hook("claude", "tool")
        self.assertEqual(seen, [self.payload])

    def test_the_light_hook_script_reads_hindi_text(self):
        source = crewchat.hook_script()
        defs, main = source.split('\nif __name__ == "__main__":\n')
        space = {"__name__": "light_hook"}
        exec(compile(defs, "light_hook", "exec"), space)
        seen = []
        space["tool_hook"] = lambda client, data: seen.append(data)
        space["__name__"] = "__main__"
        with mock.patch("sys.stdin", windows_stdin(self.payload)), mock.patch.object(sys, "argv", ["hook", "claude", "tool"]):
            exec(compile('if __name__ == "__main__":\n' + main, "light_hook", "exec"), space)
        self.assertEqual(seen, [self.payload])

    def test_a_byte_order_mark_is_not_a_reason_to_do_nothing(self):
        seen = []
        stdin = io.TextIOWrapper(io.BytesIO(b"\xef\xbb\xbf" + json.dumps(self.payload).encode("utf-8")), encoding="cp1252")
        with mock.patch("sys.stdin", stdin), mock.patch.object(crewchat, "tool_hook", lambda c, data: seen.append(data)):
            crewchat.run_hook("claude", "tool")
        self.assertEqual(seen, [self.payload])


class HookOutputOnWindows(unittest.TestCase):
    """Round-3 re-review: on Windows a hook's stdout is a pipe in the ANSI code page (cp1252), which has no ✅ and no
    Hindi letter. The prompt hook's print failed, the error was swallowed, and the messages were marked as read."""

    def windows_stdout(self):
        raw = io.BytesIO()
        return raw, io.TextIOWrapper(raw, encoding="cp1252", errors="strict")

    def test_messages_with_hindi_and_emoji_reach_claude_code(self):
        text = "✅ done. " + HINDI
        raw, out = self.windows_stdout()
        with mock.patch("sys.stdout", out):
            crewchat.prompt_context("claude", text)
        self.assertEqual(raw.getvalue().decode("utf-8"), text + "\n")

    def test_a_print_that_fails_leaves_the_messages_unread(self):
        source = Path(crewchat.__file__).read_text(encoding="utf-8")
        start = source.index('if text and client in BLOCK_CLIENTS:  # shown first')
        self.assertLess(start, source.index("save(chain + 1 if woken and not from_owner else 0, last, 0)"))


class CommandOutputOnWindows(unittest.TestCase):
    def test_a_hindi_task_title_prints_into_a_pipe(self):
        """Round-4 re-review: `crewchat tasks list` into a pipe on Windows raised on a Hindi title (cp1252)."""
        raw = io.BytesIO()
        out = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", newline="\n")  # as written, not Windows' line end
        with mock.patch("sys.stdout", out), mock.patch("sys.stderr", io.TextIOWrapper(io.BytesIO(), encoding="cp1252")):
            crewchat.utf8_output(windows=True)
            print("✅ " + HINDI)
            sys.stdout.flush()
        self.assertEqual(raw.getvalue().decode("utf-8"), "✅ " + HINDI + "\n")


class TheLogsOneRewrite(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="rewrite-", dir=TMP))
        crewchat.save_config({"project": "rewrite", "port": 9}, self.root)
        self.hub = crewchat.Hub(crewchat.Roster(self.root))
        self.addCleanup(self.hub.events.close)
        self.log = self.root / "messages.jsonl"

    def test_lines_that_are_not_messages_are_kept_and_the_new_log_is_on_disk_first(self):
        task = self.hub.add_row(crewchat.OWNER, "an open task")["id"]
        self.hub.send(crewchat.OWNER, "all", "a message")
        odd = [b"\xff\xfe not text", b"[1, 2]", b'{"id": "99", "text": "half writ']  # the last torn: no newline
        with open(self.log, "ab") as f:
            f.write(b"\n".join(odd))
        before = self.log.read_bytes()
        order = []
        real_fsync, real_replace = os.fsync, os.replace
        with mock.patch.object(crewchat.os, "fsync", side_effect=lambda fd: (order.append("fsync"), real_fsync(fd))), \
                mock.patch.object(crewchat.os, "replace",
                                  side_effect=lambda a, b: (order.append("replace"), real_replace(a, b))):
            new = self.hub.adopt_open_tasks("A", "device-1")[task]
        self.assertEqual(order[-2:], ["fsync", "replace"])  # synced, then swapped
        lines = self.log.read_bytes().split(b"\n")
        for line in odd:
            self.assertIn(line, lines)  # copied through as they were
        messages = [json.loads(line) for line in lines if line and line not in odd]
        self.assertIn(new, [m["id"] for m in messages])  # the moved task, whole, on a line of its own
        self.assertIn("a message", [m.get("text") for m in messages])
        self.assertEqual((self.root / "messages.jsonl.bak").read_bytes(), before)
        if os.name != "nt":
            self.assertEqual((self.root / "messages.jsonl.bak").stat().st_mode & 0o777, 0o600)
        again = crewchat.Hub(crewchat.Roster(self.root))
        self.addCleanup(again.events.close)
        self.assertIn(new, again.by_id)


class GitThatCannotAnswer(unittest.TestCase):
    def repo(self):
        folder = Path(tempfile.mkdtemp(prefix="git-", dir=TMP))
        (folder / ".git").mkdir()
        (folder / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        return folder

    def test_an_empty_or_stray_git_folder_is_not_a_repository(self):
        stray = Path(tempfile.mkdtemp(prefix="stray-", dir=TMP))
        (stray / ".git").mkdir()  # no HEAD: not a repository, and git says so
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError("git")):
            self.assertFalse(crewchat.tracked_by_git(stray, ".mcp.json"))
        worktree = Path(tempfile.mkdtemp(prefix="worktree-", dir=TMP))
        (worktree / ".git").write_text("gitdir: /somewhere/.git/worktrees/x\n")
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError("git")):
            self.assertTrue(crewchat.tracked_by_git(worktree, ".mcp.json"))

    def test_a_folder_in_a_repository_counts_as_committed_when_git_cannot_say(self):
        repo, plain = self.repo(), Path(tempfile.mkdtemp(prefix="plain-", dir=TMP))
        (repo / "sub").mkdir()
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError("git")):  # git not installed
            self.assertTrue(crewchat.tracked_by_git(repo, ".mcp.json"))
            self.assertTrue(crewchat.tracked_by_git(repo / "sub", ".mcp.json"))  # .git above it
            self.assertFalse(crewchat.tracked_by_git(plain, ".mcp.json"))
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 10)):
            self.assertTrue(crewchat.tracked_by_git(repo, ".mcp.json"))
        refused = subprocess.CompletedProcess([], 128, stdout=b"", stderr=b"fatal: detected dubious ownership")
        with mock.patch.object(subprocess, "run", return_value=refused):
            self.assertTrue(crewchat.tracked_by_git(repo, ".mcp.json"))
        not_tracked = subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"")
        with mock.patch.object(subprocess, "run", return_value=not_tracked):
            self.assertFalse(crewchat.tracked_by_git(repo, ".mcp.json"))

    def test_the_token_is_not_written_and_the_owner_is_told_why(self):
        repo, out = self.repo(), io.StringIO()
        with mock.patch.object(subprocess, "run", side_effect=FileNotFoundError("git")), mock.patch("sys.stdout", out):
            self.assertFalse(crewchat.write_mcp_json(repo, "http://127.0.0.1:9", "secret-token"))
        self.assertFalse((repo / ".mcp.json").exists())
        self.assertIn("git could not say whether .mcp.json is committed", out.getvalue())


class TwoWritersOfOneFile(unittest.TestCase):
    def test_two_threads_writing_one_file_both_succeed(self):
        # Two refresh_guides on one folder at once: with one shared <file>.tmp, the second rename found it gone.
        path = Path(tempfile.mkdtemp(prefix="writers-", dir=TMP)) / "AGENTS.md"
        both_open = threading.Barrier(2, timeout=5)
        real_fsync = os.fsync

        def fsync(fd):  # each writer has its temporary file written before either renames
            real_fsync(fd)
            both_open.wait()
        errors = []

        def write(text):
            try:
                crewchat.write_text(path, text)
            except Exception as e:  # noqa: BLE001 - the test reports it
                errors.append(e)
        with mock.patch.object(crewchat.os, "fsync", fsync):
            threads = [threading.Thread(target=write, args=(t,)) for t in ("one\n", "two\n")]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        self.assertEqual(errors, [])
        self.assertIn(path.read_text(), ("one\n", "two\n"))
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["AGENTS.md"])  # nothing left behind

    def test_a_failed_write_leaves_no_temporary_file(self):
        path = Path(tempfile.mkdtemp(prefix="failed-", dir=TMP)) / "settings.json"
        with mock.patch.object(crewchat.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                crewchat.write_json(path, {"a": 1})
        self.assertEqual(list(path.parent.iterdir()), [])


class TakingCrewchatOut(unittest.TestCase):
    def test_disconnecting_a_folder_takes_the_browser_bridge_and_its_token_out(self):
        folder = Path(tempfile.mkdtemp(prefix="bridge-out-", dir=TMP)).resolve()
        crewchat.install_client("cursor", folder, "http://127.0.0.1:9", "tok")
        crewchat.sync_browser_server(folder, "bridge-secret-123")
        self.assertIn("bridge-secret-123", (folder / ".cursor/mcp.json").read_text())
        crewchat.disconnect_folder(folder)
        path = folder / ".cursor/mcp.json"
        self.assertNotIn("bridge-secret-123", path.read_text() if path.exists() else "")

    def test_an_uninstall_goes_on_past_a_folder_whose_settings_are_not_json(self):
        broken, fine = (Path(tempfile.mkdtemp(prefix=name, dir=TMP)).resolve() for name in ("broken-", "fine-"))
        crewchat.save_config({"project": "uninstall", "port": 9, "folders": {str(broken): {}, str(fine): {}}})
        done = []

        def disconnect(folder):
            if folder == broken:
                raise SystemExit("crewchat: %s exists but is not valid JSON" % (folder / ".mcp.json"))
            done.append(folder)
            return [".mcp.json"]
        out = io.StringIO()
        with mock.patch.object(crewchat, "disconnect_folder", disconnect), \
                mock.patch.object(crewchat, "stop_server"), mock.patch.object(crewchat, "service_installed", lambda: False), \
                mock.patch.object(crewchat, "claude_plugin_on", lambda *a: False), \
                mock.patch.object(crewchat, "USER_LEVEL_TOOLS", ()), \
                mock.patch.object(crewchat, "sync_codex_mcp", lambda servers: False), \
                mock.patch.object(crewchat, "uninstall_codex", lambda: False), \
                mock.patch.object(crewchat, "installed_by_installer", lambda: False), mock.patch("sys.stdout", out):
            crewchat.cmd_uninstall(crewchat.argparse.Namespace(yes=True, purge=False))
        self.assertEqual(done, [fine])
        self.assertIn("Could not take crewchat out of %s" % broken, out.getvalue())


if __name__ == "__main__":
    unittest.main()
