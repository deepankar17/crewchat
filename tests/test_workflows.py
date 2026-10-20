"""The GitHub Actions workflows run only for main (owner, 19 Oct 2026): pushes to main and pull requests into it,
plus releases, the schedule and a manual start, which GitHub runs from main. Never for pushes to, or pull requests
into, other branches (dev, task branches)."""
import re
import unittest
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
BRANCH_EVENTS = ("push", "pull_request", "pull_request_target")
ALLOWED = BRANCH_EVENTS + ("release", "schedule", "workflow_dispatch")


def triggers(text):
    """{event: its lines} of a workflow's `on:` block (standard library only: no YAML reader)."""
    block = re.search(r"^on:[^\n]*\n((?:[ \t]+[^\n]*\n|\n)*)", text, re.M)
    assert block, "no on: block"
    out, event = {}, None
    for line in block.group(1).splitlines():
        found = re.match(r"^  ([a-z_]+):", line)
        if found:
            event = found.group(1)
            out[event] = []
        elif event and line.strip():
            out[event].append(line.strip())
    return out


class Workflows(unittest.TestCase):
    def test_workflows_run_only_for_main(self):
        files = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
        self.assertTrue(files)
        for path in files:
            for event, lines in triggers(path.read_text(encoding="utf-8")).items():
                self.assertIn(event, ALLOWED, "%s: %s" % (path.name, event))
                if event in BRANCH_EVENTS:
                    self.assertEqual(lines, ["branches: [main]"], "%s: %s must be limited to main" % (path.name, event))


if __name__ == "__main__":
    unittest.main()
