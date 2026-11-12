"""crewchat inside Claude Code (roadmap W14): the mod in plugin/hooks/register.js draws a band that says what needs the
owner and a pane with the chat and Approve and Deny. These tests check the plugin's files, that the mod calls only the
local server's endpoints, and run it in Node (tests/desktop_mod_harness.mjs stands in for Claude Code's `$`) against a
real server: what it draws, and that its buttons answer."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_crewchat  # noqa: E402  (the shared server fixture; by its module, so its tests are not run again here)
from test_crewchat import ROOT, TMP, Base, crewchat  # noqa: E402

PLUGIN = ROOT / "plugin"
MOD = PLUGIN / "hooks" / "register.js"
HARNESS = Path(__file__).resolve().parent / "desktop_mod_harness.mjs"
NODE = shutil.which("node")
CLAUDE = shutil.which("claude")


def run_mod(cwd, *op, home=None, windows=False):
    """What the mod drew and did, run once in Node (see the harness), as on Windows (the OS variable Windows sets) or
    not, whatever this machine is."""
    env = {k: v for k, v in os.environ.items() if k.upper() != "OS"}
    env.update({"CREWCHAT_HOME": str(home)} if home else {}, **({"OS": "Windows_NT"} if windows else {}))
    # Node writes UTF-8 whatever the system's code page (Windows' is not), and what it draws has "·" and "→".
    out = subprocess.run([NODE, str(HARNESS), str(MOD), str(cwd), *op], capture_output=True, text=True,
                         encoding="utf-8", env=env, timeout=60)
    if out.returncode != 0:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout)


def texts(tree):
    """Every string a drawn tree holds, in order, joined."""
    if isinstance(tree, str):
        return tree
    if not isinstance(tree, dict) or "type" not in tree:
        return ""
    own = tree["props"].get("label", "") if tree["type"] == "Button" else ""
    return " ".join(t for t in [own] + [texts(c) for c in tree.get("children", [])] if t)


def buttons(tree):
    if not isinstance(tree, dict) or "type" not in tree:
        return []
    found = [tree["props"]["key"]] if tree["type"] == "Button" else []
    return found + [k for c in tree.get("children", []) for k in buttons(c)]


class PluginFiles(unittest.TestCase):
    def test_the_plugin_names_its_hooks_module(self):
        manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["name"], "crewchat")
        self.assertEqual(json.loads((PLUGIN / "hooks" / "hooks.json").read_text()), {"modules": ["./register.js"]})
        self.assertTrue(MOD.is_file())
        self.assertIn("export const register = on =>", MOD.read_text())

    def test_cursor_does_not_read_claudes_hooks_file(self):
        # Cursor reads hooks/hooks.json too unless its manifest names hooks of its own; its hooks are set up per
        # folder by `crewchat start`, so the plugin gives it none.
        cursor = json.loads((PLUGIN / ".cursor-plugin" / "plugin.json").read_text())
        self.assertEqual(cursor["hooks"], {"version": 1, "hooks": {}})

    @unittest.skipUnless(NODE, "node is not installed")
    def test_node_reads_the_module(self):
        out = subprocess.run([NODE, "--check", str(MOD)], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)

    @unittest.skipUnless(CLAUDE, "claude is not installed")
    def test_claude_code_accepts_what_it_draws(self):
        # The engine's own kit, which refuses a tree a surface cannot draw (Node's harness would not).
        folder = Path(tempfile.mkdtemp(prefix="mod-kit-", dir=TMP)) / "crewchat"
        shutil.copytree(PLUGIN, folder)
        shutil.copy(Path(__file__).resolve().parent / "desktop_mod_draw.test.ts", folder / "draw.test.ts")
        out = subprocess.run([CLAUDE, "plugin", "test", str(folder)], capture_output=True, text=True, timeout=300)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("1 pass", out.stdout + out.stderr)

    def test_it_reaches_only_the_local_server(self):
        source = MOD.read_text()
        self.assertEqual(re.findall(r"\w+://[^'\"\s]*", source), ["http://127.0.0.1:"])
        endpoints = set(re.findall(r"/api/[a-z-]+", source))
        self.assertEqual(endpoints, {"/api/poll", "/api/needs", "/api/login-code"})
        self.assertIn("/api/needs", crewchat.POST_PATHS)  # answers go where the page's do
        self.assertIn("/api/login-code", crewchat.POST_PATHS)
        self.assertNotIn("/api/poll", crewchat.POST_PATHS)  # a read, as the page's
        self.assertIn("'/login?code='", source)  # the page opened as `crewchat ui` opens it

    @unittest.skipUnless(NODE, "node is not installed")
    def test_the_band_counts_every_kind_of_item(self):
        # Every kind the server puts in Needs you, and one added later, counts: never "Nothing needs you" while one waits.
        kinds = sorted(crewchat.NEED_KINDS) + ["a-kind-added-later"]
        script = ("import { countLine } from %s\n"
                  "process.stdout.write(JSON.stringify(%s.map(kind => countLine([{ kind }]))))"
                  % (json.dumps(MOD.as_uri()), json.dumps(kinds)))
        out = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = dict(zip(kinds, json.loads(out.stdout)))
        self.assertEqual([k for k, line in lines.items() if line == "Nothing needs you"], [])
        self.assertEqual((lines["stuck"], lines["lease"]), ("1 stuck agent", "1 stopped commit"))
        self.assertEqual(lines["a-kind-added-later"], "1 other item")

    def test_public_ships_the_plugin_folder(self):
        public = ROOT / "tools" / "publish" / "public.txt"
        if not public.exists():  # the public copy itself has no publish list
            self.skipTest("no publish list here")
        listed = public.read_text().split()
        self.assertIn("plugin/", listed)
        self.assertIn("tests/", listed)


class OwnerReads(Base):
    def test_the_owner_token_reads_what_the_page_reads(self):
        # The mod has no browser sign-in: it reads the chat with the owner's token, as `crewchat` commands do.
        path = "/api/poll?after=-1&v=-1&wait=0"
        status, body, headers = self.raw(path, headers={"Authorization": "Bearer " + self.owner})
        self.assertEqual(status, 200, body)
        data = json.loads(body)
        self.assertIn("needs", data)
        self.assertIn("messages", data)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.raw(path, headers={"Authorization": "Bearer " + self.token})[0], 403)  # an agent's
        self.assertEqual(self.raw(path, headers={"Authorization": "Bearer nope", "X-Forwarded-For": "9.9.9.3"})[0], 401)
        self.assertEqual(self.raw(path)[0], 401)  # nobody
        status, body, _ = self.raw(path, headers={"Authorization": "Bearer " + self.owner, "X-Crewchat-Chat": "nope"})
        self.assertEqual(status, 404)

    def test_the_mods_light_read_is_not_an_open_page(self):
        # Web Push skips the owner while a chat page polls. The mod reads every few seconds with the owner's token: that
        # is not a page, so it must not hold back the owner's notifications for as long as Claude Code runs.
        self.say("all", "light read")
        owner = {"Authorization": "Bearer " + self.owner}
        self.hub.page_seen = 0
        status, body, _ = self.raw("/api/poll?lite=1&after=-1", headers=owner)
        self.assertEqual(status, 200, body)
        data = json.loads(body)
        self.assertEqual(set(data), {"chat", "first", "project", "version", "messages", "more", "needs"})
        self.assertEqual((data["first"], data["project"], data["messages"][-1]["text"]), (True, "Demo", "light read"))
        last = data["messages"][-1]["seq"]
        self.assertEqual(json.loads(self.raw("/api/poll?lite=1&after=%d" % last, headers=owner)[1])["messages"], [])
        self.assertEqual(self.raw("/api/poll?after=-1&v=-1&wait=0", headers=owner)[0], 200)  # a token's full read too
        self.assertEqual(self.hub.page_seen, 0)
        opener, _ = self.owner_browser()
        self.assertEqual(self.raw("/api/poll?after=-1&v=-1&wait=0", opener=opener)[0], 200)
        self.assertGreater(self.hub.page_seen, 0)  # the page


@unittest.skipUnless(NODE, "node is not installed")
class TheMod(Base):
    # The hook command and a linked session, as the hook tests have them.
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.folder = Path(tempfile.mkdtemp(prefix="hooks-", dir=TMP))
        crewchat.install_claude(cls.folder, cls.base, cls.token)
        cls.state = tempfile.mkdtemp(prefix="hookstate-", dir=TMP)

    setUp, hook, linked = test_crewchat.Hooks.setUp, test_crewchat.Hooks.hook, test_crewchat.Hooks.linked

    def ask(self, conversation, tool="Bash", text="git push"):
        """A permission prompt waiting in Needs you: (thread, what it got, the item)."""
        session, name = self.linked(conversation)
        place = self.hub.agents[name]["place"]
        key = next(k for k, n in self.hub.links.items() if n == name)
        got = {}
        t = threading.Thread(target=lambda: got.update(self.hub.ask_permission(place, key, tool, text, wait=30)))
        t.start()
        for _ in range(200):
            item = next((n for n in self.hub.needs_you() if n["kind"] == "permission" and n["agent"] == name), None)
            if item:
                return t, got, item
            time.sleep(0.05)
        self.fail("no permission prompt")

    def test_the_band_says_what_needs_you_and_the_pane_shows_the_chat(self):
        session, name = self.linked("mod-band")
        session.call("hub_send", to="Owner", text="The outbox drains on reconnect now.", ask=True)
        self.hub.page_seen = 0
        drew = run_mod(self.folder)
        band, pane = texts(drew["band"]), texts(drew["pane"])
        self.assertIn("crewchat · 1 question", band)
        self.assertIn("The outbox drains on reconnect now.", band)  # the last message
        self.assertEqual(buttons(drew["band"]), ["pane", "page"])
        self.assertIn("Demo · 1 question", pane)
        self.assertRegex(pane, re.escape(name) + r"\s+→ Owner: The outbox drains on reconnect now\.")
        self.assertNotIn("yes:message:", " ".join(buttons(drew["pane"])))  # a message is answered on the page
        self.assertEqual(drew["commands"], ["crewchat"])
        reads = [f for f in drew["fetched"] if "/api/poll" in f["url"]]
        self.assertTrue(reads and all(f["url"].startswith(self.base + "/api/poll?lite=1&") for f in reads))
        self.assertEqual(reads[0]["headers"]["Authorization"], "Bearer " + self.owner)
        self.assertNotIn("X-Crewchat-Chat", reads[0]["headers"])  # the server finds the project from the folder
        self.assertEqual(urllib.parse.unquote(reads[0]["headers"]["X-Crewchat-Folder"]), str(self.folder))
        self.assertEqual(self.hub.page_seen, 0)  # not an open page: Web Push still reaches the owner
        self.assertEqual(run_mod(self.folder, "command")["opened"], ["crewchat"])  # /crewchat opens the pane

    def test_its_timer_redraws_only_what_changed_and_reads_the_server_once(self):
        # Every few seconds the mod asks the server; the band and the pane are drawn again only when what they show
        # changed, and crewchat's config and token are read once a session.
        drew = run_mod(self.folder, "tick", "3")
        self.assertEqual(len([f for f in drew["fetched"] if "/api/poll" in f["url"]]), 4)  # the start, and each tick
        self.assertEqual(drew["invalidated"], 0)
        self.assertEqual(len([p for p in drew["read"] if p.endswith("config.json")]), 1)

    def test_two_reads_at_once_add_a_message_once(self):
        # The timer and /crewchat (or an answer's redraw) can ask at the same moment: both must not add the same batch.
        drew = run_mod(self.folder, "overlap", "Said once, read twice.")
        self.assertEqual(texts(drew["after"]["pane"]).count("Said once, read twice."), 1)

    def test_approve_and_deny_answer_a_permission_prompt(self):
        t, got, item = self.ask("mod-yes")
        drew = run_mod(self.folder, "press", "yes:" + item["key"])
        t.join(30)
        self.assertEqual(got["answer"], "allow")
        self.assertIn("to approve", texts(drew["band"]))
        self.assertIn("Bash: git push", texts(drew["pane"]))
        self.assertEqual(drew["toasts"], ["Approved: " + item["agent"]])
        answered = [f for f in drew["fetched"] if f["method"] == "POST"]
        self.assertEqual([(f["url"], json.loads(f["body"])) for f in answered],
                         [(self.base + "/api/needs", {"key": item["key"], "answer": "allow"})])
        self.assertNotIn("yes:" + item["key"], buttons(drew["after"]["pane"]))  # gone once answered
        self.assertEqual(drew["invalidated"], 1)  # drawn again: the item went

        t, got, item = self.ask("mod-no", tool="Write", text="notes.md")
        drew = run_mod(self.folder, "press", "no:" + item["key"])
        t.join(30)
        self.assertEqual(got["answer"], "deny")
        self.assertEqual(drew["toasts"], ["Denied: " + item["agent"]])

    def test_a_long_command_is_shown_whole(self):
        # The owner approves what they read: a long command is never cut in the pane (e2e test, 11 Nov 2026).
        command = "git add README.md && " + " && ".join("git commit -m step%d" % i for i in range(30)) + " && git push"
        t, got, item = self.ask("mod-long", text=command)
        drew = run_mod(self.folder)
        self.assertIn(command, texts(drew["pane"]))
        self.hub.answer_permission(item["id"], "deny")
        t.join(30)

    def test_a_plan_is_approved_without_accepting_edits(self):
        t, got, item = self.ask("mod-plan", tool=crewchat.PLAN_TOOL, text="## Plan")
        self.assertEqual(item["choices"], ["allow_edits", "allow", "deny"])
        run_mod(self.folder, "press", "yes:" + item["key"])
        t.join(30)
        self.assertEqual(got["answer"], "allow")

    def test_open_page_signs_in_with_a_fresh_code(self):
        drew = run_mod(self.folder, "press", "page")
        url = drew["ran"][-1][-1]
        self.assertEqual(drew["ran"], [["uname"], ["open", url]])  # the harness says it is a Mac
        self.assertTrue(url.startswith(self.base + "/login?code="), url)
        status, _, headers = self.raw(url[len(self.base):])
        self.assertEqual((status, headers["Location"]), (303, "/"))  # the code signs the browser in
        drew = run_mod(self.folder, "press", "page", windows=True)  # Windows has no uname, and opens a URL so
        url = drew["ran"][-1][-1]
        self.assertEqual(drew["ran"], [["rundll32", "url.dll,FileProtocolHandler", url]])
        self.assertTrue(url.startswith(self.base + "/login?code="), url)

    def test_the_folder_picks_its_project(self):
        # As `crewchat` commands find it: the connected folder the session is in or below, by its real path, so a
        # session opened through a symlink (/tmp and /private/tmp on macOS, a linked ~/code) is still in its project.
        projects = self.server.RequestHandlerClass.projects
        other = crewchat.create_project("mod-other")
        projects.refresh()
        self.addCleanup(projects.remove, other["id"])
        folder = Path(tempfile.mkdtemp(prefix="mod-folder-", dir=TMP)).resolve()
        (folder / "src" / "app").mkdir(parents=True)
        link = Path(tempfile.mkdtemp(prefix="mod-link-", dir=TMP)) / "code"
        link.symlink_to(folder, target_is_directory=True)
        config = crewchat.load_config()
        self.addCleanup(crewchat.save_config, json.loads(json.dumps(config)))
        config.setdefault("folders", {})[str(folder)] = "mac"
        config.setdefault("folder_chats", {})[str(folder)] = other["id"]
        crewchat.save_config(config)
        projects.get(other["id"]).hub.send(crewchat.OWNER, "all", "Only in the other project.")
        drew = run_mod(link / "src" / "app")
        self.assertIn("Only in the other project.", texts(drew["band"]))
        self.assertIn("mod-other · ", texts(drew["pane"]))
        drew = run_mod(link / "src", "press", "page")
        self.assertIn("&chat=" + other["id"], drew["ran"][-1][-1])  # its page, signed in

    def test_without_crewchat_it_stays_out_of_the_way(self):
        home = Path(tempfile.mkdtemp(prefix="mod-none-", dir=TMP))
        drew = run_mod(self.folder, home=home)
        self.assertEqual((drew["band"]["passed"], drew["fetched"]), (True, []))  # the band is left to others
        self.assertIn("not set up", texts(drew["pane"]))

    def test_a_server_that_is_not_running_says_so(self):
        home = Path(tempfile.mkdtemp(prefix="mod-down-", dir=TMP))
        (home / "tokens").mkdir()
        (home / "tokens" / "Owner.token").write_text("x" * 40)
        (home / "config.json").write_text(json.dumps({"port": 9}))  # nothing listens there
        drew = run_mod(self.folder, home=home)
        self.assertIn("crewchat is not running here", texts(drew["band"]))


if __name__ == "__main__":
    unittest.main()
