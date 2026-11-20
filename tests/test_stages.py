"""Task stages (K2): the owner gives a task stages, each done by an agent with its role; a stage's done hands it on,
a later stage sends it back a stage with what failed (MAX_RETURNS rounds, then the lead or the owner), and the last
stage's done is the task's done."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from crewchat_fixture import TMP, Base, cli, crewchat

WRAP = "\nDecisions: none\nLeft over: none"  # the last done of a task carries its wrap-up (U1)

SPEC = "build:developer,test:qa,review:lead"


class TaskStages(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="stages-", dir=TMP))
        crewchat.save_config({"project": "stages", "port": 9}, self.root)
        self.hub = self.open()
        self.dev, self.qa, self.lead = (self.agent(role) for role in ("developer", "qa", "lead"))
        self.mid = self.hub.add_row(crewchat.OWNER, "Build the login page")["id"]

    def open(self):
        hub = crewchat.Hub(crewchat.Roster(self.root))
        self.addCleanup(hub.events.close)
        return hub

    def agent(self, role):
        name = self.hub.agent_for(self.hub.open_session("mac", "claude"))
        with self.hub.lock:
            self.hub.agents[name]["role"] = role
        return name

    def row(self, hub=None):
        return (hub or self.hub).sheet[self.mid]

    def told(self, name):
        return [m["text"] for m in self.hub.messages if m["to"] == name and m["kind"] == "assign"]

    def test_a_task_goes_through_its_stages_and_back(self):
        self.assertEqual(self.hub.set_stages(self.mid, SPEC)["order"], ["build", "test", "review"])
        self.hub.take(self.dev, self.mid)
        _, to = self.hub.update(self.dev, self.mid, "done", "Built it")
        self.assertEqual((to, self.hub.taken[self.mid], self.row()["active"]), (self.qa, self.qa, {"test": self.qa}))
        self.assertNotEqual(self.hub._row_status(self.mid), "done")  # a stage passed, not the task
        self.assertIn("stage test (qa)", self.told(self.qa)[-1])
        self.assertIn("hub_task return", self.told(self.qa)[-1])

        msg = self.hub.return_task(self.qa, self.mid, "The login button does nothing")
        self.assertEqual((msg["to"], self.hub.taken[self.mid], self.row()["active"]), (self.dev, self.dev, {"build": self.dev}))
        self.assertIn("The login button does nothing", self.told(self.dev)[-1])
        self.assertEqual(self.row()["visits"]["build"], 2)

        self.hub.update(self.dev, self.mid, "done", "Fixed")
        self.assertEqual(self.hub.taken[self.mid], self.qa)  # the same tester again
        self.hub.update(self.qa, self.mid, "done", "Passes")
        self.assertEqual((self.hub.taken[self.mid], self.row()["active"]), (self.lead, {"review": self.lead}))
        self.hub.update(self.lead, self.mid, "done", "Looks good" + WRAP)
        self.assertEqual(self.hub._row_status(self.mid), "done")  # the last stage's done is the task's
        self.hub._save()
        again = self.open()  # every machine and a restart agree: the stages are messages
        self.assertEqual(again.sheet[self.mid]["graph"], self.row()["graph"])
        self.assertEqual(again.sheet[self.mid]["active"], {})

    def test_only_the_agents_at_a_stage_can_say_it_is_done(self):
        # Review: a tester who did not hold the task marked it done and skipped the later stages.
        self.hub.set_stages(self.mid, SPEC)
        self.hub.take(self.dev, self.mid)
        with self.assertRaisesRegex(crewchat.HubError, "at none of them"):
            self.hub.update(self.qa, self.mid, "done", "Looks fine")
        self.assertEqual(self.row()["active"], {"build": self.dev})

    def test_a_staged_task_is_taken_by_the_role_its_stage_needs(self):
        self.hub.set_stages(self.mid, SPEC)
        with self.assertRaisesRegex(crewchat.HubError, "developer role"):
            self.hub.take(self.qa, self.mid)
        self.hub.take(self.dev, self.mid)
        self.assertEqual(self.row()["active"], {"build": self.dev})

    def test_the_lead_steps_in_after_too_many_rounds(self):
        self.hub.set_stages(self.mid, SPEC)
        self.hub.take(self.dev, self.mid)
        for _ in range(crewchat.MAX_RETURNS):
            self.hub.update(self.dev, self.mid, "done", "Built it")
            self.hub.return_task(self.qa, self.mid, "Still broken")
        self.hub.update(self.dev, self.mid, "done", "Built it")
        msg = self.hub.return_task(self.qa, self.mid, "Still broken")
        self.assertEqual((msg["status"], msg["to"]), ("blocked", self.lead))
        self.assertIn("needs you to step in", msg["text"])

    def test_no_agent_for_the_next_stage_goes_to_the_lead(self):
        self.hub.set_stages(self.mid, "build:developer,secure:security")
        self.hub.take(self.dev, self.mid)
        msg, to = self.hub.update(self.dev, self.mid, "done", "Built it")
        self.assertEqual((msg["status"], to, self.hub.taken[self.mid]), ("blocked", self.lead, self.dev))
        self.assertIn("no agent has the security role", msg["text"])
        self.assertEqual(self.row()["active"], {"build": self.dev})  # still at its stage

    def test_stages_are_checked_and_can_be_taken_off(self):
        for bad in ("build", "build:developer", "Build:developer,test:qa", "a:qa,a:qa", "a:qa > b", "a:qa > b; b:dev after c",
                    "a:qa after b; b:dev", "a:qa | x; b:dev", "a:qa > end; end:dev"):
            with self.assertRaises(crewchat.HubError):
                self.hub.set_stages(self.mid, bad)
        with mock.patch.object(self.hub, "_flow_of_task", return_value={"key": "KAN-1"}):
            with self.assertRaisesRegex(crewchat.HubError, "Jira"):
                self.hub.set_stages(self.mid, SPEC)
        self.hub.set_stages(self.mid, SPEC)
        self.assertIsNone(self.hub.set_stages(self.mid, "off"))
        self.assertNotIn("graph", self.row())
        self.hub.take(self.dev, self.mid)
        self.hub.update(self.dev, self.mid, "done", "Built it" + WRAP)
        self.assertEqual(self.hub._row_status(self.mid), "done")  # a plain task again

    def test_agents_see_the_stage_on_the_sheet(self):
        self.hub.set_stages(self.mid, SPEC)
        row = next(r for r in self.hub.sheet_rows(self.dev, "all") if r["id"] == self.mid)
        self.assertIn("stages build (developer) > test; test (qa) > review; review (lead); now at the start",
                      crewchat.sheet_line(row))
        self.hub.take(self.dev, self.mid)
        row = next(r for r in self.hub.sheet_rows(self.dev, "all") if r["id"] == self.mid)
        self.assertIn("now at build (%s)" % self.dev, crewchat.sheet_line(row))

    GRAPH = ("build:developer > test,docs; test:qa > review | flaky > test; docs:writer > review; "
             "review:lead after test,docs | changes > build")

    def test_a_graph_runs_stages_side_by_side_and_waits_for_both(self):
        writer = self.agent("writer")
        self.hub.set_stages(self.mid, self.GRAPH)
        self.hub.take(self.dev, self.mid)
        self.hub.update(self.dev, self.mid, "done", "Built it")
        self.assertEqual(self.row()["active"], {"test": self.qa, "docs": writer})  # both at once
        self.assertIn("At the same time: docs", self.told(self.qa)[-1])
        self.hub.update(writer, self.mid, "done", "Docs written")
        self.assertEqual(self.row()["active"], {"test": self.qa})  # review waits for test too
        self.assertEqual(self.hub.taken[self.mid], self.qa)
        self.hub.update(self.qa, self.mid, "done", "outcome:flaky one test failed once")
        self.assertEqual(self.row()["active"], {"test": self.qa})  # its own outcome: test again
        with self.assertRaisesRegex(crewchat.HubError, "no outcome nope"):
            self.hub.update(self.qa, self.mid, "done", "outcome:nope")
        self.hub.update(self.qa, self.mid, "done", "All pass")
        self.assertEqual(self.row()["active"], {"review": self.lead})
        self.hub.update(self.lead, self.mid, "done", "outcome:changes rename the button")
        self.assertEqual(self.row()["active"], {"build": self.dev})  # a branch back by the reviewer's outcome
        self.assertIn("rename the button", self.told(self.dev)[-1] + " ".join(
            m["text"] for m in self.hub.messages if m["to"] == self.dev))
        self.hub.update(self.dev, self.mid, "done", "Renamed")
        self.hub.update(writer, self.mid, "done", "Docs still fine")
        self.hub.update(self.qa, self.mid, "done", "Pass")
        self.hub.update(self.lead, self.mid, "done", "Ship it" + WRAP)
        self.assertEqual(self.hub._row_status(self.mid), "done")

    def test_a_branch_that_ends_lets_the_others_finish(self):
        self.hub.set_stages(self.mid, "build:developer > test,notes; test:qa; notes:lead > end")
        self.hub.take(self.dev, self.mid)
        self.hub.update(self.dev, self.mid, "done", "Built")
        self.hub.update(self.lead, self.mid, "done", "Noted")
        self.assertNotEqual(self.hub._row_status(self.mid), "done")  # test is still at work
        self.hub.update(self.qa, self.mid, "done", "Pass" + WRAP)
        self.assertEqual(self.hub._row_status(self.mid), "done")


class GraphEdges(TaskStages):
    """Review of the graph: joins a finished branch could skip, a parallel stage given back, two stages for one role,
    and a note that only looks like an outcome."""

    def test_a_join_still_waiting_starts_when_nothing_else_can(self):
        writer = self.agent("writer")
        self.hub.set_stages(self.mid, "a:developer > b,c; b:qa > d; c:lead > end; d:writer after b,c > end")
        self.hub.take(self.dev, self.mid)
        self.hub.update(self.dev, self.mid, "done", "A")
        self.hub.update(self.qa, self.mid, "done", "B")  # d waits for c too
        self.assertEqual(self.row()["active"], {"c": self.lead})
        self.hub.update(self.lead, self.mid, "done", "C ends its branch")
        self.assertEqual(self.row()["active"], {"d": writer})  # not done: d had not run
        self.hub.update(writer, self.mid, "done", "D" + WRAP)
        self.assertEqual(self.hub._row_status(self.mid), "done")

    def test_parallel_stages_for_one_role_go_to_two_agents(self):
        qa2 = self.agent("qa")
        self.hub.set_stages(self.mid, "a:developer > b,c; b:qa; c:qa")
        self.hub.take(self.dev, self.mid)
        self.hub.update(self.dev, self.mid, "done", "A")
        self.assertEqual(sorted(self.row()["active"].values()), sorted([self.qa, qa2]))

    def test_a_stage_given_back_is_taken_by_its_role(self):
        lead2 = self.agent("lead")
        self.hub.set_stages(self.mid, "a:developer > b; b:lead")
        self.hub.take(self.dev, self.mid)
        self.hub.update(self.dev, self.mid, "done", "A")
        self.hub.update(self.lead, self.mid, "todo", "Cannot do it")
        self.assertEqual(self.row()["active"], {"b": ""})
        with self.assertRaisesRegex(crewchat.HubError, "at none of them"):
            self.hub.update(self.lead, self.mid, "done", "B")  # given back: no longer theirs
        with self.assertRaisesRegex(crewchat.HubError, "lead role"):
            self.hub.take(self.qa, self.mid)
        self.hub.take(lead2, self.mid)
        self.assertEqual(self.row()["active"], {"b": lead2})

    def test_a_parallel_stage_cannot_be_given_back(self):
        self.hub.set_stages(self.mid, "a:developer > b,c; b:qa; c:lead")
        self.hub.take(self.dev, self.mid)
        self.hub.update(self.dev, self.mid, "done", "A")
        with self.assertRaises(crewchat.HubError):
            self.hub.update(self.qa, self.mid, "todo", "Not me")
        self.assertEqual(self.row()["active"], {"b": self.qa, "c": self.lead})

    def test_only_an_exact_outcome_word_picks_an_outcome(self):
        self.hub.set_stages(self.mid, SPEC)
        self.hub.take(self.dev, self.mid)
        self.hub.update(self.dev, self.mid, "done", "Outcome: the login works")  # a plain note
        self.assertEqual(self.row()["active"], {"test": self.qa})

    def test_the_chat_page_shows_where_a_task_is_in_its_stages(self):
        self.hub.set_stages(self.mid, SPEC)
        self.hub.take(self.dev, self.mid)
        self.hub.update(self.dev, self.mid, "done", "Built it")
        row = next(r for r in self.hub.sheet_rows() if r["id"] == self.mid)  # what the page is sent
        self.assertEqual((row["graph"]["order"], row["active"], row["passed"]), (["build", "test", "review"], {"test": self.qa}, ["build"]))
        page = crewchat.CHAT_PAGE
        self.assertIn("function stageWords(r)", page)
        self.assertIn('"tchip stages"', page)
        self.assertIn('field("Stages"', page)
        self.assertIn("r.active, r.passed, openPieces(r)]", page)  # the board redraws when a stage moves on


class StagesFromTheCommandLine(Base):
    def test_the_owner_sets_a_graph_and_takes_it_off(self):
        mid = crewchat.post_json(self.base + "/api/task", self.owner, {"op": "add", "title": "Ship the page"})["id"]
        out = cli("tasks", "stages", mid, "build:developer > test,docs; test:qa > review; docs:writer > review;",
                  "review:lead after test,docs")
        self.assertIn("review (lead) after test,docs", out)
        self.assertEqual(self.hub.sheet[mid]["graph"]["start"], "build")
        self.assertIn("none", cli("tasks", "stages", mid, "off"))
        self.assertNotIn("graph", self.hub.sheet[mid])


if __name__ == "__main__":
    unittest.main()
