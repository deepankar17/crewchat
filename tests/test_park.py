"""C11: when a task waits on the owner (a blocked task, a question with Needs a reply, a permission card) and the owner
has not answered for the owner's park time (30 minutes unless set), its lead is told, and may park it: it goes back in
the open queue with the question kept, its agent is freed for other work, and once the owner answers the task can be
carried on, by the same agent or another, with the answer on it."""
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_crewchat import TMP, crewchat  # noqa: E402  (the shared module)

MIN = 60


class LeadParksTasks(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="park-", dir=TMP))
        crewchat.save_config({"project": "web", "port": 9}, self.root)
        self.hub = crewchat.Hub(crewchat.Roster(self.root))
        self.lead, self.dev, self.dev2 = self.agent("Lead"), self.agent("Dev"), self.agent("Dev2")
        self.hub.set_role(crewchat.OWNER, self.lead, "lead")
        self.hub.set_role(crewchat.OWNER, self.dev, "developer")
        self.hub.set_role(crewchat.OWNER, self.dev2, "developer")
        self.mid = self.hub.add_row(crewchat.OWNER, "Rotate the API keys")["id"]
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

    def to(self, who, word):
        return [m["text"] for m in self.hub.messages if m["to"] == who and word in m["text"]]

    def blocked(self, ago):
        """Dev marks its task blocked with a question for Owner, `ago` seconds back."""
        self.hub.update(self.dev, self.mid, "blocked", "Which key vault: staging or prod?")
        with self.hub.lock:
            self.hub.messages[-1]["ts"] -= ago

    def test_the_lead_is_told_once_when_a_blocked_task_has_waited_long_enough(self):
        self.assertEqual(self.hub.crew_settings()["park"], 30)  # the owner's default (11 Nov 2026)
        self.blocked(29 * MIN)
        self.assertEqual(self.hub.park_tick(), [])
        now = time.time() + MIN
        self.assertEqual(self.hub.park_tick(now), [self.mid])
        told = self.to(self.lead, "hub_task action=park id=%s" % self.mid)
        self.assertEqual(len(told), 1)
        self.assertIn("Which key vault: staging or prod?", told[0])
        self.assertEqual(self.hub.park_tick(now + 10 * MIN), [])  # once for each wait

    def test_an_answer_from_the_owner_ends_the_wait(self):
        self.blocked(40 * MIN)
        self.hub.send(crewchat.OWNER, self.dev, "Re #%s: prod" % self.mid, re=self.mid)
        self.assertEqual(self.hub.park_tick(), [])
        with self.assertRaisesRegex(crewchat.HubError, "does not wait on Owner"):
            self.call(self.lead, action="park", id=self.mid)

    def test_parking_frees_the_agent_and_keeps_the_question_until_the_owner_answers(self):
        self.blocked(31 * MIN)
        out = self.call(self.lead, action="park", id=self.mid)
        self.assertIn("is parked", out)
        row = self.hub.sheet[self.mid]
        self.assertNotIn(self.mid, self.hub.taken)  # back in the open queue
        self.assertEqual((row["parked"], row["pending"]), (self.dev, "Which key vault: staging or prod?"))
        self.assertEqual(self.hub._row_status(self.mid), "todo")
        self.assertTrue(self.to(self.dev, "stop work on it now"))
        # Nobody takes it before the owner answers; the lead may still give it out.
        with self.assertRaisesRegex(crewchat.HubError, "parked until Owner answers"):
            self.hub.take(self.dev2, self.mid)
        self.assertNotIn(self.mid, [r["id"] for r in self.hub.sheet_rows(self.dev2, "next")])
        self.assertIn("parked while Dev waited on Owner", self.call(self.lead, action="view", show="all"))
        # Owner sees it in Needs you, with the question, and answers it there.
        item = next(n for n in self.hub.needs_you() if n["kind"] == "parked")
        self.assertEqual((item["task"], item["agent"], item["text"], item["answer"]),
                         (self.mid, self.dev, "Which key vault: staging or prod?", "reply"))
        crewchat.answer_need(self.hub, {"key": item["key"], "answer": "reply", "text": "prod, the new vault"})
        self.assertEqual(self.hub.sheet[self.mid]["owner_said"], "prod, the new vault")
        self.assertFalse([n for n in self.hub.needs_you() if n["kind"] == "parked"])
        self.assertTrue(self.to(self.lead, "Owner answered parked task #%s" % self.mid))
        # Whoever carries on gets the question and the answer.
        out = self.call(self.dev2, action="take", id=self.mid)
        self.assertIn('Owner answered: "prod, the new vault"', out)
        self.assertIn("Which key vault: staging or prod?", out)
        self.hub.update(self.dev2, self.mid, "done", "Rotated.")
        self.assertNotIn("parked", self.hub.sheet[self.mid])  # its work is over: so is the wait

    def test_a_parked_task_whose_agent_was_removed_is_still_answered(self):
        # Review: the card's answer went to the parked agent by name, and failed once that agent was removed.
        self.blocked(31 * MIN)
        self.call(self.lead, action="park", id=self.mid)
        self.hub.remove(self.dev)
        item = next(n for n in self.hub.needs_you() if n["kind"] == "parked")
        crewchat.answer_need(self.hub, {"key": item["key"], "answer": "reply", "text": "prod, the new vault"})
        self.assertEqual(self.hub.sheet[self.mid]["owner_said"], "prod, the new vault")
        self.assertTrue(self.to(self.lead, "prod, the new vault"))
        self.assertFalse([n for n in self.hub.needs_you() if n["kind"] == "parked"])

    def test_the_lead_may_give_a_parked_task_to_its_agent_again(self):
        self.blocked(31 * MIN)
        self.call(self.lead, action="park", id=self.mid)
        self.hub.send(crewchat.OWNER, self.dev, "Re #%s: staging" % self.mid, re=self.mid)
        out = self.hub.assign_row(self.lead, self.dev, self.mid)
        self.assertIn('Owner answered: "staging"', out["text"])
        self.assertEqual(self.hub.taken[self.mid], self.dev)

    def test_only_its_lead_parks_and_only_after_the_owners_time(self):
        self.blocked(10 * MIN)
        with self.assertRaisesRegex(crewchat.HubError, "has waited on Owner for 10 minutes; a lead may park it after 30"):
            self.call(self.lead, action="park", id=self.mid)
        with self.hub.lock:
            self.hub.messages[-1]["ts"] -= 30 * MIN
        with self.assertRaisesRegex(crewchat.HubError, "only the lead"):
            self.call(self.dev2, action="park", id=self.mid)
        self.hub.set_crew(park=0)  # the owner turns it off
        self.assertEqual(self.hub.park_tick(time.time() + 600 * MIN), [])
        with self.assertRaisesRegex(crewchat.HubError, "turned parking off"):
            self.call(self.lead, action="park", id=self.mid)
        self.hub.set_crew(park=60)
        with self.assertRaisesRegex(crewchat.HubError, "after 60"):
            self.call(self.lead, action="park", id=self.mid)
        for bad in (-1, "x", True, crewchat.PARK_MOST + 1):
            with self.assertRaises(crewchat.HubError):
                self.hub.set_crew(park=bad)
        self.assertEqual(self.hub.crew_settings()["park"], 60)

    def test_a_question_with_needs_a_reply_counts(self):
        self.hub.send(self.dev, crewchat.OWNER, "May I drop the old keys table?", ask=True)
        with self.hub.lock:
            self.hub.open_asks[self.hub.messages[-1]["id"]]["ts"] -= 31 * MIN
        self.assertEqual(self.hub.park_tick(), [self.mid])
        self.call(self.lead, action="park", id=self.mid)
        self.assertEqual(self.hub.sheet[self.mid]["pending"], "May I drop the old keys table?")

    def test_a_question_is_not_put_on_a_task_when_the_agent_holds_several(self):
        other = self.hub.add_row(crewchat.OWNER, "Write the changelog")["id"]
        self.hub.take(self.dev, other)
        self.hub.send(self.dev, crewchat.OWNER, "Lunch?", ask=True)
        with self.hub.lock:
            self.hub.open_asks[self.hub.messages[-1]["id"]]["ts"] -= 31 * MIN
        self.assertEqual(self.hub.park_tick(), [])

    def test_a_permission_card_counts_and_parking_answers_it_with_a_deny(self):
        place = self.hub.agents[self.dev]["place"]
        key = "devkey12345"
        with self.hub.lock:
            self.hub.links[key] = self.dev
        got = {}
        t = threading.Thread(target=lambda: got.update(self.hub.ask_permission(place, key, "Bash", "git push", wait=30)))
        t.start()
        for _ in range(200):
            if self.hub.asks.since.get(self.dev):
                break
            time.sleep(0.05)
        with self.hub.lock:
            self.hub.asks.since[self.dev]["ts"] -= 31 * MIN
        self.assertEqual(self.hub.park_tick(), [self.mid])
        self.call(self.lead, action="park", id=self.mid)
        t.join(30)
        self.assertEqual(got["answer"], "deny")
        self.assertIn("was parked by Lead", got["note"])
        self.assertEqual(self.hub.sheet[self.mid]["pending"], "git push")
        self.assertNotIn(self.dev, self.hub.asks.since)

    def test_a_question_card_counts_and_parking_answers_it(self):
        # Review: OWNER_WAIT_KINDS lacked "question", so an AskUserQuestion card never counted as a wait on the owner,
        # and parking left the agent blocked on it.
        place = self.hub.agents[self.dev]["place"]
        key = "devkey12345"
        with self.hub.lock:
            self.hub.links[key] = self.dev
        got = {}
        question = [{"question": "Which vault?", "header": "Vault", "options": [{"label": "prod"}, {"label": "staging"}]}]
        t = threading.Thread(target=lambda: got.update(self.hub.ask_question(place, key, question, wait=30)))
        t.start()
        for _ in range(200):
            if self.hub.asks.waiting("question"):
                break
            time.sleep(0.05)
        with self.hub.lock:
            self.assertIn(self.dev, self.hub.asks.since)
            self.hub.asks.since[self.dev]["ts"] -= 31 * MIN
        self.assertEqual(self.hub.park_tick(), [self.mid])
        self.call(self.lead, action="park", id=self.mid)
        t.join(30)
        self.assertIn("was parked by Lead", got["answers"]["Which vault?"])
        self.assertEqual(self.hub.sheet[self.mid]["pending"], "Which vault?")
        self.assertNotIn(self.dev, self.hub.asks.since)

    def test_what_an_agent_waits_for_names_the_program_not_the_command(self):
        # Review: the first 80 characters of the prompt (often a whole command, a token in it) went to hub_agents for
        # every agent, and to linked machines.
        place = self.hub.agents[self.dev]["place"]
        with self.hub.lock:
            self.hub.links["devkey12345"] = self.dev
        token = "tok" + "x" * 30
        t = threading.Thread(target=lambda: self.hub.ask_permission(
            place, "devkey12345", "Bash", "TOKEN=%s git push origin main" % token, wait=30))
        t.start()
        try:
            for _ in range(200):
                if self.hub.asks.waiting("permission"):
                    break
                time.sleep(0.05)
            with self.hub.lock:
                waiting = self.hub._waiting_on_owner()[self.dev]
            self.assertEqual(waiting, "permission for Bash: running git push")
            row = next(r for r in self.hub.local_state() if r["agent"] == self.dev)
            self.assertNotIn(token, row["waiting"])
            self.assertNotIn(token, self.call(self.lead, action="view", show="all"))
        finally:
            for ask in self.hub.asks.waiting("permission"):
                self.hub.answer_permission(ask["id"], "deny")
            t.join(30)

    def test_a_permission_wait_outlives_the_card_until_the_agent_moves_on(self):
        place = self.hub.agents[self.dev]["place"]
        key = "devkey12345"
        with self.hub.lock:
            self.hub.links[key] = self.dev
        self.assertEqual(self.hub.ask_permission(place, key, "Bash", "git push", wait=0.2)["answer"], "")
        self.assertIn(self.dev, self.hub.asks.since)  # it now waits in its own terminal
        self.assertEqual(list(self.hub._owner_waits()), [self.mid])
        self.hub.hook(place, key, 0, 0, event="tool")  # the call ran: answered in its terminal
        self.assertEqual(self.hub._owner_waits(), {})
        # Parked while it waits there: the lead is told the agent is not free.
        self.hub.ask_permission(place, key, "Bash", "git push", wait=0.2)
        with self.hub.lock:
            self.hub.asks.since[self.dev]["ts"] -= 31 * MIN
        self.assertIn("still waits at a permission prompt in its own terminal",
                      self.call(self.lead, action="park", id=self.mid))

    def test_a_rename_keeps_the_parked_task_and_the_wait(self):
        # Review: rename missed sheet rows' parked and asks.since.
        place = self.hub.agents[self.dev]["place"]
        with self.hub.lock:
            self.hub.links["devkey12345"] = self.dev
        self.hub.ask_permission(place, "devkey12345", "Bash", "git push", wait=0.2)
        self.hub.rename(self.dev, "Dev7")
        self.assertIn("Dev7", self.hub.asks.since)
        self.assertEqual([w["agent"] for w in self.hub._owner_waits().values()], ["Dev7"])
        with self.hub.lock:
            self.hub.asks.since["Dev7"]["ts"] -= 31 * MIN
        self.call(self.lead, action="park", id=self.mid)
        self.hub.rename("Dev7", "Dev8")
        self.assertEqual(self.hub.sheet[self.mid]["parked"], "Dev8")
        item = next(n for n in self.hub.needs_you() if n["kind"] == "parked")
        self.assertEqual(item["agent"], "Dev8")

    def test_the_owner_sets_the_time_on_the_page(self):
        self.hub.set_crew(park=45)
        self.assertEqual(self.hub.crew_settings()["park"], 45)
        self.assertTrue([m for m in self.hub.messages if "park a task after 45 minutes" in m["text"]])


if __name__ == "__main__":
    unittest.main()
