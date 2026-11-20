"""Path-to-10 guards for workspaces: a network drive warns, one server per workspace folder (the open-here mark),
and the page's not-found menu, Forget and held styling."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from crewchat_fixture import (  # noqa: E402,F401  (the shared server fixture)
    Path, TMP, crewchat, json, mock, shutil, socket, subprocess, tempfile, threading, time, types, unittest,
)


class OneServerPerWorkspace(unittest.TestCase):
    """Path-to-10 review: a workspace on a network drive warns; each server marks the workspaces it has open."""

    def test_a_network_drive_is_told_apart(self):
        mounts = [("/", "ext4"), ("/mnt/share", "cifs"), ("/Volumes/team", "smbfs"), ("/mnt/share/local", "ext4")]
        nd = crewchat.network_drive
        self.assertEqual(nd("/mnt/share/ws/Bluebird", windows=False, mounts=mounts), "cifs")
        self.assertEqual(nd("/Volumes/team/x", windows=False, mounts=mounts), "smbfs")
        self.assertEqual(nd("/mnt/share/local/x", windows=False, mounts=mounts), "")  # the longest mount point wins
        self.assertEqual(nd("/home/u/crewchat-workspaces/x", windows=False, mounts=mounts), "")
        self.assertEqual(nd("/mnt/sharex/y", windows=False, mounts=mounts), "")
        self.assertIn("server", nd("\\\\nas\\team\\ws", windows=False, mounts=[]))
        self.assertEqual(nd("/x", windows=False, mounts=[]), "")

    def test_new_project_warns_about_a_network_drive(self):
        folder = Path(tempfile.mkdtemp(prefix="nd-", dir=TMP)).resolve() / "ws"
        with mock.patch.object(crewchat, "network_drive", lambda path: "nfs"):
            got = crewchat.workspace_warnings(folder, [])
        self.assertEqual(len(got), 1, got)
        self.assertIn("network drive (nfs)", got[0])

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="mark-home-", dir=TMP)).resolve()
        self.ws = Path(tempfile.mkdtemp(prefix="mark-ws-", dir=TMP)).resolve()

    def mark(self, **fields):
        (self.ws / crewchat.OPEN_HERE_FILE).write_text(json.dumps(fields))

    def test_the_mark_says_this_machine_and_skips_the_crewchat_home(self):
        inside = self.base / "projects" / "web"
        inside.mkdir(parents=True)
        crewchat.mark_open_here(inside, self.base)
        self.assertFalse((inside / crewchat.OPEN_HERE_FILE).exists())
        crewchat.mark_open_here(self.ws, self.base)
        mark = json.loads((self.ws / crewchat.OPEN_HERE_FILE).read_text())
        self.assertEqual(mark["machine"], crewchat.machine_mark(self.base))
        self.assertEqual(mark["machine"], (self.base / crewchat.MACHINE_ID_FILE).read_text().strip())
        self.assertEqual(mark["host"], socket.gethostname())
        self.assertLess(abs(mark["time"] - time.time()), 5)
        self.assertEqual(crewchat.open_elsewhere(self.ws, base=self.base), "")
        (self.ws / crewchat.OPEN_HERE_FILE).write_text("{")
        self.assertEqual(crewchat.open_elsewhere(self.ws, base=self.base), "")

    def test_the_refusal_says_how_long_until_a_stale_mark_clears(self):
        """A reinstalled machine met its own old mark: the message says when it stops counting, not only "wait"."""
        self.mark(machine="0123456789abcdef", host="another-pc", time=int(time.time()) - 60)
        with self.assertRaises(crewchat.HubError) as refused:
            crewchat.refuse_open_elsewhere(self.ws, base=self.base)
        self.assertIn("wait about 9 minutes", str(refused.exception))
        self.assertEqual(crewchat.open_mark_wait(self.ws, now=time.time() + crewchat.OPEN_FRESH), 0)

    def test_two_machines_with_one_host_name_are_told_apart_by_their_ids(self):
        """Two Macs both called "MacBook-Air": the host name alone would let both open one workspace."""
        self.mark(machine="0123456789abcdef", host=socket.gethostname(), time=int(time.time()))
        self.assertEqual(crewchat.open_elsewhere(self.ws, base=self.base), socket.gethostname())
        with self.assertRaises(crewchat.HubError):
            crewchat.refuse_open_elsewhere(self.ws, base=self.base)

    def test_a_renamed_machine_still_knows_its_own_mark(self):
        crewchat.mark_open_here(self.ws, self.base)
        with mock.patch.object(crewchat.socket, "gethostname", lambda: "renamed"):
            self.assertEqual(crewchat.open_elsewhere(self.ws, base=self.base), "")
            crewchat.refuse_open_elsewhere(self.ws, base=self.base)

    def test_a_mark_with_no_machine_id_falls_back_to_the_host_name(self):
        self.mark(host="other-pc", time=int(time.time()))
        self.assertEqual(crewchat.open_elsewhere(self.ws, base=self.base), "other-pc")
        self.mark(host=socket.gethostname(), time=int(time.time()))
        self.assertEqual(crewchat.open_elsewhere(self.ws, base=self.base), "")

    def test_a_home_that_cannot_be_written_still_knows_its_own_mark(self):
        with mock.patch.object(crewchat, "write_text", side_effect=OSError("read-only")):
            first = crewchat.machine_mark(self.base)
            self.assertEqual(crewchat.machine_mark(self.base), first)
            crewchat.mark_open_here(self.ws, self.base)
            self.assertEqual(crewchat.open_elsewhere(self.ws, base=self.base), "")

    def test_two_processes_making_the_id_at_once_agree_on_it(self):
        """Round-9 review: two servers starting together each wrote their own id, and one saw its own mark as another
        machine's. The id is put in place only if none is there yet; the loser reads the winner's."""
        real_link = os.link

        def first_wins(src, dst):  # another process made the id between the read and this one's write
            Path(dst).write_text("feedfacecafebeef\n")
            real_link(src, dst)
        with mock.patch.object(crewchat.os, "link", side_effect=first_wins):
            self.assertEqual(crewchat.machine_mark(self.base), "feedfacecafebeef")
        self.assertEqual(crewchat.machine_mark(self.base), "feedfacecafebeef")
        self.assertEqual(sorted(p.name for p in self.base.iterdir() if p.name.startswith(crewchat.MACHINE_ID_FILE)),
                         [crewchat.MACHINE_ID_FILE])  # no spare left behind

    def test_a_stale_mark_is_ignored_either_way_round(self):
        """A machine whose clock runs ahead writes times from the future: those count too, and go stale too."""
        now = time.time()
        self.mark(machine="0123456789abcdef", host="other-pc", time=int(now))
        self.assertEqual(crewchat.open_elsewhere(self.ws, now=now + crewchat.OPEN_FRESH + 1, base=self.base), "")
        self.assertEqual(crewchat.open_elsewhere(self.ws, now=now - 120, base=self.base), "other-pc")
        self.assertEqual(crewchat.open_elsewhere(self.ws, now=now - crewchat.OPEN_FRESH - 1, base=self.base), "")

    def test_remove_clears_only_this_machine_s_mark(self):
        crewchat.mark_open_here(self.ws, self.base)
        crewchat.clear_open_mark(self.ws, self.base)
        self.assertFalse((self.ws / crewchat.OPEN_HERE_FILE).exists())
        self.mark(machine="0123456789abcdef", host="other-pc", time=int(time.time()))
        crewchat.clear_open_mark(self.ws, self.base)
        self.assertTrue((self.ws / crewchat.OPEN_HERE_FILE).exists())
        crewchat.clear_open_mark(self.base / "not-there", self.base)  # nothing to clear is not an error

    def hub(self):
        return types.SimpleNamespace(roster=types.SimpleNamespace(root=self.ws), machine_root=self.base,
                                     lock=threading.RLock(), _append=mock.Mock())

    def test_each_minute_marks_again_unless_another_machine_has_it_open(self):
        """Round-7 review: this machine asleep or off past OPEN_FRESH, another opened the workspace meanwhile. The
        minute's look leaves the other machine's mark and tells the owner once, not every minute."""
        hub = self.hub()
        self.assertEqual(crewchat.open_mark_tick(hub), "")
        self.assertEqual(json.loads((self.ws / crewchat.OPEN_HERE_FILE).read_text())["machine"],
                         crewchat.machine_mark(self.base))
        hub._append.assert_not_called()
        self.mark(machine="0123456789abcdef", host="other-pc", time=int(time.time()))
        self.assertEqual(crewchat.open_mark_tick(hub), "other-pc")
        self.assertEqual(crewchat.open_mark_tick(hub), "other-pc")
        self.assertEqual(json.loads((self.ws / crewchat.OPEN_HERE_FILE).read_text())["host"], "other-pc")  # left
        hub._append.assert_called_once()
        sender, to, text, kind = hub._append.call_args[0]
        self.assertEqual((sender, to, kind), (crewchat.SERVER_NAME, crewchat.OWNER, "msg"))
        self.assertIn("open in crewchat on other-pc too", text)
        (self.ws / crewchat.OPEN_HERE_FILE).unlink()  # removed there: marked here again
        self.assertEqual(crewchat.open_mark_tick(hub), "")
        self.assertTrue((self.ws / crewchat.OPEN_HERE_FILE).is_file())

    def test_the_first_look_is_at_once_not_after_a_minute(self):
        hub = self.hub()
        projects = types.SimpleNamespace(all=lambda: [types.SimpleNamespace(hub=hub)])
        with mock.patch.object(crewchat.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                crewchat.routine_checks(projects)
        self.assertTrue((self.ws / crewchat.OPEN_HERE_FILE).is_file())

    def test_open_and_locate_mark_the_workspace_at_once(self):
        home = Path(tempfile.mkdtemp(prefix="mark-open-", dir=TMP)).resolve()
        ws = Path(tempfile.mkdtemp(prefix="mark-open-ws-", dir=TMP)).resolve() / "Marked"
        crewchat.create_project("Marked", home, folder=ws)
        self.assertFalse((ws / crewchat.OPEN_HERE_FILE).exists())  # made, not opened by a server yet
        other = Path(tempfile.mkdtemp(prefix="mark-open-2-", dir=TMP)).resolve()
        crewchat.open_workspace(str(ws), other)
        mark = json.loads((ws / crewchat.OPEN_HERE_FILE).read_text())
        self.assertEqual(mark["machine"], crewchat.machine_mark(other))

    def test_a_double_slash_posix_path_is_not_a_network_share(self):
        nd = crewchat.network_drive
        self.assertEqual(nd("//home/u/ws", windows=False, mounts=[("/", "ext4")]), "")
        self.assertIn("server", nd("//nas/team/ws", windows=True, mounts=[]))


class PathTo10Page(unittest.TestCase):
    """Path-to-10 review (UX): on a phone the switcher is hidden, so a workspace not found must be reachable from the
    project menu; Forget deletes nothing, so it is not dressed as a danger; a held workspace is a choice, not an error."""

    page = crewchat.CHAT_PAGE

    def test_the_phone_menu_lists_the_workspaces_not_found_and_opens_locate(self):
        self.assertIn("}), ...missingGroup());", self.page)
        self.assertIn('new Option(m.name + " (not found)", "missing:" + m.id)', self.page)
        handler = self.page[self.page.index('$("projects").addEventListener("change"'):]
        handler = handler[:handler.index("});")]
        self.assertIn('if (m) askLocate(m);', handler)
        self.assertIn("e.target.dataset.current", handler)
        self.assertIn('id="locate-forget"', self.page)
        self.assertIn("if (m) askForget(m);", self.page)

    def test_the_menu_s_not_found_group(self):
        if not shutil.which("node"):
            self.skipTest("needs node")
        code = self.page[self.page.index("function missingGroup()"):]
        code = code[:code.index("\n}\n") + 3]
        js = ("const state = {missing: [{id: 'a', name: 'Bluebird', path: '/x'}]};"
              "class Option { constructor(t, v) { this.text = t; this.value = v; } }"
              "const document = {createElement: () => ({kids: [], append(...k) { this.kids.push(...k); }})};"
              + code + "const g = missingGroup(); state.missing = [];"
              "console.log(JSON.stringify([g[0].label, g[0].kids, missingGroup()]))")
        out = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(out, ["Not found", [{"text": "Bluebird (not found)", "value": "missing:a"}], []])

    def test_forget_and_held_are_not_shown_as_failures(self):
        self.assertIn('<button type="submit" id="forget-go">Forget</button>', self.page)
        self.assertNotIn('"secondary danger", "Forget"', self.page)
        for rule in ("#held-banner {", "#held-card {"):
            css = self.page[self.page.index(rule):]
            css = css[:css.index("}")]
            self.assertIn("var(--warn)", css, rule)
            self.assertNotIn("var(--err)", css, rule)
