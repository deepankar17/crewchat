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


def workflow_files():
    files = sorted(WORKFLOWS.glob("*.yml")) + sorted(WORKFLOWS.glob("*.yaml"))
    assert files, "no workflows"
    return files


class Workflows(unittest.TestCase):
    def test_a_manual_security_run_checks_everything(self):
        # Release review: a manual run had no base, so it compared the code with itself and checked nothing; and
        # the script stopped on uncommitted changes as if it had found something.
        flow = (WORKFLOWS / "security.yml").read_text(encoding="utf-8")
        self.assertIn("SCAN_FULL: ${{ github.event_name == 'workflow_dispatch' && '1' || '' }}", flow)
        script = (WORKFLOWS.parents[1] / "scripts" / "security-scan.sh").read_text(encoding="utf-8")
        self.assertIn('FULL="${SCAN_FULL:-}"', script)
        self.assertIn("commit or stash them first", script)

    def test_workflows_run_only_for_main(self):
        for path in workflow_files():
            for event, lines in triggers(path.read_text(encoding="utf-8")).items():
                self.assertIn(event, ALLOWED, "%s: %s" % (path.name, event))
                if event in BRANCH_EVENTS:
                    self.assertEqual(lines, ["branches: [main]"], "%s: %s must be limited to main" % (path.name, event))

    def test_actions_are_pinned_to_a_commit(self):
        """A tag like @v4 can be moved to other code; a commit cannot. The version is kept in a comment."""
        for path in workflow_files():
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                found = re.search(r"\buses:\s*(\S+)(.*)", line)
                if not found or found.group(1).startswith("./"):
                    continue
                self.assertRegex(found.group(1), r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$", "%s:%d" % (path.name, n))
                self.assertRegex(found.group(2), r"^\s+# v\d+\.\d+\.\d+$", "%s:%d: keep the version in a comment" % (path.name, n))

    def test_nothing_downloaded_is_piped_into_a_shell(self):
        """A download is saved and checked (a checksum or the repository's own copy) before it runs."""
        for path in workflow_files():
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                self.assertNotRegex(line, r"\|\s*(?:env\b[^|]*\s)?(?:sh|bash|zsh|python3?|tar)\b", "%s:%d" % (path.name, n))


if __name__ == "__main__":
    unittest.main()
