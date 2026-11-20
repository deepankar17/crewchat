"""Commits and leases, `crewchat start`, the welcome screen and the lean install."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from crewchat_fixture import (  # noqa: E402,F401  (the shared server fixture)
    Base, GIT_PEOPLE, Path, ROOT, Session, TMP, argparse, cli, committed_repo, contextlib, crewchat, io, json, mock,
    os, shutil, subprocess, tempfile, threading, time, types, unittest,
)


class CommitsAndLeases(Base):
    """W8: an agent's commits carry Crewchat-Task and Crewchat-Chat trailers and show in their task's thread with Why;
    agents lease files with hub_task lease, and another agent's commit over a lease is stopped, with Ask the holder and
    Release in Needs you. The git hooks chain with the ones a repository has (husky, pre-commit), never replace them."""

    def launched(self):
        """An agent crewchat started: linked with its start key, which its shell has as CREWCHAT_LINK_KEY."""
        session = Session(self.base, self.token)
        key = self.hub.new_start()
        session.call("hub_link", key=key)
        return session, session.me, key

    def working(self, title="Offline queue"):
        session, name, key = self.launched()
        task = self.say(name, title, "task")
        session.call("hub_task", action="take", id=task)
        return session, name, key, task

    def repo(self, hooks=None):
        """A connected folder that is a git repository, with these hooks (name -> text) in .git/hooks first."""
        folder = committed_repo({"README.md": "x\n"})
        crewchat.install_claude(folder, self.base, self.token)
        crewchat.git_exclude(folder, [".mcp.json", ".claude/", "CLAUDE.md", "AGENTS.md"])
        for name, text in (hooks or {}).items():
            path = folder / ".git" / "hooks" / name
            path.write_text(text)
            path.chmod(0o755)
        return folder

    def commit(self, folder, rel, key=None, message="Change"):
        (folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (folder / rel).write_text("%s %s\n" % (rel, time.time()))
        env = dict(os.environ, **GIT_PEOPLE)
        env.pop("CREWCHAT_LINK_KEY", None)
        if key:
            env["CREWCHAT_LINK_KEY"] = key
        subprocess.run(["git", "add", rel], cwd=folder, check=True, capture_output=True)
        return subprocess.run(["git", "commit", "-qm", message], cwd=folder, capture_output=True, text=True, env=env,
                              timeout=60)

    def head(self, folder):
        return subprocess.run(["git", "log", "-1", "--format=%H%n%B"], cwd=folder, capture_output=True, text=True).stdout

    def test_a_lease_is_an_action_of_hub_task_and_ends_with_its_task(self):
        luca, luca_name, _, task = self.working()
        said = luca.text("hub_task", action="lease", areas="app/src/**/Outbox*.kt", minutes=45)
        self.assertIn("Leased app/src/**/Outbox*.kt until", said)
        row = next(r for r in self.hub.sheet_rows() if r["id"] == task)
        self.assertEqual([(l["by"], l["paths"]) for l in row["leases"]], [(luca_name, ["app/src/**/Outbox*.kt"])])
        self.assertAlmostEqual(row["leases"][0]["until"], time.time() + 45 * 60, delta=5)
        self.assertEqual(self.hub.messages[-1]["kind"], "lease")
        self.assertIn("[LEASE]", crewchat.fmt(self.hub.messages[-1], "x"))
        ravi, _, _ = self.launched()
        refused = ravi.call("hub_task", action="lease", areas="app/src/data/OutboxStore.kt")
        self.assertTrue(refused["isError"])
        self.assertIn("leased to %s" % luca_name, refused["content"][0]["text"])
        for bad in ({"areas": "../secrets"}, {"areas": "/etc/x"}, {"areas": "a", "minutes": 9999}, {"areas": ""}):
            self.assertTrue(ravi.call("hub_task", action="lease", **bad)["isError"], bad)
        self.assertIn("Ended your lease", luca.text("hub_task", action="lease", minutes=0))
        self.assertEqual(next(r for r in self.hub.sheet_rows() if r["id"] == task)["leases"], [])
        luca.text("hub_task", action="lease", areas="docs/")
        luca.call("hub_task", action="update", id=task, status="done", note="shipped\nDecisions: none\nLeft over: none")
        self.assertEqual(next(r for r in self.hub.sheet_rows() if r["id"] == task)["leases"], [])  # ended with its task
        self.assertFalse(any(l["by"] == luca_name for l in self.hub._leases()))

    def test_leases_come_back_from_the_log(self):
        luca, luca_name, _, task = self.working()
        luca.text("hub_task", action="lease", areas="lib/logged/", minutes=30)
        again = crewchat.Hub(self.hub.roster)
        self.assertIn((luca_name, ["lib/logged/"], task), [(l["by"], l["paths"], l["task"]) for l in again._leases()])
        again.close_stores()

    def test_what_a_lease_covers(self):
        for pattern, path, covered in (("app/", "app/a.kt", True), ("app", "app/b/c.kt", True), ("app", "apple.kt", False),
                                       ("src/**/Outbox*.kt", "src/data/x/OutboxStore.kt", True),
                                       ("src/**/Outbox*.kt", "src/OutboxStore.kt", True),
                                       ("src/*.py", "src/a/b.py", False), ("*.md", "docs/guide.md", True),
                                       ("docs/*.md", "docs/a/guide.md", False), ("a?.txt", "ab.txt", True),
                                       ("README.md", "README.md", True)):
            self.assertEqual(crewchat.lease_covers(pattern, path), covered, (pattern, path))
        self.assertTrue(crewchat.leases_overlap("src/", "src/app.py"))
        self.assertTrue(crewchat.leases_overlap("src/**", "src/app.py"))
        self.assertFalse(crewchat.leases_overlap("src/", "docs/"))

    def test_an_agents_commit_gets_its_task_and_chat_and_shows_in_the_thread(self):
        marker = Path(tempfile.mkdtemp(prefix="theirs-", dir=TMP)) / "ran"
        folder = self.repo({"post-commit": "#!/bin/sh\necho post >> '%s'\n" % marker,
                            "prepare-commit-msg": "#!/usr/bin/env python3\nimport sys\n"
                                                  "open(sys.argv[1], 'a').write('\\nTheirs: kept\\n')\n"})
        self.assertEqual(sorted(crewchat.install_git_hooks(folder)), sorted(crewchat.GIT_HOOKS))
        hooks = folder / ".git" / "hooks"
        self.assertTrue((hooks / "post-commit.before-crewchat").exists())
        if os.name != "nt":  # crewchat's hooks run only for this user's agents: nobody else may run them
            self.assertEqual({(hooks / name).stat().st_mode & 0o777 for name in crewchat.GIT_HOOKS}, {0o700})
        self.assertEqual(crewchat.install_git_hooks(folder), [])  # nothing changed: nothing written
        _, name, key, task = self.working()
        done = self.commit(folder, "app/outbox.py", key, "Clear the outbox on logout")
        self.assertEqual(done.returncode, 0, done.stderr)
        sha, *body = self.head(folder).splitlines()
        self.assertIn("Crewchat-Task: %s" % task, body)
        self.assertIn("Crewchat-Chat: %s" % self.server.RequestHandlerClass.project.id, body)
        self.assertIn("Theirs: kept", body)  # their prepare-commit-msg (Python) ran too
        self.assertEqual(marker.read_text(), "post\n")  # and their post-commit
        card = self.hub.messages[-1]
        self.assertEqual((card["kind"], card["from"], card["task"], card["thread"], card["commit"], card["text"]),
                         ("commit", name, task, task, sha, "Clear the outbox on logout"))
        self.assertEqual(card["areas"], "app/outbox.py")
        self.assertIn("[COMMIT %s for #%s]" % (sha[:7], task), crewchat.fmt(card, "x"))
        _, other, _ = self.launched()
        with self.hub.lock:
            self.hub._set_cursor(other, card["seq"] - 1)
            self.assertNotIn(card, self.hub._unread(other))  # on the page and in history, not in every inbox
        human = self.commit(folder, "README.md")  # the owner's own commit: nothing added, no card
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertNotIn("Crewchat-Task", self.head(folder))
        self.assertFalse([m for m in self.hub.messages if m["kind"] == "commit" and m["seq"] > card["seq"]])
        self.assertEqual(marker.read_text(), "post\npost\n")
        self.assertEqual(sorted(crewchat.remove_git_hooks(folder)), sorted(crewchat.GIT_HOOKS))
        self.assertIn("echo post", (hooks / "post-commit").read_text())  # theirs back in its place
        self.assertFalse((hooks / "pre-commit").exists())

    def test_a_commit_from_a_worktree_in_the_project_names_the_projects_files(self):
        # Owner's two-machine test, 11 Nov: an agent committing from its worktree under .claude/worktrees/ got a
        # card whose areas read .claude/worktrees/a45-cart-discount/shop/__init__.py instead of shop/__init__.py.
        folder = self.repo()
        crewchat.install_git_hooks(folder)
        tree = folder / ".claude" / "worktrees" / "a45-cart-discount"
        subprocess.run(["git", "worktree", "add", "-q", "-b", "a45", str(tree)], cwd=folder, check=True,
                       capture_output=True)
        _, _, key, task = self.working()
        done = self.commit(tree, "shop/__init__.py", key, "Cart discount")
        self.assertEqual(done.returncode, 0, done.stderr)
        card = self.hub.messages[-1]
        self.assertEqual((card["kind"], card["task"]), ("commit", task))
        self.assertEqual(card["areas"], "shop/__init__.py")

    def test_why_shows_the_task_its_decisions_and_messages(self):
        luca, name, key, task = self.working("Offline queue for messages")
        luca.call("hub_task", action="update", id=task, status="in_progress", note="Room table, then a worker")
        crewchat.post_json(self.base + "/api/send", self.owner, {"to": name, "text": "Reuse SyncWorker.", "thread": task})
        with self.hub.lock:
            place = self.hub.agents[name]["place"]
        card = self.hub.commit_made(place, key, "4f2c9a1" + "0" * 33, "Clear the outbox on logout", task, ["a.kt"])
        self.assertEqual(self.hub.commit_made(place, key, "4f2c9a1" + "0" * 33, "again", task, []), {"id": ""})  # once
        why = crewchat.post_json(self.base + "/api/task", self.owner, {"op": "why", "id": card["id"]})
        self.assertEqual((why["task"]["id"], why["task"]["title"], why["task"]["holder"]),
                         (task, "Offline queue for messages", name))
        self.assertEqual([m["text"] for m in why["decisions"]], ["Reuse SyncWorker."])
        texts = [m["text"] for m in why["messages"]]
        self.assertIn("Room table, then a worker", texts)
        self.assertEqual(texts[-1], "Clear the outbox on logout")
        status, _, _ = self.raw("/api/task", json.dumps({"op": "why", "id": task}).encode(),
                                {"Authorization": "Bearer " + self.owner, "Content-Type": "application/json"})
        self.assertEqual(status, 400)  # a task is not a commit

    def test_a_commit_over_another_agents_lease_is_stopped_until_released(self):
        marker = Path(tempfile.mkdtemp(prefix="theirs-", dir=TMP)) / "ran"
        folder = self.repo({"pre-commit": "#!/bin/sh\necho lint >> '%s'\n" % marker})
        crewchat.install_git_hooks(folder)
        luca, luca_name, _, task = self.working()
        luca.text("hub_task", action="lease", areas="app/src/**/Outbox*.kt", minutes=45)
        ravi, ravi_name, ravi_key = self.launched()
        stopped = self.commit(folder, "app/src/data/OutboxStore.kt", ravi_key, "Rename outbox column")
        self.assertNotEqual(stopped.returncode, 0)
        self.assertIn("app/src/data/OutboxStore.kt is leased to %s" % luca_name, stopped.stderr)
        self.assertFalse(marker.exists())  # stopped before their hook ran
        self.assertIn("%s's commit was stopped" % ravi_name, self.hub.messages[-1]["text"])
        need = next(n for n in self.hub.needs_you() if n["kind"] == "lease")
        self.assertEqual((need["agent"], need["holder"], need["task"], need["files"], need["choices"]),
                         (ravi_name, luca_name, task, ["app/src/data/OutboxStore.kt"], ["ask", "release"]))
        self.commit(folder, "app/src/data/OutboxStore.kt", ravi_key)  # again: still one item, told once
        self.assertEqual(len([n for n in self.hub.needs_you() if n["kind"] == "lease"]), 1)
        crewchat.post_json(self.base + "/api/needs", self.owner, {"key": need["key"], "answer": "ask"})
        asked = self.hub.messages[-1]
        self.assertEqual((asked["from"], asked["to"]), ("Owner", luca_name))
        self.assertIn("minutes=0", asked["text"])
        self.assertTrue(next(n for n in self.hub.needs_you() if n["kind"] == "lease")["asked"])
        human = self.commit(folder, "app/src/data/OutboxStore.kt")  # the owner's own commit is not an agent's
        self.assertEqual(human.returncode, 0, human.stderr)
        crewchat.post_json(self.base + "/api/needs", self.owner, {"key": need["key"], "answer": "release"})
        self.assertFalse([n for n in self.hub.needs_you() if n["kind"] == "lease"])
        told = [m for m in self.hub.messages[-3:] if m["to"] == ravi_name]
        self.assertIn("you may commit", told[0]["text"])
        done = self.commit(folder, "app/src/data/OutboxStore.kt", ravi_key)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(marker.read_text(), "lint\nlint\n")  # their hook ran for both commits that went ahead
        self.assertEqual(self.commit(folder, "app/src/Other.kt", ravi_key).returncode, 0)

    def test_the_owner_releases_a_lease_from_its_task(self):
        luca, luca_name, _, task = self.working()
        luca.text("hub_task", action="lease", areas="lib/released/")
        lease = next(r for r in self.hub.sheet_rows() if r["id"] == task)["leases"][0]
        crewchat.post_json(self.base + "/api/task", self.owner, {"op": "release", "lease": lease["id"]})
        self.assertEqual(next(r for r in self.hub.sheet_rows() if r["id"] == task)["leases"], [])
        self.assertIn("Released %s's lease on lib/released/" % luca_name, self.hub.messages[-1]["text"])
        status, _, _ = self.raw("/api/task", json.dumps({"op": "release", "lease": lease["id"]}).encode(),
                                {"Authorization": "Bearer " + self.owner, "Content-Type": "application/json"})
        self.assertEqual(status, 400)  # ended already

    def test_the_hooks_chain_with_husky_and_pre_commit(self):
        marker = Path(tempfile.mkdtemp(prefix="theirs-", dir=TMP)) / "ran"
        # husky 9: core.hooksPath .husky/_, whose wrappers find the owner's script by their own name ($0).
        folder = self.repo()
        husky = folder / ".husky"
        (husky / "_").mkdir(parents=True)
        (husky / "_" / "h").write_text('#!/usr/bin/env sh\nn=$(basename "$0")\ns=$(dirname "$(dirname "$0")")/$n\n'
                                       '[ ! -f "$s" ] && exit 0\nsh -e "$s" "$@"\n')
        (husky / "_" / "pre-commit").write_text('#!/usr/bin/env sh\n. "$(dirname "$0")/h"\n')
        (husky / "_" / "pre-commit").chmod(0o755)
        (husky / "pre-commit").write_text("echo husky >> '%s'\n" % marker)
        subprocess.run(["git", "config", "core.hooksPath", ".husky/_"], cwd=folder, check=True)
        crewchat.install_git_hooks(folder)
        self.assertTrue(crewchat.is_our_git_hook(husky / "_" / "pre-commit"))
        self.assertFalse((folder / ".git" / "hooks" / "pre-commit").exists())
        self.assertEqual(self.commit(folder, "a.txt").returncode, 0)
        self.assertEqual(marker.read_text(), "husky\n")  # husky's hook still runs, found by its name
        # husky writes its wrappers again (npm install): its own work, then crewchat's chained again at the next start.
        (husky / "_" / "pre-commit").write_text('#!/usr/bin/env sh\n. "$(dirname "$0")/h"\n')
        crewchat.install_git_hooks(folder)
        self.assertEqual(self.commit(folder, "b.txt").returncode, 0)
        self.assertEqual(marker.read_text(), "husky\nhusky\n")
        # pre-commit (the framework): a bash hook in .git/hooks; `pre-commit install` later moves crewchat's to .legacy.
        folder = self.repo({"pre-commit": '#!/usr/bin/env bash\nARGS=(pre commit)\necho "${ARGS[@]}" >> \'%s\'\n' % marker})
        crewchat.install_git_hooks(folder)
        self.assertEqual(self.commit(folder, "a.txt").returncode, 0)
        self.assertEqual(marker.read_text(), "husky\nhusky\npre commit\n")
        hooks = folder / ".git" / "hooks"
        os.replace(hooks / "pre-commit", hooks / "pre-commit.legacy")
        (hooks / "pre-commit").write_text('#!/usr/bin/env bash\n"$(dirname "$0")/pre-commit.legacy" "$@" || exit\n'
                                          "echo framework >> '%s'\n" % marker)
        (hooks / "pre-commit").chmod(0o755)
        crewchat.install_git_hooks(folder)
        self.assertIn("framework", (hooks / "pre-commit").read_text())  # its own, left as it is
        self.assertEqual(self.commit(folder, "b.txt").returncode, 0)
        self.assertEqual(marker.read_text().splitlines()[-2:], ["pre commit", "framework"])
        _, _, key, _ = self.working()
        self.assertIn("Crewchat-Task", self.head(folder) if self.commit(folder, "c.txt", key).returncode == 0 else "")
        # A hooks folder for every repository (~/.gitconfig) is not this folder's: crewchat leaves it alone.
        shared = Path(tempfile.mkdtemp(prefix="all-hooks-", dir=TMP)).resolve()
        folder = self.repo()
        subprocess.run(["git", "config", "core.hooksPath", str(shared)], cwd=folder, check=True)
        self.assertIsNone(crewchat.git_hooks_dir(folder))
        self.assertEqual(crewchat.install_git_hooks(folder), [])
        self.assertEqual(list(shared.iterdir()), [])

    def test_a_folder_gets_the_git_hooks_with_the_others_and_loses_them_when_disconnected(self):
        folder = self.repo()
        self.assertIn("pre-commit", crewchat.update_hooks(folder))
        self.assertTrue(crewchat.is_our_git_hook(folder / ".git" / "hooks" / "pre-commit"))
        crewchat.disconnect_folder(folder)
        self.assertFalse((folder / ".git" / "hooks" / "pre-commit").exists())
        plain = Path(tempfile.mkdtemp(prefix="plain-", dir=TMP))  # not a repository: nothing to do
        self.assertEqual(crewchat.install_git_hooks(plain), [])

    def test_a_commit_is_told_once_through_an_amend_and_a_rebase(self):
        folder = self.repo()
        crewchat.install_git_hooks(folder)
        _, name, key, task = self.working()
        cards = lambda: [m for m in self.hub.messages if m["kind"] == "commit" and m.get("task") == task]  # the class's
        self.assertEqual(self.commit(folder, "app/outbox.py", key, "Clear the outbox").returncode, 0)  # hub is shared
        self.assertEqual(len(cards()), 1)
        env = dict(os.environ, **GIT_PEOPLE, CREWCHAT_LINK_KEY=key)
        env.pop("GIT_REFLOG_ACTION", None)  # as a newer git leaves it, in a rebase too
        amend = subprocess.run(["git", "commit", "-q", "--amend", "-m", "Clear the outbox on logout"], cwd=folder,
                               capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(amend.returncode, 0, amend.stderr)
        self.assertEqual(len(cards()), 1)  # the amend takes the place of the commit in the chat
        mine = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=folder, capture_output=True,
                              text=True).stdout.strip()
        subprocess.run(["git", "checkout", "-qb", "base", "HEAD~1"], cwd=folder, check=True, capture_output=True)
        self.assertEqual(self.commit(folder, "README2.md").returncode, 0)  # the owner's, on the base
        subprocess.run(["git", "checkout", "-q", mine], cwd=folder, check=True, capture_output=True)
        rebase = subprocess.run(["git", "rebase", "-q", "base"], cwd=folder, capture_output=True, text=True, env=env,
                                timeout=60)
        self.assertEqual(rebase.returncode, 0, rebase.stderr)
        self.assertIn("Crewchat-Task: %s" % task, self.head(folder))  # the rewritten commit, still for the task
        self.assertEqual(len(cards()), 1)  # and not told again
        self.assertEqual(self.commit(folder, "app/next.py", key, "Retry the outbox").returncode, 0)
        self.assertEqual([m["text"] for m in cards()], ["Clear the outbox", "Retry the outbox"])  # a new one is

    def test_a_commit_told_on_two_linked_machines_shows_once(self):
        _, name, key, task = self.working()
        with self.hub.lock:
            place = self.hub.agents[name]["place"]
        sha = "8e1d0b2" + "0" * 33
        card = self.hub.commit_made(place, key, sha, "Clear the outbox on logout", task, ["a.kt"])
        theirs = dict(self.hub.by_id[card["id"]], id="win-7", origin="win-1", commit=sha.upper())
        self.assertFalse(self.hub.ingest(theirs))  # the other machine's card for the same commit
        self.assertEqual([m["id"] for m in self.hub.messages if m.get("commit", "").lower() == sha], [card["id"]])
        self.assertTrue(self.hub.ingest(dict(theirs, id="win-8", commit="5" * 40)))  # another commit is shown
        self.assertEqual(self.hub.commit_made(place, key, "6" * 40, "Amended", task, [], was="5" * 40), {"id": ""})
        again = self.hub.commit_made(place, key, "7" * 40, "Clear the outbox on logout", task, ["a.kt"])
        self.assertEqual(again, {"id": ""})  # the same commit made again (reset and commit): a new sha, told once

    def test_with_the_chat_down_a_commit_goes_ahead(self):
        folder = self.repo()
        crewchat.install_git_hooks(folder)
        (folder / ".mcp.json").write_text(json.dumps({"mcpServers": {"crewchat": {
            "type": "http", "url": "http://127.0.0.1:9/mcp", "headers": {"Authorization": "Bearer x"}}}}))
        _, _, key = self.launched()
        done = self.commit(folder, "a.txt", key)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("Crewchat-Task", self.head(folder))

class StartOpensThePage(unittest.TestCase):
    """A1: `crewchat start` with no folder starts the server and opens the page; it links no folder and makes no
    project. A first machine's only project is an unlisted placeholder until a folder is connected."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="start-home-", dir=TMP)).resolve()
        self.cwd = Path(tempfile.mkdtemp(prefix="start-cwd-", dir=TMP)).resolve()
        self.here = os.getcwd()
        self.ensure_server, self.cmd_ui, self.write_shortcut = mock.Mock(), mock.Mock(), mock.Mock(return_value=[])
        for patch in (mock.patch.dict(os.environ, {"CREWCHAT_HOME": str(self.home)}),
                      mock.patch.object(crewchat, "CURRENT_CHAT", ""),
                      mock.patch.object(crewchat, "write_shortcut", self.write_shortcut),
                      mock.patch.object(crewchat, "ensure_server", self.ensure_server),
                      mock.patch.object(crewchat, "cmd_ui", self.cmd_ui)):
            patch.start()
            self.addCleanup(patch.stop)
        os.chdir(str(self.cwd))
        self.addCleanup(os.chdir, self.here)

    def serve(self):
        """A real server on this home, for the commands that talk to it (start with a folder)."""
        config = crewchat.load_config()
        server = crewchat.make_server(0)
        config["port"] = server.server_address[1]
        crewchat.save_config(config)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        projects = server.RequestHandlerClass.projects

        def stop():
            crewchat.flush_all(projects)
            server.shutdown()
            server.server_close()
            for project in projects.all() + ([projects.shared] if projects.shared is not None else []):
                project.hub.close_stores()
        self.addCleanup(stop)

    def test_what_crewchat_tells_the_owner_to_run_links_a_folder_only_by_naming_it(self):
        """A bare `crewchat start` sets the machine up and links no folder: the message says both ways."""
        with self.assertRaises(SystemExit) as stop:
            crewchat.cmd_restart(argparse.Namespace(project=None, port=None, no_service=False))
        said = str(stop.exception.code)
        self.assertIn("run `crewchat start .` in a project folder first", said)
        self.assertIn("a bare `crewchat start` to set this machine up and link folders on the page", said)
        out = io.BytesIO()
        hello = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        with contextlib.redirect_stderr(io.StringIO()) as err:  # `crewchat stdio` in a folder with no chat
            crewchat.serve_unconnected(str(self.cwd), io.BytesIO(json.dumps(hello).encode() + b"\n"), out)
        told = json.loads(out.getvalue().decode().splitlines()[0])["result"]["instructions"]
        self.assertIn("run `crewchat start .` in this folder", told)
        self.assertIn("run `crewchat start .` in this folder", err.getvalue())

    def test_start_with_no_folder_writes_nothing_into_the_working_folder(self):
        out = cli("start", "--no-open", "--no-service")
        self.assertEqual(sorted(os.listdir(str(self.cwd))), [])
        self.assertEqual(crewchat.list_projects(self.home), [])  # the placeholder is not listed
        config = crewchat.load_config(self.home)
        self.assertEqual(config.get("folders") or {}, {})
        self.assertEqual((config["project"], config["placeholder"]), ("crewchat", True))
        self.assertIn("Create a project or link folders on the page.", out)

    def test_start_with_no_folder_starts_the_server_and_opens_the_page_unless_told_not_to(self):
        cli("start", "--no-service")
        self.ensure_server.assert_called_once()
        self.cmd_ui.assert_called_once()
        self.ensure_server.reset_mock()
        self.cmd_ui.reset_mock()
        out = cli("start", "--no-service", "--no-open", "--pack", "youtube")
        self.ensure_server.assert_called_once()
        self.cmd_ui.assert_not_called()
        self.assertIn("--pack applies when a project is made", out)

    def test_start_with_no_folder_and_a_port_sets_the_machine_up_on_that_port(self):
        cli("start", "--no-open", "--no-service", "--port", "8811")
        self.assertEqual(crewchat.load_config(self.home)["port"], 8811)

    def test_projects_says_how_a_project_and_a_folder_are_added_now(self):
        crewchat.setup_host("shop")
        out = cli("projects")
        self.assertIn("crewchat start FOLDER --chat NAME", out)
        self.assertNotIn("in a new folder", out)

    def test_a_project_named_crewchat_does_not_take_the_placeholders_id(self):
        crewchat.setup_host(None)
        made = crewchat.create_project("crewchat")
        self.assertNotEqual(made["id"], crewchat.project_id("crewchat"))
        projects = crewchat.Projects(self.home)
        self.assertEqual(projects.get(made["id"]).root, made["root"])
        self.assertTrue(projects.get(None).primary)
        self.assertEqual(crewchat.find_chat("crewchat")["id"], made["id"])

    def test_start_with_no_folder_on_a_set_up_machine_adds_no_project(self):
        crewchat.setup_host("shop")
        before = crewchat.list_projects()
        out = cli("start", "--no-open", "--no-service")
        self.assertEqual([p["name"] for p in before], ["shop"])
        self.assertEqual(crewchat.list_projects(), before)
        self.assertEqual(sorted(os.listdir(str(self.cwd))), [])
        self.assertIn("Create a project or link folders on the page.", out)

    def test_start_with_no_folder_says_a_newer_release_is_out(self):
        """e2e, 16 Nov: only a start in a folder said so, and a bare start is the usual one now."""
        crewchat.setup_host("shop")
        (self.home / "update.json").write_text(json.dumps({"latest": "99.0.0"}), encoding="utf-8")
        self.assertIn("crewchat 99.0.0 is available (this is %s): run `crewchat update`." % crewchat.__version__,
                      cli("start", "--no-open", "--no-service"))

    @unittest.skipIf(os.name == "nt", "the suite's user can write everywhere on Windows")
    def test_start_in_a_folder_that_cannot_be_written_does_not_fail(self):
        os.chmod(str(self.cwd), 0o500)
        self.addCleanup(os.chmod, str(self.cwd), 0o700)
        self.assertIn("Create a project or link folders on the page.", cli("start", "--no-open", "--no-service"))

    def test_start_with_a_folder_links_it_as_before(self):
        crewchat.setup_host("shop")
        self.serve()
        repo = Path(tempfile.mkdtemp(prefix="linked-", dir=TMP)).resolve()
        cli("start", str(repo), "--no-open", "--no-service")
        place, _, _, chosen = crewchat.connected_project(repo)
        self.assertTrue(place)
        self.assertEqual(chosen["name"], repo.name)  # a project named after the folder, as before
        self.assertEqual([p["name"] for p in crewchat.list_projects()], ["shop", repo.name])

    def test_a_placeholder_project_is_listed_once_a_folder_is_connected(self):
        crewchat.setup_host(None)
        self.serve()
        repo = Path(tempfile.mkdtemp(prefix="myapp-", dir=TMP)).resolve()
        cli("start", str(repo), "--no-open", "--no-service")
        place, _, _, chosen = crewchat.connected_project(repo)
        self.assertTrue(place)
        self.assertEqual(chosen["name"], repo.name)  # a project named after the folder, not the placeholder
        self.assertEqual([p["name"] for p in crewchat.list_projects()], [repo.name])
        self.assertEqual(crewchat.Projects(self.home).get(None).name, "crewchat")  # still there, unlisted


class WelcomeScreen(Base):
    """A2: the page opens on a welcome screen until a project has a folder, and shows a project's name only once it
    has one. The server says so (boot "welcome", each project row's "folders"); the page does the rest."""

    def boot(self):
        return json.loads(self.raw("/api/boot", opener=self.owner_browser()[0])[1])

    def poll(self):
        return json.loads(self.raw("/api/poll?after=-1&v=-1&wait=0", opener=self.owner_browser()[0])[1])

    def listed(self):
        return [p["id"] for p in self.poll()["projects"]]

    def connect(self):
        folder = Path(tempfile.mkdtemp(prefix="welcome-", dir=TMP)).resolve()
        cli("start", str(folder), "--no-open", "--no-service", "--client", "claude", "--chat", "demo")
        self.addCleanup(crewchat.disconnect_folder, folder)
        return folder

    def placeholder(self, **kept):
        """This machine as `crewchat start` with no folder leaves it: a placeholder first project, with only the
        folders given (the ones the other tests connected are put back after)."""
        saved = crewchat.load_config()

        def machine(change):
            crewchat.change_config(change)
            self.hub.roster.refresh(force=True)  # within the clock's last tick
        machine(lambda config: config.update(placeholder=True, folders=kept.get("folders", {}),
                                             folder_chats=kept.get("folder_chats", {})))
        self.addCleanup(machine, lambda config: (config.pop("placeholder", None), config.update(
            folders=saved.get("folders") or {}, folder_chats=saved.get("folder_chats") or {})))

    def elsewhere(self, name):
        """A new project: its id, and what its page asks for (boot, the poll's row)."""
        made = crewchat.post_json(self.base + "/api/projects", self.owner, {"op": "create", "name": name})
        opener = self.owner_browser()[0]
        boot = lambda: json.loads(self.raw("/api/boot?chat=" + made["id"], opener=opener)[1])  # noqa: E731
        row = lambda: next(p for p in self.poll()["projects"] if p["id"] == made["id"])  # noqa: E731
        return made["id"], boot, row

    def test_boot_says_welcome_with_no_project(self):
        self.placeholder()
        self.assertIs(self.boot()["welcome"], True)
        self.assertNotIn("demo", self.listed())  # the other tests' projects stay

    def test_a_stale_folder_entry_does_not_list_the_placeholder(self):
        gone = str(Path(TMP) / "no-such-folder")
        self.placeholder(folders={gone: "mac"})  # in the config, but not there
        self.assertNotIn("demo", self.listed())
        self.assertNotIn("demo", [p["id"] for p in crewchat.list_projects(crewchat.home())])
        self.assertIs(self.boot()["welcome"], True)

    def test_boot_says_welcome_for_a_project_with_no_folder(self):
        self.assertIn("demo", self.listed())
        self.assertIs(self.boot()["welcome"], True)
        self.connect()
        self.assertIs(self.boot()["welcome"], False)

    def test_a_project_row_counts_its_folders(self):
        row = lambda: next(p for p in self.poll()["projects"] if p["id"] == "demo")  # noqa: E731
        self.assertEqual((row()["folders"], row()["new"]), (0, True))
        self.connect()
        self.assertEqual((row()["folders"], row()["new"]), (1, False))
        self.connect()
        self.assertEqual(row()["folders"], 2)

    def test_a_project_with_no_folder_here_is_new_only_while_it_is_empty_everywhere(self):
        # Its folders may be on a linked machine: an agent (from any machine) or a message says it is in use.
        quiet, quiet_boot, quiet_row = self.elsewhere("Quiet")
        self.assertEqual((quiet_boot()["welcome"], quiet_row()["new"], quiet_row()["folders"]), (True, True, 0))
        busy, busy_boot, busy_row = self.elsewhere("Busy")
        code = crewchat.post_json(self.base + "/api/invite?chat=" + busy, self.owner, {})["code"]
        token = crewchat.post_json(self.base + "/api/join", None, {"code": code, "place": "mac"})["token"]
        Session(self.base, token).call("hub_link", key=self.server.RequestHandlerClass.projects.get(busy).hub.new_start())
        self.assertEqual((busy_boot()["welcome"], busy_row()["new"], busy_row()["folders"]), (False, False, 0))
        talked, talked_boot, talked_row = self.elsewhere("Talked")
        crewchat.post_json(self.base + "/api/send?chat=" + talked, self.owner, {"to": "all", "text": "Hello"})
        self.assertEqual((talked_boot()["welcome"], talked_row()["new"]), (False, False))
        self.assertEqual((quiet_boot()["welcome"], quiet_row()["new"]), (True, True))  # the others did not change it

    def test_a_linked_folder_can_be_removed_from_the_welcome_list(self):
        folder = self.connect()
        owner = self.owner_browser()[0]
        post = lambda body: self.raw("/api/folders", json.dumps(body).encode(), {  # noqa: E731
            "Content-Type": "application/json", "Origin": self.base}, owner)
        self.assertEqual(json.loads(post({"op": "project"})[1])["folders"][-1]["path"], str(folder))
        (folder / "notes.txt").write_text("mine", encoding="utf-8")  # not crewchat's
        other = Path(tempfile.mkdtemp(prefix="welcome-other-", dir=TMP)).resolve()
        made = crewchat.post_json(self.base + "/api/projects", self.owner, {"op": "create", "name": "Other"})["id"]
        cli("start", str(other), "--no-open", "--no-service", "--client", "claude", "--chat", made)
        self.addCleanup(crewchat.disconnect_folder, other)
        self.assertEqual(post({"op": "unlink", "path": str(folder / "..")})[0], 400)  # only a folder of this project
        self.assertEqual(post({"op": "unlink", "path": str(other)})[0], 400)  # not another project's
        self.assertTrue(crewchat.folder_chat(crewchat.load_config(), other))
        self.assertEqual(post({"op": "unlink", "path": str(folder)})[0], 200)
        self.assertEqual(json.loads(post({"op": "project"})[1])["folders"], [])
        self.assertFalse((folder / ".mcp.json").exists())  # crewchat is out of it too
        self.assertEqual((folder / "notes.txt").read_text(encoding="utf-8"), "mine")  # and the folder is left alone
        config = crewchat.load_config()
        self.assertNotIn(str(folder), config["folders"])
        self.assertNotIn(str(folder), config.get("folder_chats") or {})
        self.assertIn(str(other), config["folders"])
        self.assertEqual(next(p for p in self.poll()["projects"] if p["id"] == "demo")["folders"], 0)

    def test_the_page_has_the_welcome_steps_and_the_exact_copy(self):
        for text in ('id="welcome"', 'id="welcome-create"', 'id="welcome-link"', 'id="welcome-folders"',
                     'id="welcome-done"', "New project: link a folder to start", "Link a folder first"):
            self.assertIn(text, crewchat.CHAT_PAGE)
        self.assertIn("projectLabel(", crewchat.CHAT_PAGE)

    def test_a_poll_that_changes_the_welcome_state_reads_boot_again(self):
        page = crewchat.CHAT_PAGE
        self.assertIn("state.welcomeKey", page)
        self.assertIn("try { await syncWelcome(data.projects, data.chat); } catch (e) {}", page)  # the poll body still renders

    def test_fresh_rows_that_show_a_folder_now_turn_the_welcome_state_over(self):
        # Under node: the page's own syncWelcome. A new project's row coming back with a folder (new false) reads boot
        # again; the same rows twice do not; and a boot that fails is tried again with the next rows.
        if not shutil.which("node"):
            self.skipTest("needs node")
        page = crewchat.CHAT_PAGE
        code = page[page.index("function welcomeKey("):page.index("// A folder can be linked by a terminal")]
        js = "const state = {}; let boots = 0, failing = false;" \
             "async function loadBoot() { boots++; if (failing) throw new Error('down'); }" + code + (
            "(async () => {"
            "const row = (n, f) => [{id: 'bluebird', name: 'Bluebird', new: n, folders: f}], out = [];"
            "const sync = async (rows) => { try { await syncWelcome(rows, 'bluebird'); } catch (e) { out.push('threw'); } out.push(boots); };"
            "await sync(row(true, 0)); await sync(row(true, 0));"  # remembers, then no change: 0 boots
            "failing = true; await sync(row(false, 1));"  # a change, boot fails: threw, 1 boot
            "await sync(row(false, 1));"  # still a change, tried again: threw, 2 boots
            "failing = false; await sync(row(false, 1)); await sync(row(false, 1));"  # read: 3 boots, then no change
            "await sync([]);"  # the list emptied: a change, 4 boots
            "console.log(JSON.stringify(out)); })();")
        out = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(out, [0, 0, "threw", 1, "threw", 2, 3, 3, 4])

    def test_the_welcome_screen_rechecks_every_three_seconds_while_it_shows(self):
        # String assertions only (no browser): the timer exists, runs while the welcome shows and the tab is seen,
        # stops otherwise, and asks for the rows without waiting, with the chat the page's other requests carry.
        page = crewchat.CHAT_PAGE
        for text in ("setInterval(recheckWelcome, 3000)", "state.welcome && document.visibilityState === \"visible\"",
                     "clearInterval(welcomeTimer)", 'document.addEventListener("visibilitychange", watchWelcome)',
                     '"/api/poll?after=" + state.after + "&v=-2&wait=0"', "  watchWelcome();\n}"):
            self.assertIn(text, page.replace("\r\n", "\n"))
        self.assertIn("if (welcomeBusy || !state.welcome) return;", page)
        self.assertIn('const res = await fetch(api(path)', page)  # request() adds chat=CHAT, as boot does

    def test_the_sidebar_and_projects_copy_no_longer_describe_the_old_start(self):
        self.assertNotIn("run crewchat start in", crewchat.CHAT_PAGE)
        self.assertIn("No agents yet. Use New agent below, or open Claude Code, Cursor, Codex or another agent tool "
                      "in a linked folder.", crewchat.CHAT_PAGE)

    def test_a_machine_with_only_the_placeholder_lists_no_project_on_the_page(self):
        # Runs the page's own function under node: an empty poll list is not a project, and the stand-in row stays out
        # of state.projects (which step 1 and the switcher's count read).
        if not shutil.which("node"):
            self.skipTest("needs node")
        page = crewchat.CHAT_PAGE
        code = page[page.index("const projectLabel = "):page.index("function renderProjects(")]
        js = code + (
            "const empty = projectRows([], 'crewchat', 'crewchat');"
            "const made = projectRows([{id: 'bluebird', name: 'Bluebird', new: true}], 'bluebird', 'Bluebird');"
            "console.log(JSON.stringify([empty.listed, empty.rows.map(projectLabel), made.listed, made.rows.map(projectLabel)]));")
        out = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(out[0], False)  # not listed: the page says "No project yet" and step 1 names none
        self.assertEqual(out[2], True)
        self.assertEqual(out[3], ["New project: link a folder to start"])
        render = page[page.index("function renderProjects("):]
        render = render[:render.index("const key = ")]
        self.assertIn("state.projects = view.listed ? list : [];", render)

    def test_a_held_project_is_not_new_so_its_trust_card_shows(self):
        """F2: an opened, held workspace with no folder here is not new: the welcome screen would hide Needs you, where
        the trust card comes first (spec 4a)."""
        pid, boot, row = self.elsewhere("Heldnew")
        self.assertEqual((boot()["welcome"], row()["new"]), (True, True))
        projects = self.server.RequestHandlerClass.projects

        def hold(on):
            crewchat.change_workspaces(lambda d: next(r for r in d["workspaces"] if r["id"] == pid).update(held=on),
                                       crewchat.home())
            projects.refresh()
        hold(True)
        self.addCleanup(hold, False)
        self.assertTrue(projects.get(pid).hub.held)
        self.assertEqual((boot()["welcome"], row()["new"]), (False, False))

    def test_the_welcome_screen_takes_only_the_chats_place(self):
        """F2: Needs you, Tasks and Activity stay reachable on a new project; the welcome screen replaces the chat."""
        if not shutil.which("node"):
            self.skipTest("needs node")
        page = crewchat.CHAT_PAGE
        code = page[page.index("function applyWelcome() {"):page.index("for (const view of VIEWS) $(")]
        ids = ["welcome", "log", "form", "needs-you", "review", "tasks", "analytics"]
        out = {}
        for view in ("chat", "needs", "tasks", "analytics", None):
            shown = {"chat": ["log", "form"], "needs": ["needs-you"], "tasks": ["tasks"], "analytics": ["analytics"],
                     None: ["log", "form"]}[view]
            js = ("const els = {}; for (const id of %s) els[id] = { hidden: !%s.includes(id) };\n"
                  "const $ = (id) => els[id]; function paintWelcomeProject() {}\n"
                  "const state = { welcome: true, view: %s };\n" % (json.dumps(ids), json.dumps(shown), json.dumps(view))
                  + code + "\napplyWelcome();\nconsole.log(JSON.stringify(Object.keys(els).filter((id) => !els[id].hidden)));")
            out[view] = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)
        self.assertEqual(out["chat"], ["welcome"])
        self.assertEqual(out[None], ["welcome"])
        self.assertEqual(out["needs"], ["needs-you"])  # the owner picked it: the trust card and the rest stay in view
        self.assertEqual(out["tasks"], ["tasks"])
        self.assertEqual(out["analytics"], ["analytics"])

    def test_step_one_shows_the_project_made_and_the_switcher_says_none_yet(self):
        page = crewchat.CHAT_PAGE
        for text in ('id="welcome-project"', '"Project: "', "Make another…", "No project yet"):
            self.assertIn(text, page)

    def test_a_long_folder_path_wraps_in_the_connect_panel(self):
        self.assertIn(".ready .head { font-weight: 650; overflow-wrap: anywhere; }", crewchat.CHAT_PAGE)
        # F3: a tool's row is a flex row, and its list of questions (the folder's path in them) shrinks and wraps
        # rather than scrolling the panel sideways.
        for rule in (".ready .item > * { min-width: 0; }", ".ready .qs li { overflow-wrap: anywhere; }"):
            self.assertTrue(rule in crewchat.CHAT_PAGE, rule)


class LeanInstall(unittest.TestCase):
    """2.0.1: the plain install leaves cloud sync's libraries out; what adds them, and what keeps them."""

    def test_the_machines_panel_says_how_to_add_cloud_sync_when_its_libraries_are_missing(self):
        hub = types.SimpleNamespace(sync=None, device="mac")
        with mock.patch.object(crewchat, "cloud_libs", return_value=False), \
                mock.patch.object(crewchat, "installed_by_installer", return_value=True):
            self.assertIn("crewchat update --cloud", crewchat.machines_status(hub)["cloud_missing"])
        with mock.patch.object(crewchat, "cloud_libs", return_value=True):
            self.assertNotIn("cloud_missing", crewchat.machines_status(hub))
        self.assertIn("s.cloud_missing", crewchat.CHAT_PAGE)  # the Link with cloud sync card shows it
        self.assertIn("mach.login.rules", crewchat.CHAT_PAGE)  # and, after sign-in, the rules that lock the project

    def test_update_with_cloud_adds_the_extra_and_an_update_keeps_it(self):
        with mock.patch.object(crewchat.importlib.util, "find_spec", return_value=None):
            self.assertEqual(crewchat.update_specs("1.2.3")[0], "crewchat==1.2.3")
            self.assertEqual(crewchat.update_specs("1.2.3", cloud=True)[0], "crewchat[cloud]==1.2.3")
            self.assertIn("crewchat[cloud] @ ", crewchat.update_specs("1.2.3", cloud=True)[1])
        with mock.patch.object(crewchat.importlib.util, "find_spec", return_value=object()):
            self.assertEqual(crewchat.update_specs("")[0], "crewchat[cloud]")  # a copy that has them keeps them

    def test_update_through_the_installer_passes_cloud_on(self):
        ran = []

        def run(command, env=None, **kwargs):
            ran.append(env)
            return subprocess.CompletedProcess(command, 0, "", "")

        for flag, has, want in ((True, False, "1"), (False, True, "1"), (False, False, None)):
            ran.clear()
            with mock.patch.object(crewchat, "installed_by_installer", return_value=True), \
                    mock.patch.object(crewchat, "find_uv", return_value=None), \
                    mock.patch.object(crewchat, "latest_release", return_value="99.0.0"), \
                    mock.patch.object(crewchat, "server_up", return_value=False), \
                    mock.patch.object(crewchat, "cloud_libs", return_value=has), \
                    mock.patch.object(crewchat.shutil, "which", return_value="/usr/bin/curl"), \
                    mock.patch.object(crewchat.subprocess, "run", side_effect=run), \
                    mock.patch.dict(os.environ, {}), contextlib.redirect_stdout(io.StringIO()):
                for name in ("CREWCHAT_SOURCE", "CREWCHAT_VERSION", "CREWCHAT_CLOUD"):
                    os.environ.pop(name, None)
                crewchat.cmd_update(argparse.Namespace(version=None, cloud=flag))
            self.assertEqual(ran[0].get("CREWCHAT_CLOUD"), want)

    def test_the_cli_takes_update_cloud(self):
        args = crewchat.build_parser().parse_args(["update", "--cloud"])
        self.assertTrue(args.cloud)

    @unittest.skipIf(os.name == "nt", "install.sh is for macOS and Linux")
    def test_install_sh_adds_cloud_only_when_asked_or_already_in_use(self):
        def install(env_extra=(), firebase=False, old_cloud=None):
            tmp = Path(tempfile.mkdtemp(prefix="install-sh-"))
            self.addCleanup(shutil.rmtree, tmp, True)
            fake, bin_, tools = tmp / "fake", tmp / "bin", tmp / "tools"
            for d in (fake, bin_, tools / "crewchat" / "bin", tmp / "home"):
                d.mkdir(parents=True)
            log = tmp / "uv.log"
            (fake / "uv").write_text(
                '#!/bin/sh\ncase "$1 $2" in\n'
                '  "tool install") echo "$*" >> "%s" ;;\n'
                '  "tool dir") if [ "${3:-}" = "--bin" ]; then echo "%s"; else echo "%s"; fi ;;\n'
                'esac\nexit 0\n' % (log, bin_, tools))
            (bin_ / "crewchat").write_text('#!/bin/sh\n[ "$1" = "--version" ] && echo "crewchat 9.9.9"\nexit 0\n')
            if old_cloud is not None:  # the copy installed before, with or without the libraries
                (tools / "crewchat" / "bin" / "python").write_text("#!/bin/sh\nexit %d\n" % (0 if old_cloud else 1))
            if firebase:
                (tmp / "home" / ".crewchat" / "cloud").mkdir(parents=True)
                (tmp / "home" / ".crewchat" / "cloud" / "firebase.json").write_text("{}")
            for f in (fake / "uv", bin_ / "crewchat", tools / "crewchat" / "bin" / "python"):
                if f.exists():
                    f.chmod(0o755)
            env = {"PATH": "%s:/usr/bin:/bin" % fake, "HOME": str(tmp / "home"), "CREWCHAT_SOURCE": "/src"}
            env.update(dict(env_extra))
            out = subprocess.run(["sh", str(ROOT / "install.sh")], env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            return log.read_text().strip().splitlines()[-1], out.stdout

        spec, said = install()
        self.assertTrue(spec.endswith(" crewchat @ /src"), spec)  # plain by default
        self.assertIn("crewchat update --cloud", said)
        self.assertTrue(install({"CREWCHAT_CLOUD": "1"})[0].endswith(" crewchat[cloud] @ /src"))
        self.assertTrue(install(firebase=True)[0].endswith(" crewchat[cloud] @ /src"))  # cloud sync set up here
        self.assertTrue(install(old_cloud=True)[0].endswith(" crewchat[cloud] @ /src"))  # an upgrade keeps them
        self.assertTrue(install(old_cloud=False)[0].endswith(" crewchat @ /src"))
        self.assertTrue(install({"CREWCHAT_LEAN": "1"}, firebase=True)[0].endswith(" crewchat @ /src"))

    @unittest.skipIf(os.name == "nt", "install.sh is for macOS and Linux")
    def test_install_sh_says_python_may_download_and_shows_pypis_error(self):
        """Path-to-10 review (Mac user): the first install was silent while uv downloaded Python, and when every source
        failed the error shown was the last one's (GitHub's tarball), not PyPI's."""
        tmp = Path(tempfile.mkdtemp(prefix="install-sh-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        fake, home_ = tmp / "fake", tmp / "home"
        for d in (fake, home_):
            d.mkdir()
        (fake / "uv").write_text('#!/bin/sh\ncase "$1 $2" in\n'
                                 '  "tool install") echo "uv says: could not install $*" >&2; exit 2 ;;\n'
                                 '  "tool dir") echo "%s" ;;\nesac\nexit 0\n' % (tmp / "bin"))
        (fake / "uv").chmod(0o755)
        env = {"PATH": "%s:/usr/bin:/bin" % fake, "HOME": str(home_), "CREWCHAT_VERSION": "9.9.9"}
        out = subprocess.run(["sh", str(ROOT / "install.sh")], env=env, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("can take a minute or two", out.stdout)
        shown = [line for line in out.stderr.splitlines() if line.startswith("uv says:")]
        self.assertEqual(len(shown), 1, out.stderr)
        self.assertIn("crewchat==9.9.9", shown[0])

    def test_install_ps1_says_python_may_download_too(self):
        """Round-7 review (Windows user): the CHANGELOG said both installers do; only install.sh did."""
        text = (ROOT / "install.ps1").read_text(encoding="utf-8")
        install = text.index('& $Uv tool install --force')
        self.assertIn("can take a minute or two", text[:install])
        self.assertIn("uv downloads Python 3.12 first", text[:install])

    def test_install_ps1_adds_cloud_only_when_asked_or_already_in_use(self):
        text = (ROOT / "install.ps1").read_text(encoding="utf-8")
        self.assertIn('$Name = if ($Cloud) { "crewchat[cloud]" } else { "crewchat" }', text)
        self.assertIn('$Cloud = [bool]$env:CREWCHAT_CLOUD', text)
        self.assertIn('Join-Path $CrewchatDir "cloud\\firebase.json"', text)
        self.assertIn('& $OldPython -c "import cryptography, google.cloud.firestore"', text)
        self.assertLess(text.index("$Cloud = [bool]"), text.index("$Name = if ($Cloud)"))
        self.assertLess(text.index("$Uv = (Get-Command uv"), text.index("$ToolDir = "))  # uv is found first


if __name__ == "__main__":
    unittest.main()
