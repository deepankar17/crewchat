"""U1: a task closes with a wrap-up, after PAUL's "unify" step. Its holder's last report (review or done) says what was
done against the task's "Done when", the decisions made, and what is left over. An agent's done is refused without one
(owner, 18 Nov 2026: required for agents); the owner's done is not. The wrap-up is kept on the task, rebuilt from the
message log, and shown on the task's card and panel."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from crewchat_fixture import TMP, crewchat  # noqa: E402  (the shared module)

WRAPUP = "Rotated the staging and prod keys\nDecisions: kept the old keys valid for a day\nLeft over: none"


class WrapUp(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="wrapup-", dir=TMP))
        crewchat.save_config({"project": "web", "port": 9}, self.root)
        self.hub = crewchat.Hub(crewchat.Roster(self.root))
        self.dev, self.qa = self.agent("Dev"), self.agent("QA")
        self.mid = self.hub.add_row(crewchat.OWNER, "Rotate the API keys. Done when: old keys stop working")["id"]
        self.hub.take(self.dev, self.mid)

    def tearDown(self):
        self.hub.events.close()

    def agent(self, name):
        sid = self.hub.open_session("mac-" + name, "claude")
        self.hub.rename(self.hub.agent_for(sid), name)
        return name

    def call(self, who, **args):
        sid = next(s for s, r in self.hub.sessions.items() if r["agent"] == who)
        return crewchat.call_tool(self.hub, sid, "hub_task", args)

    def row(self, hub=None):
        return {r["id"]: r for r in (hub or self.hub).sheet_rows()}[self.mid]

    def test_what_counts_as_a_wrapup(self):
        self.assertTrue(crewchat.is_wrapup(WRAPUP))
        self.assertTrue(crewchat.is_wrapup("Fixed it\n- decision: none\n- leftover: docs"))
        for note in ("finished", "Done.", "Decisions: none\nLeft over: none", "Did it\nDecisions: none", None, 3):
            self.assertFalse(crewchat.is_wrapup(note), note)

    def test_an_agents_done_without_a_wrapup_is_refused_with_the_format(self):
        with self.assertRaises(crewchat.HubError) as refused:
            self.hub.update(self.dev, self.mid, "done", "finished")
        self.assertIn("needs a wrap-up", str(refused.exception))
        self.assertIn("Left over:", str(refused.exception))
        self.assertNotEqual(self.hub.progress.get(self.mid, {}).get("status"), "done")
        self.assertIn("marked done", self.call(self.dev, action="update", id=self.mid, status="done", note=WRAPUP))
        self.assertEqual(self.row()["status"], "done")
        self.assertEqual((self.row()["wrapup"], self.row()["wrapup_by"]), (WRAPUP, self.dev))

    def test_the_owner_marks_done_without_one(self):
        self.hub.owner_status(self.mid, "done")
        self.assertEqual(self.row()["status"], "done")
        self.hub.update(crewchat.OWNER, self.mid, "done", "")
        self.assertNotIn("wrapup", self.row())

    def test_the_holders_review_wrapup_lets_qa_mark_it_done(self):
        self.hub.update(self.dev, self.mid, "review", WRAPUP)
        self.hub.update(self.qa, self.mid, "done", "Passed")
        self.assertEqual((self.row()["status"], self.row()["wrapup"], self.row()["wrapup_by"]), ("done", WRAPUP, self.dev))

    def test_work_starting_again_drops_the_old_wrapup(self):
        self.hub.update(self.dev, self.mid, "review", WRAPUP)
        self.hub.update(self.dev, self.mid, "in_progress", "One more fix")
        self.assertNotIn("wrapup", self.row())
        with self.assertRaises(crewchat.HubError):
            self.hub.update(self.qa, self.mid, "done", "Passed")

    def test_the_wrapup_survives_a_restart(self):
        self.hub.update(self.dev, self.mid, "done", WRAPUP)
        self.hub.flush()
        hub = crewchat.Hub(crewchat.Roster(self.root))
        try:
            self.assertEqual((self.row(hub)["status"], self.row(hub)["wrapup"]), ("done", WRAPUP))
        finally:
            hub.events.close()

    def test_another_machine_gets_it_from_the_update_message(self):
        self.hub.update(self.dev, self.mid, "review", WRAPUP)
        msg = self.hub.messages[-1]
        self.assertEqual((msg["kind"], msg["status"], msg["text"]), ("update", "review", WRAPUP))
        with self.hub.lock:  # what _effects makes of the message, on any machine it lands on
            self.hub.sheet[self.mid].pop("wrapup")
            self.hub._effects(msg)
        self.assertEqual(self.row()["wrapup"], WRAPUP)

    def test_the_card_and_panel_show_it(self):
        page = crewchat.CHAT_PAGE
        self.assertIn('"✓ wrap-up"', page)
        self.assertIn('field("Wrap-up"', page)

    def test_the_guide_and_roles_ask_for_it(self):
        self.assertIn("Done when", crewchat.ROLES["lead"][1])
        self.assertIn("Left over:", crewchat.ROLES["developer"][1])
        self.assertIn('"Done when:"', crewchat.RULES)
        self.assertIn("`Left over: ...`", crewchat.RULES)


if __name__ == "__main__":
    unittest.main()
