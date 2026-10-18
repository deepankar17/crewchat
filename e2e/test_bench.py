"""The benchmark (bench.py, roadmap P0) runs, measures what it says, and catches a regression."""
import json
import statistics
import os
import tempfile
import unittest

import bench


class Bench(unittest.TestCase):
    def test_a_small_run_measures_everything(self):
        numbers = bench.measure(agents=2, messages=30, runs=2, say=lambda *a: None, log_mb=2)
        self.assertGreater(numbers.pop("messages.jsonl bytes, grown"), 2 * 1024 * 1024 * 0.9)
        self.assertTrue(numbers.pop("server startup, 2 MB log (s)"))
        self.assertLess(statistics.median(numbers.pop("analytics: answer, 50,000 messages (s)")), 0.2)  # X1's limit
        self.assertTrue(numbers.pop("analytics: build, 50,000 messages (s)"))
        for k in ("server memory after startup (MB)", "server memory, 2 MB log (MB)"):
            self.assertGreater(numbers.pop(k, 1), 0, k)  # where ps can read it
        self.assertEqual(set(numbers), {
            "messages.jsonl bytes", "hook: tool call, tells the server (s)", "hook: tool call, skipped (s)",
            "hook: prompt (s)", "hook: end of turn, nothing new (s)", "poll: open the chat (s)",
            "poll: open the chat (bytes)", "poll: one new message (s)", "poll: one new message (bytes)",
            "page: first load (bytes)", "page: reload (bytes)",
            "tools/list (chars)", "hub_inbox, 50 unread (chars)", "state.json writes, a busy load (count)",
            "server startup (s)"})
        for k, v in numbers.items():
            if k.endswith("(s)"):
                self.assertEqual(len(v), 2, k)
                self.assertTrue(all(0 < t < 30 for t in v), (k, v))
            else:
                self.assertGreater(v, 0, k)
        self.assertGreater(numbers["poll: open the chat (bytes)"], numbers["poll: one new message (bytes)"])
        self.assertIn("server startup (s)", bench.table(numbers))

    def test_compare_fails_on_a_median_over_20_percent_worse(self):
        base = {"a (s)": 0.100, "b (bytes)": 1000, "c (s)": 0.0}
        self.assertEqual(bench.regressions(base, {"a (s)": 0.119, "b (bytes)": 900, "c (s)": 5, "new (s)": 1}), [])
        self.assertEqual(bench.regressions(base, {"a (s)": 0.121, "b (bytes)": 1300}),
                         [("a (s)", 0.100, 0.121), ("b (bytes)", 1000, 1300)])
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "base.json")
            with open(path, "w") as f:
                json.dump({"server startup (s)": 1e-9}, f)
            self.assertEqual(bench.main(["--agents", "1", "--messages", "5", "--runs", "1", "--compare", path]), 1)


if __name__ == "__main__":
    unittest.main()
