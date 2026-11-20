"""Learning from the trace (K3): crewchat counts repeated trouble in the step trace and the edits, and asks the lead
(else the owner) to write a process note, approved as every note is. It never changes a note, prompt or rule itself."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from crewchat_fixture import TMP, crewchat


class LearningFromTheTrace(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="learn-", dir=TMP))
        crewchat.save_config({"project": "learn", "port": 9}, self.root)
        self.hub = self.open()
        self.now = time.time()

    def open(self):
        hub = crewchat.Hub(crewchat.Roster(self.root))
        self.addCleanup(hub.events.close)
        return hub

    def runs(self, command, agents, count):
        for i in range(count):
            self.hub.events.add_steps(agents[i % len(agents)], [(self.now - 3600 + i, "Bash", "Running " + command)], self.now)

    def raised(self, hub=None):
        hub = hub or self.hub
        return [m for m in hub.messages if m["kind"] == "process" and '"learn"' in m.get("process", "")]

    def test_a_command_many_agents_keep_running_asks_for_a_note_once(self):
        self.runs("npm test", ["luca", "jade"], crewchat.LEARN_RUNS)
        self.assertEqual(self.hub.learn_from_trace(self.now), ["command:npm test"])
        said = self.raised()
        self.assertEqual(len(said), 1)
        self.assertEqual(said[0]["to"], crewchat.OWNER)  # no lead here: the owner
        self.assertIn("`npm test` %d times" % crewchat.LEARN_RUNS, said[0]["text"])
        self.assertIn("hub_process propose", said[0]["text"])
        self.assertEqual(self.hub.process_rows(), [])  # only asked: no note written by crewchat
        later = self.now + crewchat.LEARN_EVERY
        self.assertEqual(self.hub.learn_from_trace(later), [])  # raised once
        self.hub._save()
        self.hub.events.flush()
        again = self.open()  # after a restart too: the finding is a message
        self.assertEqual(again.learn_from_trace(later), [])

    def test_what_agents_run_to_look_around_is_not_raised(self):
        for command in ("git status", "ls", "cat", "grep", "git diff"):
            self.runs(command, ["luca", "jade", "nova"], 100)
        self.assertEqual(self.hub.trace_findings(self.now), [])

    def test_a_finding_raised_long_ago_is_not_raised_again_after_a_restart(self):
        # The messages in memory may not reach back LEARN_KEPT days (forget_hours): state.json keeps what was raised.
        self.runs("npm test", ["luca", "jade"], crewchat.LEARN_RUNS)
        self.assertEqual(self.hub.learn_from_trace(self.now), ["command:npm test"])
        self.hub._save()
        self.hub.events.flush()
        with self.hub.lock:
            self.hub.messages[:] = [m for m in self.hub.messages if '"learn"' not in m.get("process", "")]
        with mock.patch.object(crewchat.Hub, "_replay_processes", lambda hub: None):
            again = self.open()
        again.messages[:] = [m for m in again.messages if '"learn"' not in m.get("process", "")]
        self.assertIn("command:npm test", again._learn_seen())
        self.assertEqual(again.learn_from_trace(self.now + crewchat.LEARN_EVERY), [])

    def test_looks_at_most_every_few_hours(self):
        self.assertEqual(self.hub.learn_from_trace(self.now), [])
        self.runs("npm test", ["luca", "jade"], crewchat.LEARN_RUNS)
        self.assertEqual(self.hub.learn_from_trace(self.now + 60), [])
        self.assertEqual(self.hub.learn_from_trace(self.now + crewchat.LEARN_EVERY), ["command:npm test"])

    def test_one_agent_or_a_command_a_note_already_has_is_not_raised(self):
        self.runs("make", ["luca"], crewchat.LEARN_RUNS * 2)
        self.runs("npm test", ["luca", "jade"], crewchat.LEARN_RUNS)
        self.hub.propose_process(crewchat.OWNER, {"name": "testing", "when": "Before you say done",
                                                  "steps": ["Run the tests"], "pitfalls": ["Watch mode never ends"], "commands": ["npm test -- --ci"]})
        self.assertEqual(self.hub.trace_findings(self.now), [])

    def test_a_file_edited_over_and_over_across_tasks_asks_for_its_pitfalls(self):
        for i in range(crewchat.LEARN_EDITS):
            self.hub.events.add_edit("luca", "CHANGELOG.md", "task-%d" % (i % crewchat.LEARN_TASKS), self.now - 60 + i)
            self.hub.events.add_edit("luca" if i % 2 else "jade", "src/app.py", "task-%d" % (i % crewchat.LEARN_TASKS),
                                     self.now - 60 + i)
        with mock.patch.object(self.hub, "edits_on", return_value=True):
            found = self.hub.trace_findings(self.now)
            self.assertEqual([f["key"] for f in found], ["file:src/app.py"])  # not the changelog, edited as often
            self.assertIn("across %d tasks" % crewchat.LEARN_TASKS, found[0]["text"])
            self.hub.propose_process(crewchat.OWNER, {"name": "app", "when": "Before you change the app",
                                                      "steps": ["Read it first"], "pitfalls": ["Two agents undo each other"], "files": ["src/app.py"]})
            self.assertEqual(self.hub.trace_findings(self.now), [])  # a note covers it now


if __name__ == "__main__":
    unittest.main()
