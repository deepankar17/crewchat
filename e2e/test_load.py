"""Many agents on several machines at once: nothing lost, nothing twice, one taker per task."""
import random
import re
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

import harness as H
from harness import ToolError, eventually
from test_linked_machines import same_chat

PER_AGENT = int(__import__("os").environ.get("CREWCHAT_LOAD", "20"))  # messages each agent sends


class Load(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.machines = [H.Machine(n) for n in ("north", "south", "east")]
        for m in cls.machines:
            m.start()
        cls.machines[1].link_to(cls.machines[0])
        cls.machines[2].link_to(cls.machines[0])
        cls.agents = []
        for m in cls.machines:
            for i in range(4):
                a = m.agent(client="claude" if i % 2 == 0 else "cursor")
                a.link()
                cls.agents.append(a)
        names = [a.name for a in cls.agents]
        cls.assert_unique = len(names) == len(set(names))
        for m in cls.machines:
            eventually(lambda: all(m.owner.sees(n) for n in names), timeout=60, what="everyone everywhere")
        for a in cls.agents:
            a.call("hub_inbox")

    @classmethod
    def tearDownClass(cls):
        for m in cls.machines:
            m.stop()

    def test_names_are_unique_across_machines(self):
        self.assertTrue(self.assert_unique, [a.name for a in self.agents])

    def test_hundreds_of_messages_at_once(self):
        rng = random.Random(7)
        plan = []  # (sender, recipient, text)
        for a in self.agents:
            for i in range(PER_AGENT):
                to = rng.choice([b.name for b in self.agents if b is not a] + ["all"])
                plan.append((a, to, "load %s %d" % (a.name, i)))
        expected = {a.name: set() for a in self.agents}
        for a, to, text in plan:
            for b in self.agents:
                if b is not a and to in ("all", b.name):
                    expected[b.name].add(text)
        by_sender = {}
        for a, to, text in plan:
            by_sender.setdefault(a.name, []).append((a, to, text))

        def send_all(items):
            for a, to, text in items:
                a.call("hub_send", to=to, text=text)

        with ThreadPoolExecutor(len(self.agents)) as pool:
            list(pool.map(send_all, by_sender.values()))
        got = {a.name: [] for a in self.agents}

        def drained():
            for a in self.agents:
                got[a.name] += re.findall(r"load \S+ \d+", a.call("hub_inbox"))
            return all(set(got[n]) >= expected[n] for n in expected)

        eventually(drained, timeout=120, what="every agent to get its messages", every=1)
        for name, texts in got.items():
            self.assertEqual(len(texts), len(set(texts)), "%s got a message twice" % name)
            self.assertEqual(set(texts), expected[name], name)
        chat = same_chat(*self.machines, timeout=60)
        self.assertEqual(len([m for m in chat if m[3].startswith("load ")]), len(plan))

    def test_many_tasks_raced_by_everyone(self):
        tasks = []
        for i in range(8):
            origin = self.machines[i % 3]
            tasks.append(origin.owner.task("all", "load task %d" % i))
        for a in self.agents:
            eventually(lambda: all(t in a.call("hub_history", limit=50) for t in tasks), timeout=60,
                       what="the tasks to reach %s" % a.name)
        wins = {t: [] for t in tasks}
        lock = threading.Lock()

        def race(agent):
            for t in random.Random(agent.name).sample(tasks, len(tasks)):
                try:
                    agent.call("hub_take", id=t)
                    with lock:
                        wins[t].append(agent.name)
                except ToolError as e:
                    self.assertIn("already taken", str(e))

        with ThreadPoolExecutor(len(self.agents)) as pool:
            list(pool.map(race, self.agents))
        for t, winners in wins.items():
            self.assertEqual(len(winners), 1, "task %s: %s" % (t, winners))
        for m in self.machines:
            eventually(lambda: all(m.owner.poll()["taken"].get(t) == w[0] for t, w in wins.items()), timeout=60,
                       what="%s to agree on who took what" % m.name)


if __name__ == "__main__":
    unittest.main()
