"""The chat page: faces, analytics, threads, joining, themes, web push and the commands tab."""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from crewchat_fixture import (  # noqa: E402,F401  (the shared server fixture)
    Base, Path, ROOT, Session, TMP, argparse, base64, cli, contextlib, crewchat, gzip, io, json, mock, os, re,
    shutil, subprocess, sys, tempfile, threading, time, unittest, urllib,
)


class Faces(unittest.TestCase):
    """A new agent's face, made of parts (a skin, hair, a hat, glasses, clothes, ...) at random, and drawn as SVG."""

    def test_every_part_draws_a_well_formed_picture(self):
        import xml.etree.ElementTree as ET
        codes = []
        for i, (_, n) in enumerate(crewchat.MIX_PARTS):  # each option of each part, the others at their first
            for option in range(n):
                parts = [0] * len(crewchat.MIX_PARTS)
                parts[i] = option
                codes.append(".".join(map(str, parts)))
        codes += [crewchat.new_mix()[4:] for _ in range(300)]
        for code in codes:
            svg = crewchat.mix_svg(code)
            root = ET.fromstring(svg)
            self.assertEqual((root.tag, root.get("viewBox")), ("{http://www.w3.org/2000/svg}svg", "0 0 128 128"), code)
            self.assertNotIn(b"<script", svg)

    def test_a_code_is_one_number_per_part_in_its_range(self):
        last = ".".join(str(n - 1) for _, n in crewchat.MIX_PARTS)
        self.assertIsNotNone(crewchat.mix_parts(last))
        too_far = ".".join(str(n) for _, n in crewchat.MIX_PARTS)
        for bad in ("", "1.2.3", too_far, last + ".0", "a" + last[1:], "0" + last, "<svg>", "-" + last):
            self.assertIsNone(crewchat.mix_parts(bad), bad)
            self.assertIsNone(crewchat.mix_svg(bad), bad)
        self.assertEqual(crewchat.clean_picture("mix:" + last), "mix:" + last)
        self.assertEqual(crewchat.clean_picture("mix:" + too_far), "")  # another machine's odd one: initials
        self.assertIsNotNone(crewchat.mix_parts(last.rsplit(".", 1)[0]))  # a code from before looks (eleven parts)
        self.assertIsNone(crewchat.mix_parts(last.rsplit(".", 2)[0]))  # but not ten

    def test_a_face_suits_its_name(self):
        for name, want in crewchat.NAME_LOOKS.items():
            for _ in range(5):
                parts = crewchat.mix_parts(crewchat.new_mix(0, name=name)[4:])
                self.assertIn(parts[4], crewchat.MIX_HAIR_FOR[want], name)  # hair that goes with the name
                self.assertTrue(crewchat.mix_fits(parts, name))
                if name not in crewchat.INDIAN_NAMES:
                    self.assertIn(parts[11], crewchat.MIX_LOOK_FOR[want], name)  # lashes for "f"; a beard maybe for "m"
        self.assertIn("Maya", crewchat.NAME_LOOKS)
        self.assertNotIn("Noor", crewchat.NAME_LOOKS)  # a name for anyone: any face
        self.assertTrue(set(crewchat.NAME_LOOKS) <= set(crewchat.AGENT_NAMES))
        maya = crewchat.mix_parts(crewchat.new_mix(0, name="Maya")[4:])
        self.assertFalse(crewchat.mix_fits(maya, "Teo"))
        self.assertTrue(crewchat.mix_fits(maya, "Noor") and crewchat.mix_fits(maya, "Someone"))

    def test_an_indian_name_gets_an_indian_face(self):
        self.assertTrue(crewchat.INDIAN_NAMES <= set(crewchat.AGENT_NAMES))
        self.assertEqual(len(set(crewchat.AGENT_NAMES)), len(crewchat.AGENT_NAMES))
        bindis = 0
        for name in crewchat.INDIAN_NAMES:
            for _ in range(8):
                parts = crewchat.mix_parts(crewchat.new_mix(0, name=name)[4:])
                self.assertIn(parts[1], crewchat.MIX_INDIAN["skin"], name)  # a warm brown skin
                self.assertIn(parts[5], crewchat.MIX_INDIAN["haircolour"], name)  # black or dark brown hair
                if crewchat.NAME_LOOKS.get(name) == "f":
                    self.assertIn(parts[11], (1, 3), name)  # lashes, and half the time a bindi
                    bindis += parts[11] == 3
                else:
                    self.assertNotEqual(parts[11], 3, name)
        self.assertGreater(bindis, 0)
        self.assertIn(b"#c8283c", crewchat.mix_svg("0.0.0.0.0.0.0.0.0.0.0.3"))  # the bindi drawn
        for _ in range(200):  # never on a face whose name does not call for it
            self.assertNotEqual(crewchat.mix_parts(crewchat.new_mix(0, name="Noor")[4:])[11], 3)
        light = [0] * 12
        light[11] = 1
        self.assertFalse(crewchat.mix_fits(light, "Priya"))  # the lightest skin tone is not one an Indian name gets

    def test_a_new_face_works_at_the_machines_device_and_is_nobody_elses(self):
        for device in range(3):
            self.assertEqual(crewchat.mix_parts(crewchat.new_mix(device)[4:])[10], device)  # the device part
        taken = {crewchat.new_mix(0) for _ in range(50)}
        self.assertNotIn(crewchat.new_mix(0, taken), taken)
        self.assertIn(crewchat.device_kind(), (0, 1, 2))
        try:
            crewchat.device_kind.cache_clear()
            with mock.patch.dict(os.environ, {"CLAUDE_CODE_REMOTE": "true"}):
                self.assertEqual(crewchat.device_kind(), 2)  # a cloud session sits on a cloud
        finally:
            crewchat.device_kind.cache_clear()


class FacesOnThePage(Base):
    """The server draws a face from its code, for the page; the dice on the page give an agent a new one."""

    def test_the_server_draws_a_face_and_refuses_a_bad_code(self):
        code = crewchat.new_mix()[4:]
        with urllib.request.urlopen(self.base + "/pictures/mix/%s.svg" % code) as r:
            self.assertEqual(r.headers["Content-Type"], "image/svg+xml")
            self.assertEqual(r.headers["Content-Security-Policy"], "default-src 'none'")
            self.assertEqual(r.read(), crewchat.mix_svg(code))
        for bad in ("9.9.9", "..%2Fart"):
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(self.base + "/pictures/mix/%s.svg" % bad)
            self.assertEqual(e.exception.code, 404)

    def test_a_renamed_agent_gets_a_face_that_suits_its_new_name(self):
        name = self.hub.agent_for(self.hub.open_session("macbook", "claude"))
        self.hub.rename(name, "Maya")
        self.hub.set_profile("Maya", "mix:new")
        face = self.hub.agents["Maya"]["picture"]
        self.assertTrue(crewchat.mix_fits(crewchat.mix_parts(face[4:]), "Maya"))
        self.hub.rename("Maya", "Teo")
        after = self.hub.agents["Teo"]["picture"]
        self.assertNotEqual(after, face)
        self.assertTrue(crewchat.mix_fits(crewchat.mix_parts(after[4:]), "Teo"))
        self.assertEqual(crewchat.mix_parts(after[4:])[10], crewchat.mix_parts(face[4:])[10])  # at the same device
        self.hub.rename("Teo", "Noor")
        self.assertEqual(self.hub.agents["Noor"]["picture"], after)  # it suits a name for anyone: kept
        self.hub.set_profile("Noor", "art:owl")
        self.hub.rename("Noor", "Maya")
        self.assertEqual(self.hub.agents["Maya"]["picture"], "art:owl")  # a picture chosen stays
        self.hub.remove("Maya")

    def test_the_dice_give_an_agent_a_new_face(self):
        name = self.hub.agent_for(self.hub.open_session("macbook", "claude"))
        before = self.hub.agents[name]["picture"]
        self.hub.set_profile(name, "mix:new")
        after = self.hub.agents[name]["picture"]
        self.assertTrue(after.startswith("mix:") and crewchat.mix_parts(after[4:]) and after != before)
        with self.assertRaises(crewchat.HubError):
            self.hub.set_profile(name, "mix:1.2")


class AnalyticsTotals(unittest.TestCase):
    """/api/analytics' numbers, worked out from the whole message log (roadmap X1)."""

    def hub(self, lines=()):
        root = Path(tempfile.mkdtemp(prefix="analytics-", dir=TMP))
        crewchat.save_config({"project": "numbers"}, root)
        (root / "messages.jsonl").write_text("".join(json.dumps(m) + "\n" for m in lines))
        return crewchat.Hub(crewchat.Roster(root))

    def test_a_day_of_work_in_numbers(self):
        t0 = time.time() - 3600
        msg = lambda seq, sender, to, kind="msg", **kw: dict({"id": str(seq), "seq": seq, "ts": t0 + seq * 60,  # noqa: E731
                                                            "from": sender, "to": to, "kind": kind, "text": "x"}, **kw)
        lines = [msg(1, "Owner", "all", "task"), msg(2, "Maya", "all", "take", task="1"),
                 msg(3, "Maya", "Owner", "update", task="1", status="in_progress"),
                 msg(4, "Maya", "Owner", "update", task="1", status="blocked"),
                 msg(6, "Maya", "Owner", "update", task="1", status="in_progress"),
                 msg(7, "Maya", "Owner", "update", task="1", status="review"),
                 msg(8, "Maya", "Owner", "update", task="1", status="done"),
                 msg(9, "Owner", "all", "task"), msg(10, "Teo", "all", "take", task="9"),
                 msg(11, "Teo", "Owner", "update", task="9", status="review"),
                 msg(12, "Teo", "Owner", "update", task="9", status="in_progress"),
                 msg(13, "Teo", "Owner", "update", task="9", status="review"),
                 msg(14, "crewchat", "all", "update", task="9", status="todo", reclaimed="Teo"),
                 msg(15, "Wren", "Teo", "answer", answers="9", verdict="approve"),
                 msg(16, "Teo", "Maya", files=[{"id": "a" * 16}, {"id": "b" * 16}]),
                 msg(17, "crewchat", "all", "event")]
        hub = self.hub(lines)
        report = hub.analytics.report(hub)
        self.assertEqual(report["messages"], 15)  # the event (who joined) is not a message
        tasks = report["tasks"]
        self.assertEqual((tasks["posted"], tasks["taken"], tasks["done"]), (2, 2, 1))
        self.assertEqual(tasks["time_to_take"], 60)
        self.assertEqual(tasks["cycle_time"], 7 * 60)
        self.assertEqual(tasks["time_blocked"], 2 * 60)  # blocked at 4, going again at 6
        self.assertEqual(tasks["review_pass_rate"], 0.5)  # #1 passed its first review, #9 did not
        self.assertEqual((report["reclaims"], report["files"], report["verdicts"]), (1, 2, {"approve": 1}))
        self.assertIn(["Maya", "Owner", 5], report["pairs"])
        self.assertEqual(report["agents"]["Teo"]["messages"], 5)  # the take, three updates, the files
        self.assertNotIn("crewchat", report["agents"])
        self.assertEqual(sum(h["messages"] for h in report["hours"]), 15)
        # New messages count as they come, once.
        with hub.lock:
            hub._append("Maya", "all", "and one more", "msg")
        self.assertEqual(hub.analytics.report(hub)["messages"], 16)

    def test_a_task_still_in_review_has_not_failed_it(self):
        t0 = time.time() - 3600
        msg = lambda seq, sender, kind, **kw: dict({"id": str(seq), "seq": seq, "ts": t0 + seq * 60, "from": sender,  # noqa: E731
                                                    "to": "all", "kind": kind, "text": "x"}, **kw)
        hub = self.hub([msg(1, "Owner", "task"), msg(2, "Maya", "take", task="1"),
                        msg(3, "Maya", "update", task="1", status="review")])
        self.assertIsNone(hub.analytics.report(hub)["tasks"]["review_pass_rate"])  # no review has ended yet: no rate
        with hub.lock:
            hub._append("Maya", "all", "x", "update", task="1", status="done")
        self.assertEqual(hub.analytics.report(hub)["tasks"]["review_pass_rate"], 1.0)

    def test_the_whole_log_counts_not_only_what_is_in_memory(self):
        n = crewchat.KEEP_MESSAGES + 500
        hub = self.hub([{"id": str(i), "seq": i, "ts": 1.0 + i, "from": "Maya", "to": "all", "kind": "msg", "text": "x"}
                        for i in range(1, n + 1)])
        with hub.lock:
            hub._append("Teo", "all", "after the start", "msg")  # before the build: still counted once
        self.assertEqual(len(hub.messages), crewchat.KEEP_MESSAGES)
        report = hub.analytics.report(hub)
        self.assertEqual(report["messages"], n + 1)
        self.assertEqual(hub.analytics.report(hub)["messages"], n + 1)  # built once


class TokenPrices(unittest.TestCase):
    def test_prices_and_transcripts(self):
        self.assertEqual(crewchat.model_price("claude-opus-5-5"), crewchat.PRICES["claude-opus-5-5"])
        self.assertEqual(crewchat.model_price("claude-opus-5-5[1m]"), crewchat.PRICES["claude-opus-5-5"])
        self.assertEqual(crewchat.model_price("claude-haiku-4-5-20251001"), crewchat.PRICES["claude-haiku-4-5"])
        self.assertEqual(crewchat.model_price("claude-opus-5"), crewchat.PRICES["claude-opus-5"])  # not 5-5's
        self.assertIsNone(crewchat.model_price("gpt-9"))
        self.assertEqual(crewchat.usage_cost({"gpt-9": [1, 1, 1, 1, 1]}), (0.0, ["gpt-9"]))
        path = Path(tempfile.mkdtemp(prefix="transcript-", dir=TMP)) / "t.jsonl"
        line = lambda mid, n: json.dumps({"type": "assistant", "message": {"id": mid, "model": "m",  # noqa: E731
                                                                         "usage": {"input_tokens": n, "output_tokens": n}}})
        path.write_text(line("a", 1) + "\n" + line("a", 1) + "\n" + line("b", 2) + "\n" + line("c", 3)[:20])  # c: half written
        found, offset, last = crewchat.transcript_usage(path)
        self.assertEqual((found, last), ({"m": [3, 3, 0, 0, 0]}, "b"))
        with open(path, "a") as f:
            f.write(line("c", 3)[20:] + "\n" + json.dumps({"type": "assistant", "message": {"id": "s", "model": "<synthetic>",
                                                                                          "usage": {"input_tokens": 0}}}) + "\n")
        self.assertEqual(crewchat.transcript_usage(path, offset, last)[0], {"m": [3, 3, 0, 0, 0]})  # c, finished; nothing for 0s
        path.write_text(line("z", 5) + "\n")  # a shorter, new transcript: read from its start
        self.assertEqual(crewchat.transcript_usage(path, offset, last)[0], {"m": [5, 5, 0, 0, 0]})


class SearchHistory(Base):
    """W7: every message the chat ever had, found by its words (and sender), in this project or in all of them."""

    @staticmethod
    def reopen(index):
        """The index as a fresh start finds it, after a test that turned full-text search off."""
        with index.lock:
            if index.db is not None:
                index.db.close()
            index.db, index.fts = None, None

    def test_search_finds_any_message_by_its_words(self):
        old = self.say("all", "The outbox drains oldest first, through SyncWorker.")
        for i in range(crewchat.PAGE_BATCH + 5):  # more than the page loads
            self.say("all", "filler %d" % i)
        index = self.hub.chat_index
        self.assertEqual([h["id"] for h in index.search("syncworker outbox")], [old])
        self.assertEqual(index.search("outb")[0]["id"], old)  # a word's start matches
        self.assertEqual(index.search("syncworker", sender="nobody"), [])
        self.assertEqual(index.search('"); DROP TABLE bodies; --'), [])  # words only, never SQL
        self.assertEqual(index.search("   "), [])
        index.fts = False  # SQLite without full-text search: a plain scan, the same answer
        self.assertEqual([h["id"] for h in index.search("syncworker outbox")], [old])

    def test_a_hindi_word_is_found_whole(self):
        hello = self.say("all", "नमस्ते दुनिया, build हो गया")
        bare = self.say("all", "नमस तक")  # the same letters without the vowel signs
        index = self.hub.chat_index
        self.addCleanup(self.reopen, index)
        for fts in (index.fts, False):  # False: SQLite without full-text search
            index.fts = fts
            found = [h["id"] for h in index.search("नमस्ते")]
            self.assertIn(hello, found)
            self.assertNotIn(bare, found)
            self.assertIn(hello, [h["id"] for h in index.search("दुनिया build")])

    def test_a_letter_with_a_nukta_is_found_however_it_was_typed(self):
        """Round-2 re-review: ज़ is one code point (U+095B) or ज and a dot (U+091C U+093C); search matched only the same."""
        one, two = "\u095brooree", "\u091c\u093crooree"
        said = self.say("all", "yeh %s hai" % one)
        index = self.hub.chat_index
        self.addCleanup(self.reopen, index)
        for fts in (index.fts, False):
            index.fts = fts
            self.assertIn(said, [h["id"] for h in index.search(two)])
            self.assertIn(said, [h["id"] for h in index.search(one)])

    def test_a_message_stored_before_normalising_is_found_too(self):
        """Audit, round 3: a message stored decomposed before search normalised text (NFC) stayed that way, so a word
        typed the usual way missed it. The bodies are normalised once, and the index made again."""
        one, two = "\u095bindagi", "\u091c\u093cindagi"
        said = self.say("all", "yeh %s hai" % two)
        index = self.hub.chat_index
        self.addCleanup(self.reopen, index)
        index.search("yeh")
        with index.lock:  # what an older crewchat stored: the text as typed (U+095B, not NFC), and no version
            db = index._conn()
            db.execute("UPDATE bodies SET text = ? WHERE id = ?", ("yeh %s hai" % one, said))
            db.execute("INSERT INTO bodies_words (bodies_words) VALUES ('rebuild')")
            db.execute("PRAGMA user_version = 0")
            db.commit()
            db.close()
            index.db, index.fts = None, None
        for fts in (None, False):
            index.fts = fts
            self.assertIn(said, [h["id"] for h in index.search(two)])

    def test_an_index_from_before_is_built_again_with_whole_words(self):
        hello = self.say("all", "नमस्ते दुनिया")
        index = self.hub.chat_index
        index.search("दुनिया")
        with index.lock:  # the index an older crewchat made
            db = index._conn()
            db.execute("DROP TABLE bodies_words")
            db.execute("CREATE VIRTUAL TABLE bodies_fts USING fts5(text, content='bodies', content_rowid='rowid')")
            db.commit()
            db.close()
            index.db, index.fts = None, None
        self.assertIn(hello, [h["id"] for h in index.search("नमस्ते")])
        names = [r[0] for r in index._conn().execute("SELECT name FROM sqlite_master WHERE name LIKE 'bodies_%'")]
        self.assertNotIn("bodies_fts", names)

    def test_an_index_left_empty_by_a_stop_is_filled_again(self):
        """Round-2 re-review: a stop between making the new index and filling it left old messages unsearchable."""
        hello = self.say("all", "नमस्ते from before the stop")
        index = self.hub.chat_index
        index.search("before")
        with index.lock:  # the index as a stop left it: made, never filled
            db = index._conn()
            db.execute("DROP TABLE bodies_words")
            db.execute("CREATE VIRTUAL TABLE bodies_words USING fts5(text, content='bodies', content_rowid='rowid', "
                       "tokenize=\"unicode61 categories 'L* N* Co M*'\")")
            db.commit()
            db.close()
            index.db, index.fts = None, None
        self.assertIn(hello, [h["id"] for h in index.search("नमस्ते")])

    def test_the_owner_searches_every_project_from_the_page(self):
        made = crewchat.post_json(self.base + "/api/projects", self.owner, {"op": "create", "name": "Second"})
        crewchat.post_json(self.base + "/api/send?chat=" + made["id"], self.owner,
                           {"to": "all", "text": "Retry headers ship on Friday"})
        here = self.say("all", "Retry headers are pushed")
        search = lambda **b: crewchat.post_json(self.base + "/api/search", self.owner, b)["hits"]  # noqa: E731
        self.assertEqual([h["id"] for h in search(q="retry headers")], [here])  # this project
        everywhere = search(q="retry headers", all=True)
        self.assertEqual(sorted(h["project"] for h in everywhere), sorted(["Second", self.hub.roster.project]))
        self.assertEqual(next(h for h in everywhere if h["project"] == "Second")["chat"], made["id"])
        with self.assertRaises(urllib.error.HTTPError):
            crewchat.post_json(self.base + "/api/search", self.token, {"q": "retry"})  # the owner's only


class Threads(Base):
    """W7: a message's replies are its thread, joined by the owner on the page and by agents (hub_send's thread);
    agents search all history with hub_history's q."""

    def test_agents_reply_in_a_thread_and_see_it_marked(self):
        session = Session(self.base, self.token)
        session.call("hub_link", key=self.hub.new_start())
        root = self.say(session.me, "Which backoff for the outbox?")
        session.call("hub_send", to="Owner", text="30 s, 2 min, 10 min.", thread=root)
        reply = self.hub.messages[-1]
        self.assertEqual(reply["thread"], root)
        self.assertIn("[in thread #%s]" % root, crewchat.fmt(reply, "Owner"))
        crewchat.post_json(self.base + "/api/send", self.owner, {"to": session.me, "text": "Cap it at 5.", "thread": root})
        self.assertEqual(self.hub.messages[-1]["thread"], root)
        refused = session.call("hub_send", to="Owner", text="x", thread="nope")
        self.assertTrue(refused.get("isError"))
        self.assertIn("no message #nope", refused["content"][0]["text"])
        crewchat.post_json(self.base + "/api/send", self.owner, {"to": session.me, "text": "Ready?", "ask": True})
        self.assertIn("[NEEDS A REPLY]", crewchat.fmt(self.hub.messages[-1], session.me))  # W6: the agent knows

    def test_agents_search_the_history(self):
        session = Session(self.base, self.token)
        session.call("hub_link", key=self.hub.new_start())
        self.say("all", "The outbox drains through SyncWorker.")
        said = session.text("hub_history", q="syncworker")
        self.assertIn("The outbox drains through SyncWorker.", said)
        self.assertIn("nothing", session.text("hub_history", q="zebra").lower())


class WhoCanJoin(Base):
    """A project lets only agents crewchat started join (the owner, 9 Nov: new chats, and new projects as their host's
    first project says), or any session opened in its connected folders (projects from before, until switched)."""

    def test_only_crewchats_own_agents_join(self):
        self.assertEqual(crewchat.post_json(self.base + "/api/who-joins", self.owner, {})["mode"], "anyone")  # from before
        crewchat.post_json(self.base + "/api/who-joins", self.owner, {"mode": "own"})
        stray = Session(self.base, self.token)  # a session the owner opened in the folder, not from the page
        said = stray.call("hub_agents")
        self.assertTrue(said.get("isError"))
        self.assertIn("+ New agent", said["content"][0]["text"])
        refused = stray.call("hub_link", key="k" * 12)  # a key it made up, or a hook gave it
        self.assertTrue(refused.get("isError"))
        self.assertEqual(self.hub.hook(self.place, "q" * 12, 0, 0, "prompt"), {"quiet": True})  # its hooks: say nothing
        ours = Session(self.base, self.token)  # started from the page: its start key
        ours.call("hub_link", key=self.hub.new_start())
        self.assertFalse(ours.call("hub_agents").get("isError"))
        crewchat.post_json(self.base + "/api/who-joins", self.owner, {"mode": "anyone"})
        self.assertFalse(stray.call("hub_agents").get("isError"))  # anyone again
        with self.assertRaises(urllib.error.HTTPError):
            crewchat.post_json(self.base + "/api/who-joins", self.owner, {"mode": "everyone"})

    def test_new_projects_let_only_their_own_agents_join(self):
        made = crewchat.post_json(self.base + "/api/projects", self.owner, {"op": "create", "name": "Fresh"})
        self.assertEqual(crewchat.load_config(crewchat.find_chat(made["id"])["root"])["join"], "anyone")  # as the host
        cli("setup", "--join", "own")
        self.addCleanup(cli, "setup", "--join", "anyone")
        made = crewchat.post_json(self.base + "/api/projects", self.owner, {"op": "create", "name": "Fresher"})
        self.assertEqual(crewchat.load_config(crewchat.find_chat(made["id"])["root"])["join"], "own")
        home = Path(tempfile.mkdtemp(prefix="fresh-home-", dir=TMP))
        with mock.patch.dict(os.environ, {"CREWCHAT_HOME": str(home)}):
            self.assertEqual(crewchat.setup_host("fresh", 9)["join"], "own")
            self.assertEqual(crewchat.setup_host("fresh", 9, join="anyone")["join"], "anyone")  # crewchat setup --join
            self.assertEqual(crewchat.setup_host("fresh", 9)["join"], "anyone")  # kept


class Linking(Base):
    """Hooks reach an agent through a link key that the agent ties to itself with hub_link."""

    def hook(self, key, token=None, **extra):
        out = crewchat.post_json(self.base + "/api/hook", token or self.token, dict({"key": key}, **extra))
        self.assertFalse(out.pop("capped"))  # every answer says whether the loop limit was reached
        return out

    def test_link_then_messages_arrive_through_the_hook(self):
        session = Session(self.base, self.token)
        self.assertEqual(self.hook("key-first-0001"), {"link": True})
        note = session.text("hub_link", key="key-first-0001")
        name = session.me
        self.assertIn("Linked. You are %s, in the project Demo." % name, note)
        self.assertIn("%s (you)" % name, note)
        self.assertEqual(self.hook("key-first-0001"), {"agent": name, "text": "", "last": 0, "owner": False,
                                                       "pids": "want"})  # a new agent: which processes it runs under
        number = self.say(name, "through the hook")
        answer = self.hook("key-first-0001")
        self.assertIn("Owner -> you: through the hook", answer["text"])
        self.assertEqual(answer["last"], self.hub.seq_of(number))
        self.assertIn("through the hook", self.hook("key-first-0001")["text"])  # still unread
        self.assertEqual(self.hook("key-first-0001", ack=self.hub.seq_of(number))["text"], "")

    def test_a_resumed_conversation_gets_its_name_back(self):
        first = Session(self.base, self.token)
        first.call("hub_link", key="key-resume-0001")
        name = first.me
        before = self.names()
        # The tool restarts: new MCP session, same conversation, so the hook has the same key.
        again = Session(self.base, self.token)
        self.assertIn("This session is %s again, in the project Demo." % name, again.text("hub_link", key="key-resume-0001"))
        self.assertEqual(again.me, name)
        self.assertEqual(self.names(), before)

    def test_a_reconnect_that_already_spoke_is_merged_back(self):
        first = Session(self.base, self.token)
        first.call("hub_link", key="key-merge-00001")
        name = first.me
        time.sleep(0.05)
        again = Session(self.base, self.token)
        stand_in = again.me  # used a tool before linking: it was given a name of its own
        self.assertNotEqual(stand_in, name)
        self.assertEqual(self.hook("key-merge-00001"), {"link": True, "again": True})  # the hook notices and asks
        again.call("hub_link", key="key-merge-00001")
        self.assertEqual(again.me, name)
        self.assertNotIn(stand_in, self.names())
        self.assertIn("* %s is %s (session resumed)" % (stand_in, name), again.text("hub_history"))
        self.assertIn("agent", self.hook("key-merge-00001"))

    def test_a_new_neighbour_makes_the_hook_ask_only_once(self):
        mine = Session(self.base, self.token)
        mine.call("hub_link", key="key-neighbour-01")
        name = mine.me
        time.sleep(0.05)
        neighbour = Session(self.base, self.token)
        self.assertEqual(self.hook("key-neighbour-01"), {"link": True, "again": True})
        self.assertIn("agent", self.hook("key-neighbour-01"))  # asked once, then carries on
        mine.call("hub_link", key="key-neighbour-01")  # the no-op re-link changes nothing
        self.assertEqual(mine.me, name)
        self.assertNotEqual(neighbour.me, name)

    def test_a_new_neighbour_while_an_agent_listens_does_not_leave_it_deaf(self):
        # Windows run: Maya was asked twice to link again (a launch, then an orphaned session nearby) and did. Listening,
        # she was asked again when Theo started beside her: past the two asks, her wait ended with nothing said, and
        # with nothing to wake an idle agent on Windows (W2) she never heard the chat again.
        folder = Path(tempfile.mkdtemp(prefix="relisten-", dir=TMP)).resolve()
        crewchat.install_client("claude", folder, "http://127.0.0.1:9", "tok")
        session = "maya-" + crewchat.secrets.token_hex(4)
        state = crewchat.hook_state_file("claude", session)
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"key": "key-maya-0000001", "asks": crewchat.MAX_LINK_ASKS, "chain": 0, "pending": 0}))
        self.addCleanup(lambda: state.unlink() if state.exists() else None)
        answers = iter([{"agent": "Maya", "text": "", "last": 0},  # linked, nothing yet: she listens
                        {"link": True, "again": True}])  # Theo's new session, not linked yet
        out = io.StringIO()
        env = {k: v for k, v in os.environ.items() if k not in ("CREWCHAT_LINK_KEY", "CREWCHAT_LISTEN")}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(crewchat, "find_project", return_value=folder), \
                mock.patch.object(crewchat, "listen_seconds", return_value=1800), \
                mock.patch.object(crewchat, "inbox_of", return_value=None), \
                mock.patch.object(crewchat, "post_json", lambda *a, **k: next(answers)), \
                mock.patch("sys.stdin", io.StringIO(json.dumps({"session_id": session}))), mock.patch("sys.stdout", out):
            crewchat.run_hook("claude", "stop")
        said = json.loads(out.getvalue())  # she is asked to link again, in a turn: she does, and listens again
        self.assertEqual(said["decision"], "block")
        self.assertIn('key "key-maya-0000001"', said["reason"])
        self.assertEqual(json.loads(state.read_text())["asks"], 1)  # her asks count again from this one
        # At the start of a hook (not listening yet), a session asked twice is still left alone.
        answers = iter([{"link": True, "again": True}])
        state.write_text(json.dumps({"key": "key-maya-0000001", "asks": crewchat.MAX_LINK_ASKS, "chain": 0, "pending": 0}))
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(crewchat, "find_project", return_value=folder), \
                mock.patch.object(crewchat, "listen_seconds", return_value=1800), \
                mock.patch.object(crewchat, "inbox_of", return_value=None), \
                mock.patch.object(crewchat, "post_json", lambda *a, **k: next(answers)), \
                mock.patch("sys.stdin", io.StringIO(json.dumps({"session_id": session}))), mock.patch("sys.stdout", out):
            crewchat.run_hook("claude", "stop")
        self.assertEqual(out.getvalue(), "")

    def test_keys_do_not_cross_places_and_bad_keys_are_refused(self):
        session = Session(self.base, self.token)
        session.call("hub_link", key="key-private-001")
        other_place, other_token = self.join_place("other")
        self.assertEqual(self.hook("key-private-001", token=other_token), {"link": True})
        intruder = Session(self.base, other_token)
        intruder.call("hub_link", key="key-private-001")
        self.assertNotEqual(intruder.me, session.me)
        self.assertTrue(session.call("hub_link", key="short")["isError"])
        with self.assertRaises(urllib.error.HTTPError):
            self.hook("no")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            crewchat.post_json(self.base + "/api/hook", self.owner, {"key": "key-private-001"})
        self.assertEqual(ctx.exception.code, 403)


class AgentCards(Base):
    """The owner's actions on an agent's card: rename, remove, listening."""

    def op(self, **body):
        try:
            return crewchat.post_json(self.base + "/api/agent", self.owner, body)
        except urllib.error.HTTPError as e:
            return json.loads(e.read())

    def test_rename_remove_and_listening(self):
        session, name = self.agent()
        self.assertEqual(self.op(op="listen", agent=name, mode="off"), {"ok": True})
        self.assertEqual(next(r for r in self.hub.rows() if r["agent"] == name)["listen"], "off")
        self.assertEqual(self.op(op="rename", agent=name, new="page-named"), {"ok": True})
        self.assertIn("page-named", self.names())
        self.assertEqual(self.hub.agents["page-named"]["listen"], "off")  # the setting goes with the name
        self.assertIn("reserved", self.op(op="rename", agent="page-named", new="Owner")["error"])
        self.assertIn("listening is on, off or default", self.op(op="listen", agent="page-named", mode="loud")["error"])
        task = self.say("all", "held by the one we remove", "task")
        session.call("hub_task", action="take", id=task)
        self.assertEqual(self.op(op="remove", agent="page-named"), {"ok": True})
        self.assertNotIn("page-named", self.names())
        row = next(r for r in self.hub.sheet_rows() if r["id"] == task)
        self.assertEqual((row["status"], row["holder"], row["reclaimed"]), ("todo", "", "page-named"))  # free at once
        self.assertIn("no agent called nobody", self.op(op="listen", agent="nobody", mode="on")["error"])
        self.assertIn("op is rename, remove, listen, stop, reopen, reconnect, start, model, mode, screen, attach, fork, profile, browser, mobile, crew-save, crew-start or crew-stop", self.op(op="kick", agent=name)["error"])

    def test_cards_never_wait_for_the_process_list(self):
        _, name = self.agent()
        self.hub.agents[name]["session"] = {"id": "sess-0011-abc", "pid": 999999, "started": "x", "app": "terminal",
                                            "tool": "claude", "folder": "/tmp", "since": time.time()}
        crewchat._PROCESSES.clear()
        with mock.patch.object(crewchat, "process_table", mock.Mock(side_effect=AssertionError("ps under the lock"))):
            self.assertFalse(next(r for r in self.hub.rows() if r["agent"] == name)["session"]["running"])
        before = self.hub.version  # the server's refresher finds it running, and the page hears it
        table = {999999: (1, "x", "/x/claude --resume sess-0011-abc")}
        with mock.patch.object(crewchat, "process_table", lambda pids=None: table):
            crewchat.refresh_processes(mock.Mock(all=lambda: [mock.Mock(hub=self.hub)]))
        self.assertGreater(self.hub.version, before)
        self.assertTrue(next(r for r in self.hub.rows() if r["agent"] == name)["session"]["running"])

    def test_crewchat_stop_finds_a_windows_terminal_agent(self):
        # Windows run: five Terminal-window agents ran `...\claude.EXE "..."`; their hooks recorded no session, and
        # `crewchat stop` said "No agent was running" and left them all running.
        _, name = self.agent()
        command = 'C:\\Users\\x\\.local\\bin\\claude.EXE "You were started from the crewchat by your owner."'
        table = {10: (9, "2026-11-11T09:20:01", "cmd /c hook"), 9: (1, "2026-11-11T09:20:00", command)}
        with mock.patch.object(crewchat.os, "getppid", lambda: 10), mock.patch.dict(os.environ, {"CREWCHAT_TEST_PROCESS": ""}):
            found = crewchat.agent_process("claude", table)  # what its hook reports
        self.hub.agents[name]["session"] = dict(found, id="sess-0012-abc", tool="claude", folder="/tmp", since=time.time())
        stopped = []
        with mock.patch.object(crewchat, "process_table", lambda pids=None: {9: table[9]}), \
                mock.patch.object(crewchat.Hub, "stop_agent", lambda hub, agent: stopped.append(agent)):
            self.assertEqual(crewchat.stop_all_agents(mock.Mock(all=lambda: [mock.Mock(hub=self.hub)])), ([name], {}))
        self.assertEqual(stopped, [name])
        crewchat._PROCESSES.clear()

    def test_windows_stop_is_forced(self):
        calls = []
        with mock.patch.object(crewchat.os, "name", "nt"), \
                mock.patch.object(crewchat.subprocess, "run", lambda c, **kw: calls.append(c) or mock.Mock(returncode=0)):
            crewchat.stop_process(4242)
        self.assertEqual(calls[0], ["taskkill", "/PID", "4242", "/T", "/F"])

    def test_a_stopped_agent_is_stopped_whatever_taskkill_says(self):
        # Windows re-test (B8): taskkill /T /F on an agent's tree (claude.EXE, its hooks' Git Bash, MCP servers, a
        # console host) ended them all and still exited non-zero: Stop said "could not stop it".
        session = {"pid": 4242, "started": "x", "tool": "claude"}
        running = [True]
        taskkill = lambda c, **kw: running.clear() or mock.Mock(returncode=128)  # noqa: E731  (the tree ends anyway)
        with mock.patch.object(crewchat.os, "name", "nt"), mock.patch.object(crewchat.subprocess, "run", taskkill), \
                mock.patch.object(crewchat, "process_running", lambda s, table=None: bool(running)):
            crewchat.end_session("Ben", session)  # no HubError
        with mock.patch.object(crewchat.os, "name", "nt"), mock.patch.object(crewchat.time, "sleep", lambda s: None), \
                mock.patch.object(crewchat.subprocess, "run", lambda c, **kw: mock.Mock(returncode=128)), \
                mock.patch.object(crewchat, "process_running", lambda s, table=None: True), \
                self.assertRaises(crewchat.HubError) as caught:
            crewchat.end_session("Ben", session)  # still running after the wait: it says so
        self.assertEqual(str(caught.exception), "Ben: could not stop it")

    def stand_in(self, name, app="terminal"):
        """A real process that looks like the agent's claude (it is sleep), recorded as its session."""
        proc = subprocess.Popen(["claude", "60"], executable="/bin/sleep")
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        started = crewchat.process_table()[proc.pid][1]
        self.hub.agents[name]["session"] = {"id": "sess-0002-abc", "pid": proc.pid, "started": started, "app": app,
                                            "tool": "claude", "folder": "/tmp", "since": time.time()}
        crewchat._PROCESSES.clear(); crewchat._PROCESSES.update(crewchat.process_table([proc.pid]))  # as the server's look would
        return proc

    @unittest.skipIf(os.name == "nt", "a stand-in process with another name needs POSIX exec")
    def test_stop_ends_the_agents_process_and_frees_its_tasks(self):
        session, name = self.agent()
        task = self.say("all", "held when stopped", "task")
        session.call("hub_task", action="take", id=task)
        proc = self.stand_in(name)
        self.assertTrue(next(r for r in self.hub.rows() if r["agent"] == name)["session"]["running"])
        self.assertEqual(self.op(op="stop", agent=name), {"ok": True})
        proc.wait(timeout=5)
        row = next(r for r in self.hub.sheet_rows() if r["id"] == task)
        self.assertEqual((row["status"], row["reclaimed"]), ("todo", name))
        self.assertIn(name, self.names())  # stopped, not removed
        self.assertFalse(next(r for r in self.hub.rows() if r["agent"] == name)["session"]["running"])
        self.assertIn("is not running", self.op(op="stop", agent=name)["error"])

    @unittest.skipIf(os.name == "nt", "a stand-in process with another name needs POSIX exec")
    def test_crewchat_stop_closes_the_agents_running_here(self):
        # crewchat stop asks the server to end its agents' sessions before it goes: no agent CLI is left running.
        session, name = self.agent()
        task = self.say("all", "held when crewchat stops", "task")
        session.call("hub_task", action="take", id=task)
        _, idle = self.agent()  # never had a session: nothing to close
        proc = self.stand_in(name)
        out = crewchat.post_json(self.base + "/api/admin", self.owner, {"op": "stop-agents"})
        self.assertEqual(out["stopped"], [name])
        proc.wait(timeout=5)
        row = next(r for r in self.hub.sheet_rows() if r["id"] == task)
        self.assertEqual((row["status"], row["reclaimed"]), ("todo", name))
        self.assertIn(idle, self.names())
        self.assertEqual(crewchat.post_json(self.base + "/api/admin", self.owner, {"op": "stop-agents"})["stopped"], [])

    def test_crewchat_stop_says_which_agents_it_closed_unless_kept(self):
        asked = []

        def call(path, body, **kw):
            asked.append(body["op"])
            return {"ok": True, "stopped": ["ada", "bo"]}
        with mock.patch.object(crewchat, "server_up", lambda config: True), \
                mock.patch.object(crewchat, "stop_server", lambda config: True), \
                mock.patch.object(crewchat, "owner_call", call):
            out = cli("stop")
            self.assertEqual(asked, ["stop-agents"])
            self.assertIn("Stopped crewchat and closed its agents: ada, bo.", out)
            out = cli("stop", "--keep-agents")
            self.assertEqual(asked, ["stop-agents"])  # not asked again
            self.assertIn("Agents keep working", out)
        with mock.patch.object(crewchat, "server_up", lambda config: True), \
                mock.patch.object(crewchat, "stop_server", lambda config: True), \
                mock.patch.object(crewchat, "owner_call", mock.Mock(side_effect=SystemExit("unknown operation"))):
            self.assertIn("Agents keep working", cli("stop"))  # an older server still stops

    def test_crewchat_stop_says_which_agents_it_could_not_close(self):
        # Never "Agents keep working" when the server closed some: what failed is named, and a server that did not
        # answer in time may have closed some.
        answers = [{"ok": True, "stopped": ["ada"], "failed": {"bo": "Claude Code could not stop it"}},
                   {"ok": True, "stopped": [], "failed": {"bo": "no such process"}}]
        with mock.patch.object(crewchat, "server_up", lambda config: True), \
                mock.patch.object(crewchat, "stop_server", lambda config: True), \
                mock.patch.object(crewchat, "owner_call", lambda path, body, **kw: answers.pop(0)):
            out = cli("stop")
            self.assertIn("closed its agents: ada.", out)
            self.assertIn("Could not close bo (Claude Code could not stop it): it keeps working", out)
            self.assertNotIn("Agents keep working", out)
            out = cli("stop")
            self.assertNotIn("No agent was running", out)
            self.assertIn("Could not close bo (no such process)", out)
        with mock.patch.object(crewchat, "server_up", lambda config: True), \
                mock.patch.object(crewchat, "stop_server", lambda config: True), \
                mock.patch.object(crewchat, "owner_call", lambda path, body, offline=None, **kw: offline()):
            out = cli("stop")  # it timed out, maybe part-way
            self.assertIn("did not say in time which agents it closed", out)
            self.assertNotIn("Agents keep working", out)

    def test_crewchat_stop_closes_the_agents_at_once_and_past_a_failure(self):
        # One thread per agent across projects: each may wait up to 10 s for its tool, inside one 60 s request. An
        # agent that fails (an OSError from its tool, or one that would not stop) is said, and the rest still stop.
        class Hub:
            def __init__(self, names):
                self.running = set(names)

            def session_pids(self):
                return []

            def running_agents(self):
                return sorted(self.running)

            def stop_agent(self, name):
                time.sleep(0.6)
                if name == "cy":
                    raise OSError("claude is missing")
                if name == "di":
                    raise crewchat.HubError("di: it did not stop; try again")
                self.running.discard(name)
                if name == "ed":
                    raise crewchat.HubError("ed is not running")  # it ended on its own meanwhile
        hubs = [Hub(["ada", "bo", "cy"]), Hub(["di", "ed", "ada"])]
        projects = mock.Mock(all=lambda: [mock.Mock(hub=h) for h in hubs])
        started = time.time()
        stopped, failed = crewchat.stop_all_agents(projects)
        self.assertLess(time.time() - started, 2.0)  # not 6 x 0.6 s
        self.assertEqual(stopped, ["ada", "bo"])  # ada in both projects, once; ed had ended
        self.assertEqual(failed, {"cy": "claude is missing", "di": "di: it did not stop; try again"})

    @unittest.skipIf(os.name == "nt", "a stand-in process with another name needs POSIX exec")
    def test_stop_does_not_touch_a_reused_process_number(self):
        _, name = self.agent()
        proc = self.stand_in(name)
        self.hub.agents[name]["session"]["started"] = "Mon Jan  1 00:00:00 2024"  # another process had that number
        self.assertIn("is not running", self.op(op="stop", agent=name)["error"])
        self.assertIsNone(proc.poll())
        other = subprocess.Popen(["sleep", "60"])  # the right start time, but not an agent's command
        self.addCleanup(other.kill)
        self.hub.agents[name]["session"].update(pid=other.pid, started=crewchat.process_table()[other.pid][1])
        self.assertIn("is not running", self.op(op="stop", agent=name)["error"])
        self.assertIsNone(other.poll())
        _, never = self.agent()
        self.assertIn("no session recorded for %s" % never, self.op(op="stop", agent=never)["error"])

    def test_an_agent_on_another_machine_is_changed_there(self):
        self.hub.set_remote("dev-x", "laptop", [{"agent": "far-away", "client": "cursor", "place": "x", "seen": 1,
                                                 "listen": "on"}], 1)
        try:
            for body in ({"op": "rename", "new": "near"}, {"op": "remove"}, {"op": "listen", "mode": "on"},
                         {"op": "stop"}, {"op": "reopen"}):
                self.assertIn("runs on laptop", self.op(agent="far-away", **body)["error"])
            self.assertEqual(next(r for r in self.hub.rows() if r["agent"] == "far-away")["listen"], "on")  # shown here
        finally:
            self.hub.drop_remote("dev-x")

    def test_the_page_needs_the_owner(self):
        _, name = self.agent()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            crewchat.post_json(self.base + "/api/agent", self.token, {"op": "remove", "agent": name})
        self.assertIn(caught.exception.code, (401, 403))
        self.assertIn(name, self.names())


class Profiles(Base):
    """An agent's picture and colour, its tool's brand and its machine's OS."""

    def test_every_known_client_has_a_brand_row(self):
        for client in crewchat.KNOWN_CLIENTS + ("antigravity", "grok"):
            row = crewchat.tool_info(client)
            self.assertRegex(row["colour"], r"^#[0-9a-f]{6}$", client)
            self.assertTrue(1 <= len(row["mark"]) <= 2, client)
        self.assertEqual(crewchat.tool_info("claude")["colour"], "#d97757")
        self.assertEqual(crewchat.tool_info("something-new"), crewchat.TOOLS_INFO["agent"])
        self.assertIn(crewchat.os_name(), ("mac", "windows", "linux", "other"))

    def op(self, **body):
        try:
            return crewchat.post_json(self.base + "/api/agent", self.owner, body)
        except urllib.error.HTTPError as e:
            return json.loads(e.read())

    def row(self, name):
        return next(r for r in self.hub.rows() if r["agent"] == name)

    def test_owner_sets_a_preset_and_a_colour(self):
        _, name = self.agent()
        self.assertTrue(self.row(name)["picture"].startswith("mix:"))  # a face of its own to begin with
        self.assertEqual(self.row(name)["colour"], "")
        self.assertEqual(self.row(name)["os"], crewchat.os_name())
        self.assertEqual(self.op(op="profile", agent=name, picture="preset:rocket", colour="#FF8800"), {"ok": True})
        self.assertEqual((self.row(name)["picture"], self.row(name)["colour"]), ("preset:rocket", "#ff8800"))
        self.assertEqual(self.op(op="profile", agent=name, colour=""), {"ok": True})  # back to the brand colour
        self.assertEqual((self.row(name)["picture"], self.row(name)["colour"]), ("preset:rocket", ""))
        self.assertIn("no picture preset", self.op(op="profile", agent=name, picture="preset:nope")["error"])
        self.assertIn("#rrggbb", self.op(op="profile", agent=name, colour="red")["error"])
        self.assertIn("#rrggbb", self.op(op="profile", agent=name, colour="#12345g")["error"])

    def test_the_built_in_3d_pictures(self):
        status, body, headers = self.raw("/pictures/bot.webp")  # not secret: no sign-in needed
        self.assertEqual((status, headers["Content-Type"], body[:4], body[8:12]), (200, "image/webp", b"RIFF", b"WEBP"))
        self.assertIn("max-age", headers["Cache-Control"])
        for bad in ("/pictures/nope.webp", "/pictures/bot.png", "/pictures/../files/x"):
            self.assertEqual(self.raw(bad)[0], 404, bad)
        session, name = self.agent()
        self.assertEqual(self.op(op="profile", agent=name, picture="art:owl"), {"ok": True})
        self.assertEqual(self.row(name)["picture"], "art:owl")
        self.assertIn("no picture called nope", self.op(op="profile", agent=name, picture="art:nope")["error"])
        self.assertIn("picture art:fox", session.text("hub_profile", picture="fox"))
        self.assertEqual(crewchat.clean_picture("art:../x"), "")
        self.assertEqual(set(crewchat.ART_DATA), set(crewchat.ART_PICTURES))
        self.assertEqual(self.hub.boot()["art"]["bot"], "Robot")
        self.assertIn("picture preset:robot", session.text("hub_profile", picture="robot"))  # the emoji, still

    def test_uploaded_picture_limits(self):
        _, name = self.agent()
        png = self.hub.add_file("me.png", b"\x89PNG" + b"0" * 100, "image/png")
        big = self.hub.add_file("big.png", b"\x89PNG" + b"0" * (512 * 1024), "image/png")
        pdf = self.hub.add_file("cv.pdf", b"%PDF-1.4", "application/pdf")
        self.assertEqual(self.op(op="profile", agent=name, picture="file:" + png["id"]), {"ok": True})
        self.assertEqual(self.row(name)["picture"], "file:" + png["id"])
        self.assertIn("512 KB", self.op(op="profile", agent=name, picture="file:" + big["id"])["error"])
        self.assertIn("PNG, JPEG or WebP", self.op(op="profile", agent=name, picture="file:" + pdf["id"])["error"])
        self.assertIn("no shared file", self.op(op="profile", agent=name, picture="file:" + "0" * 16)["error"])
        self.assertIn("no shared file", self.op(op="profile", agent=name, picture="file:../state")["error"])

    def test_profile_survives_rename_restart_and_travels(self):
        _, name = self.agent()
        self.op(op="profile", agent=name, picture="preset:star", colour="#123456")
        self.assertEqual(self.op(op="rename", agent=name, new="starry"), {"ok": True})
        self.assertEqual(self.row("starry")["picture"], "preset:star")
        again = crewchat.Hub(crewchat.Roster())  # what a restarted server loads
        self.assertEqual((again.agents["starry"]["picture"], again.agents["starry"]["colour"]), ("preset:star", "#123456"))
        mine = next(r for r in self.hub.local_state() if r["agent"] == "starry")
        self.assertEqual((mine["picture"], mine["colour"], mine["os"]), ("preset:star", "#123456", crewchat.os_name()))
        self.hub.set_remote("dev-x", "pc", [dict(mine, agent="faraway", os="windows"),
                                            dict(mine, agent="odd", picture="<img>", colour="red", os="beos")],
                            time.time())
        try:
            far = self.row("faraway")
            self.assertEqual((far["picture"], far["colour"], far["os"], far["remote"]),
                             ("preset:star", "#123456", "windows", True))
            odd = self.row("odd")  # whatever another machine sends, the page gets only what it can draw
            self.assertEqual((odd["picture"], odd["colour"], odd["os"]), ("", "", "other"))
            self.assertIn("runs on pc", self.op(op="profile", agent="faraway", colour="#000000")["error"])
        finally:
            self.hub.drop_remote("dev-x")
        self.op(op="remove", agent="starry")

    def test_agent_sets_its_own_profile(self):
        session, name = self.agent()
        self.assertIn("picture preset:camera", session.text("hub_profile", picture="camera", colour="#00AA00"))
        self.assertEqual((self.row(name)["picture"], self.row(name)["colour"]), ("preset:camera", "#00aa00"))
        self.assertIn("picture initials", session.text("hub_profile", picture="initials"))
        self.assertEqual((self.row(name)["picture"], self.row(name)["colour"]), ("", "#00aa00"))
        self.assertIn("the tool's colour", session.text("hub_profile", colour=""))
        with tempfile.TemporaryDirectory() as folder:
            picture = Path(folder) / "me.png"
            picture.write_bytes(b"\x89PNG" + b"0" * 64)
            with mock.patch.object(crewchat.Hub, "agent_folders", return_value=[Path(folder)]):
                session.text("hub_profile", file=str(picture))
            fid = self.row(name)["picture"].partition(":")[2]
            self.assertEqual(self.hub.file(fid)[0]["type"], "image/png")
            with mock.patch.object(crewchat.Hub, "agent_folders", return_value=[]):  # a folder from another machine
                answer = session.call("hub_profile", file=str(picture))
            self.assertTrue(answer["isError"])
            self.assertIn("another machine", answer["content"][0]["text"])
        self.assertTrue(session.call("hub_profile", picture="nope")["isError"])

    def test_agent_picture_is_checked_before_it_is_kept(self):
        session, _ = self.agent()
        before = set(os.listdir(self.hub.home / "files")) if (self.hub.home / "files").exists() else set()
        with tempfile.TemporaryDirectory() as folder:
            big, text = Path(folder) / "big.png", Path(folder) / "notes.txt"
            big.write_bytes(b"\x89PNG" + b"0" * (512 * 1024))
            text.write_text("not a picture")
            with mock.patch.object(crewchat.Hub, "agent_folders", return_value=[Path(folder)]):
                self.assertIn("512 KB", session.call("hub_profile", file=str(big))["content"][0]["text"])
                self.assertIn("PNG, JPEG or WebP", session.call("hub_profile", file=str(text))["content"][0]["text"])
        after = set(os.listdir(self.hub.home / "files")) if (self.hub.home / "files").exists() else set()
        self.assertEqual(after, before)  # nothing kept for a refused picture
        self.assertTrue(session.call("hub_profile", colour="blue")["isError"])


class PageAccessibility(unittest.TestCase):
    """Roadmap U1: no text on the chat page or its analytics view under 10px, and an agent's or a machine's state told
    by shape as well as colour."""

    def test_no_text_is_set_under_ten_pixels(self):
        small = []
        for name, text in (("CHAT_PAGE", crewchat.CHAT_PAGE), ("ANALYTICS_JS", crewchat.ANALYTICS_JS)):
            for m in re.finditer(r"font-size:\s*([\d.]+)px|font:\s*(?:\d+\s+)?([\d.]+)px|\"(?:\d+ )?([\d.]+)px ", text):
                size = float(next(g for g in m.groups() if g))
                if size < 10:
                    small.append("%s: %s" % (name, text[max(0, m.start() - 60):m.end()].split("\n")[-1]))
        self.assertEqual(small, [])

    def test_states_differ_by_shape_not_only_colour(self):
        css = crewchat.CHAT_PAGE + crewchat.ANALYTICS_JS
        rules = {sel: body for sel, body in re.findall(r"(?:^|(?<=\} ))(\.(?:ax-)?dot(?:\.[a-z]+)?) \{([^}]*)\}", css, re.M)}
        for away, here, lately in ((".dot", ".dot.on", ".dot.recent"), (".ax-dot", ".ax-dot.on", ".ax-dot.listen")):
            self.assertIn("background: transparent", rules[away])  # a faint ring
            self.assertIn("box-shadow: none", rules[here])  # filled
            self.assertRegex(rules[lately], r"inset 0 0 0 2\.5px")  # a thick ring
            self.assertNotIn("background", rules[lately])


class PageNavigation(unittest.TestCase):
    """Roadmap U2, W16: the sidebar's places are Chat, Needs you, Tasks and Activity; Commands, a reference, is in
    Settings › Advanced, and no ⋯ menu is left."""

    def test_the_places_are_chat_needs_tasks_and_activity_and_commands_is_in_settings(self):
        page = crewchat.CHAT_PAGE
        places = page[page.index('<div class="places"'):page.index('<h2 class="rail-head">')]
        self.assertEqual(re.findall(r'id="(view-[a-z]+)"', places), ["view-projects", "view-chat", "view-needs", "view-tasks", "view-analytics"])
        self.assertIn(">Activity</button>", places)
        self.assertNotIn("view-commands", page)
        self.assertNotIn('id="space-menu"', page)
        settings = page[page.index('<section id="settings"'):]
        self.assertIn('<div id="commands" hidden>', settings[:settings.index('<dialog')])
        self.assertIn('advanced: ["app", "Advanced"', page)
        self.assertIn('const SETTINGS_ALIAS = { commands: "advanced", roles: "crew" };', page)  # old names, one release
        self.assertIn('setAttribute("aria-current", "page")', page)  # showView marks the current place


class PhoneHeader(unittest.TestCase):
    """Roadmap U3: on a phone, one header bar, whose project name is the picker, and a count that opens Needs you."""

    def test_the_picker_is_in_the_header_and_needs_you_has_a_button(self):
        page = crewchat.CHAT_PAGE
        head = page[page.index('<header id="space-head">'):]
        head = head[:head.index("</header>")]
        self.assertIn('<select id="projects"', head)
        self.assertIn('id="needs-open"', head)
        self.assertNotIn('id="topbar"', page)
        self.assertIn('$("needs-open").addEventListener("click", () => { showView("needs"); $("needs-you").focus(); });', page)
        self.assertNotIn("needs you\" :", page)  # the picker counts, it does not say "needs you"


class StatusWords(unittest.TestCase):
    """Roadmap U4: the owner reads one word for each status: To do, In progress, Blocked, In review, Done."""

    def test_the_board_the_analytics_and_needs_you_say_the_same(self):
        page, ax = crewchat.CHAT_PAGE, crewchat.ANALYTICS_JS
        for words in (re.findall(r'\["(?:todo|doing|blocked|review|done)", "([^"]+)"', page),
                      re.findall(r'\["(?:todo|doing|blocked|review|done)", "([^"]+)"', ax)):
            self.assertEqual(words[:5], ["To do", "In progress", "Blocked", "In review", "Done"])  # COLUMNS, STAGES
        self.assertEqual(crewchat.NEED_KINDS["review"][1], "In review")
        self.assertIn('const updateWord = (status) => (status === "review" ? "in review"', page)
        self.assertNotIn('"pill " + m.status, "#" + m.task + " " + (state.statuses', page)  # the chat uses updateWord
        self.assertEqual(crewchat.STATUSES["review"], "ready for review")  # agents keep their words


class NewAgentOrder(unittest.TestCase):
    """Roadmap U8: New agent asks for what is required first (workspace, engine), then the optional identity and
    briefing; Start is on every step."""

    def test_the_steps_put_what_is_required_first(self):
        page = crewchat.CHAT_PAGE
        steps = page[page.index('<ol class="steps" id="l-steps">'):]
        self.assertEqual(re.findall(r"<li>([A-Za-z]+)", steps[:steps.index("</ol>")]),
                         ["Folder", "Engine", "Identity", "Briefing"])
        body = page[page.index('<div class="step-body">'):page.index('<div id="launch-note"')]
        order = [body.index(i) for i in ('id="l-folder"', 'id="l-tool"', 'id="l-name"', 'id="l-task"')]
        self.assertEqual(order, sorted(order))
        self.assertNotIn('$("l-start").hidden', page)  # Start shows on every step

    def test_the_dialog_fits_a_laptop_window(self):
        # At 1568x715 the Engine step overflowed a 480px-wide dialog, and the dialog itself scrolled its buttons away.
        page = crewchat.CHAT_PAGE
        self.assertIn("#launch { width: min(calc(100vw - 32px), 600px); max-height: calc(100dvh - 16px); }", page)
        # Smaller still: only the steps scroll, between the title and the buttons.
        self.assertIn("#launch[open], #launch-form { display: flex; flex-direction: column; min-height: 0; }", page)
        self.assertIn("#launch-form > .step-body { flex: 0 1 auto; min-height: 0; overflow-y: auto; }", page)
        form = page[page.index('<form id="launch-form"'):page.index("</form>", page.index('<form id="launch-form"'))]
        self.assertLess(form.index('<div class="step-body">'), form.index('<div class="buttons">'))
        self.assertEqual(form.count('class="buttons"'), 1)  # the buttons are the form's own child, out of the steps


class ProjectSettingsMenu(unittest.TestCase):
    """Roadmap U9, W16: the sidebar's foot opens Settings three ways (Project settings, Machines & devices, You), and
    Settings lists each section once: crewchat's, then the project's."""

    def test_the_sidebar_and_settings_name_each_section_once(self):
        page = crewchat.CHAT_PAGE
        foot = page[page.index('<div class="rail-foot"'):]
        foot = foot[:foot.index("</nav>")]
        # the foot opens Global settings only: Project settings is in the project's own menu above (owner, 17 Nov)
        self.assertEqual(re.findall(r'data-panel="([a-z]+)"', foot), ["general"])
        self.assertEqual(len(re.findall(r'data-panel="', page)), 1)
        self.assertIn('<button type="button" class="place" data-pm="settings">Project settings</button>', page)
        self.assertIn('app: ["general", "appearance", "notifications", "integrations", "machines", "phone", "privacy", "advanced", "about"]', page)  # no Agents or Accounts: their parts are in the project or Integrations
        self.assertIn('project: ["project", "crew", "safety", "routines", "folders"]', page)
        self.assertNotIn("Rules, skills and machines", page)
        self.assertIn('<details class="help" data-section="project"><summary>How it works</summary>', page)  # help, not a card


class Themes(unittest.TestCase):
    """W16: every theme the page offers is one set of CSS variables on [data-theme], with the same names in each."""

    def test_each_theme_is_one_variable_set(self):
        page = crewchat.CHAT_PAGE
        ids = re.findall(r'^  \["([a-z-]+)", "[^"]+", "[^"]+"\],$', page[page.index("const THEMES = ["):], re.M)[:12]
        self.assertEqual(ids[0], "system")
        names = None
        for theme in ids[1:]:
            if theme.startswith("classic"):
                self.assertIn('[data-theme="%s"]' % theme, page)
                continue
            block = re.search(r'\[data-theme="%s"\] \{([^}]*)\}' % theme, page).group(1)
            these = sorted(set(re.findall(r"(--[a-z0-9-]+):", block)))
            self.assertEqual(these, names or these, theme)  # the same variables, so no theme leaks another's
            names = these
        self.assertIn("--page", names)
        self.assertNotIn("fonts.googleapis", page)  # no font from anywhere but this machine

    def test_the_logo_is_back_and_the_default_themes_wear_its_blue(self):
        # 11 Nov: the logo and the name sit at the top of the sidebar again, and Paper and Ink (System, the default)
        # take their accent from the logo's blues.
        page = crewchat.CHAT_PAGE
        rail = page[page.index('<nav id="rail"'):]
        rail = rail[:rail.index('<details id="tab-add"')]
        self.assertIn('<a class="brand" href="/" title="crewchat">' + crewchat.LOGO_INLINE, rail)
        self.assertIn('<span class="wordmark">crew<b>chat</b></span>', rail)
        self.assertIn(".brand .logo { width: 30px; height: 30px;", page)
        logo = crewchat.LOGO_SVG.lower()
        # Ink's links are a lighter tint of the bubble's blue, to read on charcoal.
        for theme, keys in (("paper", ("--accent", "--link", "--rail-link", "--mention", "--msg-link", "--badge")),
                            ("ink", ("--accent", "--on-accent"))):
            block = re.search(r'\[data-theme="%s"\] \{([^}]*)\}' % theme, page).group(1)
            for key in keys:
                colour = re.search(r"%s: (#[0-9A-Fa-f]{6});" % key, block).group(1).lower()
                self.assertIn(colour, logo, (theme, key))  # a colour of the logo's
            self.assertIn("--head-font: var(--font);", block)  # one font for headings and text
        self.assertIn(".pillbox #send { border-radius: 999px; }", page)  # round, like the box it sits in
        # The + is Send's height, so both sit on one centre line, and drawn, not a font's "+" that sits low (12 Nov).
        plus = re.search(r"#plus > summary \{([^}]*)\}", page).group(1)
        self.assertIn("height: 38px;", plus)
        self.assertIn("font-size: 0;", plus)
        self.assertIn('#plus > summary::before { content: "";', page)
        # New agent's steps scroll; room inside them keeps a focused field's ring from being cut at the edges (12 Nov).
        self.assertIn("#launch-form > .step-body { padding: 5px; margin: -5px; }", page)
        login = crewchat.LOGIN_PAGE  # the sign-in page wears the same look: Paper and Ink, the logo's blue, no old glass
        self.assertIn("--page:#FAF9F5", login)
        self.assertIn("--page:#262624", login)
        self.assertIn("--accent:#2F5FD6", login)
        self.assertNotIn("backdrop-filter", login)


class ProjectIcons(unittest.TestCase):
    """11 Nov: each project gets an icon and a colour, from the words in its name or else a hash of it, not a letter."""

    def test_a_project_wears_an_icon_picked_from_its_name(self):
        import subprocess
        page = crewchat.CHAT_PAGE
        self.assertNotIn('letter(p.name)', page)
        self.assertNotIn('letter(state.project)', page)
        self.assertIn('paintPav($("space-avatar"), state.project);', page)
        self.assertIn('paintPav($("switch-avatar"), here.name);', page)
        start = page.index("const PROJECT_ICONS = {")
        code = page[start:page.index("function paintPav(")]
        if not shutil.which("node"):
            self.skipTest("needs node")
        js = code + ("console.log(JSON.stringify(['Notes App', 'my-android-app', 'Company website', 'zzqx', 'zzqx', 'Email guide', 'AI helper']"
                     ".map(projectLook)));")
        out = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)
        self.assertEqual([o["icon"] for o in out[:3]], ["note", "phone", "globe"])
        self.assertEqual(out[3], out[4])  # the same name, the same icon and colour
        self.assertIn(out[3]["colour"], ["--a%d" % i for i in range(8)])
        self.assertNotEqual(out[5]["icon"], "spark")  # "ai" in a word is not AI, "ui" in guide is not design
        self.assertEqual(out[6]["icon"], "spark")


class AgentBacklog(unittest.TestCase):
    """Roadmap U10: the count on an agent is what it has not read, muted, and never styled like the owner's counts."""

    def test_an_agents_count_is_muted_and_says_unread(self):
        page = crewchat.CHAT_PAGE
        rule = re.search(r"\.agent \.count \{([^}]*)\}", page).group(1)
        self.assertIn("background: transparent", rule)
        self.assertIn("color: var(--muted)", rule)
        self.assertIn('el("span", "count", a.unread + " unread")', page)
        self.assertIn('" has not read yet"', page)  # its tooltip


class FreshMessages(unittest.TestCase):
    """Roadmap U11: only messages that arrive while the page is open rise in; the history shows at once."""

    def test_the_animation_is_for_fresh_messages_only(self):
        page = crewchat.CHAT_PAGE
        self.assertNotIn(".msg, .sys { animation: rise", page)
        self.assertIn(".msg.fresh, .sys.fresh { animation: rise", page)
        self.assertIn("const fresh = !first && list.length <= FRESH_MAX;", page)
        self.assertIn(".msg.fresh, .sys.fresh, #working .dots::after { animation: none; }", page)  # reduced motion still wins


class AnalyticsAndNotesPolish(unittest.TestCase):
    """Roadmap U12: the task river spans the grid, the Learning panel labels its numbers, and the process notes' rule
    menu has short choices with the full rule written under it."""

    def test_labels_and_the_rivers_width(self):
        ax, page = crewchat.ANALYTICS_JS, crewchat.CHAT_PAGE
        self.assertIn('card("ax-river", "Tasks, from posted to done", true);', ax)
        self.assertIn('w.recorded + " recorded · " + w.approved + " approved · " + w.used + " used"', ax)
        menu = page[page.index('id="process-rule"'):page.index('id="process-rule-note"')]
        options = re.findall(r'<option value="(?:default|owner|lead)">([^<]+)</option>', menu)
        self.assertEqual(len(options), 3)
        self.assertTrue(all(len(o) <= 26 for o in options), options)  # short enough not to be cut off
        self.assertIn('id="process-rule-note"', page)


class ChatPage(Base):
    def test_pages_need_the_owner(self):
        status, _, headers = self.raw("/")
        self.assertEqual((status, headers["Location"]), (303, "/login"))
        self.assertEqual(self.raw("/api/poll")[0], 401)
        self.assertEqual(self.raw("/api/send", b"{}", {"Content-Type": "application/json"})[0], 401)
        status, body, headers = self.raw("/login")
        self.assertEqual(status, 200)
        self.assertIn(b"Sign in", body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Referrer-Policy"], "same-origin")

    def test_sign_in_codes(self):
        place = {"Authorization": "Bearer " + self.token}
        self.assertEqual(self.raw("/api/login-code", b"{}", place)[0], 403)
        self.assertEqual(self.raw("/api/invite", b"{}", place)[0], 403)
        self.assertEqual(self.raw("/api/admin", b'{"op":"remove","name":"x"}', place)[0], 403)
        self.assertEqual(self.raw("/login?code=WRONGCOD", headers={"X-Forwarded-For": "7.7.7.1"})[0], 401)
        code = crewchat.post_json(self.base + "/api/login-code", self.owner, {})["code"]
        typed = ("code=%s-%s" % (code[:4].lower(), code[4:])).encode()
        form = {"Content-Type": "application/x-www-form-urlencoded"}
        # Browsers send "Origin: null" when a page's referrer policy is no-referrer: must be refused.
        self.assertEqual(self.raw("/login", typed, dict(form, Origin="null"))[0], 403)
        status, _, headers = self.raw("/login", typed, dict(form, Origin=self.base))
        self.assertEqual(status, 303)
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertEqual(self.raw("/login?code=" + code, headers={"X-Forwarded-For": "7.7.7.2"})[0], 401)

    def test_analytics_are_the_owners_only(self):
        self.assertEqual(self.raw("/api/analytics", b"{}", {"Content-Type": "application/json"})[0], 401)
        self.assertEqual(self.raw("/api/analytics", b"{}", {"Content-Type": "application/json",
                                                           "Authorization": "Bearer " + self.token})[0], 403)
        report = crewchat.post_json(self.base + "/api/analytics", self.owner, {"days": 3})
        for key in ("messages", "hours", "days", "pairs", "tasks", "agents", "usage", "verdicts"):
            self.assertIn(key, report)
        status, body, _ = self.raw("/api/analytics", b'{"days": "x"}', {"Content-Type": "application/json",
                                                                       "Authorization": "Bearer " + self.owner})
        self.assertEqual((status, json.loads(body)["error"]), (400, "days is a number"))

    def test_the_page_is_compressed_and_a_reload_is_a_304(self):
        opener, _ = self.owner_browser()
        status, body, headers = self.raw("/", headers={"Accept-Encoding": "gzip, deflate, br"}, opener=opener)
        self.assertEqual((status, headers["Content-Encoding"]), (200, "gzip"))
        self.assertEqual(gzip.decompress(body), crewchat.PAGE_BYTES)
        self.assertLess(len(body), len(crewchat.PAGE_BYTES) / 3)
        self.assertEqual(headers["Vary"], "Accept-Encoding")
        etag = headers["ETag"]
        for k in ("Content-Security-Policy", "X-Frame-Options", "X-Content-Type-Options", "Referrer-Policy"):
            self.assertEqual(headers[k], crewchat.PAGE_HEADERS[k], k)  # the page's protections, unchanged
        # A reload: the browser asks with the ETag it kept, and gets nothing but headers.
        for asked in (etag, "W/" + etag, '"other", ' + etag):
            status, body, headers = self.raw("/", headers={"If-None-Match": asked, "Accept-Encoding": "gzip"}, opener=opener)
            self.assertEqual((status, body), (304, b""), asked)
            self.assertEqual(headers["ETag"], etag)
            self.assertEqual(headers["Content-Security-Policy"], crewchat.PAGE_HEADERS["Content-Security-Policy"])
            self.assertLess(len(str(headers)), 1024)  # the whole answer: under 1 KB
        status, body, _ = self.raw("/", headers={"If-None-Match": '"an older page"'}, opener=opener)
        self.assertEqual((status, body), (200, crewchat.PAGE_BYTES))  # changed: the whole page, uncompressed here
        for refused in ("identity", "gzip;q=0", "br"):
            self.assertNotIn("Content-Encoding", self.raw("/", headers={"Accept-Encoding": refused}, opener=opener)[2], refused)
        # Signed out, a kept page's ETag gets the sign-in page, never a 304.
        status, _, headers = self.raw("/", headers={"If-None-Match": etag})
        self.assertEqual((status, headers["Location"]), (303, "/login"))
        self.assertEqual(json.loads(self.raw("/api/poll?after=-1&v=-1&wait=0", opener=opener)[1]) and
                         self.raw("/api/boot", opener=opener)[2]["Cache-Control"], "no-store")  # the chat's data never kept

    def test_the_analytics_view_is_its_own_script_fetched_when_opened(self):
        # Roadmap X2: the view's code is not in the page every phone loads; the page fetches it when it opens.
        self.assertIn('id="view-analytics"', crewchat.CHAT_PAGE)
        self.assertNotIn("window.crewAnalytics = {", crewchat.CHAT_PAGE)
        self.assertIn("window.crewAnalytics = {", crewchat.ANALYTICS_JS)
        self.assertEqual(self.raw("/analytics.js")[0], 401)  # signed out
        self.assertEqual(self.raw("/analytics.js", headers={"Authorization": "Bearer " + self.token})[0], 401)  # an agent
        opener, _ = self.owner_browser()
        status, body, headers = self.raw("/analytics.js", headers={"Accept-Encoding": "gzip"}, opener=opener)
        self.assertEqual((status, headers["Content-Type"], headers["Content-Encoding"]),
                         (200, "text/javascript; charset=utf-8", "gzip"))
        self.assertEqual(gzip.decompress(body), crewchat.ANALYTICS_BYTES)
        self.assertEqual(headers["Content-Security-Policy"], crewchat.PAGE_HEADERS["Content-Security-Policy"])
        status, body, _ = self.raw("/analytics.js", headers={"If-None-Match": headers["ETag"]}, opener=opener)
        self.assertEqual((status, body), (304, b""))  # opened again: the browser's copy

    def test_the_analytics_report_has_the_days_messages_and_the_files_edited(self):
        session, name = self.agent()
        task = self.say("all", "Polish the analytics", "task")
        session.call("hub_task", action="take", id=task)
        with self.hub.lock:
            for path in ("app/a.kt", "app/a.kt", "app/b.kt"):
                self.hub.events.add_edit(name, path, task)
            self.hub.events.add_edit("someone-else", "app/b.kt", task)
            self.hub.events.add_edit("someone-else", "app/b.kt", task)
        report = crewchat.post_json(self.base + "/api/analytics", self.owner, {"days": 1})
        rows = [r for r in report["stream"] if r[4] == task]
        self.assertEqual([r[3] for r in rows[:2]], ["task", "take"])  # [ts, from, to, kind, task, status]
        self.assertEqual(rows[0][1:3], ["Owner", "all"])
        self.assertFalse(any(r[3] in ("event", "plan") for r in report["stream"]))
        files = {f["path"]: f for f in report["edits"]}
        self.assertEqual((files["app/a.kt"]["edits"], files["app/a.kt"]["top"]), (2, name))
        self.assertEqual(files["app/b.kt"]["top"], "someone-else")  # who edited it most
        with mock.patch.object(crewchat.Hub, "edits_on", lambda hub: False):
            self.assertEqual(crewchat.post_json(self.base + "/api/analytics", self.owner, {"days": 1})["edits"], [])

    def test_chat_page_and_api(self):
        session, name = self.agent("cursor")
        opener, _ = self.owner_browser()
        status, body, headers = self.raw("/", opener=opener)
        self.assertEqual(status, 200)
        self.assertIn(b"<title>crewchat</title>", body)
        self.assertIn(b'id="panel"', body)  # the page from 0.14
        self.assertEqual(headers["Cache-Control"], "private, no-cache")  # kept, and asked about each time
        self.assertEqual(self.raw("/classic", opener=opener)[0], 404)  # the page before 0.14 is gone
        send = lambda obj, extra=None: self.raw(  # noqa: E731
            "/api/send", json.dumps(obj).encode(), dict({"Content-Type": "application/json"}, **(extra or {})), opener)
        self.assertEqual(send({"to": "all", "text": "x"}, {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(send({"to": "all", "text": "hello agents"}, {"Origin": self.base})[0], 200)
        for bad in ({"to": "Owner", "text": "x"}, {"to": "all", "text": " "}, {"to": "all"}, {"to": "ghost", "text": "x"}):
            self.assertEqual(send(bad)[0], 400, bad)
        data = json.loads(self.raw("/api/poll?after=-1&v=-1&wait=0", opener=opener)[1])
        self.assertEqual(data["project"], "Demo")
        row = next(a for a in data["agents"] if a["agent"] == name)
        self.assertEqual((row["client"], row["place"], row["online"]), ("cursor", self.place, True))
        self.assertEqual(data["messages"][-1]["text"], "hello agents")
        self.assertTrue(any(m["kind"] == "event" and name + " joined" in m["text"] for m in data["messages"]))
        got = {}
        path = "/api/poll?after=%d&v=%d&wait=20" % (data["messages"][-1]["seq"], data["version"])
        thread = threading.Thread(target=lambda: got.update(d=json.loads(self.raw(path, opener=opener)[1])))
        start = time.time()
        thread.start()
        time.sleep(0.5)
        session.call("hub_send", to="Owner", text="a question")
        thread.join()
        self.assertLess(time.time() - start, 4)
        self.assertEqual(got["d"]["messages"][0]["text"], "a question")

    def test_a_page_that_was_away_catches_up_on_everything(self):
        opener, _ = self.owner_browser()
        poll = lambda after, v=-1: json.loads(self.raw(  # noqa: E731
            "/api/poll?after=%d&v=%d&wait=0" % (after, v), opener=opener)[1])
        mark = poll(-1)
        after = mark["messages"][-1]["seq"] if mark["messages"] else 0
        for i in range(crewchat.PAGE_BATCH + 20):
            self.say("all", "busy night %d" % i)
        latest = poll(-1)  # a new page: the latest messages only
        self.assertEqual(len(latest["messages"]), crewchat.PAGE_BATCH)
        self.assertFalse(latest["more"])
        seen = []
        first = poll(after, mark["version"])  # the page that was open: everything, in order
        self.assertTrue(first["more"])
        seen += first["messages"]
        rest = poll(seen[-1]["seq"], first["version"])
        self.assertFalse(rest["more"])
        seen += rest["messages"]
        self.assertEqual([m["text"] for m in seen], ["busy night %d" % i for i in range(crewchat.PAGE_BATCH + 20)])

    def test_ui_print_gives_a_code(self):
        self.assertRegex(cli("ui", "--print"), r"Sign-in code .*: [A-Z2-9]{4}-[A-Z2-9]{4}")

    def test_installs_as_an_app(self):
        status, body, headers = self.raw("/manifest.webmanifest")
        self.assertEqual((status, headers["Content-Type"]), (200, "application/manifest+json"))
        manifest = json.loads(body)
        self.assertEqual((manifest["display"], manifest["start_url"]), ("standalone", "/"))
        self.assertNotIn("Demo", body.decode())  # served without signing in: no project name
        for size in (192, 512):
            status, body, headers = self.raw("/icon-%d.png" % size)
            self.assertEqual((status, headers["Content-Type"], body[:8]), (200, "image/png", b"\x89PNG\r\n\x1a\n"))
            self.assertEqual(int.from_bytes(body[16:20], "big"), size)
        status, body, headers = self.raw("/sw.js")
        self.assertEqual(status, 200)
        self.assertIn(b"addEventListener('fetch'", body)
        _, login, headers = self.raw("/login")
        self.assertIn(b'<link rel="manifest" href="/manifest.webmanifest">', login)
        self.assertIn("manifest-src 'self'", headers["Content-Security-Policy"])
        opener, _ = self.owner_browser()
        self.assertIn(b"serviceWorker.register('/sw.js')", self.raw("/", opener=opener)[1])


class WebPush(Base):
    ENDPOINT = "https://fcm.googleapis.com/fcm/send/abc123"

    def projects(self):
        return self.server.RequestHandlerClass.projects

    def store(self):
        return crewchat.push_store(self.projects().base)

    def tearDown(self):
        store = self.store()
        for sub in list(store.subs):
            store.forget(sub["endpoint"])

    def test_the_key_and_its_signatures(self):
        d = 0xC9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721  # RFC 6979, A.2.5
        self.assertEqual(crewchat.p256_public(d).hex(),
                         "04" "60fed4ba255a9d31c961eb74c6356d68c049b8923b61fa6ce669622e60f29fb6"
                         "7903fe1008b8bc99a41ae9e95628bc64f2f1b20c2d7e9f5177a3c294d4462299")
        message = b"a push"
        sig = crewchat.es256_sign(d, message)
        r, s_ = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:], "big")
        n, z = crewchat._N, int.from_bytes(crewchat.hashlib.sha256(message).digest(), "big")
        w = pow(s_, n - 2, n)  # ECDSA verification, by the book
        point = crewchat._p256_add(crewchat._p256_mul(z * w % n), crewchat._p256_mul(r * w % n, crewchat._p256_mul(d)))
        self.assertEqual(point[0] % n, r)
        self.assertNotEqual(crewchat.es256_sign(d, message), sig)  # a fresh nonce each time
        try:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import ec, utils
        except ImportError:
            return
        key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), crewchat.p256_public(d))
        key.verify(utils.encode_dss_signature(r, s_), message, ec.ECDSA(hashes.SHA256()))

    def test_only_push_services_are_posted_to(self):
        for ok in (self.ENDPOINT, "https://web.push.apple.com/QGuA", "https://updates.push.services.mozilla.com/wpush/v2/x",
                   "https://wns2-par02p.notify.windows.com/w/?token=x"):
            self.assertTrue(crewchat.push_host_ok(ok), ok)
        for bad in ("http://fcm.googleapis.com/x", "https://evil.example/x", "https://fcm.googleapis.com.evil.example/x",
                    "https://127.0.0.1/x", "https://user@fcm.googleapis.com/x", "https://fcm.googleapis.com:8443/x",
                    "https://push.apple.com.evil/x", "https://evilpush.apple.com/x", "https://x/" + "a" * 1000, "", None):
            self.assertFalse(crewchat.push_host_ok(bad or ""), bad)

    def test_subscriptions_and_the_vapid_headers(self):
        store = self.store()
        key = store.public_key()
        self.assertEqual(len(base64.urlsafe_b64decode(key + "=")), 65)
        if os.name != "nt":  # Windows has no such modes: the file is in the user's own profile
            self.assertEqual(oct(os.stat(store.path).st_mode & 0o777), "0o600")
        store.set_mode(self.ENDPOINT, "demo", "all")
        store.set_mode(self.ENDPOINT, "other", "mentions")
        self.assertEqual(store.wanted("demo"), [(self.ENDPOINT, "all")])
        self.assertEqual(crewchat.PushStore(store.path.parent).subs, store.subs)  # kept on disk
        self.assertEqual(crewchat.PushStore(store.path.parent).public_key(), key)
        with self.assertRaises(crewchat.HubError):
            store.set_mode("https://evil.example/x", "demo", "all")
        with self.assertRaises(crewchat.HubError):
            store.set_mode(self.ENDPOINT, "demo", "loud")
        headers = store.headers(self.ENDPOINT)
        jwt, k = re.match(r"vapid t=([^,]+), k=(.+)$", headers["Authorization"]).groups()
        self.assertEqual(k, key)
        head, claims, sig = jwt.split(".")
        claims = json.loads(base64.urlsafe_b64decode(claims + "=" * (-len(claims) % 4)))
        self.assertEqual(claims["aud"], "https://fcm.googleapis.com")
        self.assertGreater(claims["exp"], time.time())
        self.assertEqual(len(base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))), 64)
        store.set_mode(self.ENDPOINT, "demo", "off")
        store.set_mode(self.ENDPOINT, "other", "off")
        self.assertEqual(store.subs, [])  # no project left: the browser is forgotten

    def test_what_is_new_is_pushed_only_while_no_page_is_open(self):
        store, seen, sent = self.store(), {}, []
        store.set_mode(self.ENDPOINT, self.projects().primary.id, "all")
        send = lambda st, endpoint: sent.append(endpoint)  # noqa: E731
        self.hub.page_seen = 0
        session, name = self.agent()
        session.call("hub_send", to="Owner", text="first, before the server looked")
        crewchat.push_round(self.projects(), store, seen, send)
        self.assertEqual((sent, store.take(self.ENDPOINT)), ([], []))  # what was there already is not news
        session.call("hub_send", to="Owner", text="Can I merge?", ask=True)
        crewchat.push_round(self.projects(), store, seen, send)
        time.sleep(0.2)
        self.assertEqual(sent, [self.ENDPOINT])
        notes = store.take(self.ENDPOINT)
        self.assertEqual((notes[0]["title"], notes[0]["body"], notes[0]["url"]), ("Demo: Question for you · " + name, "Can I merge?", "/"))
        self.assertEqual(store.take(self.ENDPOINT), [])  # handed over once
        self.hub.page_seen = time.time()  # a page is open: it notifies by itself
        session.call("hub_send", to="Owner", text="Merged")
        crewchat.push_round(self.projects(), store, seen, send)
        self.assertEqual(sent, [self.ENDPOINT])
        self.hub.page_seen = 0
        store.set_mode(self.ENDPOINT, self.projects().primary.id, "mentions")
        tid = self.say("all", "Ship it", "task")
        session.call("hub_task", action="take", id=tid)
        session.call("hub_task", action="update", id=tid, status="blocked", note="Which branch?")
        crewchat.push_round(self.projects(), store, seen, send)
        self.assertEqual(sent, [self.ENDPOINT])  # messages only: a blocked task is not pushed

    def test_push_keys_on_the_items_key(self):
        # R8: push and the page share the server's key, so an item seen once is not pushed again.
        store, seen, sent = self.store(), {}, []
        store.set_mode(self.ENDPOINT, self.projects().primary.id, "all")
        send = lambda st, endpoint: sent.append(endpoint)  # noqa: E731
        self.hub.page_seen = 0
        item = {"kind": "message", "ref": "m1", "key": "message:m1", "id": "other-id", "agent": "luca", "text": "hi", "ts": 1}
        with mock.patch.object(type(self.hub), "needs_you", lambda hub: [item]):
            crewchat.push_round(self.projects(), store, seen, send)
            item = dict(item, id="changed-id", ts=2)  # the same item, its other fields moved on
            crewchat.push_round(self.projects(), store, seen, send)
        self.assertEqual(sent, [])
        self.assertEqual(seen[self.projects().primary.id], {"message:m1"})
        notice = crewchat.push_notice(self.projects().primary, dict(item, kind="review", task="", text="x"))
        self.assertIn("In review", notice["title"])

    def test_a_subscription_the_service_dropped_is_forgotten(self):
        store = self.store()
        store.set_mode(self.ENDPOINT, "demo", "all")
        gone = urllib.error.HTTPError(self.ENDPOINT, 410, "Gone", {}, io.BytesIO(b""))
        with mock.patch.object(crewchat.urllib.request, "urlopen", side_effect=gone):
            self.assertEqual(crewchat.send_push(store, self.ENDPOINT), 410)
        self.assertEqual(store.subs, [])

    def test_the_page_subscribes_and_its_service_worker_fetches(self):
        opener, _ = self.owner_browser()
        post = lambda body, extra=None: self.raw("/api/push", json.dumps(body).encode(), dict(  # noqa: E731
            {"Content-Type": "application/json", "Origin": self.base}, **(extra or {})), opener)
        self.assertEqual(self.raw("/api/push", b'{"op":"key"}', {"Content-Type": "application/json"})[0], 401)
        self.assertEqual(post({"op": "key"}, {"Origin": "https://evil.example"})[0], 403)
        status, body, _ = post({"op": "key"})
        self.assertEqual((status, json.loads(body)["key"]), (200, self.store().public_key()))
        self.assertEqual(post({"op": "mode", "endpoint": "https://evil.example/x", "mode": "all"})[0], 400)
        self.assertEqual(post({"op": "mode", "endpoint": self.ENDPOINT, "mode": "all"})[0], 200)
        self.assertEqual(self.store().wanted(self.projects().primary.id), [(self.ENDPOINT, "all")])
        self.store().queue(self.ENDPOINT, {"title": "t", "body": "b", "tag": "x", "url": "/"})
        self.assertEqual(json.loads(post({"op": "notices", "endpoint": self.ENDPOINT})[1])["notices"][0]["title"], "t")
        self.assertEqual(json.loads(post({"op": "notices", "endpoint": self.ENDPOINT})[1])["notices"], [])
        self.assertEqual(post({"op": "nope"})[0], 400)
        _, sw, _ = self.raw("/sw.js")
        self.assertIn(b"addEventListener('push'", sw)
        self.assertIn(b"addEventListener('notificationclick'", sw)

    def test_a_notification_can_be_answered_and_sets_the_badge(self):
        # W4: a permission prompt's notification has Allow and Deny, a question's has Reply; the service worker answers
        # the item by its key, in its project, and sets the app icon's badge to what needs the owner.
        primary = self.projects().primary
        asks = crewchat.push_notice(primary, {"kind": "permission", "key": "permission:a1", "agent": "luca", "tool": "Bash",
                                              "text": "git push", "plan": False})
        self.assertEqual(asks["actions"], [{"action": "allow", "title": "Allow"}, {"action": "deny", "title": "Deny"}])
        self.assertEqual((asks["key"], asks["chat"]), ("permission:a1", ""))
        plan = crewchat.push_notice(primary, {"kind": "permission", "key": "permission:a2", "agent": "luca",
                                              "tool": "ExitPlanMode", "text": "## Plan", "plan": True})
        self.assertEqual(plan["actions"], [])  # a plan is read on the page
        blocked = crewchat.push_notice(primary, {"kind": "blocked", "key": "blocked:t1", "task": "t1", "title": "Ship",
                                                 "agent": "luca", "text": "Which branch?"})
        self.assertEqual(blocked["actions"], [{"action": "reply", "title": "Reply", "type": "text"}])
        opener, _ = self.owner_browser()
        status, body, _ = self.raw("/api/push", json.dumps({"op": "notices", "endpoint": self.ENDPOINT}).encode(),
                                   {"Content-Type": "application/json", "Origin": self.base}, opener)
        self.assertEqual(json.loads(body)["badge"], sum(len(p.hub.needs_you()) for p in self.projects().all()))
        _, sw, _ = self.raw("/sw.js")
        for part in (b"setAppBadge", b"e.action", b"'/api/needs'", b"e.reply"):
            self.assertIn(part, sw)


class CommandsTab(Base):
    """The chat page's connection line and Commands tab (help for every command, and Run for the safe ones)."""

    def run_cmd(self, body, headers=None):
        status, raw, _ = self.raw("/api/run", json.dumps(body).encode(),
                                  dict({"Authorization": "Bearer " + self.owner, "Content-Type": "application/json"},
                                       **(headers or {})))
        return status, json.loads(raw)

    def test_poll_says_how_machines_are_linked(self):
        opener, _ = self.owner_browser()
        data = json.loads(self.raw("/api/poll?after=-1&v=-1&wait=0", opener=opener)[1])
        self.assertEqual(data["link"], {"via": "none"})
        self.assertIn(b'id="link"', self.raw("/", opener=opener)[1])

    def test_link_info_says_what_the_link_reports(self):
        class Link:
            def link_status(self):
                return {"via": "cloud", "me": "mac", "online": True, "approved": True}

        class Coordinated(Link):
            def coordinator(self):
                return {"name": "laptop", "me": False, "term": 3, "preferred": ""}

        hub = lambda sync: type("Hub", (), {"sync": sync})()  # noqa: E731
        self.assertEqual(crewchat.link_info(hub(Link()), shared=False),  # a link that cannot say who coordinates
                         {"via": "cloud", "me": "mac", "online": True, "approved": True, "shared": False,
                          "coordinator": None})
        self.assertEqual(crewchat.link_info(hub(Coordinated()))["coordinator"],
                         {"name": "laptop", "me": False, "term": 3, "preferred": ""})
        self.assertEqual(crewchat.link_info(hub(None)), {"via": "none"})

    def test_every_runnable_command_parses(self):
        """A Run button that no longer matches the command line would only fail when tapped."""
        parser = crewchat.build_parser()
        for usage, spec in crewchat.RUNNABLE.items():
            argv = [("laptop" if part == "{NAME}" else "6b56 fa65 a4af 57c4" if part.startswith("{") else part)
                    for part in spec["argv"]] + (["--chat", "demo"] if spec.get("chat") else [])
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    parser.parse_args(argv)
                except SystemExit:
                    self.fail("the page runs `crewchat %s`, which does not parse" % " ".join(argv))

    def test_commands_need_the_owner(self):
        self.assertEqual(self.raw("/api/commands")[0], 401)
        self.assertEqual(self.raw("/api/commands", headers={"Authorization": "Bearer " + self.token})[0], 401)
        place = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"}
        self.assertEqual(self.raw("/api/run", b'{"usage": "projects"}', place)[0], 403)
        opener, _ = self.owner_browser()
        status, body, _ = self.raw("/api/commands", opener=opener)
        self.assertEqual(status, 200)
        groups = json.loads(body)["groups"]
        rows = {c["usage"]: c for g in groups for c in g["commands"]}
        self.assertTrue(rows["projects"]["run"])
        self.assertFalse(rows["uninstall [--yes] [--purge]"]["run"])
        self.assertIn("service install [--keep-awake]", rows)
        approve = rows["cloud approve NAME --fingerprint FINGERPRINT"]
        self.assertEqual((approve["needs"], approve["ask"]), (["NAME", "FINGERPRINT"], True))
        # The page refuses a cross-site POST, like every other owner action.
        status, _, _ = self.raw("/api/run", b'{"usage": "projects"}',
                                {"Content-Type": "application/json", "Origin": "https://evil.example"}, opener)
        self.assertEqual(status, 403)

    def test_every_command_action_and_option_is_listed(self):
        """The Commands tab lists everything the command line takes: each command, each of its actions
        (service install, tasks assign, ...) and each of its options (--keep-awake, ...)."""
        usages = [c["usage"] for g in crewchat.commands_catalog() for c in g["commands"]]
        words = lambda usage: set(re.split(r"[\s\[\]|]+", usage)) - {""}  # noqa: E731
        sub = next(a for a in crewchat.build_parser()._actions if isinstance(a, argparse._SubParsersAction))
        missing = []
        for name, parser in sub.choices.items():
            if name in ("hook", "browser-proxy", "device-proxy"):  # run by the hooks, and by agent tools as their browser or device
                continue
            mine = [words(u) for u in usages if u.split()[0] == name]
            if not mine:
                missing.append(name)
                continue
            for action in parser._actions:
                if action.help == argparse.SUPPRESS or isinstance(action, argparse._HelpAction):
                    continue
                wanted = [o for o in action.option_strings if o.startswith("--") and o != "--chat"][:1]
                if not action.option_strings and action.choices:
                    wanted = list(action.choices)
                for word in wanted:
                    if not any(word in w for w in mine):
                        missing.append("%s %s" % (name, word))
        self.assertEqual(missing, [], "add these to COMMANDS in crewchat.py")

    def test_runs_listed_commands_only(self):
        status, answer = self.run_cmd({"usage": "projects"})
        self.assertEqual((status, answer["code"]), (200, 0), answer)
        self.assertIn("Demo", answer["output"])
        status, answer = self.run_cmd({"usage": "tasks [list]"})
        self.assertEqual((status, answer["code"]), (200, 0), answer)
        for usage in ("uninstall [--yes] [--purge]", "stop", "uninstall", "projects; rm -rf /", ""):
            self.assertEqual(self.run_cmd({"usage": usage})[0], 400, usage)
        approve = "cloud approve NAME --fingerprint FINGERPRINT"
        for name in ("", "--purge", "../x", "a" * 80, "x\ny"):
            values = {"NAME": name, "FINGERPRINT": "6b56 fa65 a4af 57c4"}
            self.assertEqual(self.run_cmd({"usage": approve, "values": values})[0], 400, name)
        self.assertEqual(self.run_cmd({"usage": approve, "values": {"NAME": "laptop"}})[0], 400)  # no fingerprint
        argv = []
        real = crewchat.subprocess.run
        try:  # the page passes both values, so the command never stops to ask at a terminal it lacks
            crewchat.subprocess.run = lambda cmd, **kw: argv.extend(cmd) or type("Done", (), {
                "stdout": "", "stderr": "", "returncode": 0})()
            crewchat.run_listed_command(approve, {"NAME": "laptop", "FINGERPRINT": "6b56 fa65 a4af 57c4"})
        finally:
            crewchat.subprocess.run = real
        self.assertEqual(argv[-5:], ["cloud", "approve", "laptop", "--fingerprint", "6b56 fa65 a4af 57c4"])

class Joining(Base):
    def project(self):
        folder = Path(tempfile.mkdtemp(prefix="project-", dir=TMP))
        subprocess.run(["git", "init", "-q", str(folder)], check=False)
        return folder

    def invite(self, *extra):
        out = cli("invite", "--local", *extra)
        line = next(l.strip() for l in out.splitlines() if l.strip().startswith("crewchat join"))
        return line.split()[1:]  # the join arguments

    def test_one_join_connects_claude_code_and_cursor(self):
        folder = self.project()
        (folder / ".claude").mkdir()
        (folder / ".claude" / "settings.local.json").write_text(json.dumps(
            {"permissions": {"allow": ["Bash(ls:*)"]},
             "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}))
        out = cli(*self.invite("--place", "Work Laptop"), "--project", str(folder))
        self.assertIn('joined the crewchat for Demo as "work-laptop"', out)
        token = crewchat.read_token("work-laptop")
        mcp = json.loads((folder / ".mcp.json").read_text())["mcpServers"]["crewchat"]
        self.assertEqual((mcp["url"], mcp["headers"]["Authorization"]), (self.mcp, "Bearer " + token))
        cursor = json.loads((folder / ".cursor" / "mcp.json").read_text())["mcpServers"]["crewchat"]
        self.assertEqual(cursor["headers"]["Authorization"], "Bearer " + token)
        settings = json.loads((folder / ".claude" / "settings.local.json").read_text())
        allow = settings["permissions"]["allow"]
        self.assertEqual(allow[0], "Bash(ls:*)")  # the user's own rule stays
        self.assertIn("mcp__crewchat__hub_inbox", allow)  # crewchat's tools do not ask...
        self.assertIn("mcp__crewchat__hub_send", allow)  # ...sending too: a file comes only from its own folder (#33)
        self.assertIn("mcp__crewchat__hub_code", allow)  # the lead's own tool too: it only reads
        self.assertNotIn("mcp__crewchat", allow)
        cursor_permissions = json.loads((folder / ".cursor" / "cli.json").read_text())["permissions"]
        cursor_allow = cursor_permissions["allow"]
        self.assertEqual(cursor_permissions["deny"], [])  # Cursor's agent refuses a cli.json without one
        self.assertIn("Mcp(crewchat:hub_task)", cursor_allow)
        self.assertIn("Mcp(crewchat:hub_send)", cursor_allow)
        self.assertIn("Mcp(crewchat:hub_code)", cursor_allow)
        self.assertIn("crewchat", settings["enabledMcpjsonServers"])
        stop = [h["command"] for g in settings["hooks"]["Stop"] for h in g["hooks"]]
        self.assertEqual(stop[0], "echo mine")
        self.assertTrue(stop[1].endswith(" hook claude stop") and "crewchat.py" in stop[1])
        self.assertEqual(len(settings["hooks"]["UserPromptSubmit"]), 1)
        hooks = json.loads((folder / ".cursor" / "hooks.json").read_text())
        self.assertTrue(hooks["hooks"]["stop"][0]["command"].endswith(" hook cursor stop"))
        self.assertIn(".mcp.json", (folder / ".git" / "info" / "exclude").read_text())
        self.assertNotIn("do not commit", out)
        # Two sessions opened in that folder are two agents, each with a first name of its own.
        one, two = Session(self.base, token), Session(self.base, token, "cursor")
        self.assertEqual({one.me in crewchat.AGENT_NAMES, two.me in crewchat.AGENT_NAMES, one.me != two.me}, {True})
        # Joining again replaces our hooks instead of stacking them, and gets its own place.
        cli(*self.invite("--place", "Work Laptop"), "--project", str(folder))
        settings = json.loads((folder / ".claude" / "settings.local.json").read_text())
        self.assertEqual(len(settings["hooks"]["Stop"]), 2)
        self.assertEqual(settings["enabledMcpjsonServers"].count("crewchat"), 1)
        self.assertIn("work-laptop-2", crewchat.list_places())

    def test_join_for_one_client_only(self):
        folder = self.project()
        cli(*self.invite("--client", "cursor"), "--project", str(folder))
        self.assertTrue((folder / ".cursor" / "mcp.json").exists())
        self.assertFalse((folder / ".mcp.json").exists())

    def test_the_place_defaults_to_the_machine_name(self):
        folder = self.project()
        out = cli(*self.invite(), "--project", str(folder))
        place = re.search(r'as "([^"]+)"', out).group(1)
        self.assertRegex(place, r"^[a-z][a-z0-9-]{0,15}$")

    def test_generic_join_prints_settings(self):
        out = cli(*self.invite("--client", "generic", "--place", "other-tool"))
        self.assertIn('"url": "%s"' % self.mcp, out)
        self.assertIn(crewchat.read_token("other-tool"), out)

    def test_start_connects_the_folder_once(self):
        folder = self.project().resolve()
        out = cli("start", str(folder), "--no-open", "--no-service", "--place", "Desk", "--chat", "Demo")
        self.assertIn("The server is running at %s." % self.base, out)
        self.assertRegex(out, r'- (Connected|Added) %s to the project "Demo", as "desk"\.' % re.escape(str(folder)))
        token = crewchat.read_token("desk")
        mcp = json.loads((folder / ".mcp.json").read_text())["mcpServers"]["crewchat"]
        self.assertEqual(mcp["headers"]["Authorization"], "Bearer " + token)
        self.assertTrue((folder / ".cursor" / "hooks.json").exists())
        places = crewchat.list_places()
        out = cli("start", str(folder), "--no-open", "--no-service")
        self.assertIn('%s is connected to the project "Demo", as "desk".' % folder, out)
        self.assertEqual(crewchat.list_places(), places)  # no second place for the same folder

    def test_a_new_folder_is_a_new_project_and_projects_are_apart(self):
        first = self.project().resolve()
        cli("start", str(first), "--no-open", "--no-service", "--chat", "demo")
        app = (Path(tempfile.mkdtemp(dir=TMP)) / "notes-app").resolve()
        app.mkdir()
        out = cli("start", str(app), "--no-open", "--no-service")
        self.assertIn('Created the project "notes-app"', out)
        self.assertIn('- Connected %s to the project "notes-app", as ' % app, out)
        entry = crewchat.find_chat("notes-app")
        self.assertEqual(entry["root"], crewchat.home() / "projects" / "notes-app")
        self.assertEqual(crewchat.load_config(entry["root"])["join"], "anyone")  # as the host's (the tests' setup)
        place = crewchat.list_places(entry["root"])[0]
        # Its own token, even where the first project has a folder by the same name.
        self.assertNotEqual(crewchat.read_token(place, entry["root"]), crewchat.read_token(place))
        # Another folder joins it with --chat, named after itself.
        site = (Path(tempfile.mkdtemp(dir=TMP)) / "site").resolve()
        site.mkdir()
        out = cli("start", str(site), "--no-open", "--no-service", "--chat", "notes-app")
        self.assertIn('- Added %s to the project "notes-app", as "site".' % site, out)
        self.assertIn("notes-app  [--chat notes-app]", cli("projects"))
        # Agents in different projects do not see or reach each other.
        here, _ = self.agent()
        # Names are random per project, so both could get the same one: give this one its own.
        here_name = "first-project-agent"
        self.assertIn("You are now %s." % here_name, here.text("hub_rename", name=here_name))
        token = crewchat.read_token(place, entry["root"])
        there = Session(self.base, token)
        there_name = there.me
        self.assertNotIn(here_name, there.text("hub_agents"))
        self.assertIn("nobody here is called", there.text("hub_send", to=here_name, text="hello?"))
        crewchat.post_json(self.base + "/api/send", self.owner, {"to": "all", "text": "for the first project"})
        self.assertNotIn("for the first project", there.text("hub_inbox"))
        crewchat.post_json(self.base + "/api/send", self.owner, {"to": "all", "text": "for notes-app"},
                           headers={"X-Crewchat-Chat": "notes-app"})
        self.assertIn("for notes-app", there.text("hub_inbox"))
        self.assertNotIn("for notes-app", here.text("hub_inbox"))
        # An agent's token is its project's only, whatever project it asks for.
        ping = b'{"jsonrpc":"2.0","id":1,"method":"ping"}'
        status = self.raw("/mcp", ping, {"Authorization": "Bearer " + token, "X-Crewchat-Chat": crewchat.list_projects()[0]["id"]})[0]
        self.assertEqual(status, 403)
        status = self.raw("/mcp", ping, {"Authorization": "Bearer " + token, "X-Crewchat-Chat": "notes-app"})[0]
        self.assertEqual(status, 200)
        self.assertEqual(self.raw("/mcp", ping, {"Authorization": "Bearer " + self.owner, "X-Crewchat-Chat": "nope"})[0], 404)

    def test_the_banner_is_plain_when_not_on_a_terminal(self):
        self.assertEqual(crewchat.banner(io.StringIO()), "crewchat " + crewchat.__version__)

    def test_a_join_code_works_once_and_wrong_codes_fail(self):
        args = self.invite("--client", "generic")
        cli(*args)
        with self.assertRaises(SystemExit):
            cli(*args)
        with self.assertRaises(SystemExit):
            cli("join", "--url", self.base, "--code", "AAAA-AAAA", "--client", "generic")


class OtherNames(Base):
    """crewchat registered under a name of the user's or a directory's (`claude mcp add --scope user
    crewchat-mcp -- uvx crewchat stdio`): its tools are still allowed without asking, and in a folder
    with no chat it is a server with no tools, not a failed one."""

    def setUp(self):
        self.user = crewchat.user_home()
        shutil.rmtree(self.user, ignore_errors=True)
        self.addCleanup(shutil.rmtree, self.user, True)

    def project(self):
        folder = Path(tempfile.mkdtemp(prefix="project-", dir=TMP)).resolve()
        subprocess.run(["git", "init", "-q", str(folder)], check=False)
        return folder

    def register(self, folder=None, **servers):
        """User-scope servers for Claude Code, or local-scope ones for folder."""
        path = self.user / ".claude.json"
        data = json.loads(path.read_text()) if path.exists() else {}
        target = data.setdefault("projects", {}).setdefault(str(folder), {}) if folder else data
        target.setdefault("mcpServers", {}).update(servers)
        self.user.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def stdio(self, folder, lines=b"", home=None):
        env = dict(os.environ, HOME=str(home or self.user), USERPROFILE=str(home or self.user))  # Windows: USERPROFILE
        return subprocess.run([sys.executable, str(ROOT / "crewchat.py"), "stdio", "--project", str(folder)],
                              input=lines, capture_output=True, env=env, timeout=60)

    def test_what_counts_as_crewchat(self):
        yes = crewchat.is_crewchat_server
        self.assertTrue(yes({"command": "uvx", "args": ["crewchat", "stdio"]}))
        self.assertTrue(yes({"command": "uvx", "args": ["crewchat@latest", "stdio"]}))
        self.assertTrue(yes({"command": "crewchat", "args": ["stdio", "--project", "/x"]}))
        self.assertTrue(yes({"command": "/usr/bin/python3", "args": ["/opt/crewchat/crewchat.py", "stdio"]}))
        self.assertTrue(yes({"command": "C:\\Tools\\crewchat.exe", "args": ["stdio"]}))
        self.assertTrue(yes({"type": "http", "url": self.mcp}, urls=(self.mcp,)))
        self.assertTrue(yes({"url": "https://elsewhere/mcp", "headers": {"Authorization": "Bearer t0k"}}, tokens={"t0k"}))
        self.assertFalse(yes({"command": "uvx", "args": ["crewchat", "serve"]}))
        self.assertFalse(yes({"command": "npx", "args": ["crewchat-lookalike", "stdio"]}))
        self.assertFalse(yes({"url": "https://elsewhere/mcp", "headers": {"Authorization": "Bearer other"}},
                             urls=(self.mcp,), tokens={"t0k"}))
        self.assertFalse(yes("crewchat"))

    def test_a_server_named_otherwise_is_allowed_and_the_double_is_noted(self):
        folder = self.project()
        self.register(**{"crewchat-mcp": {"type": "stdio", "command": "uvx", "args": ["crewchat", "stdio"]},
                         "other": {"command": "npx", "args": ["other-mcp", "stdio"]}})
        self.register(folder, chat={"type": "http", "url": self.mcp, "headers": {}},
                      **{"bad name!": {"command": "crewchat", "args": ["stdio"]}})
        (self.user / ".cursor").mkdir()
        (self.user / ".cursor" / "mcp.json").write_text(json.dumps(
            {"mcpServers": {"crew": {"command": "/usr/local/bin/crewchat", "args": ["stdio"]}}}))
        out = cli("start", str(folder), "--no-open", "--no-service", "--place", "Named", "--chat", "Demo")
        allow = json.loads((folder / ".claude" / "settings.local.json").read_text())["permissions"]["allow"]
        for name in ("crewchat", "crewchat-mcp", "chat"):
            self.assertIn("mcp__%s__hub_send" % name, allow)
            self.assertIn("mcp__%s__hub_inbox" % name, allow)
        self.assertFalse([r for r in allow if r.startswith(("mcp__other__", "mcp__bad"))])
        cursor = json.loads((folder / ".cursor" / "cli.json").read_text())["permissions"]["allow"]
        self.assertIn("Mcp(crew:hub_send)", cursor)
        self.assertIn("Mcp(crewchat:hub_send)", cursor)
        # crewchat twice for one client: start says so, and how to keep one.
        self.assertIn("Claude Code sees crewchat 3 times: crewchat (.mcp.json), chat (~/.claude.json (local scope)), "
                      "crewchat-mcp (~/.claude.json (user scope))", out)
        self.assertIn("`claude mcp remove --scope local chat`", out)
        self.assertIn("Cursor sees crewchat 2 times", out)
        # Taking crewchat out of the folder takes every name's rules with it.
        crewchat.disconnect_folder(folder)
        settings = folder / ".claude" / "settings.local.json"
        left = json.loads(settings.read_text()).get("permissions", {}).get("allow", []) if settings.exists() else []
        self.assertFalse([r for r in left if "__hub_" in r])

    def test_one_registration_says_nothing(self):
        folder = self.project()
        out = cli("start", str(folder), "--no-open", "--no-service", "--place", "Single", "--chat", "Demo")
        self.assertNotIn("sees crewchat", out)
        self.assertIsNone(crewchat.duplicate_note(folder, "claude"))

    def test_a_name_given_later_is_allowed_when_its_server_starts(self):
        folder = self.project()
        cli("start", str(folder), "--no-open", "--no-service", "--place", "Later", "--chat", "Demo")
        settings = folder / ".claude" / "settings.local.json"
        self.assertNotIn("mcp__crewchat-mcp__hub_send", settings.read_text())
        self.register(**{"crewchat-mcp": {"command": "uvx", "args": ["crewchat", "stdio"]}})
        done = self.stdio(folder)  # the client starts it: no input, so it relays nothing and ends
        self.assertEqual(done.returncode, 0, done.stderr)
        allow = json.loads(settings.read_text())["permissions"]["allow"]
        self.assertIn("mcp__crewchat-mcp__hub_send", allow)
        self.assertEqual(allow.count("mcp__crewchat__hub_send"), 1)
        self.assertEqual(crewchat.allow_registered(folder), [])  # nothing left to add

    def test_stdio_in_a_folder_with_no_chat_is_a_server_with_no_tools(self):
        folder = Path(tempfile.mkdtemp(prefix="elsewhere-", dir=TMP)).resolve()
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": 3, "method": "ping"},
                    [{"jsonrpc": "2.0", "id": 4, "method": "prompts/list"}, {"jsonrpc": "2.0", "id": 5, "method": "x"}]]
        done = self.stdio(folder, ("\n".join(json.dumps(r) for r in requests) + "\nnot json\n").encode())
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(b"is not connected to a crewchat", done.stderr)
        answers = [json.loads(line) for line in done.stdout.decode().splitlines()]
        hello = answers[0]["result"]
        self.assertEqual((hello["protocolVersion"], hello["serverInfo"]["name"]), ("2025-06-18", "crewchat"))
        self.assertIn("run `crewchat start .` in this folder", hello["instructions"])
        self.assertEqual(answers[1], {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}})
        self.assertEqual(answers[2]["result"], {})
        self.assertEqual([a.get("result") for a in answers[3]], [{"prompts": []}, None])
        self.assertEqual(answers[3][1]["error"]["code"], -32601)
        self.assertEqual(answers[4]["error"]["code"], -32700)
        self.assertEqual(len(answers), 5)  # nothing for the notification

    def test_stdio_registered_for_every_folder_follows_only_this_machines_chat(self):
        repo = Path(tempfile.mkdtemp(prefix="cloned-", dir=TMP)).resolve()  # a repository with its own .mcp.json
        (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"crewchat": {
            "url": "https://chat.example/mcp", "headers": {"Authorization": "Bearer theirs"}}}}))
        self.assertFalse(crewchat.local_chat("https://chat.example/mcp", "theirs"))
        env = dict(os.environ, HOME=str(self.user), USERPROFILE=str(self.user))
        done = subprocess.run([sys.executable, str(ROOT / "crewchat.py"), "stdio", "--local-only", "--project", str(repo)],
                              input=b"", capture_output=True, env=env, timeout=60)
        self.assertIn(b"is not connected to a crewchat", done.stderr)  # not relayed to chat.example
        mine = Path(tempfile.mkdtemp(prefix="mine-", dir=TMP)).resolve()
        cli("start", str(mine), "--no-open", "--no-service", "--chat", "demo", "--client", "claude")
        self.assertTrue(crewchat.local_chat(*crewchat.client_config(mine, "claude")))

    def test_the_chat_tools_are_not_shown_as_activity_by_any_name(self):
        for name in ("mcp__crewchat-mcp__hub_send", "mcp__chat__hub_inbox", "hub_send", "Mcp(crew:hub_task)"):
            self.assertEqual(crewchat.tool_summary("claude", {"tool_name": name}), "", name)


class AgentTerminal(unittest.TestCase):
    """The agent's pane has its terminal always: a background agent's screen (claude logs), read again every few seconds
    while the pane shows it; an agent in its own Terminal window says so. Without the fix the pane had only a button."""

    def test_the_pane_shows_the_terminal_and_refreshes_it(self):
        page = crewchat.CHAT_PAGE
        self.assertIn("function agentTerminal(name)", page)
        self.assertIn("agentTerminal(name), el(\"p\", \"panel-note meta\", said)", page)
        self.assertIn("setInterval(refreshScreen, 4000);", page)
        self.assertIn('postJSON("/api/agent", { op: "screen", agent: name })', page)
        self.assertIn("refreshScreen();  // the terminal shows at once", page)
        self.assertIn("is its own window on this machine", page)

    def test_starting_agents_show_a_loader_with_what_they_do(self):
        """An agent being started shows a spinner on the agents list, with its stage and how long, or why it did not
        start (Hub.pending_starts). Without it, a start in the background looked like nothing was happening."""
        page = crewchat.CHAT_PAGE
        self.assertIn("state.starting = data.starting || [];", page)
        self.assertIn('el("div", "agent starting")', page)
        self.assertIn('el("span", "spinner")', page)
        self.assertIn("def pending_starts(self):", inspect.getsource(crewchat.Hub))


class InPageDialogs(unittest.TestCase):
    """Path to 10 (UX): the page asks in its own box, styled as the page (light, dark, the phone), never in the
    browser's confirm(), prompt() or alert(), which look like another site's and block the whole tab."""

    def test_no_native_dialog_is_left(self):
        code = re.sub(r"//[^\n]*", "", crewchat.CHAT_PAGE)  # comments may name them
        self.assertEqual(re.findall(r"(?<![.\w])(confirm|prompt|alert)\(", code), [])

    def test_the_box_is_on_the_page(self):
        page = crewchat.CHAT_PAGE
        self.assertIn('<dialog id="ask-dialog" role="alertdialog"', page)
        for fn in ("function ask(text, word)", "function askText(text, value, word)", "function tell(text)"):
            self.assertIn(fn, page)
        self.assertGreater(page.count("await ask("), 20)
        # Round 10: a question whose first word is not the action names its button (not "OK")
        self.assertIn('"\\n\\nApprove it only if it does.", "Approve")', page)
        self.assertIn("const verb = word || (ASK_VERBS.includes(first)", page)
        # /agent in a held or new project says why instead of doing nothing (a disabled button fires no click)
        self.assertIn('if ($("add-agent").disabled) throw new Error($("add-agent").title', page)


class PageScriptSyntax(unittest.TestCase):
    """Every inline script of the chat page and the sign-in page parses. A real line break inside a JavaScript
    string (17 Nov: the Google Workspace preset's placeholder) stopped the whole page, which hung on "Loading the
    chat..."; no test read the script as JavaScript, so it shipped."""

    def scripts(self, page):
        found = []
        for m in re.finditer(r"<script([^>]*)>(.*?)</script>", page, re.S):
            attrs, body = m.group(1), m.group(2)
            if "src=" in attrs or not body.strip():
                continue
            kind = re.search(r'type="([^"]+)"', attrs)
            if kind and kind.group(1) not in ("module", "text/javascript"):
                continue  # JSON and other data blocks
            found.append((".mjs" if kind and kind.group(1) == "module" else ".js", body))
        return found

    def test_every_inline_script_parses(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("needs node")
        for name, page in (("chat", crewchat.CHAT_PAGE), ("login", crewchat.LOGIN_PAGE)):
            scripts = self.scripts(page)
            if name == "chat":
                self.assertTrue(scripts, "the chat page has inline scripts")
            for i, (ext, body) in enumerate(scripts):
                path = Path(tempfile.mkdtemp(prefix="page-script-", dir=TMP)) / ("script" + ext)
                path.write_text(body, encoding="utf-8")
                run = subprocess.run([node, "--check", str(path)], capture_output=True, text=True, encoding="utf-8")
                self.assertEqual(run.returncode, 0, "%s page, script %d: %s" % (name, i, run.stderr[-800:]))


if __name__ == "__main__":
    unittest.main()


class SettingsPanes(unittest.TestCase):
    """Settings' cards sit in the pane of their section's group (2.1.0 e2e, 17 Nov)."""

    def test_every_element_the_page_script_names_is_on_the_page(self):
        # Accounts' line went with #210 while loadJiraWatch still set it: the Jira card showed the TypeError
        import re
        page = crewchat.CHAT_PAGE
        ids = set(re.findall(r'id="([^"]+)"', page)) | set(re.findall(r'\.id = "([^"]+)"', page))  # made by the script
        self.assertEqual(sorted(set(re.findall(r'\$\("([A-Za-z0-9_-]+)"\)', page)) - ids), [])

    def test_the_routines_card_is_a_project_card(self):
        # It stayed inside Integrations' page when Integrations became global (#208), so it showed there and never
        # under Routines, whose pane is the project's.
        page = crewchat.CHAT_PAGE
        project = page[page.index('<div id="pane-project"'):]
        self.assertIn('id="routines-card"', project)
        integrations = page[page.index('<div id="integ-page"'):page.index('id="shortcuts-card"')]
        self.assertNotIn('id="routines-card"', integrations)


class PageNames(unittest.TestCase):
    """Release review (20 Nov): the task panel's Ticket cycle field called FLOW_STAGE and flowTimeline after their
    definitions left with the cycle's panel; the ReferenceError was caught and the field quietly dropped."""

    BROWSER = {"clearInterval", "clearTimeout", "decodeURIComponent", "encodeURIComponent", "getComputedStyle",
               "matchMedia", "onClick", "parseInt", "requestAnimationFrame", "setInterval", "setTimeout",
               "translateX", "translateY"}  # the browser's own, and CSS functions in the page's style

    def test_every_helper_and_table_the_page_uses_is_defined(self):
        import re
        page = crewchat.PAGE_BYTES.decode("utf-8")
        defined = set(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)", page))
        defined |= set(re.findall(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", page))
        tables = set(re.findall(r"\b([A-Z][A-Z0-9_]{2,})\[", page))
        helpers = {n for n in re.findall(r"(?<![\w.$])([a-z]+[A-Z][\w$]*)\(", page)} - self.BROWSER
        self.assertEqual(sorted(tables - defined), [])
        self.assertEqual(sorted(helpers - defined), [])
