"""install.sh for real, into a throwaway home: a fresh install, and an upgrade that keeps the chat.

Uses uv's own cache and Pythons (read-only) so nothing is downloaded that is not there already.
The release being upgraded from comes from this checkout's git tag, not from GitHub.
"""
import os
import shutil
import subprocess
import unittest
from pathlib import Path

import harness as H

OLD = os.environ.get("CREWCHAT_E2E_FROM", "v0.9.2")
UV = shutil.which("uv")


def uv_dir(*args):
    return subprocess.run([UV] + list(args), capture_output=True, text=True, check=True).stdout.strip()


@unittest.skipUnless(UV, "needs uv")
class Install(unittest.TestCase):
    def setUp(self):
        self.home = H.TMP / ("install-home-%d" % id(self))
        self.home.mkdir()
        self.env = dict(os.environ, HOME=str(self.home), UV_TOOL_DIR=str(self.home / "uv-tools"),
                        UV_TOOL_BIN_DIR=str(self.home / "bin"), UV_CACHE_DIR=uv_dir("cache", "dir"),
                        UV_PYTHON_INSTALL_DIR=uv_dir("python", "dir"), CREWCHAT_HOME=str(self.home / ".crewchat"),
                        PATH=os.path.dirname(UV) + ":/usr/bin:/bin")
        self.exe = self.home / "bin" / "crewchat"

    def install(self, source, lean=False):
        env = dict(self.env, CREWCHAT_SOURCE=str(source))
        if lean:
            env["CREWCHAT_LEAN"] = "1"
        out = subprocess.run(["sh", str(H.ROOT / "install.sh")], env=env, capture_output=True, text=True, timeout=600)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return out.stdout

    def checkout(self, tag):
        folder = H.TMP / ("src-" + tag)
        if not folder.exists():
            folder.mkdir()
            archive = subprocess.run(["git", "-C", str(H.ROOT), "archive", tag], capture_output=True, check=True).stdout
            subprocess.run(["tar", "-x", "-C", str(folder)], input=archive, check=True)
        return folder

    def test_a_fresh_install_runs_a_chat(self):
        out = self.install(H.ROOT)
        self.assertIn("Installed crewchat", out)
        self.assertIn("crewchat start", out)
        version = subprocess.run([str(self.exe), "--version"], capture_output=True, text=True).stdout
        self.assertIn(H.subprocess.run([H.sys.executable, str(H.ROOT / "crewchat.py"), "--version"],
                                       capture_output=True, text=True).stdout.strip(), version)
        # Cloud sync's libraries came with it.
        py = next((self.home / "uv-tools" / "crewchat" / "bin").glob("python3*"))
        subprocess.run([str(py), "-c", "import cryptography, crewchat_cloud, crewchat_peers"], check=True)
        # Run a chat with the installed command, as a person would.
        os.environ["CREWCHAT_BIN"] = str(self.exe)
        try:
            m = H.Machine("installed")
            m.env.update(HOME=str(self.home))
            m.start()
            settings = (m.folder / ".claude" / "settings.local.json").read_text()
            self.assertIn(str(self.home / "uv-tools" / "crewchat"), settings)  # hooks run the installed copy
            a = m.agent()
            a.link()
            a.stop_hook()
            m.owner.send(a.name, "installed and working")
            self.assertIn("installed and working", a.stop_hook())
            m.stop()
        finally:
            del os.environ["CREWCHAT_BIN"]

    def test_an_upgrade_keeps_the_chat_agents_and_links(self):
        self.install(self.checkout(OLD), lean=True)
        self.assertIn(OLD.lstrip("v"), subprocess.run([str(self.exe), "--version"], capture_output=True, text=True).stdout)
        os.environ["CREWCHAT_BIN"] = str(self.exe)
        try:
            m = H.Machine("upgrade")
            m.env.update(HOME=str(self.home))
            m.start()
            a = m.agent()
            a.link()
            m.owner.role(a.name, "developer")
            tid = m.owner.task("all", "made before the upgrade")
            a.call("hub_take", id=tid)
            a.stop_hook()
            m.kill(hard=False)  # the installer on Windows stops it; here the owner restarts after
            self.install(H.ROOT)
            new = subprocess.run([str(self.exe), "--version"], capture_output=True, text=True).stdout
            self.assertNotIn(OLD.lstrip("v") + "\n", new)
            m.cli("start", "--no-service", "--no-open")
            m.wait_up()
            m._owner = None
            poll = m.owner.poll()
            self.assertEqual(poll["taken"][tid], a.name)
            self.assertEqual(m.owner.row(a.name)["role"], "developer")
            # The same session, still running from before, carries on with its old hook commands.
            m.owner.send(a.name, "after the upgrade")
            self.assertIn("after the upgrade", a.stop_hook())
            a.call("hub_update", id=tid, status="in_progress", note="still mine")
            m.owner.wait_for("still mine", kind="update")
            m.stop()
        finally:
            del os.environ["CREWCHAT_BIN"]


if __name__ == "__main__":
    unittest.main()
