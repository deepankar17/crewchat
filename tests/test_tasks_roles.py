"""Tasks, areas, roles, teams, Needs you and files."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from crewchat_fixture import (  # noqa: E402,F401  (the shared server fixture)
    Base, PNG, Path, Session, TMP, base64, cli, crewchat, eventually, io, json, mock, os, re, sys, tempfile,
    threading, time, unittest, urllib,
)


class TrimmedTools(Base):
    """What every agent loads is short (roadmap P2): one hub_task for the task sheet, and inbox and history
    answers that fit a cap without losing a message."""

    def test_the_tool_definitions_are_short_and_task_tools_are_one(self):
        without = [t for t in crewchat.TOOLS if t["name"] != "hub_process"]
        self.assertLess(len(json.dumps(without)), 8900)  # P2's 7,000, threads and hub_search (owner, 9 Nov), return (Q1),
        # "write short" (#230) and hub_question (#237, about 1,250)
        self.assertLess(len(json.dumps(crewchat.TOOLS)) - len(json.dumps(without)), 600)  # L2's hub_process
        names = [t["name"] for t in crewchat.TOOLS]
        self.assertIn("hub_task", names)
        self.assertFalse(set(names) & set(crewchat.OLD_TASK_TOOLS))
        guide = crewchat.RULES
        for action in crewchat.TASK_ACTIONS:  # how to use them is in the guide every folder gets
            self.assertIn("`%s`" % action, guide)

    def test_an_old_task_tool_says_to_restart_the_session(self):
        session, _ = self.agent()
        for old, action in crewchat.OLD_TASK_TOOLS.items():
            out = session.call(old, id="1")
            self.assertTrue(out["isError"], old)
            text = out["content"][0]["text"]
            self.assertIn("now hub_task with action %s" % action, text)
            self.assertIn("restart it", text)

    def test_hub_task_takes_one_action_at_a_time(self):
        session, name = self.agent()
        self.assertIn("action is view, add, take, assign, areas, update", session.text("hub_task", action="peek"))
        self.assertIn("action is", session.text("hub_task"))
        added = session.text("hub_task", action="add", title="Write the release notes", areas="docs/")
        tid = re.search(r"#(\w+)", added).group(1)
        self.assertIn("Write the release notes", session.text("hub_task", action="view", show="all"))
        self.assertIn("is yours", session.text("hub_task", action="take", id=tid))
        self.assertIn("marked in progress", session.text("hub_task", action="update", id=tid, status="in_progress", note="on it"))
        self.assertIn("not both", session.text("hub_task", action="assign", to=name, id=tid, title="x"))

    def test_a_long_inbox_comes_in_parts_and_loses_nothing(self):
        sender, _ = self.agent()
        reader, reader_name = self.agent()
        reader.text("hub_inbox")  # what was there before
        texts = ["Message %02d: %s" % (i, "word " * 80) for i in range(50)]  # about 20,000 characters in all
        for t in texts:
            sender.call("hub_send", to=reader_name, text=t)
        self.assertIn("more unread", reader.text("hub_inbox", peek=True))
        first = reader.text("hub_inbox", peek=True)
        self.assertEqual(reader.text("hub_inbox", peek=True), first)  # peek marks nothing read
        seen, parts = [], 0
        while True:
            out = reader.text("hub_inbox")
            if out == "No new messages.":
                break
            parts += 1
            body = out.split("\n\n", 1)[1].split("\n\n")[0]
            self.assertLessEqual(len(body), crewchat.MAX_TOOL_OUTPUT)
            seen += re.findall(r"Message \d\d: ", out)
        self.assertGreater(parts, 2)
        self.assertEqual(seen, ["Message %02d: " % i for i in range(50)])  # every one, once, in order
        big = "x" * crewchat.MAX_TEXT
        sender.call("hub_send", to=reader_name, text=big)
        self.assertIn(big, reader.text("hub_inbox"))  # one message bigger than the cap: whole, never cut
        history = reader.text("hub_history", limit=50)
        self.assertIn("older left out", history)
        self.assertIn(big, history)  # the newest that fit

    def test_messages_show_a_short_time(self):
        now = time.time()
        self.assertEqual(crewchat.short_time(now), time.strftime("%H:%M", time.gmtime(now)))
        self.assertEqual(crewchat.short_time(now - 3 * 86400), time.strftime("%m-%d %H:%M", time.gmtime(now - 3 * 86400)))
        msg = {"id": "7", "ts": now, "kind": "msg", "from": "Maya", "to": "all", "text": "hi"}
        self.assertTrue(crewchat.fmt(msg, "Teo", True).startswith("[#7 %s]" % crewchat.short_time(now)))
        self.assertIn("UTC", crewchat.fmt(msg, "Teo"))  # hooks keep the full time

    def test_the_old_task_tools_leave_the_allow_lists(self):
        settings = {"permissions": {"allow": ["mcp__crewchat__hub_take", "mcp__crewchat__hub_send", "Bash(ls)"]}}
        self.assertTrue(crewchat.allow_rule(settings, crewchat.CLAUDE_ALLOW))
        allow = settings["permissions"]["allow"]
        self.assertNotIn("mcp__crewchat__hub_take", allow)
        self.assertIn("mcp__crewchat__hub_task", allow)
        self.assertIn("Bash(ls)", allow)  # the user's own stay
        self.assertFalse(crewchat.allow_rule(settings, crewchat.CLAUDE_ALLOW))  # up to date


class Tasks(Base):
    def setUp(self):
        self.crew = [self.agent() for _ in range(4)]

    def test_exactly_one_agent_wins_a_race_for_a_task(self):
        task = self.say("all", "Add a dark icon", "task")
        self.assertIn("Owner -> all: [TASK, open] Add a dark icon", self.crew[0][0].text("hub_inbox"))
        results = {}
        threads = [threading.Thread(target=lambda s=s, n=n: results.update({n: s.call("hub_task", action="take", id=task)}))
                   for s, n in self.crew]
        [t.start() for t in threads]
        [t.join() for t in threads]
        winners = [n for n, r in results.items() if not r["isError"]]
        self.assertEqual(len(winners), 1)
        for name, result in results.items():
            if name != winners[0]:
                self.assertIn("already taken by " + winners[0], result["content"][0]["text"])
        history = self.crew[0][0].text("hub_history")
        self.assertIn("%s -> all: I am taking task #%s" % (winners[0], task), history)
        self.assertIn("[TASK, taken by %s]" % winners[0], history)

    def test_a_task_for_one_agent_is_only_that_agents(self):
        (a, a_name), (b, _) = self.crew[:2]
        task = self.say(a_name, "Only for you", "task")
        self.assertIn("was given to " + a_name, b.text("hub_task", action="take", id=task))
        self.assertFalse(a.call("hub_task", action="take", id=task)["isError"])

    def test_a_task_number_can_be_written_with_a_hash(self):
        (a, a_name) = self.crew[0]
        task = self.say("all", "hash me", "task")
        self.assertIn("Task #%s is yours" % task, a.text("hub_task", action="take", id="#%s" % task))

    def test_only_tasks_can_be_taken(self):
        message = self.say("all", "just a note")
        self.assertTrue(self.crew[0][0].call("hub_task", action="take", id=message)["isError"])
        self.assertTrue(self.crew[0][0].call("hub_task", action="take", id=999999)["isError"])

    def test_agents_cannot_post_as_owner(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            crewchat.post_json(self.base + "/api/send", self.token, {"to": "all", "text": "x"})
        self.assertEqual(ctx.exception.code, 403)

    def test_say_command(self):
        a, a_name = self.crew[0]
        self.assertIn("Posted task #", cli("say", "--task", "--to", a_name, "Check", "the", "build"))
        self.assertIn("Owner -> you: [TASK, open] Check the build", a.text("hub_inbox"))
        self.assertIn("Posted message #", cli("say", "plain note"))
        with self.assertRaises(SystemExit):
            cli("say", "--to", "nobody-here", "hello")


class TaskAreas(Base):
    """Roadmap C2: a task's areas suggested from the files its holder edited (C0), accepted by the owner or the
    lead, and an overlap check that sees imports (C1)."""

    def setUp(self):
        self.crew = [self.agent() for _ in range(2)]
        maps = mock.patch.object(self.hub, "_maps", None)  # the code map: none unless a test makes one
        maps.start()
        self.addCleanup(maps.stop)

    def task(self, title, areas=""):
        return crewchat.post_json(self.base + "/api/task", self.owner, {"op": "add", "title": title, "areas": areas})["id"]

    def sheet(self, mid):
        return self.hub.sheet[mid]

    def suggestions(self, mid):
        return [m for m in self.hub.messages if m.get("task") == mid and m.get("suggested")]

    def test_the_folders_it_edited_are_suggested_when_it_goes_to_review(self):
        (a, a_name), _ = self.crew
        mid = self.task("Fix the login", "login/")
        a.call("hub_task", action="take", id=mid)
        for path in ("login/form.kt", "session/store.kt", "session/keys/token.kt", "docs/login.md", "README.md"):
            self.hub.events.add_edit(a_name, path, mid)
        self.hub.events.add_edit(a_name, "unrelated/file.kt", "")  # not while it held the task
        a.text("hub_task", action="update", id=mid, status="review", note="ready")
        self.assertEqual(self.sheet(mid)["suggested"], ["README.md", "docs/", "session/"])  # login/ it has already
        [notice] = self.suggestions(mid)
        self.assertEqual((notice["from"], notice["to"]), (crewchat.SERVER_NAME, crewchat.OWNER))  # no lead: the owner
        self.assertIn("outside its areas: README.md, docs/, session/", notice["text"])
        self.assertIn("crewchat tasks areas %s accept" % mid, notice["text"])
        self.assertIn("suggested files README.md, docs/, session/", cli("tasks"))
        crewchat.post_json(self.base + "/api/task", self.owner, {"op": "status", "task": mid, "status": "done"})
        self.assertEqual(len(self.suggestions(mid)), 1)  # done, with nothing new since: not suggested again
        out = crewchat.post_json(self.base + "/api/task", self.owner, {"op": "areas", "task": mid, "accept": True})
        self.assertEqual(out["areas"], ["login/", "README.md", "docs/", "session/"])
        self.assertNotIn("suggested", self.sheet(mid))
        with self.assertRaises(urllib.error.HTTPError):
            crewchat.post_json(self.base + "/api/task", self.owner, {"op": "areas", "task": mid, "accept": True})

    def test_a_file_in_its_areas_is_not_outside_them(self):
        # Owner's two-machine test, 11 Nov: a task whose area was one file warned "changed files outside its areas:
        # shop/" when only that file changed, because the file was grouped into its folder before the check.
        (a, a_name), _ = self.crew
        mid = self.task("Cart discount", "shop/__init__.py")
        a.call("hub_task", action="take", id=mid)
        self.hub.events.add_edit(a_name, "shop/__init__.py", mid)
        a.text("hub_task", action="update", id=mid, status="review", note="ready")
        self.assertEqual(self.suggestions(mid), [])
        self.assertNotIn("suggested", self.sheet(mid))
        self.hub.events.add_edit(a_name, "shop/cart.py", mid)  # another file in the folder is outside it
        a.text("hub_task", action="update", id=mid, status="done", note="shipped\nDecisions: none\nLeft over: none")
        self.assertEqual(self.sheet(mid)["suggested"], ["shop/"])

    def test_the_lead_accepts_them_with_hub_task(self):
        (a, a_name), (lead, lead_name) = self.crew
        crewchat.post_json(self.base + "/api/role", self.owner, {"agent": lead_name, "role": "lead"})
        self.addCleanup(crewchat.post_json, self.base + "/api/role", self.owner, {"agent": lead_name, "role": "none"})
        mid = self.task("Add search")
        a.call("hub_task", action="take", id=mid)
        self.hub.events.add_edit(a_name, "web/search/box.tsx", mid)
        a.text("hub_task", action="update", id=mid, status="review", note="ready")
        inbox = lead.text("hub_inbox")
        self.assertIn("but has no areas: web/search/", inbox)
        self.assertIn("hub_task action=areas id=%s accept=true" % mid, inbox)
        self.assertIn("only the lead or the owner", a.text("hub_task", action="areas", id=mid, accept=True))
        self.assertIn("Task #%s's files: web/search/." % mid, lead.text("hub_task", action="areas", id=mid, accept=True))
        self.assertIn("Task #%s's files: web/, api/." % mid, lead.text("hub_task", action="areas", id=mid, areas="web/,api/"))
        self.assertIn("files web/, api/", lead.text("hub_task", action="view", show="all"))
        self.assertIn("Task #%s's files: web/." % mid, cli("tasks", "areas", mid, "web/"))

    def test_an_edit_goes_to_the_task_its_holder_works_on_now(self):
        (a, a_name), _ = self.crew
        first, second, third = self.task("Holding one"), self.task("Holding two"), self.task("Holding three")
        for mid in (first, second):
            a.call("hub_task", action="take", id=mid)
        with self.hub.lock:
            self.assertEqual(self.hub._usage_task(a_name), second)  # taken last
        a.text("hub_task", action="update", id=first, status="in_progress", note="on it")
        with self.hub.lock:
            self.assertEqual(self.hub._usage_task(a_name), first)  # reported on last
        a.call("hub_task", action="take", id=third)
        with self.hub.lock:
            self.assertEqual(self.hub._usage_task(a_name), third)

    def test_nothing_is_suggested_without_edit_tracking(self):
        (a, a_name), _ = self.crew
        mid = self.task("Quiet one", "quiet/")
        a.call("hub_task", action="take", id=mid)
        self.hub.events.add_edit(a_name, "elsewhere/x.py", mid)
        with mock.patch.object(crewchat.Hub, "edits_on", lambda hub: False):
            a.text("hub_task", action="update", id=mid, status="review", note="ready")
        self.assertEqual(self.suggestions(mid), [])
        a.text("hub_task", action="update", id=mid, status="in_progress", note="again")
        a.text("hub_task", action="update", id=mid, status="review", note="ready")  # tracking on: suggested now
        self.assertEqual(self.sheet(mid)["suggested"], ["elsewhere/"])

    def test_a_changed_interface_is_told_to_the_tasks_that_import_it(self):
        # Roadmap C3, done when: renaming a parameter of a function another agent's area imports sends exactly
        # one notice; a change to a body alone sends none.
        code = Path(tempfile.mkdtemp(prefix="iface-", dir=TMP)).resolve()
        files = {"paylib/parse.py": "def read(path):\n    return path\n", "paylib/__init__.py": "",
                 "payapp/main.py": "from paylib.parse import read\nread('x')\n", "paydocs/notes.py": ""}
        for rel, text in files.items():
            (code / rel).parent.mkdir(parents=True, exist_ok=True)
            (code / rel).write_text(text)
        folders = mock.patch.object(crewchat.Hub, "project_folders", lambda hub, place=None: [code])
        folders.start()
        self.addCleanup(folders.stop)
        import crewchat_map
        self.hub._maps = crewchat_map.Maps(Path(tempfile.mkdtemp(prefix="iface-maps-", dir=TMP)))
        self.addCleanup(self.hub._maps.close)
        self.hub.update_map()
        (a, a_name), (b, b_name) = self.crew
        c, c_name = self.agent()
        # A works on scripts and changes the parser on the way; B's task is on the app, which imports it.
        mine, theirs, docs = self.task("The scripts", "payscripts/"), self.task("The app", "payapp/"), \
            self.task("Notes", "paydocs/")
        for agent, mid in ((a, mine), (b, theirs), (c, docs)):
            self.assertNotIn("Error", agent.text("hub_task", action="take", id=mid))
        for agent in (a, b, c):
            agent.text("hub_inbox")

        def edit(text):
            (code / "paylib/parse.py").write_text(text)
            later = time.time() + 30 + len(self.hub.messages)
            os.utime(code / "paylib/parse.py", (later, later))
            with self.hub.lock:
                self.hub._edited(a_name, "paylib/parse.py")
            self.hub.update_map()

        start = time.time()
        edit("def read(source):\n    return source\n")
        edit("def read(source):\n    return source.strip()\n")  # still at it: one notice, when it settles
        self.assertEqual(self.hub.interface_notices(self.hub._maps, now=start + 10), [])  # not settled yet
        [notice] = self.hub.interface_notices(self.hub._maps, now=start + crewchat.NOTICE_SETTLE + 5)
        self.assertEqual((notice["to"], notice["task"], notice["changed"]), (b_name, theirs, "paylib/parse.py"))
        inbox = b.text("hub_inbox")
        self.assertIn("%s changed paylib/parse.py, which your task #%s uses: read(path) is now read(source)" % (
            a_name, theirs), inbox)
        self.assertNotIn("paylib/parse.py", a.text("hub_inbox") + c.text("hub_inbox"))  # not its editor, nor others
        edit("def read(source):\n    return source.lower()\n")  # a body only
        self.assertEqual(self.hub.interface_notices(self.hub._maps, now=start + 10000), [])

    def test_taking_or_giving_a_task_warns_of_the_code_others_work_on(self):
        # Roadmap C4: a note naming the other task and its holder, firm on certain links, a hint on guesses;
        # never in the way.
        code = Path(tempfile.mkdtemp(prefix="blast-", dir=TMP)).resolve()
        # The guess: a service injected by a name two files define (a guess with either readers, C9's too).
        files = {"blastlib/parse.py": "def read():\n    pass\n", "blastlib/__init__.py": "",
                 "blastapp/main.py": "from blastlib.parse import read\n",
                 "blastng/cart.component.ts": "@Component({ selector: 'app-cart', template: `<p></p>` })\n"
                                              "export class CartComponent { constructor(private prices: PriceService) {} }\n",
                 "blastng/a/price.service.ts": "@Injectable()\nexport class PriceService {}\n",
                 "blastng/b/price.service.ts": "@Injectable()\nexport class PriceService {}\n", "blastdocs/notes.py": ""}
        for rel, text in files.items():
            (code / rel).parent.mkdir(parents=True, exist_ok=True)
            (code / rel).write_text(text)
        import crewchat_map
        maps = crewchat_map.Maps(Path(tempfile.mkdtemp(prefix="blast-maps-", dir=TMP)))
        self.addCleanup(maps.close)
        maps.check([code])
        self.hub._maps = maps
        (a, a_name), (b, b_name) = self.crew
        pricing, cart = self.task("Pricing rules", "blastng/a/"), self.task("The cart", "blastng/cart.component.ts")
        lib, app, docs = self.task("The parser", "blastlib/"), self.task("The app", "blastapp/"), self.task("Notes", "blastdocs/")
        a.call("hub_task", action="take", id=pricing)
        out = b.text("hub_task", action="take", id=cart)  # a guess only (a name defined twice): taken, with a hint
        self.assertIn("Task #%s is yours" % cart, out)
        self.assertIn("Heads up: #%s (%s) may touch code this task uses (a guess from names)" % (pricing, a_name), out)
        a.call("hub_task", action="take", id=lib)
        # The owner gives the app to b: certain, so a firm note, in the message b gets; nothing stopped.
        crewchat.post_json(self.base + "/api/task", self.owner, {"op": "assign", "task": app, "agent": b_name})
        self.assertEqual(self.hub.taken[app], b_name)
        inbox = b.text("hub_inbox")
        self.assertIn("Heads up: #%s (%s) works on code that this task's files import or are imported by" % (lib, a_name), inbox)
        self.assertIn("agree any change to what they share with %s first" % a_name, inbox)
        self.assertNotIn("Heads up", a.text("hub_task", action="take", id=docs))  # nothing it shares: nothing said
        self.hub._maps = None
        self.assertEqual(self.hub.blast_radius(b_name, cart), "")  # no map: no note

    def test_joining_agents_get_a_brief_of_the_code(self):
        # Roadmap C6: a skill (and a Cursor rule) made from the code map and the sheet, stamped with the commit;
        # AGENTS.md only points to it (owner, 18 Oct: not every turn's cost).
        code = Path(tempfile.mkdtemp(prefix="brief-", dir=TMP)).resolve()
        for rel, text in {"brieflib/core.py": "class Engine:\n    def run(self):\n        pass\n", "brieflib/__init__.py": "",
                          "briefapp/main.py": "from brieflib.core import Engine\n", "AGENTS.md": "# Our notes\n"}.items():
            (code / rel).parent.mkdir(parents=True, exist_ok=True)
            (code / rel).write_text(text)
        folders = mock.patch.object(crewchat.Hub, "project_folders", lambda hub, place=None: [code])
        folders.start()
        self.addCleanup(folders.stop)
        import crewchat_map
        self.hub._maps = crewchat_map.Maps(Path(tempfile.mkdtemp(prefix="brief-maps-", dir=TMP)))
        self.addCleanup(self.hub._maps.close)
        (a, a_name), _ = self.crew
        mid = self.task("Speed up the engine", "brieflib/")
        a.call("hub_task", action="take", id=mid)
        self.hub.update_map()
        brief = (code / crewchat.BRIEF_SKILL).read_text()
        self.assertTrue(brief.startswith("---\nname: crewchat-brief\n"))
        self.assertIn("at commit `none`", brief)  # not a git folder: stamped as such
        self.assertIn("- `brieflib/core.py`: used by 1; defines Engine", brief)
        self.assertIn("- `briefapp/main.py`", brief)  # where it starts
        self.assertIn("- #%s Speed up the engine: %s, in progress; files brieflib/" % (mid, a_name), brief)
        self.assertIn("alwaysApply: false", (code / crewchat.BRIEF_RULE).read_text())
        self.assertIn("read %s when you join" % crewchat.BRIEF_SKILL, (code / "AGENTS.md").read_text())
        self.assertNotIn("used by 1", (code / "AGENTS.md").read_text())  # a pointer, not the brief
        with mock.patch.object(crewchat_map.CodeMap, "overview") as overview:
            self.assertEqual(self.hub.update_map(), {})
        self.assertFalse(overview.called)  # no edit, no commit, the same sheet: not even made again
        (code / "NOTES.md").write_text("not code\n")
        self.hub._map_edited.add("NOTES.md")
        with mock.patch.object(crewchat_map.CodeMap, "overview") as overview:
            self.assertEqual(list(self.hub.update_map().values()), [[]])  # an edit the map does not read
        self.assertFalse(overview.called)
        self.assertEqual(self.hub.write_briefs(self.hub._maps), [])  # nothing new: nothing written
        a.text("hub_task", action="update", id=mid, status="done", note="faster\nDecisions: none\nLeft over: none")
        self.assertEqual(self.hub.write_briefs(self.hub._maps), [str(code)])  # the sheet moved: written again
        self.assertNotIn("#%s Speed up the engine" % mid, (code / crewchat.BRIEF_SKILL).read_text())
        crewchat.remove_guides(code)
        self.assertFalse((code / crewchat.BRIEF_SKILL).exists() or (code / crewchat.BRIEF_RULE).exists())

    def test_suggested_areas_travel_as_a_message_field(self):
        mid = self.task("From another machine")
        self.assertTrue(self.hub.ingest({"id": "far-%s" % mid, "ts": time.time(), "from": crewchat.SERVER_NAME,
                                         "to": crewchat.OWNER, "text": "suggested", "kind": "msg", "task": mid,
                                         "suggested": "api/,docs/"}))
        self.assertEqual(self.sheet(mid)["suggested"], ["api/", "docs/"])

    def test_tasks_whose_code_imports_the_others_do_not_run_at_once(self):
        # Done when: two tasks that share no prefix, one importing the other's code, overlap; without the map
        # (C0 alone) only prefixes count.
        code = Path(tempfile.mkdtemp(prefix="areas-", dir=TMP)).resolve()
        for rel, text in {"shopapp/main.py": "from shoplib.parse import read\n", "shoplib/__init__.py": "",
                          "shoplib/parse.py": "def read():\n    pass\n", "shoptools/run.py": "import json\n"}.items():
            (code / rel).parent.mkdir(parents=True, exist_ok=True)
            (code / rel).write_text(text)
        (a, a_name), (b, b_name) = self.crew
        first, second, third = self.task("Change the parser", "shoplib/"), self.task("Change the app", "shopapp/"), \
            self.task("Change the tools", "shoptools/")
        a.call("hub_task", action="take", id=first)
        free = lambda: [r["id"] for r in self.hub.sheet_rows(b_name, "next")]  # noqa: E731
        self.assertIn(second, free())  # no map: shopapp/ and shoplib/ share no prefix
        import crewchat_map
        maps = crewchat_map.Maps(Path(tempfile.mkdtemp(prefix="areas-maps-", dir=TMP)))
        self.addCleanup(maps.close)
        maps.check([code])
        self.hub._maps = maps
        self.assertNotIn(second, free())
        self.assertIn(third, free())
        self.assertIn("import, or are imported by, those of #%s, which %s is working on" % (first, a_name),
                      b.text("hub_task", action="take", id=second))
        a.text("hub_task", action="update", id=first, status="done", note="done\nDecisions: none\nLeft over: none")
        self.assertIn(second, free())  # the parser is done: the app task may start
        self.assertEqual(self.row(second).get("blocker"), "")  # and the sheet no longer says it is held back

    def row(self, mid):
        return next(r for r in self.hub.sheet_rows() if r["id"] == mid)

    def test_the_sheet_shows_what_holds_a_task_back_before_anyone_assigns_it(self):
        # The lead reads why on the sheet, not only from the agent it gave the task to (a question on X, 30 Oct).
        code = Path(tempfile.mkdtemp(prefix="blocker-", dir=TMP)).resolve()
        for rel, text in {"shopapp/main.py": "from shoplib.parse import read\n", "shoplib/__init__.py": "",
                          "shoplib/parse.py": "def read():\n    pass\n", "shoptools/run.py": "import json\n"}.items():
            (code / rel).parent.mkdir(parents=True, exist_ok=True)
            (code / rel).write_text(text)
        (a, a_name), (b, b_name) = self.crew
        first, second, third = self.task("Change the parser", "shoplib/"), self.task("Change the app", "shopapp/"), \
            self.task("Change the tools", "shoptools/")
        same = self.task("Change the parser's tests", "shoplib/parse.py")
        a.call("hub_task", action="take", id=first)
        import crewchat_map
        maps = crewchat_map.Maps(Path(tempfile.mkdtemp(prefix="blocker-maps-", dir=TMP)))
        self.addCleanup(maps.close)
        maps.check([code])
        self.hub._maps = maps
        why = "its files import, or are imported by, those of #%s, which %s is working on" % (first, a_name)
        self.assertEqual(self.row(second)["blocker"], why)
        self.assertEqual(self.row(same)["blocker"], "its files overlap #%s, which %s is working on" % (first, a_name))
        self.assertEqual(self.row(third)["blocker"], "")
        own = self.task("Change the parser's docs", "shoplib/parse.py")
        self.assertIn(own, [r["id"] for r in self.hub.sheet_rows(a_name, "next")])  # its own work holds nothing back
        self.assertEqual(next(r for r in self.hub.sheet_rows(a_name) if r["id"] == own)["blocker"], "")
        self.assertEqual(self.row(first)["blocker"], "")  # it has a holder: nothing holds it back
        self.assertIn("held back: " + why, b.text("hub_task", action="view", show="all"))
        self.assertIn(first, [r["id"] for r in self.hub.sheet_rows(a_name, "mine")])

    def test_the_page_shows_the_blocker(self):
        for shown in ('"held back: " + r.blocker', 'field("Held back", r.blocker)', "r.parent, r.blocker,"):
            self.assertIn(shown, crewchat.CHAT_PAGE)


class QuickStart(unittest.TestCase):
    def test_the_server_does_not_look_its_own_name_up(self):
        # HTTPServer looks up the name of 127.0.0.1 as it starts: a Mac with slow reverse DNS waited 30 s (seen on
        # GitHub's macOS runner, 3 Nov). Nothing uses that name.
        root = Path(tempfile.mkdtemp(prefix="quick-", dir=TMP))
        crewchat.save_config({"project": "web", "port": 9}, root)
        with mock.patch("socket.getfqdn", side_effect=AssertionError("looked its own name up")):
            server = crewchat.make_server(0, root)
        crewchat.flush_all(server.RequestHandlerClass.projects)
        server.server_close()
        self.assertEqual(server.server_name, "127.0.0.1")


class LeadTools(Base):
    """Owner, 18 Oct: the lead and the advisor get hub_code (C5 history, C7 split); every other agent's tools stay
    as short as they are. A session whose role changes is told its tool list changed (MCP list_changed)."""

    def test_a_new_lead_on_an_older_protocol_is_told_to_start_again_for_hub_code(self):
        for streams, told in ((False, True), (True, False)):
            sid = self.hub.open_session("macbook", "claude")
            name = self.hub.agent_for(sid)
            with self.hub.lock:
                self.hub.sessions[sid]["streams"] = streams  # what its initialize said (list_changed from 2025-03-26)
            msg = self.hub.set_role(crewchat.OWNER, name, "lead")
            self.assertEqual("start a new session to have it" in msg["text"], told, streams)
            self.hub.set_role(crewchat.OWNER, name, "")
            self.hub.remove(name)

    def role(self, name, role):
        crewchat.post_json(self.base + "/api/role", self.owner, {"agent": name, "role": role})

    def tools(self, session):
        return [t["name"] for t in crewchat.rpc(session.mcp, self.token, "tools/list", {}, session=session.sid)["result"]["tools"]]

    def call_stream(self, session, tool, **arguments):
        """A tool call, every message of the answer: notifications first, the result last."""
        body = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
        return crewchat.post_json(session.mcp, self.token, body, headers={"Mcp-Session-Id": session.sid}, stream=True)

    def test_only_the_lead_and_the_advisor_have_hub_code(self):
        self.assertLessEqual(len(json.dumps(crewchat.TOOLS)), 9500)  # everyone's: P2's 7,200, L2's hub_process, return (Q1), #230, hub_question (#237)
        self.assertLessEqual(len(json.dumps(crewchat.TOOLS + crewchat.LEAD_TOOLS)), 10150)  # + threads (owner, 9 Nov), return (Q1), #230, #237
        self.assertTrue(Session(self.base, self.token).hello["capabilities"]["tools"]["listChanged"])
        session, name = self.agent()
        self.assertNotIn("hub_code", self.tools(session))
        self.assertIn("is for the lead and the advisor", session.text("hub_code", action="history", path="app/x.py"))
        self.assertEqual([m.get("method") for m in self.call_stream(session, "hub_agents")], [None])  # nothing new
        self.role(name, "lead")
        told = self.call_stream(session, "hub_agents")  # its next answer says its tools changed
        self.assertEqual(told[0], {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        self.assertIn("result", told[-1])
        self.assertIn("hub_code", self.tools(session))
        self.assertEqual(len(self.call_stream(session, "hub_agents")), 1)  # told once
        self.assertIn("hub_code split", session.text("hub_role"))  # the lead's instructions say how
        self.role(name, "none")
        self.assertEqual(self.call_stream(session, "hub_agents")[0].get("method"), "notifications/tools/list_changed")
        self.assertNotIn("hub_code", self.tools(session))
        # A client on the 2024-11-05 protocol, which knew no answers as event streams, is not told: plain JSON.
        old = Session(self.base, self.token)
        old_name = old.me
        with self.hub.lock:
            self.hub.sessions[old.sid]["streams"] = False
        self.tools(old)
        self.role(old_name, "advisor")
        self.assertEqual([m.get("method") for m in self.call_stream(old, "hub_agents")], [None])
        self.assertIn("hub_code", self.tools(old))  # it gets the tool when it asks again (a new session)
        self.role(old_name, "none")

    def test_a_files_history_says_who_for_which_task_and_what_was_said(self):
        # Roadmap C5.
        (lead, lead_name), (dev, dev_name) = self.agent(), self.agent()
        self.role(lead_name, "advisor")
        task = self.say("all", "Fix the sync of history/engine.py", "task")
        dev.call("hub_task", action="take", id=task)
        with self.hub.lock:
            for _ in range(3):
                self.hub.events.add_edit(dev_name, "history/engine.py", task)
            self.hub.events.add_edit(lead_name, "history/engine.py", "")
            self.hub.events.add_edit(dev_name, "history/other.py", task)
        dev.call("hub_task", action="update", id=task, status="review", note="engine.py retries now")
        dev.call("hub_send", to="all", text="heads up: engine.py changed its retry loop")
        out = lead.text("hub_code", action="history", path="history/engine.py")
        self.assertIn("history/engine.py: edited 4 times, by %s (3), %s (1)." % (dev_name, lead_name), out)
        self.assertIn("Tasks: #%s Fix the sync of history/engine.py (review, %s)." % (task, dev_name), out)
        self.assertIn("[REVIEW] engine.py retries now", out)
        self.assertIn("heads up: engine.py changed its retry loop", out)  # named in a message
        self.assertIn("edited 5 times", lead.text("hub_code", action="history", path="history/"))  # a folder
        self.assertIn("no edits recorded", lead.text("hub_code", action="history", path="nowhere/x.py"))
        if os.name == "nt":
            return  # another thread's connection still holds the file open, and Windows will not delete it
        self.hub.chat_index.close()
        (self.hub.home / crewchat.HISTORY_FILE).unlink()  # derived: deleted, it is built again
        self.assertIn("[REVIEW] engine.py retries now", lead.text("hub_code", action="history", path="history/engine.py"))

    def test_a_task_split_into_groups_that_hang_together(self):
        # Roadmap C7: groups by imports, then folders.
        code = Path(tempfile.mkdtemp(prefix="split-", dir=TMP)).resolve()
        files = {"splitapp/sync/engine.py": "from splitapp.sync import queue\n", "splitapp/sync/queue.py": "",
                 "splitapp/sync/__init__.py": "", "splitapp/ui/view.py": "from splitapp.ui import theme\n",
                 "splitapp/ui/theme.py": "", "splitapp/ui/__init__.py": "", "splitapp/__init__.py": ""}
        for rel, text in files.items():
            (code / rel).parent.mkdir(parents=True, exist_ok=True)
            (code / rel).write_text(text)
        import crewchat_map
        maps = crewchat_map.Maps(Path(tempfile.mkdtemp(prefix="split-maps-", dir=TMP)))
        self.addCleanup(maps.close)
        maps.check([code])
        saved, self.hub._maps = self.hub._maps, maps
        self.addCleanup(setattr, self.hub, "_maps", saved)
        lead, lead_name = self.agent()
        self.role(lead_name, "lead")
        self.addCleanup(self.role, lead_name, "none")
        task = crewchat.post_json(self.base + "/api/task", self.owner, {"op": "add", "title": "The app", "areas": "splitapp/"})["id"]
        out = lead.text("hub_code", action="split", id=task, count=2)
        self.assertIn("Files of task #%s (splitapp/) in 2 groups" % task, out)
        self.assertRegex(out, r"(?m)^1\. splitapp/(sync|ui)/ \(\d files\): ")
        self.assertRegex(out, r"(?m)^2\. splitapp/(sync|ui)/ ")
        self.assertIn("is not on the task sheet", lead.text("hub_code", action="split", id="999999"))

    def test_the_stdio_relay_passes_the_notice_on(self):
        session, name = self.agent()
        relay = crewchat.StdioRelay(self.base + "/mcp", self.token)
        relay.session = session.sid
        out = io.BytesIO()
        with mock.patch.object(sys, "stdout", mock.Mock(buffer=out)):
            relay.send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            self.role(name, "advisor")
            relay.send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "hub_agents", "arguments": {}}})
        lines = [json.loads(l) for l in out.getvalue().decode().splitlines()]
        self.assertEqual([l.get("id", l.get("method")) for l in lines], [1, "notifications/tools/list_changed", 2])


class Teams(Base):
    def role(self, agent, role):
        return crewchat.post_json(self.base + "/api/role", self.owner, {"agent": agent, "role": role})

    def row(self, name):
        return next(r for r in self.hub.rows() if r["agent"] == name)

    def test_the_owner_gives_roles_and_the_agent_gets_its_instructions(self):
        dev, dev_name = self.agent()
        self.role(dev_name, "developer")
        self.assertEqual(self.row(dev_name)["role"], "developer")
        inbox = dev.text("hub_inbox")
        self.assertIn("[ROLE]", inbox)
        self.assertIn("Your role in this chat is now: Developer (set by the owner)", inbox)
        self.assertIn("tell the QA agent", inbox)
        self.assertIn("%s (you) [developer]:" % dev_name, dev.text("hub_agents"))
        self.assertIn("You are a developer", dev.text("hub_role"))
        with self.assertRaises(urllib.error.HTTPError):
            self.role(dev_name, "astronaut")
        self.role(dev_name, "none")
        self.assertEqual(self.row(dev_name)["role"], "")
        self.assertIn("You have no role", dev.text("hub_role"))
        # From the chat page too, with the page's session.
        opener, _ = self.owner_browser()
        status, _, _ = self.raw("/api/role", json.dumps({"agent": dev_name, "role": "qa"}).encode(),
                                {"Content-Type": "application/json", "Origin": self.base}, opener)
        self.assertEqual((status, self.row(dev_name)["role"]), (200, "qa"))
        boot = json.loads(self.raw("/api/boot", opener=opener)[1])
        qa = next(r for r in boot["roles"] if r["name"] == "qa")
        self.assertEqual((qa["title"], qa["picture"], qa["source"]), ("QA", "art:scientist", "pack"))
        self.assertNotIn("description", qa)  # instructions come from /api/roles

    def test_there_is_one_lead(self):
        (one, n1), (two, n2) = self.agent(), self.agent()
        self.role(n1, "lead")
        self.role(n2, "lead")
        self.assertEqual((self.row(n1)["role"], self.row(n2)["role"]), ("", "lead"))
        self.assertIn("You are no longer the lead: %s is." % n2, one.text("hub_inbox"))
        self.role(n2, "none")

    def test_the_lead_assigns_and_hears_back(self):
        (lead, lead_name), (dev, dev_name), (qa, qa_name) = self.agent(), self.agent(), self.agent()
        self.role(lead_name, "lead")
        self.role(dev_name, "developer")
        self.role(qa_name, "qa")
        for s in (lead, dev, qa):
            s.text("hub_inbox")
        job = self.say("all", "Add a dark theme", "task")
        self.assertIn("With a [lead] in the chat, leave it to the lead", lead.hello["instructions"])
        self.assertIn("only the lead assigns", dev.call("hub_task", action="assign", to=qa_name, title="x")["content"][0]["text"])
        lead.call("hub_task", action="take", id=job)
        out = lead.text("hub_task", action="assign", to=dev_name, title="Build the theme switch", parent=job)
        piece = re.search(r"task #(\w+)", out).group(1)
        self.assertEqual(self.hub.taken[piece], dev_name)
        got = dev.text("hub_inbox")
        self.assertIn("[TASK, assigned to you, part of #%s] Build the theme switch" % job, got)
        # Progress goes to the lead, and shows on the task.
        self.assertIn(lead_name, dev.text("hub_task", action="update", id=piece, status="review",
                                                                note="Ready: toggle in Settings"))
        self.assertEqual(self.hub.progress[piece]["status"], "review")
        self.assertIn("[UPDATE on #%s: ready for review] Ready: toggle in Settings" % piece, lead.text("hub_inbox"))
        self.assertIn("[%s: ready for review]" % dev_name, lead.text("hub_history", limit=10))
        # Developer and QA talk directly.
        dev.call("hub_send", to=qa_name, text="Theme switch is ready to test: Settings > Theme")
        qa_in = qa.text("hub_inbox")
        self.assertIn("ready to test", qa_in)
        qa.call("hub_send", to=dev_name, text="Bug: the switch resets on restart")
        self.assertIn("resets on restart", dev.text("hub_inbox"))
        self.assertIn("status must be one of", qa.call("hub_task", action="update", id=piece, status="sideways")["content"][0]["text"])
        # Done: the lead is told the developer is free, to hand it the next piece.
        dev.call("hub_task", action="update", id=piece, status="done", note="Merged\nDecisions: none\nLeft over: none")
        done = lead.text("hub_inbox")
        self.assertIn("[UPDATE on #%s: done] Merged" % piece, done)
        self.assertIn("%s is free. Check the work, then give it its next task with hub_task assign" % dev_name, done)
        self.assertIn("give that agent its next task with hub_task assign right away", crewchat.ROLES["lead"][1])
        for name in (lead_name, dev_name, qa_name):
            self.role(name, "none")

    def test_without_a_lead_updates_go_to_whoever_posted_the_task(self):
        dev, dev_name = self.agent()
        job = self.say(dev_name, "Fix the login test", "task")
        dev.call("hub_task", action="take", id=job)
        self.assertIn("the owner has been told", dev.text("hub_task", action="update", id=job, status="in_progress"))
        update = self.hub.history(1)[0]
        self.assertEqual((update["to"], update["kind"], update["text"]), ("Owner", "update", "in progress"))

    def test_a_task_from_the_owner_is_confirmed_to_the_owner_even_with_a_lead(self):
        (lead, lead_name), (dev, dev_name) = self.agent(), self.agent()
        self.role(lead_name, "lead")
        job = self.say(dev_name, "Work on T229", "task")
        taken = dev.text("hub_task", action="take", id=job)
        self.assertIn("Before any other work, send hub_task action=update id=%s status=in_progress" % job, taken)
        self.assertIn("the owner has been told", dev.text("hub_task", action="update", id=job, status="in_progress",
                                                          note="Starting: reading the backup code"))
        update = self.hub.history(1)[0]
        self.assertEqual((update["to"], update["text"]), ("Owner", "Starting: reading the backup code"))
        self.assertIn("first hub_task update it to\n  in_progress", dev.hello["instructions"])
        self.role(lead_name, "none")

    def test_an_agent_takes_a_role_when_told_to_and_custom_roles(self):
        session, name = self.agent()
        self.assertIn("Your role in this chat is now: Reviewer (set by %s)" % name,
                      session.text("hub_role", role="reviewer"))
        self.assertEqual(self.row(name)["role"], "reviewer")
        cli("roles", "add", "designer", "--prompt", "You design screens.", "--title", "Designer")
        self.assertIn("designer     Designer", cli("roles"))
        self.assertIn("You design screens.", cli("roles", "show", "designer"))
        cli("role", name, "designer")
        self.assertIn("You design screens.", session.text("hub_inbox"))
        cli("roles", "remove", "designer")
        self.assertNotIn("designer", cli("roles"))
        cli("role", name, "none")

    def test_the_loop_limit_comes_from_the_config(self):
        self.assertEqual(self.hub.roster.max_chain, crewchat.MAX_CHAIN)
        config = crewchat.load_config()
        config["max_chain"] = 25
        crewchat.save_config(config)
        self.hub.roster.refresh()
        try:
            self.assertEqual(self.hub.roster.max_chain, 25)
            hook = lambda chain: crewchat.post_json(self.base + "/api/hook", self.token,  # noqa: E731
                                                    {"key": "k" * 16, "wait": 0, "chain": chain})["capped"]
            self.assertEqual((hook(24), hook(25)), (False, True))
        finally:
            config.pop("max_chain")
            crewchat.save_config(config)


class RolePacks(unittest.TestCase):
    """Role packs and the owner's changes to a project's roles, on a config with no server."""

    def setUp(self):
        self.packs = dict(crewchat.ROLE_PACKS)

    def change(self, config, op, **data):
        return crewchat.change_roles(config, dict(data, op=op), self.packs)[0]

    def test_a_project_from_before_packs_has_the_software_roles(self):
        self.assertEqual(crewchat.role_texts(crewchat.role_table({}, self.packs)), crewchat.ROLES)
        self.assertEqual(crewchat.project_pack({"pack": "gone"}, self.packs), "software")
        old = {"roles": {"designer": {"title": "Designer", "prompt": "You design."}}}
        row = crewchat.role_table(old, self.packs)["designer"]
        self.assertEqual((row["label"], row["description"], row["source"]), ("Designer", "You design.", "yours"))

    def test_every_pack_has_a_lead_and_an_advisor_and_says_what_its_roles_do(self):
        for pid, pack in self.packs.items():
            self.assertIn(pack["crew"], crewchat.CREWS)
            if pid == "blank":
                self.assertEqual(pack["roles"], {})
                continue
            self.assertLessEqual({"lead", "advisor"}, set(pack["roles"]), pid)
            for name, role in pack["roles"].items():
                self.assertEqual(crewchat.clean_role(role)["description"], role["description"], (pid, name))
                self.assertEqual(crewchat.clean_picture(role["picture"]), role["picture"], (pid, name))
                self.assertTrue(role["picture"].startswith("art:"), (pid, name))  # the 3D pictures
        lead = self.packs["youtube"]["roles"]["lead"]
        self.assertEqual(lead["label"], "Producer")
        self.assertIn("hub_task assign", lead["description"])
        self.assertIn("Never publish, post", self.packs["business"]["roles"]["support"]["description"])
        self.assertEqual([r["label"] for r in self.packs["instagram"]["roles"].values()][:5],
                         ["Content planner", "Caption writer", "Visual designer", "Community manager", "Analytics"])

    def test_create_edit_duplicate_delete(self):
        config = {}
        role = self.change(config, "create", name="Designer", label="Designer", description="You design.",
                           picture="preset:palette", colour="#ABCDEF", tool="cursor")["role"]
        self.assertEqual(role, {"name": "designer", "label": "Designer", "description": "You design.",
                                "picture": "preset:palette", "colour": "#abcdef", "tool": "cursor", "powers": [],
                                "reports_to": ""})
        with self.assertRaisesRegex(crewchat.HubError, "already"):
            self.change(config, "create", name="designer", description="Again.")
        with self.assertRaisesRegex(crewchat.HubError, "description"):
            self.change(config, "create", name="empty", description="  ")
        with self.assertRaisesRegex(crewchat.HubError, "letters, digits"):
            self.change(config, "create", name="../x", description="x")
        with self.assertRaisesRegex(crewchat.HubError, "no tool called no-such-tool"):
            self.change(config, "edit", name="designer", tool="no-such-tool")
        edited = self.change(config, "edit", name="designer", label="Screen designer", tool="")["role"]
        self.assertEqual((edited["label"], edited["description"], edited["tool"]), ("Screen designer", "You design.", ""))
        copy = self.change(config, "duplicate", name="designer")["role"]
        self.assertEqual((copy["name"], copy["label"]), ("designer-copy", "Screen designer (copy)"))
        self.assertEqual(self.change(config, "duplicate", name="designer")["role"]["name"], "designer-copy-2")
        self.assertEqual(self.change(config, "delete", name="designer-copy"), {"deleted": "designer-copy"})
        self.assertNotIn("designer-copy", config["roles"])

    def test_a_pack_role_can_be_changed_and_deleted_and_the_pack_changed(self):
        config = {}
        self.change(config, "edit", name="qa", label="Tester")
        self.assertEqual(crewchat.role_table(config, self.packs)["qa"]["source"], "yours")
        self.change(config, "delete", name="docs")
        self.assertEqual(config["roles"]["docs"], {"deleted": True})
        self.assertNotIn("docs", crewchat.role_table(config, self.packs))
        with self.assertRaisesRegex(crewchat.HubError, "no role called docs"):
            self.change(config, "edit", name="docs", label="Docs")
        listing, effects = crewchat.change_roles(config, {"op": "pack", "pack": "youtube"}, self.packs)
        self.assertEqual((config["pack"], config["roles"], effects), ("youtube", {}, {"changed": True}))
        self.assertEqual([r["name"] for r in listing["roles"]][:2], ["lead", "researcher"])
        with self.assertRaisesRegex(crewchat.HubError, "no pack called nope"):
            self.change(config, "pack", pack="nope")

    def test_export_and_import(self):
        source = {"pack": "business"}
        exported = self.change(source, "export")
        self.assertEqual((exported["crewchat"], exported["version"]), ("roles", 1))
        self.assertEqual(set(exported["roles"]), set(self.packs["business"]["roles"]))
        self.assertEqual(set(self.change(source, "export", names=["lead"])["roles"]), {"lead"})
        target = {}
        imported = self.change(target, "import", roles=json.loads(json.dumps(exported)))["imported"]
        self.assertIn("bookkeeping", imported)
        table = crewchat.role_table(target, self.packs)
        self.assertEqual(table["lead"]["label"], "Manager")  # an imported role replaces the pack's
        self.assertEqual(table["developer"]["label"], "Developer")  # the rest of the pack stays
        self.assertEqual(self.change({}, "import", roles=[{"name": "x", "description": "Do x."}])["imported"], ["x"])

    def test_a_malformed_import_keeps_nothing(self):
        for bad, why in ((None, "nothing to import"), ({}, "nothing to import"), ("text", "nothing to import"),
                         ([1, 2], "list of roles"), ({"ok": {"description": "Fine."}, "bad": {"label": "No text"}},
                                                     "bad has no description"),
                         ({"../up": {"description": "x"}}, "letters, digits"),
                         ({"r%d" % i: {"description": "x"} for i in range(51)}, "at most 50")):
            config = {}
            with self.assertRaisesRegex(crewchat.HubError, why):
                self.change(config, "import", roles=bad)
            self.assertFalse(config.get("roles"))

    def test_a_pack_saved_from_its_label_alone(self):
        config = {"pack": "software"}
        self.assertEqual(self.change(config, "save_pack", label="Night shift")["pack"]["id"], "night-shift")
        self.assertEqual(self.change(config, "save_pack", label="2024 crew")["pack"]["id"], "pack")
        with self.assertRaisesRegex(crewchat.HubError, "built-in"):
            self.change(config, "save_pack", label="YouTube")

    def test_a_saved_pack_is_replaced_only_when_asked(self):
        config = {"pack": "software"}
        packs = dict(self.packs, crew=dict(crewchat.clean_pack({"label": "Crew", "roles": {}}), saved=True))
        with self.assertRaisesRegex(crewchat.HubError, "already"):
            crewchat.change_roles(config, {"op": "save_pack", "label": "Crew!"}, packs)
        self.assertEqual(crewchat.change_roles(config, {"op": "save_pack", "label": "Crew!", "replace": True}, packs)[0]["pack"]["id"], "crew")

    def test_saved_packs(self):
        config = {"pack": "instagram"}
        self.change(config, "edit", name="captions", label="Copywriter")
        pack = self.change(config, "save_pack", id="My-Studio", label="My studio")["pack"]
        self.assertEqual((pack["id"], pack["label"], pack["crew"]), ("my-studio", "My studio", "general"))
        self.assertEqual(pack["roles"]["captions"]["label"], "Copywriter")
        with self.assertRaisesRegex(crewchat.HubError, "built-in"):
            self.change(config, "save_pack", id="youtube")
        with tempfile.TemporaryDirectory() as base:
            Path(base, "config.json").write_text(json.dumps({"packs": {
                "my-studio": pack, "youtube": pack, "bad": {"roles": "no"}, "../x": pack}}))
            packs = crewchat.role_packs(base)
            self.assertTrue(packs["my-studio"]["saved"])
            self.assertEqual(packs["youtube"], crewchat.ROLE_PACKS["youtube"])  # a built-in can't be replaced
            self.assertNotIn("bad", packs)
            self.assertNotIn("../x", packs)
            table = crewchat.role_table({"pack": "my-studio"}, packs)
            self.assertEqual(table["captions"]["label"], "Copywriter")
        self.assertEqual(crewchat.role_packs(os.path.join(TMP, "nowhere")), crewchat.ROLE_PACKS)

    def test_a_role_written_with_capitals_can_still_be_changed(self):
        config = {"roles": {"Designer": {"title": "Designer", "prompt": "You design."}}}
        self.assertIn("designer", crewchat.role_table(config, self.packs))
        self.assertEqual(self.change(config, "edit", name="Designer", label="Screens")["role"]["label"], "Screens")

    def test_roles_from_elsewhere_are_reduced_to_what_the_page_can_draw(self):
        row = crewchat.clean_role({"label": "  Two\n words " + "x" * 80, "description": "d" * 9000,
                                   "picture": "javascript:alert(1)", "colour": "red", "tool": "constructor"})
        self.assertEqual(len(row["label"]), crewchat.MAX_ROLE_LABEL)
        self.assertTrue(row["label"].startswith("Two words "))
        self.assertEqual(len(row["description"]), crewchat.MAX_ROLE_TEXT)
        self.assertEqual((row["picture"], row["colour"], row["tool"]), ("", "", ""))
        self.assertIsNone(crewchat.clean_role("not a dict"))


class Roles(Base):
    """The owner's /api/roles, and what agents and the page see of it."""

    def setUp(self):
        self.root = crewchat.home()
        self.saved = crewchat.load_config(self.root)

    def tearDown(self):
        crewchat.save_config(self.saved, self.root)
        mine = crewchat.load_config()
        mine.pop("packs", None)
        crewchat.save_config(mine)
        self.hub.roster.refresh(force=True)

    def op(self, token=None, **body):
        return self.op_at("/api/roles", token, **body)

    def poll(self):
        return self.hub.poll(-1, -1, 0)

    def test_the_poll_carries_a_tag_and_api_boot_the_rest(self):
        # What changes only with the server or the roles comes from /api/boot (roadmap P4); the poll says
        # when to fetch it again.
        self.assertEqual(self.raw("/api/boot")[0], 401)
        opener, _ = self.owner_browser()
        boot = json.loads(self.raw("/api/boot", opener=opener)[1])
        moved = {"roles", "statuses", "tools", "presets", "art", "pictures", "models", "names", "images",
                 "max_upload", "releases", "need_kinds"}
        # welcome, missing and recent are read fresh, outside the cached boot
        self.assertEqual(set(boot), moved | {"tag", "welcome", "missing", "recent", "held", "paused", "list_damaged"})
        poll = json.loads(self.raw("/api/poll?after=-1&v=-1&wait=0", opener=opener)[1])
        self.assertFalse(set(poll) & moved)
        self.assertEqual(poll["boot"], boot["tag"])
        self.assertEqual(self.poll()["boot"], boot["tag"])  # the same while nothing changes
        self.op(op="create", name="tagger", label="Tagger", description="You tag things.")
        try:
            changed = self.hub.boot()
            self.assertNotEqual(changed["tag"], boot["tag"])  # a role edited: the page fetches again
            self.assertEqual(self.poll()["boot"], changed["tag"])
            self.assertIn("tagger", [r["name"] for r in changed["roles"]])
        finally:
            self.op(op="delete", name="tagger")
        self.assertEqual(self.hub.boot()["tag"], boot["tag"])

    def test_only_the_owner_changes_roles(self):
        self.assertEqual(self.op(token=self.token, op="list")["status"], 403)
        listing = self.op(op="list")
        self.assertEqual(listing["pack"], "software")
        self.assertIn("youtube", [p["id"] for p in listing["packs"]])
        self.assertIn("op is", self.op(op="explode")["error"])

    def test_a_new_role_reaches_the_page_and_the_agents(self):
        role = self.op(op="create", name="designer", label="Designer", description="You design screens.",
                       picture="preset:palette", tool="cursor")["role"]
        self.assertEqual(role["name"], "designer")
        page = {r["name"]: r for r in self.hub.boot()["roles"]}
        self.assertEqual((page["designer"]["title"], page["designer"]["tool"]), ("Designer", "cursor"))
        session, name = self.agent()
        crewchat.post_json(self.base + "/api/role", self.owner, {"agent": name, "role": "designer"})
        self.assertIn("You design screens.", session.text("hub_inbox"))
        self.assertIn("Your role: Designer.", session.text("hub_role"))

    def test_a_role_deleted_while_an_agent_has_it(self):
        session, name = self.agent()
        crewchat.post_json(self.base + "/api/role", self.owner, {"agent": name, "role": "docs"})
        session.text("hub_inbox")
        self.assertEqual(self.op(op="delete", name="docs"), {"deleted": "docs"})
        self.assertEqual(next(r for r in self.hub.rows() if r["agent"] == name)["role"], "")
        self.assertIn("You no longer have a role", session.text("hub_inbox"))
        self.assertNotIn("docs", [r["name"] for r in self.hub.boot()["roles"]])
        self.assertIn("no role called docs", self.op(op="edit", name="docs", label="x")["error"])

    def test_a_pack_sets_the_crew_and_takes_away_roles_it_does_not_have(self):
        session, name = self.agent()
        crewchat.post_json(self.base + "/api/role", self.owner, {"agent": name, "role": "qa"})
        session.text("hub_inbox")
        self.assertEqual(self.poll()["crew"], "software")
        listing = self.op(op="pack", pack="youtube")
        self.assertEqual(listing["pack"], "youtube")
        poll, boot = self.poll(), self.hub.boot()
        self.assertEqual((poll["pack"], poll["crew"]), ("youtube", "general"))
        self.assertEqual(next(r for r in self.hub.rows() if r["agent"] == name)["role"], "")  # no qa in youtube
        self.assertIn("You no longer have a role", session.text("hub_inbox"))
        self.assertEqual(next(r for r in boot["roles"] if r["name"] == "lead")["title"], "Producer")
        self.op(op="pack", pack="software")
        self.assertEqual(self.poll()["crew"], "software")

    def test_a_role_another_machine_gave_is_kept_when_this_one_changes_its_roles(self):
        session, name = self.agent()
        with self.hub.lock:
            self.hub.agents[name]["role"] = "researcher"  # from a linked machine on the YouTube pack
        self.op(op="delete", name="docs")
        self.op(op="pack", pack="business")
        self.assertEqual(next(r for r in self.hub.rows() if r["agent"] == name)["role"], "researcher")
        self.assertNotIn("You no longer have a role", session.text("hub_inbox"))
        with self.hub.lock:
            self.hub.agents[name]["role"] = ""

    def test_a_command_value_cmd_would_act_on_is_refused_on_windows(self):
        # Release review: on Windows list2cmdline does not quote & | < > ^ %, and the lines run through cmd.exe.
        with mock.patch.object(crewchat.os, "name", "nt"):
            with self.assertRaises(crewchat.HubError) as e:
                crewchat.command_line("git tag {V}", {"V": "1.0&del /q *"})
            self.assertIn("cmd.exe", str(e.exception))
            self.assertEqual(crewchat.command_line("git tag {V}", {"V": "1.0.2"}), "git tag 1.0.2")
        if os.name != "nt":
            self.assertEqual(crewchat.command_line("git tag {V}", {"V": "1.0&x"}), "git tag '1.0&x'")

    def test_the_command_line_without_a_server(self):
        with mock.patch.object(crewchat, "local_url", lambda config=None: "http://127.0.0.1:9"):  # nothing listens
            out = cli("roles", "add", "offline-role", "--prompt", "You work offline.")
            self.assertIn("Role offline-role saved", out)
            self.assertIn("agents keep the role", cli("roles", "remove", "offline-role"))
        self.assertNotIn("offline-role", crewchat.role_table(crewchat.load_config(self.root), crewchat.role_packs()))

    def test_a_request_of_the_wrong_shape_is_an_error_not_a_crash(self):
        self.assertIn("no tool called", self.op(op="create", name="x", description="x", tool=["cursor"])["error"])
        self.assertIn("names start with a letter", self.op(op="create", name=["x"], description="x")["error"])

    def test_the_owner_is_told_about_a_bad_picture_colour_or_tool(self):
        for field, value, why in (("picture", "preset:nope", "is not a picture"), ("colour", "red", "#rrggbb"),
                                  ("tool", "cursr", "no tool called cursr")):
            self.assertIn(why, self.op(op="create", name="x", description="x", **{field: value})["error"])
            self.assertIn(why, self.op(op="edit", name="qa", **{field: value})["error"])
        self.assertNotIn("x", [r["name"] for r in self.op(op="list")["roles"]])

    def test_the_owner_makes_a_project_from_the_page(self):
        made = crewchat.post_json(self.base + "/api/projects", self.owner, {"op": "create", "name": " Cooking  show ",
                                                                             "pack": "youtube"})
        self.assertEqual(made["name"], "Cooking show")
        found = crewchat.find_chat(made["id"])
        self.assertEqual(crewchat.load_config(found["root"])["pack"], "youtube")
        self.assertIn(made["id"], [p.id for p in self.server.RequestHandlerClass.projects.all()])
        for body, why in (({"op": "create", "name": "Cooking show"}, "already"), ({"op": "create", "name": ""}, "1 to 60"),
                          ({"op": "create", "name": "x", "pack": "nope"}, "no pack called nope"), ({"op": "drop"}, "op must be")):
            self.assertIn(why, self.op_at("/api/projects", **body)["error"])
        self.assertEqual(self.op_at("/api/projects", token=self.token, op="create", name="sneaky")["status"], 403)

    def test_the_owner_removes_a_project_from_the_page(self):
        made = crewchat.post_json(self.base + "/api/projects", self.owner, {"op": "create", "name": "Old idea"})
        root = crewchat.find_chat(made["id"])["root"]
        (root / "messages.jsonl").write_text('{"id": 1}\n')
        projects = self.server.RequestHandlerClass.projects
        first = projects.primary
        for body, why in (({"id": made["id"], "name": "old"}, "type the project's name"),
                          ({"id": "nope", "name": "nope"}, "no project nope"),
                          ({"id": first.id, "name": first.name}, "first project, which cannot be removed")):
            self.assertIn(why, self.op_at("/api/projects", op="remove", **body)["error"])
        self.assertEqual(self.op_at("/api/projects", token=self.token, op="remove", id=made["id"], name="Old idea")["status"], 403)
        self.assertTrue(root.exists())
        gone = crewchat.post_json(self.base + "/api/projects", self.owner, {"op": "remove", "id": made["id"], "name": " old  IDEA "})
        self.assertEqual((gone["removed"], gone["path"], gone["deleted"]), ("Old idea", str(root), False))
        self.assertEqual((root / "messages.jsonl").read_text(), '{"id": 1}\n')  # kept where it was (C5), not moved
        self.assertFalse((root.parent / ".removed").exists())
        self.assertNotIn(made["id"], [p.id for p in projects.all()])
        self.assertIsNone(crewchat.find_chat(made["id"]))
        self.assertIn("Old idea", first.roster.config.get("dismissed_projects"))  # not offered straight back
        again = crewchat.post_json(self.base + "/api/projects", self.owner, {"op": "create", "name": "Old idea"})
        self.assertEqual(again["name"], "Old idea")  # the name is free again (its kept folder keeps its own id)
        self.assertNotEqual(again["id"], made["id"])

    def test_removing_a_project_closes_its_databases_before_its_folder_is_deleted(self):
        # Windows cannot delete a file that is open ("Access is denied"), and a virus scanner may hold one for a
        # moment: the project's SQLite stores are closed first, and each file is tried again.
        made = crewchat.post_json(self.base + "/api/projects", self.owner, {"op": "create", "name": "Locked"})
        project = self.server.RequestHandlerClass.projects.get(made["id"])
        hub = project.hub
        hub.events._conn()
        hub.chat_index._conn()
        maps = hub._maps = mock.Mock()
        seen, real = [], os.unlink

        def unlink(path, *args, **kwargs):
            if crewchat.path_within(path, project.root) and str(path).endswith("config.json"):
                seen.append((hub.events.db, hub.chat_index.db, hub._maps))
                if len(seen) < 3:
                    raise PermissionError(13, "Access is denied")
            return real(path, *args, **kwargs)

        with mock.patch.object(crewchat.os, "unlink", unlink), mock.patch.object(crewchat.sys, "platform", "win32"), \
                mock.patch.object(crewchat.time, "sleep"):
            gone = crewchat.post_json(self.base + "/api/projects", self.owner, {"op": "remove", "id": made["id"],
                                                                                "name": "Locked", "delete": True})
        self.assertEqual(len(seen), 3)
        self.assertEqual(seen[0], (None, None, None))
        maps.close.assert_called_once_with()
        self.assertEqual((gone["deleted"], gone["failed"]), (True, {}))
        # A late reader (the map thread) makes no code map, and a late save no state, where the project was.
        with mock.patch.object(hub, "edits_on", lambda: True):
            self.assertIsNone(hub.code_maps())
        with hub.lock:
            hub._save()
            hub._save_usage()
        self.assertFalse(project.root.exists())

    def test_closed_code_maps_make_no_map_again(self):
        import crewchat_map
        root = Path(tempfile.mkdtemp(prefix="gone-", dir=TMP))
        maps = crewchat_map.Maps(root / "project")
        maps.close()  # its project is being removed; a map thread still holds it
        self.assertIn("could not map", maps.check([root])[str(root)])
        self.assertFalse((root / "project").exists())

    def test_a_workspace_folder_that_cannot_be_deleted_whole_is_named_and_offered_again(self):
        made = crewchat.post_json(self.base + "/api/projects", self.owner, {"op": "create", "name": "Stuck"})
        project = self.server.RequestHandlerClass.projects.get(made["id"])
        root = project.root
        real = os.unlink

        def unlink(path, *args, **kwargs):
            if crewchat.path_within(path, root):
                raise PermissionError(13, "Access is denied")
            return real(path, *args, **kwargs)

        with mock.patch.object(crewchat.os, "unlink", unlink), mock.patch.object(crewchat.sys, "platform", "win32"), \
                mock.patch.object(crewchat.time, "sleep"):
            gone = crewchat.post_json(self.base + "/api/projects", self.owner, {"op": "remove", "id": made["id"],
                                                                                "name": "Stuck", "delete": True})
        self.assertFalse(gone["deleted"])
        self.assertIn("could not be deleted whole, not deleted: ", gone["failed"][str(root)])
        self.assertIn("(the first because: Access is denied)", gone["failed"][str(root)])
        self.assertTrue((root / "config.json").is_file())
        self.assertTrue((root / "crewchat-project.json").is_file())
        self.assertIsNone(self.server.RequestHandlerClass.projects.get(made["id"]))  # removed all the same
        self.assertIn(str(root), crewchat.recent_workspaces(crewchat.home()))  # still a workspace: Open offers it

    def op_at(self, path, token=None, **body):
        try:
            return crewchat.post_json(self.base + path, token or self.owner, body)
        except urllib.error.HTTPError as e:
            return dict(json.loads(e.read()), status=e.code)

    def test_save_a_pack_and_start_another_project_from_it(self):
        self.op(op="edit", name="qa", label="Tester")
        self.assertEqual(self.op(op="save_pack", id="studio", label="Studio")["pack"]["id"], "studio")
        self.assertIn("studio", [p["id"] for p in self.op(op="list")["packs"]])
        self.assertIn("studio", cli("roles", "packs"))

    def test_the_command_line(self):
        self.assertIn("Pack: software", cli("roles"))
        cli("roles", "add", "designer", "--prompt", "You design.", "--label", "Designer", "--picture", "palette",
            "--colour", "#112233", "--tool", "claude")
        row = crewchat.role_table(crewchat.load_config(self.root), crewchat.role_packs())["designer"]
        self.assertEqual((row["picture"], row["colour"], row["tool"]), ("preset:palette", "#112233", "claude"))
        cli("roles", "add", "designer", "--label", "Screen designer")  # a change keeps the instructions
        self.assertIn("Screen designer\n\nYou design.", cli("roles", "show", "designer"))
        path = os.path.join(TMP, "roles.json")
        cli("roles", "export", "designer", "--file", path)
        self.assertEqual(list(json.loads(Path(path).read_text())["roles"]), ["designer"])
        cli("roles", "export", "--file", path)
        self.assertIn("qa", json.loads(Path(path).read_text())["roles"])
        cli("roles", "remove", "designer")
        self.assertIn("Imported", cli("roles", "import", path))
        self.assertIn("designer", cli("roles"))
        cli("roles", "pack", "business")
        self.assertIn("Manager", cli("roles"))


class Activity(Base):
    """What an agent is doing, from its tool calls, in a few safe words."""

    def test_a_tool_call_in_a_few_safe_words(self):
        say = crewchat.tool_summary
        self.assertEqual(say("claude", {"tool_name": "Edit", "tool_input": {"file_path": "/x/app/Login.kt"}}), "Editing Login.kt")
        self.assertEqual(say("claude", {"tool_name": "Read", "tool_input": {"file_path": "/secret/dir/.env"}}), "Reading .env")
        self.assertEqual(say("claude", {"tool_name": "Bash", "tool_input": {"command": "TOKEN=abc npm test --token=xyz"}}),
                         "Running npm test")
        self.assertEqual(say("claude", {"tool_name": "Bash", "tool_input": {"command": "cd app && ./gradlew assemble"}}),
                         "Running gradlew assemble")
        self.assertEqual(say("claude", {"tool_name": "Bash", "tool_input": {"command": "curl -H 'Auth: s3cret' x"}}), "Running curl")
        self.assertEqual(say("claude", {"tool_name": "Grep", "tool_input": {"pattern": "password"}}), "Searching the code")
        self.assertEqual(say("claude", {"tool_name": "mcp__crewchat__hub_send"}), "")
        self.assertEqual(say("cursor", {"file_path": "/a/b/Theme.kt"}), "Editing Theme.kt")
        self.assertEqual(say("cursor", {"command": "pytest -q"}), "Running pytest")
        self.assertEqual(say("claude", {"tool_name": "<img src=x>"}), "")
        # Never an argument: only a known tool's subcommand; quotes and escapes read as the shell does.
        bash = lambda cmd: say("claude", {"tool_name": "Bash", "tool_input": {"command": cmd}})  # noqa: E731
        self.assertEqual(bash("echo hunter2 | tee x"), "Running echo")
        self.assertEqual(bash("cat credentials"), "Running cat")
        self.assertEqual(bash('PGPASSWORD="a; secretword more" psql -h db'), "Running psql")
        self.assertEqual(bash("X=a\\ secretpart npm test"), "Running npm test")
        self.assertEqual(bash("ghp_abcdefghijklmnopqrstuvwxyz0123456789"), "")
        self.assertEqual(bash("git commit -m 'a secret message'"), "Running git commit")
        self.assertEqual(bash("echo 'unbalanced"), "")
        for cmd, said in (("cd x\nnpm test", "Running npm test"), ("sudo npm install", "Running npm install"),
                          ("env X=1 npm test", "Running npm test"), ("timeout 60 npm test", "Running npm test"),
                          ("nohup npm start &", "Running npm start"), ("pytest secretdir", "Running pytest"),
                          ("npx some-internal-pkg", "Running npx"), ("make deploy-prod-db", "Running make")):
            self.assertEqual(bash(cmd), said, cmd)
        self.assertEqual(say("claude", {"tool_name": "Edit", "tool_input": {"file_path": "C:\\Users\\me\\Login.kt"}}),
                         "Editing Login.kt")
        self.assertEqual(say("claude", {"tool_name": "Read", "tool_input": {"file_path": ["a", "b"]}}), "Reading")
        self.assertEqual(say("cursor", {"tool_name": "hub_send"}), "")
        # Gemini CLI's and Codex's own tool names and arguments.
        self.assertEqual(say("gemini", {"tool_name": "replace", "tool_input": {"file_path": "/x/Login.kt"}}), "Editing Login.kt")
        self.assertEqual(say("gemini", {"tool_name": "read_file", "tool_input": {"absolute_path": "/x/.env"}}), "Reading .env")
        self.assertEqual(say("gemini", {"tool_name": "run_shell_command", "tool_input": {"command": "npm test --x=1"}}),
                         "Running npm test")
        self.assertEqual(say("qwen", {"tool_name": "edit", "tool_input": {"file_path": "/x/App.kt"}}), "Editing App.kt")
        self.assertEqual(say("opencode", {"tool_name": "read", "tool_input": {"filePath": "/x/main.py"}}), "Reading main.py")
        self.assertEqual(say("opencode", {"tool_name": "bash", "tool_input": {"command": "npm test"}}), "Running npm test")
        self.assertEqual(say("codex", {"tool_name": "shell", "tool_input": {"command": ["bash", "-lc", "pytest -q x"]}}),
                         "Running pytest")
        self.assertEqual(say("codex", {"tool_name": "exec_command", "tool_input": {"command": ["git", "status"]}}),
                         "Running git status")
        self.assertEqual(say("cursor", {"tool_name": "browser_navigate", "command": "npx @playwright/mcp"}), "Using browser_navigate")
        for leaked in ("abc", "xyz", "s3cret", "password", "/x/app", "/secret"):
            for out in (say("claude", {"tool_name": "Bash", "tool_input": {"command": "TOKEN=abc npm test --token=xyz"}}),
                        say("claude", {"tool_name": "Bash", "tool_input": {"command": "curl -H 'Auth: s3cret' x"}})):
                self.assertNotIn(leaked, out)

    def test_the_page_shows_it_while_fresh(self):
        session, name = self.agent()
        key = "k" * 16
        session.text("hub_link", key=key)
        hook = lambda **body: crewchat.post_json(self.base + "/api/hook", self.token, dict({"key": key}, **body))  # noqa: E731
        hook(event="tool", doing="Editing Login.kt")
        row = lambda: next(r for r in self.hub.rows() if r["agent"] == name)  # noqa: E731
        self.assertEqual(row()["doing"], "Editing Login.kt")
        with self.hub.lock:
            self.hub.agents[name]["doing_at"] -= crewchat.DOING_FRESH + 1
        self.assertEqual(row()["doing"], "")  # old news
        hook(event="tool", doing="Running pytest")
        hook(event="stop", wait=0)  # a turn ended: what it last did is old news too
        self.assertEqual(row()["doing"], "")
        config = crewchat.load_config()
        config["activity"] = "off"
        crewchat.save_config(config)
        self.hub.roster.refresh(force=True)
        try:
            hook(event="tool", doing="Editing Secret.kt")
            self.assertEqual(row()["doing"], "")  # the owner keeps it off the page
        finally:
            config.pop("activity")
            crewchat.save_config(config)
            self.hub.roster.refresh(force=True)


class NeedsYou(Base):
    """What waits on the owner: blocked tasks, reviews, tasks given back, unread messages."""

    def add(self, title, **kw):
        return crewchat.post_json(self.base + "/api/task", self.owner, dict({"op": "add", "title": title}, **kw))["id"]

    def setUp(self):
        # An agent's question to the owner stays until answered: one test's questions are set aside for the next.
        for aid in [aid for aid, m in list(self.hub.open_asks.items()) if m["to"] == crewchat.OWNER]:
            self.hub.dismiss_question(aid)

    def test_what_needs_the_owner_and_in_what_order(self):
        a, a_name = self.agent()
        b, b_name = self.agent()
        api = self.add("Design the API")
        client = self.add("Build the client", depends=api)
        self.add("Ship it", depends=client)
        review = self.add("Write the changelog")
        back = self.add("Clean the logs")
        a.text("hub_task", action="take", id=api)
        a.text("hub_task", action="update", id=api, status="blocked", note="Which auth: tokens or cookies?")
        b.text("hub_task", action="take", id=review)
        b.text("hub_task", action="update", id=review, status="review", note="Ready for a look")
        b.text("hub_task", action="take", id=back)
        b.text("hub_task", action="update", id=back, status="todo", note="No time, sorry")
        a.text("hub_send", to="Owner", text="A question for you", ask=True)
        needs = self.hub.needs_you()
        kinds = [(n["kind"], n.get("task") or n.get("text")) for n in needs]
        self.assertEqual(kinds[:4], [("blocked", api), ("review", review), ("message", "A question for you"),
                                     ("given_back", back)])
        blocked = needs[0]
        self.assertEqual((blocked["agent"], blocked["text"], blocked["blocking"]), (a_name, "Which auth: tokens or cookies?", 2))
        self.assertEqual(needs[3]["text"], "No time, sorry")
        # Once handled, an item leaves the list.
        a.text("hub_task", action="update", id=api, status="in_progress", note="Going with tokens")
        crewchat.post_json(self.base + "/api/task", self.owner, {"op": "status", "task": review, "status": "done"})
        msg = needs[2]
        self.assertEqual(crewchat.post_json(self.base + "/api/read", self.owner, {"seq": msg["seq"]})["read"], msg["seq"])
        self.assertIn("message", [n["kind"] for n in self.hub.needs_you()])  # read is not answered
        crewchat.post_json(self.base + "/api/send", self.owner, {"to": a_name, "text": "@%s yes" % a_name})
        left = [n["kind"] for n in self.hub.needs_you()]
        self.assertNotIn("blocked", left)
        self.assertNotIn("review", left)
        self.assertNotIn("message", left)
        self.assertIn("given_back", left)
        b.text("hub_task", action="take", id=back)
        self.assertEqual(self.hub.needs_you(), [])

    def test_an_agents_ordinary_reply_is_not_an_item(self):
        # Windows round-2 test (12 Nov 2026): every reply an agent sent the owner became a "Message from" card in
        # Needs you and pushed the real decisions down. Only what asks the owner something stays.
        a, a_name = self.agent()
        b, b_name = self.agent()
        a.text("hub_send", to="Owner", text="Done: the button is green now.")
        a.text("hub_send", to="all", text="Pushed the branch.")
        self.hub.send(b_name, crewchat.OWNER, "All tests pass.", kind="answer")
        self.assertEqual([n for n in self.hub.needs_you() if n["kind"] == "message"], [])
        a.text("hub_send", to="Owner", text="May I drop the old table?", ask=True)
        items = [n for n in self.hub.needs_you() if n["kind"] == "message"]
        self.assertEqual([(n["agent"], n["text"]) for n in items], [(a_name, "May I drop the old table?")])
        self.assertEqual(crewchat.NEED_KINDS["message"][1], "Question for you")
        # A plain message to the owner still counts on the tab while the page is away, as it is no item any more.
        self.assertIn('&& !(m.to === "Owner" && (m.kind === "ask" || m.ask))).length;', crewchat.CHAT_PAGE)

    def test_two_questions_at_the_same_moment_are_newest_first_too(self):
        """On Windows two messages can carry the same time (the clock's step): the later one is still first."""
        a, a_name = self.agent()
        b, b_name = self.agent()
        a.text("hub_send", to="Owner", text="Tea or coffee?", ask=True)
        b.text("hub_send", to="Owner", text="Now or later?", ask=True)
        with self.hub.lock:
            first, second = [m for m in self.hub.messages if m["text"] in ("Tea or coffee?", "Now or later?")]
            second["ts"] = first["ts"]
            self.hub.version += 1
        items = [n for n in self.hub.needs_you() if n["kind"] == "message"]
        self.assertEqual([n["text"] for n in items], ["Now or later?", "Tea or coffee?"])

    def test_a_question_for_the_owner_stays_until_answered_or_dismissed(self):
        # Mac retest of #161 (12 Nov 2026): "Red or blue?" left Needs you as soon as the page opened on the chat,
        # which scrolled it into view and marked it read, though the owner never answered it.
        a, a_name = self.agent()
        b, b_name = self.agent()
        a.text("hub_send", to="Owner", text="Red or blue?", ask=True)
        b.text("hub_send", to="Owner", text="Ship on Friday?", ask=True)
        items = [n for n in self.hub.needs_you() if n["kind"] == "message"]
        self.assertEqual([n["text"] for n in items], ["Ship on Friday?", "Red or blue?"])  # newest first
        crewchat.post_json(self.base + "/api/read", self.owner, {"seq": items[0]["seq"]})
        self.assertEqual([n["text"] for n in self.hub.needs_you() if n["kind"] == "message"],
                         ["Ship on Friday?", "Red or blue?"])  # seen, still unanswered
        # Answered: the owner writes to the agent that asked (from the card, or in the chat with @name).
        crewchat.post_json(self.base + "/api/send", self.owner, {"to": "all", "text": "@%s blue" % a_name})
        self.assertEqual([n["text"] for n in self.hub.needs_you() if n["kind"] == "message"], ["Ship on Friday?"])
        # Dismissed from its card: gone without an answer, and stays gone after a restart (kept in the chat).
        key = next(n["key"] for n in self.hub.needs_you() if n["kind"] == "message")
        with self.assertRaises(urllib.error.HTTPError):
            crewchat.post_json(self.base + "/api/needs", self.owner, {"key": key, "answer": "nope"})
        self.assertEqual(crewchat.post_json(self.base + "/api/needs", self.owner, {"key": key, "answer": "dismiss"}),
                         {"ok": True})
        self.assertEqual([n for n in self.hub.needs_you() if n["kind"] == "message"], [])
        again = crewchat.Hub(crewchat.Roster(self.hub.home))
        try:
            self.assertEqual([n for n in again.needs_you() if n["kind"] == "message"], [])
        finally:
            again.close_stores()
        with self.assertRaises(urllib.error.HTTPError):  # nothing left to dismiss
            crewchat.post_json(self.base + "/api/needs", self.owner, {"key": key, "answer": "dismiss"})
        self.assertIn('drop = el("button", "ghost", "Dismiss")', crewchat.CHAT_PAGE)

    def test_dismissing_one_question_leaves_the_agents_others(self):
        # Mac retest of #163 (12 Nov 2026): Dismiss on Bodhi's newest question took Bodhi's earlier ones with it.
        a, a_name = self.agent()
        for text in ("Q1?", "Q2?", "Q3?"):
            a.text("hub_send", to="Owner", text=text, ask=True)
        texts = lambda: sorted(n["text"] for n in self.hub.needs_you() if n["kind"] == "message")  # noqa: E731
        key = next(n["key"] for n in self.hub.needs_you() if n["kind"] == "message" and n["text"] == "Q2?")
        crewchat.post_json(self.base + "/api/needs", self.owner, {"key": key, "answer": "dismiss"})
        self.assertEqual(texts(), ["Q1?", "Q3?"])
        # A reply to the agent itself still answers what it asked (unchanged).
        crewchat.post_json(self.base + "/api/send", self.owner, {"to": a_name, "text": "@%s yes to all" % a_name})
        self.assertEqual(texts(), [])

    def test_every_need_has_a_key_and_a_known_kind(self):
        # R8: one item shape, keyed on the server, from one table of kinds the page and push share.
        a, a_name = self.agent()
        b, b_name = self.agent()
        blocked, review, back = self.add("Design the API"), self.add("Write the changelog"), self.add("Clean the logs")
        a.text("hub_task", action="take", id=blocked)
        a.text("hub_task", action="update", id=blocked, status="blocked", note="Tokens or cookies?")
        b.text("hub_task", action="take", id=review)
        b.text("hub_task", action="update", id=review, status="review", note="Ready")
        b.text("hub_task", action="take", id=back)
        b.text("hub_task", action="update", id=back, status="todo", note="No time")
        a.text("hub_send", to="Owner", text="A question for you", ask=True)
        waiters = []
        for kind, fields in (("permission", {"tool": "Bash"}), ("browser", {"ask": "change", "host": "a.com", "task": ""}),
                             ("browser", {"ask": "site", "host": "b.com", "task": ""})):
            def run(kind=kind, fields=fields):
                with self.hub.lock:
                    self.hub.asks.wait(kind, a_name, "t", time.time() + 30, **fields)
            waiters.append(threading.Thread(target=run))
            waiters[-1].start()
        eventually(lambda: len(self.hub.asks.waiting()) == 3, what="three asks")
        needs = self.hub.needs_you()
        self.assertEqual({n["kind"] for n in needs}, {"blocked", "review", "given_back", "message", "permission", "browser"})
        for n in needs:
            self.assertEqual(n["key"], n["kind"] + ":" + n["ref"])
            self.assertIn(n["kind"], crewchat.NEED_KINDS)
            self.assertEqual(n["answer"], crewchat.NEED_KINDS[n["kind"]][3])
        by_key = {n["key"]: n for n in needs}
        self.assertEqual(by_key["review:" + review]["ref"], review)
        self.assertEqual(sorted(n["choices"] for n in needs if n["kind"] == "browser"),
                         [["allow_site", "allow_task", "deny"], ["allow_task", "trust_site", "deny"]])
        self.assertEqual(next(n for n in needs if n["kind"] == "permission")["choices"], ["allow", "deny"])
        self.assertNotIn("choices", by_key["review:" + review])
        self.assertEqual([crewchat.NEED_KINDS[n["kind"]][0] for n in needs],
                         sorted(crewchat.NEED_KINDS[n["kind"]][0] for n in needs))  # in the table's order
        with self.hub.lock:
            self.hub.asks.drop(a_name)
        for t in waiters:
            t.join(5)
        for t in (blocked, review, back):
            crewchat.post_json(self.base + "/api/task", self.owner, {"op": "status", "task": t, "status": "done"})
        crewchat.post_json(self.base + "/api/read", self.owner, {"seq": by_key[next(k for k in by_key if k.startswith("message:"))]["seq"]})

    def test_boot_gives_the_kinds_and_the_page_keeps_none_of_its_own(self):
        kinds = self.hub.boot()["need_kinds"]
        self.assertEqual(set(kinds), set(crewchat.NEED_KINDS))
        self.assertEqual(kinds["review"], {"order": 1, "label": "In review", "colour": "--warn", "answer": "reply"})
        self.assertNotIn("const NEED_LOOK = {", crewchat.CHAT_PAGE)
        self.assertNotIn('n.kind + ":"', crewchat.CHAT_PAGE)
        self.assertFalse(hasattr(crewchat, "NEED_LABELS"))

    def test_a_reply_with_re_answers_the_review(self):
        # R8 (the 1.0 bug "a reply does not answer the review item"): the reply names the task in a field.
        a, a_name = self.agent()
        mid = self.add("Polish the docs")
        a.text("hub_task", action="take", id=mid)
        a.text("hub_task", action="update", id=mid, status="review", note="Have a look")
        self.assertIn("review:" + mid, [n["key"] for n in self.hub.needs_you()])
        self.hub.send(crewchat.OWNER, a_name, "looks fine, go ahead", re=mid)
        self.assertNotIn("review:" + mid, [n["key"] for n in self.hub.needs_you()])
        crewchat.post_json(self.base + "/api/task", self.owner, {"op": "status", "task": mid, "status": "done"})

    def test_a_reply_by_its_prefix_still_answers_and_carries_re(self):
        a, a_name = self.agent()
        mid = self.add("Pick a colour")
        a.text("hub_task", action="take", id=mid)
        a.text("hub_task", action="update", id=mid, status="blocked", note="Blue or green?")
        sent = crewchat.post_json(self.base + "/api/send", self.owner, {"to": a_name, "text": "Re #%s: blue" % mid})
        self.assertEqual(self.hub.by_id[sent["id"]].get("re"), mid)
        self.assertNotIn("blocked:" + mid, [n["key"] for n in self.hub.needs_you()])
        sent = crewchat.post_json(self.base + "/api/send", self.owner, {"to": a_name, "text": "either", "re": mid})
        self.assertEqual(self.hub.by_id[sent["id"]].get("re"), mid)  # the task panel says it in the field
        crewchat.post_json(self.base + "/api/task", self.owner, {"op": "status", "task": mid, "status": "done"})

    def test_re_to_another_agent_or_no_task_does_not_answer(self):
        a, a_name = self.agent()
        b, b_name = self.agent()
        mid = self.add("Name the release")
        a.text("hub_task", action="take", id=mid)
        a.text("hub_task", action="update", id=mid, status="review", note="Ready")
        self.hub.send(crewchat.OWNER, b_name, "fine", re=mid)  # to another agent: not an answer to this one
        self.assertIn("review:" + mid, [n["key"] for n in self.hub.needs_you()])
        msg = self.hub.send(crewchat.OWNER, a_name, "about something else", re="Z999")
        self.assertNotIn("re", msg)  # names no task: not kept
        self.assertIn("re", crewchat.EXTRA_FIELDS)  # travels between machines
        crewchat.post_json(self.base + "/api/task", self.owner, {"op": "status", "task": mid, "status": "done"})

    def test_needs_route_replies_to_a_review_and_marks_it_done(self):
        a, a_name = self.agent()
        mid = self.add("Tidy the README")
        a.text("hub_task", action="take", id=mid)
        a.text("hub_task", action="update", id=mid, status="review", note="Ready")
        key = "review:" + mid
        post = lambda body: crewchat.post_json(self.base + "/api/needs", self.owner, body)  # noqa: E731
        with self.assertRaises(urllib.error.HTTPError) as e:
            post({"key": key, "answer": "reply", "text": "  "})  # nothing written
        self.assertEqual(e.exception.code, 400)
        sent = post({"key": key, "answer": "reply", "text": "one more test, please"})
        msg = self.hub.by_id[sent["id"]]
        # With its Re # too, so a linked machine still on 1.1 (no `re`) also sees the answer (final review).
        self.assertEqual((msg["to"], msg.get("re"), msg["text"]), (a_name, mid, "Re #%s: one more test, please" % mid))
        # The task panel answers through the same route (R8: one route for every answer).
        self.assertIn('postAction("/api/needs", { key: n.key, answer: "reply", text: answer.value.trim() }', crewchat.CHAT_PAGE)
        self.assertNotIn('text: "Re #" + r.id', crewchat.CHAT_PAGE)
        self.assertNotIn(key, [n["key"] for n in self.hub.needs_you()])  # answered: the agent's turn
        with self.assertRaises(urllib.error.HTTPError) as e:
            post({"key": key, "answer": "reply", "text": "again"})  # not waiting any more
        self.assertIn("not waiting any more", crewchat.http_error_reason(e.exception))
        a.text("hub_task", action="update", id=mid, status="review", note="Test added")
        post({"key": key, "answer": "done"})
        self.assertEqual(self.hub.progress[mid]["status"], "done")

    def test_needs_route_refuses_what_has_nothing_to_answer(self):
        a, a_name = self.agent()
        a.text("hub_send", to="Owner", text="hello?", ask=True)
        item = next(n for n in self.hub.needs_you() if n["kind"] == "message")
        for body in ({"key": item["key"], "answer": "reply"}, {"key": "nope", "answer": "x"}, {"key": "permission:feedfeedfeed", "answer": "allow"},
                     {"key": 5, "answer": "allow"}, {"key": "blocked:" + item["key"], "answer": "done"}):
            with self.assertRaises(urllib.error.HTTPError, msg=body) as e:
                crewchat.post_json(self.base + "/api/needs", self.owner, body)
            self.assertEqual(e.exception.code, 400, body)
        crewchat.post_json(self.base + "/api/read", self.owner, {"seq": item["seq"]})

    def test_an_answered_question_waits_on_the_agent_not_the_owner(self):
        a, a_name = self.agent()
        mid = self.add("Pick the icons")
        a.text("hub_task", action="take", id=mid)
        a.text("hub_task", action="update", id=mid, status="blocked", note="Outline or filled?")
        self.assertIn(mid, [n.get("task") for n in self.hub.needs_you()])
        self.hub.send(crewchat.OWNER, a_name, "Re #%s: Outline" % mid)  # as the answer box in Needs you sends it
        self.assertNotIn(mid, [n.get("task") for n in self.hub.needs_you()])  # the agent's turn now
        self.hub.send(crewchat.OWNER, a_name, "Re #%s1: about another task" % mid)
        a.text("hub_task", action="update", id=mid, status="blocked", note="Which size?")  # asks again: it needs the owner again
        self.assertIn(mid, [n.get("task") for n in self.hub.needs_you()])
        b, b_name = self.agent()
        self.hub.send(crewchat.OWNER, b_name, "Re #%s: Small" % mid)  # to another agent: not an answer to this one
        self.hub.send(crewchat.OWNER, "all", "Re #%s: Small" % mid)
        self.assertIn(mid, [n.get("task") for n in self.hub.needs_you()])
        review = self.add("Polish the icons")
        a.text("hub_task", action="take", id=review)
        a.text("hub_task", action="update", id=review, status="review", note="Have a look")
        self.hub.send(crewchat.OWNER, a_name, "Re #%s: one more test, then fine" % review)
        self.assertNotIn(review, [n.get("task") for n in self.hub.needs_you()])  # answered: the agent's turn, as asked
        a.text("hub_task", action="update", id=review, status="review", note="Test added")  # back for review: owner again
        self.assertIn(review, [n.get("task") for n in self.hub.needs_you()])
        for t in (mid, review):
            crewchat.post_json(self.base + "/api/task", self.owner, {"op": "status", "task": t, "status": "done"})

    def test_a_note_is_credited_to_the_agent_that_wrote_it(self):
        a, a_name = self.agent()
        b, b_name = self.agent()
        mid = self.add("Migrate the database")
        a.text("hub_task", action="take", id=mid)
        b.text("hub_task", action="update", id=mid, status="blocked", note="OK to drop the old tables?")  # not b's task
        item = next(n for n in self.hub.needs_you() if n.get("task") == mid)
        self.assertEqual((item["agent"], item["holder"], item["text"]), (b_name, a_name, "OK to drop the old tables?"))
        a.text("hub_task", action="update", id=mid, status="in_progress", note="carrying on")

    def test_what_the_owner_blocked_is_not_listed_back(self):
        mid = self.add("Wait for the designer")
        crewchat.post_json(self.base + "/api/task", self.owner, {"op": "status", "task": mid, "status": "blocked"})
        self.assertNotIn(mid, [n.get("task") for n in self.hub.needs_you()])
        crewchat.post_json(self.base + "/api/task", self.owner, {"op": "remove", "task": mid})

    def test_the_page_hears_which_tasks_were_taken_off_the_sheet(self):
        mid = self.add("Write the release notes")
        self.assertNotIn(mid, self.hub.poll(-1, -1, 0)["removed"])
        crewchat.post_json(self.base + "/api/task", self.owner, {"op": "remove", "task": mid})
        self.assertIn(mid, self.hub.poll(-1, -1, 0)["removed"])  # its card in the chat says so, not "Open"

    def test_a_long_chain_of_tasks_is_counted_without_recursion(self):
        first = prev = self.add("Step 0")
        for i in range(1, 1200):
            prev = self.add("Step %d" % i, depends=prev)
        a, _ = self.agent()
        a.text("hub_task", action="take", id=first)
        a.text("hub_task", action="update", id=first, status="blocked", note="stuck")
        item = next(n for n in self.hub.needs_you() if n.get("task") == first)
        self.assertEqual(item["blocking"], 1199)
        a.text("hub_task", action="update", id=first, status="done", note="unstuck\nDecisions: none\nLeft over: none")

    def test_the_poll_has_every_other_projects_needs_each_with_its_project(self):
        # W16: Needs you across every project. The poll brings the other projects' open items, each labelled with its
        # project, and each is answered in place through that project (?chat=).
        made = crewchat.post_json(self.base + "/api/projects", self.owner, {"op": "create", "name": "Elsewhere"})
        hub = self.server.RequestHandlerClass.projects.get(made["id"]).hub
        code = crewchat.post_json(self.base + "/api/invite?chat=" + made["id"], self.owner, {})["code"]
        token = crewchat.post_json(self.base + "/api/join", None, {"code": code, "place": "mac"})["token"]
        session = Session(self.base, token)
        session.call("hub_link", key=hub.new_start())
        session.text("hub_send", to="Owner", text="Over here, in Elsewhere", ask=True)
        opener = self.owner_browser()[0]
        poll = lambda chat="": json.loads(self.raw("/api/poll?after=-1&v=-1&wait=0" + (  # noqa: E731
            "&chat=" + chat if chat else ""), opener=opener)[1])
        here = poll()
        item = next(n for n in here["needs_elsewhere"] if "Over here" in n.get("text", ""))
        self.assertEqual((item["chat"], item["project"]), (made["id"], "Elsewhere"))
        self.assertNotIn("Over here, in Elsewhere", json.dumps(here["needs"]))  # this project's own list stays its own
        self.assertEqual(len([n for n in here["needs_elsewhere"] if n["chat"] == made["id"]]),
                         next(p for p in here["projects"] if p["id"] == made["id"])["needs"])  # the same as its count
        there = poll(made["id"])
        self.assertTrue(any("Over here" in n.get("text", "") for n in there["needs"]))
        self.assertFalse(any(n["chat"] == made["id"] for n in there["needs_elsewhere"]))
        # Dismissed from the All projects list: that project's question is set aside, and the item leaves it.
        crewchat.post_json(self.base + "/api/needs?chat=" + made["id"], self.owner, {"key": item["key"], "answer": "dismiss"})
        self.assertFalse(any("Over here" in n.get("text", "") for n in poll()["needs_elsewhere"]))

    def test_an_open_page_offers_a_reload_when_the_server_serves_another_build(self):
        # Mac retest (11 Nov): after `crewchat restart` onto a newer build the open page kept its old Machines panel.
        poll = json.loads(self.raw("/api/poll?after=-1&v=-1&wait=0", opener=self.owner_browser()[0])[1])
        self.assertEqual(poll["page"], crewchat.PAGE_TAG)
        page = crewchat.CHAT_PAGE
        self.assertIn('const PAGE_BUILD = "%s";' % crewchat.PAGE_TAG, page)  # the page knows its own build
        self.assertNotIn("__PAGE_TAG__", page)
        self.assertEqual(crewchat.PAGE_TAG, crewchat.sha(page.replace(crewchat.PAGE_TAG, "__PAGE_TAG__", 1))[:12])
        self.assertIn("checkBuild(data.page);", page)  # every poll compares
        self.assertIn('id="reloaded"', page)
        self.assertIn('$("reloaded-run").addEventListener("click", () => location.reload());', page)

    def test_the_poll_and_the_tabs_say_so(self):
        a, _ = self.agent()
        a.text("hub_send", to="Owner", text="Look at this", ask=True)
        poll = json.loads(self.raw("/api/poll?after=-1&v=-1&wait=0", opener=self.owner_browser()[0])[1])
        self.assertTrue(any(n["kind"] == "message" for n in poll["needs"]))
        self.assertGreaterEqual(next(p for p in poll["projects"] if p["id"] == poll["chat"])["needs"], 1)
        self.assertIn("seq is", self.op_at("/api/read", seq="x")["error"])
        self.assertEqual(self.op_at("/api/read", token=self.token, seq=1)["status"], 403)
        crewchat.post_json(self.base + "/api/read", self.owner, {"seq": 10 ** 9})  # never past the last message
        self.assertEqual(self.hub.owner_cursor, self.hub.next_seq - 1)

    def op_at(self, path, token=None, **body):
        try:
            return crewchat.post_json(self.base + path, token or self.owner, body)
        except urllib.error.HTTPError as e:
            return dict(json.loads(e.read()), status=e.code)


class RoleHierarchy(Base):
    """Role powers (H1) and a per-project tree of roles (H2): custom roles can lead, and work and reports follow the
    tree."""

    def setUp(self):
        self.root = crewchat.home()
        self.saved = crewchat.load_config(self.root)

    def tearDown(self):
        for name in list(self.hub.agents):
            if self.hub.agents[name].get("role"):
                self.role(name, "none")
        crewchat.save_config(self.saved, self.root)
        self.hub.roster.refresh(force=True)
        with self.hub.lock:
            self.hub.tree, self.hub.tree_ts = {}, 0.0

    def op(self, **body):
        try:
            return crewchat.post_json(self.base + "/api/roles", self.owner, body)
        except urllib.error.HTTPError as e:
            return dict(json.loads(e.read()), status=e.code)

    def role(self, agent, role):
        return crewchat.post_json(self.base + "/api/role", self.owner, {"agent": agent, "role": role})

    def test_a_project_from_before_keeps_the_lead_and_advisor_powers(self):
        table = crewchat.role_table({"roles": {"lead": {"description": "You lead."}}}, crewchat.role_packs())
        self.assertEqual(table["lead"]["powers"], ["assign", "areas", "notes", "code", "crew"])
        self.assertEqual(table["advisor"]["powers"], ["code", "advise"])
        self.assertEqual((table["developer"]["powers"], table["developer"]["reports_to"]), ([], ""))
        self.assertEqual(crewchat.top_role(table), "lead")
        # An owner may take a power away: an empty list is kept as empty, not read as "the defaults".
        table = crewchat.role_table({"roles": {"lead": {"description": "x", "powers": []}}}, crewchat.role_packs())
        self.assertEqual((table["lead"]["powers"], crewchat.top_role(table)), ([], ""))
        self.assertEqual(crewchat.clean_powers("assign, fly,notes"), ["assign", "notes"])

    def test_a_custom_role_with_the_assign_power_hands_out_work(self):
        made = self.op(op="create", name="tech-lead", label="Tech lead", description="You run the tech side.",
                       powers=["assign", "code"], reports_to="lead")["role"]
        self.assertEqual((made["powers"], made["reports_to"]), (["assign", "code"], "lead"))
        self.assertIn("powers", next(r for r in self.hub.boot()["roles"] if r["name"] == "tech-lead"))
        (tl, tl_name), (dev, dev_name) = self.agent(), self.agent()
        self.role(tl_name, "tech-lead")
        self.role(dev_name, "developer")
        self.assertEqual(self.hub.tool_kind(tl.sid), "lead")  # hub_code comes with the code power
        # The developer works under the top lead: a sub-lead may not take it, until it works under the tech lead.
        self.assertIn("does not work under you", tl.text("hub_task", action="assign", to=dev_name, title="x"))
        self.op(op="edit", name="developer", reports_to="tech-lead")
        out = tl.text("hub_task", action="assign", to=dev_name, title="Write the parser")
        self.assertEqual(self.hub.taken[re.search(r"task #(\w+)", out).group(1)], dev_name)
        self.op(op="edit", name="tech-lead", powers=[])
        self.assertIn("only the lead assigns", tl.text("hub_task", action="assign", to=dev_name, title="x"))

    def test_work_goes_down_the_tree_and_reports_come_up(self):
        for name, label, powers, up in (("frontend-lead", "Frontend lead", ["assign"], "lead"),
                                        ("frontend-dev", "Frontend developer", [], "frontend-lead"),
                                        ("backend-dev", "Backend developer", [], "")):
            self.assertNotIn("error", self.op(op="create", name=name, label=label, description="You work.",
                                              powers=powers, reports_to=up))
        (lead, lead_name), (fl, fl_name), (fd, fd_name), (bd, bd_name) = [self.agent() for _ in range(4)]
        for agent, role in ((lead_name, "lead"), (fl_name, "frontend-lead"), (fd_name, "frontend-dev"),
                            (bd_name, "backend-dev")):
            self.role(agent, role)
        self.assertIn("You report to the Frontend lead", fd.text("hub_inbox"))
        self.assertIn("You report to the Frontend lead", fd.text("hub_role"))
        self.assertIn("Working under you: Frontend developer.", fl.text("hub_inbox"))
        self.assertIn("%s (you) [frontend-dev]" % fd_name, fd.text("hub_agents"))
        self.assertIn("reports to %s" % fl_name, fd.text("hub_agents"))
        # A sub-lead gives work in its branch only; the top lead to anyone.
        refused = fl.text("hub_task", action="assign", to=bd_name, title="Fix the API")
        self.assertIn("%s does not work under you" % bd_name, refused)
        self.assertIn("ask %s" % lead_name, refused)
        out = fl.text("hub_task", action="assign", to=fd_name, title="Build the form")
        piece = re.search(r"task #(\w+)", out).group(1)
        lead.text("hub_task", action="assign", to=bd_name, title="Fix the API")
        # The developer's update goes to its own lead, not the top lead.
        fl.text("hub_inbox")
        lead.text("hub_inbox")
        fd.call("hub_task", action="update", id=piece, status="review", note="Form ready")
        self.assertIn("[UPDATE on #%s: ready for review] Form ready" % piece, fl.text("hub_inbox"))
        self.assertNotIn("Form ready", lead.text("hub_inbox"))
        # With no frontend lead in the chat, its developer reports to the top lead.
        self.role(fl_name, "none")
        fd.call("hub_task", action="update", id=piece, status="in_progress", note="Back on it")
        self.assertIn("Back on it", lead.text("hub_inbox"))

    def test_a_loop_a_missing_role_or_a_second_top_lead_is_refused(self):
        self.assertIn("one top lead", self.op(op="create", name="boss", description="x", powers=["assign"])["error"])
        self.assertIn("no role called ghost", self.op(op="create", name="a", description="x", reports_to="ghost")["error"])
        self.op(op="create", name="a", description="x", reports_to="lead")
        self.op(op="create", name="b", description="x", reports_to="a")
        self.assertIn("makes a loop", self.op(op="edit", name="a", reports_to="b")["error"])
        self.assertEqual(self.op(op="list")["roles"][-2]["reports_to"], "lead")  # nothing was saved
        lines = crewchat.role_tree_lines(self.hub.roster.role_rows)
        self.assertEqual(lines[0], "Lead (lead) [assign, areas, notes, code, crew]")
        self.assertIn("    B (b)", lines)
        # Deleting a role moves the roles under it up to the one it reported to.
        self.assertEqual(self.op(op="delete", name="a"), {"deleted": "a", "moved": ["b"]})
        self.assertEqual(self.hub.roster.role_rows["b"]["reports_to"], "lead")

    def test_export_and_import_carry_powers_and_the_tree(self):
        self.op(op="create", name="ux-lead", description="x", powers="assign", reports_to="lead")
        self.op(op="create", name="ux", description="y", reports_to="ux-lead")
        out = self.op(op="export", names=["ux"])["roles"]["ux"]
        self.assertEqual((out["powers"], out["reports_to"]), ([], "ux-lead"))
        self.op(op="delete", name="ux")
        self.op(op="delete", name="ux-lead")
        self.op(op="import", roles={"ux": out})  # its lead is not here: it works under the top lead
        self.assertEqual(self.hub.roster.role_rows["ux"]["reports_to"], "")

    def test_the_tree_travels_to_linked_machines_and_the_newest_wins(self):
        # H4: a change to roles here goes out as a plan message with the tree.
        self.op(op="create", name="ux-lead", description="x", powers=["assign"], reports_to="lead")
        sent = next(m for m in reversed(self.hub.history(20)) if m.get("tree"))
        self.assertEqual(json.loads(sent["tree"])["ux-lead"], {"powers": ["assign"], "reports_to": "lead"})
        self.op(op="edit", name="ux-lead", label="UX lead")  # the tree is the same: nothing more is sent
        self.assertFalse([m for m in self.hub.history(50) if m.get("tree") and m["seq"] > sent["seq"]])
        # Another machine's newer tree is taken in: this machine's roles follow it, and a role only it has counts here.
        other = {"developer": {"powers": [], "reports_to": "ux-lead"},
                 "ops-lead": {"powers": ["assign", "areas"], "reports_to": "lead"}}
        self.hub.ingest({"id": "zz1", "ts": time.time() + 1, "from": "Owner", "to": "all", "text": "", "kind": "plan",
                         "tree": json.dumps(other)})
        self.assertEqual(self.hub.roster.role_rows["developer"]["reports_to"], "ux-lead")
        self.assertEqual(crewchat.load_config(self.root)["roles"]["developer"]["reports_to"], "ux-lead")
        with self.hub.lock:
            self.assertEqual(self.hub._powers("ops-lead"), {"assign", "areas"})
            self.assertEqual(crewchat.role_chain(self.hub._table(), "ops-lead"), ["lead"])
        # An older one, arriving late, does not undo it.
        self.hub.ingest({"id": "zz2", "ts": 1.0, "from": "Owner", "to": "all", "text": "", "kind": "plan",
                         "tree": json.dumps({"developer": {"powers": [], "reports_to": ""}})})
        self.assertEqual(self.hub.roster.role_rows["developer"]["reports_to"], "ux-lead")

    def test_the_command_line_sets_powers_and_reports_to_and_shows_the_tree(self):
        with mock.patch.object(crewchat, "local_url", lambda config=None: "http://127.0.0.1:9"):  # nothing listens
            cli("roles", "add", "qa-lead", "--prompt", "You run QA.", "--powers", "assign,areas", "--reports-to", "lead")
            cli("roles", "add", "qa", "--reports-to", "qa-lead")
            tree = cli("roles", "tree")
        self.assertIn("Lead (lead)", tree)
        self.assertIn("\n  Qa lead (qa-lead) [assign, areas]", tree)
        self.assertIn("    QA (qa)", tree)


class Files(Base):
    def upload(self, name, data, kind="", token=None):
        req = urllib.request.Request(self.base + "/api/upload", data=data, method="POST", headers={
            "Authorization": "Bearer " + (token or self.owner), "Content-Type": kind,
            "X-File-Name": urllib.parse.quote(name)})
        try:
            with urllib.request.urlopen(req, timeout=30) as answer:
                return json.loads(answer.read())
        except urllib.error.HTTPError as e:
            return {"status": e.code}

    def test_the_owner_shares_a_screenshot_and_an_agent_sees_it(self):
        session, name = self.agent()
        meta = self.upload("image.png", PNG, "image/png")
        self.assertEqual((meta["name"], meta["type"], meta["size"]), ("image.png", "image/png", len(PNG)))
        out = crewchat.post_json(self.base + "/api/send", self.owner, {"to": name, "text": "", "files": [meta["id"]]})
        msg = self.hub.history(1)[0]
        self.assertEqual((msg["id"], msg["text"], msg["files"]), (out["id"], "", [meta]))
        self.assertIn("[file %s: image.png, image/png, %d bytes; open it with hub_file]" % (meta["id"], len(PNG)),
                      session.text("hub_inbox"))
        content = session.call("hub_file", id=meta["id"])["content"]
        self.assertEqual(content[1], {"type": "image", "data": base64.b64encode(PNG).decode(), "mimeType": "image/png"})
        self.assertIn("saved at", content[0]["text"])

    def test_text_files_come_back_as_text_and_others_as_a_path(self):
        session, _ = self.agent()
        notes = self.upload("notes.md", "# Plan\nShip it".encode())
        self.assertEqual(notes["type"], "text/markdown")
        self.assertIn("# Plan\nShip it", session.text("hub_file", id=notes["id"]))
        pdf = self.upload("spec.pdf", b"%PDF-1.4 fake", "application/pdf")
        out = session.text("hub_file", id=pdf["id"])
        self.assertIn("Open it from that path", out)
        self.assertTrue(Path(re.search(r"saved at (.+?)\. Open", out).group(1)).is_file())
        self.assertIn("no shared file", session.text("hub_file", id="0123456789abcdef"))

    def test_an_agent_shares_a_file_it_made(self):
        session, name = self.agent()
        other, other_name = self.agent()
        shot = Path(tempfile.mkdtemp(dir=TMP)) / "bug.png"
        shot.write_bytes(PNG)
        # Only from a folder on this machine: a path from another machine's folder would be read here.
        far, far_token = self.join_place("laptop")
        remote, _ = self.agent(token=far_token)
        self.assertIn("another machine", remote.text("hub_send", to=name, text="x", files=[str(shot)]))
        crewchat.remember(shot.parent, self.place)
        self.assertIn("with 1 file(s)", session.text("hub_send", to=other_name, text="See the bug", files=[str(shot)]))
        got = other.text("hub_inbox")
        fid = re.search(r"\[file (\w+): bug.png", got).group(1)
        self.assertEqual(other.call("hub_file", id=fid)["content"][1]["data"], base64.b64encode(PNG).decode())
        self.assertIn("absolute paths", session.text("hub_send", to=other_name, text="x", files=["bug.png"]))
        self.assertIn("cannot read", session.text("hub_send", to=other_name, text="x", files=[str(shot) + ".gone"]))
        # Only from the agent's own project folder: not ~/.ssh, not another folder on the machine.
        elsewhere = Path(tempfile.mkdtemp(dir=TMP)) / "id_rsa"
        elsewhere.write_text("not a real key")
        self.assertIn("outside your project folder", session.text("hub_send", to=other_name, text="x",
                                                                  files=[str(elsewhere)]))
        doc = shot.parent / "notes" / "../notes.txt"
        (shot.parent / "notes").mkdir()
        (shot.parent / "notes.txt").write_text("plan")
        self.assertIn("with 1 file(s)", session.text("hub_send", to=other_name, text="x", files=[str(doc)]))
        # Credentials never, even inside the project, nor from the home folder's key stores.
        (shot.parent / ".netrc").write_text("machine x login y password z")
        self.assertIn("holds credentials", session.text("hub_send", to=other_name, text="x",
                                                        files=[str(shot.parent / ".netrc")]))
        fake_home = shot.parent  # as if the project folder were the user's home
        (fake_home / ".ssh").mkdir()
        (fake_home / ".ssh" / "id_ed25519").write_text("not a real key")
        with mock.patch.object(Path, "home", return_value=fake_home):
            self.assertIn("holds credentials", session.text("hub_send", to=other_name, text="x",
                                                            files=[str(fake_home / ".ssh" / "id_ed25519")]))

    def test_an_agent_shares_a_browser_screenshot_where_it_was_saved(self):
        # 11 Nov two-machine test: Claude in Chrome saves a screenshot in a private folder of its own in the temporary
        # folder, and the agent asked for a Bash mkdir and cp to bring it into the project before sharing it. It
        # shares it from there; nothing else outside the project is shared that way.
        session, name = self.agent()
        other, other_name = self.agent()
        project = Path(tempfile.mkdtemp(dir=TMP))
        crewchat.remember(project, self.place)
        shots = Path(tempfile.mkdtemp(prefix=crewchat.CHROME_SHOTS, dir=TMP))  # made 700, as Claude Code makes it
        shot = shots / "screenshot-1.png"
        shot.write_bytes(PNG)
        self.assertIn("with 1 file(s)", session.text("hub_send", to=other_name, text="example.com", files=[str(shot)]))
        fid = re.search(r"\[file (\w+): screenshot-1.png", other.text("hub_inbox")).group(1)
        self.assertEqual(other.call("hub_file", id=fid)["content"][1]["data"], base64.b64encode(PNG).decode())
        # Only an image, right in that folder.
        (shots / "notes.txt").write_text("not a screenshot")
        (shots / "inner").mkdir()
        (shots / "inner" / "x.png").write_bytes(PNG)
        for path in (shots / "notes.txt", shots / "inner" / "x.png"):
            self.assertIn("outside your project folder", session.text("hub_send", to=other_name, text="x",
                                                                      files=[str(path)]))
        # Not a folder of that name that others can open or write into, or another user's.
        if hasattr(os, "getuid"):
            open_shots = Path(tempfile.mkdtemp(prefix=crewchat.CHROME_SHOTS, dir=TMP))
            open_shots.chmod(0o755)
            (open_shots / "shot.png").write_bytes(PNG)
            self.assertIn("outside your project folder", session.text("hub_send", to=other_name, text="x",
                                                                      files=[str(open_shots / "shot.png")]))
            with mock.patch.object(crewchat.os, "getuid", return_value=os.getuid() + 1):
                self.assertFalse(crewchat.saved_screenshot(shot.resolve()))
        self.assertTrue(crewchat.saved_screenshot(shot.resolve()))
        self.assertIn("save_to_disk", crewchat.BROWSER_GUIDE)
        # The mobile bridge's screenshots, in the device proxy's own folder (screenshot_path), too.
        device = Path(tempfile.mkdtemp(prefix=crewchat.CREWCHAT_SHOTS, dir=TMP)) / "iphone17-sim.png"
        device.write_bytes(PNG)
        self.assertIn("with 1 file(s)", session.text("hub_send", to=other_name, text="sim", files=[str(device)]))

    def test_a_removed_place_takes_its_folders_with_it(self):
        # Its name can be given out again; the new place must not inherit the old folder.
        place, _ = self.join_place("old-laptop")
        folder = Path(tempfile.mkdtemp(dir=TMP))
        crewchat.remember(folder, place)
        crewchat.post_json(self.base + "/api/admin", self.owner, {"op": "remove-place", "place": place})
        self.assertNotIn(str(folder), crewchat.load_config().get("folders") or {})

    def test_an_agent_cannot_share_crewchats_keys(self):
        # Agents on linked machines can open a shared file, so a talked-into agent must not leak a token.
        session, name = self.agent()
        folder = Path(tempfile.mkdtemp(dir=TMP))
        crewchat.remember(folder, self.place)  # the agent's folder is on this machine
        inside = [crewchat.token_path("Owner"), crewchat.home() / "config.json"]
        upper = Path(str(crewchat.home()).upper()) / "config.json"
        if upper.exists():  # a case-insensitive disk (macOS, Windows): the same folder by another name
            inside.append(upper)
        link = folder / "notes.txt"
        try:
            link.symlink_to(crewchat.token_path("Owner"))  # a harmless-looking name for the owner's token
            inside.append(link)
        except OSError:
            pass  # Windows without the right to make symlinks
        for path in inside:
            self.assertIn("crewchat's own folder", session.text("hub_send", to=name, text="x", files=[str(path)]))
        config = folder / ".mcp.json"
        config.write_text(json.dumps({"headers": {"Authorization": "Bearer " + self.token}}))
        copy = folder / "backup.txt"
        copy.write_text("old token: %s\n" % crewchat.read_token("Owner"))
        for path in (config, copy):
            self.assertIn("contains a crewchat token", session.text("hub_send", to=name, text="x", files=[str(path)]))
        shot = folder / "shots" / "2026-10-13.png"
        shot.parent.mkdir()
        shot.write_bytes(PNG)
        latest = folder / "latest.png"
        try:
            latest.symlink_to(shot)
        except OSError:
            latest = shot
        self.assertIn("with 1 file", session.text("hub_send", to="all", text="x", files=[str(latest)]))
        self.assertEqual(self.hub.history(1)[0]["files"][0]["name"], latest.name)  # the name the agent gave

    def test_the_page_shows_images_and_offers_everything_else_as_a_download(self):
        png = self.upload("shot.png", PNG, "image/png")
        svg = self.upload("logo.svg", b"<svg onload='alert(1)'/>", "image/svg+xml")
        self.assertEqual(self.raw("/files/%s/shot.png" % png["id"])[0], 401)
        opener, _ = self.owner_browser()
        status, body, headers = self.raw("/files/%s/shot.png" % png["id"], opener=opener)
        self.assertEqual((status, body, headers["Content-Type"]), (200, PNG, "image/png"))
        self.assertTrue(headers["Content-Disposition"].startswith("inline"))
        self.assertIn("sandbox", headers["Content-Security-Policy"])
        status, body, headers = self.raw("/files/%s/logo.svg" % svg["id"], opener=opener)
        self.assertEqual(headers["Content-Type"], "application/octet-stream")
        self.assertTrue(headers["Content-Disposition"].startswith("attachment"))
        self.assertEqual(self.raw("/files/nothing/x", opener=opener)[0], 404)

    def test_only_the_owner_uploads_and_size_is_limited(self):
        self.assertEqual(self.upload("a.txt", b"x", token=self.token)["status"], 403)
        self.assertEqual(self.upload("a.txt", b"x", token="wrong")["status"], 401)
        limit, crewchat.MAX_UPLOAD = crewchat.MAX_UPLOAD, 10
        try:
            self.assertEqual(self.upload("big.bin", b"x" * 11)["status"], 413)
        finally:
            crewchat.MAX_UPLOAD = limit
        with self.assertRaises(urllib.error.HTTPError):
            crewchat.post_json(self.base + "/api/send", self.owner, {"to": "all", "text": "x", "files": ["0123456789abcdef"]})

    def test_say_with_a_file(self):
        doc = Path(tempfile.mkdtemp(dir=TMP)) / "release notes.txt"
        doc.write_text("v1")
        cli("say", "--file", str(doc), "Please review")
        msg = self.hub.history(1)[0]
        self.assertEqual((msg["text"], msg["files"][0]["name"]), ("Please review", "release notes.txt"))

    def test_file_names_are_cleaned(self):
        self.assertEqual(crewchat.clean_filename("../../etc/pass:wd.png"), "passwd.png")
        self.assertEqual(crewchat.clean_filename("C:\\Users\\me\\shot.png"), "shot.png")
        self.assertEqual(crewchat.clean_filename(".."), "file")
        self.assertEqual([crewchat.file_type(n) for n in ("a.md", "b.ts", "c.png")],
                         ["text/markdown", "text/plain", "image/png"])


if __name__ == "__main__":
    unittest.main()
