"""The GitHub Actions workflows run only for main (owner, 19 Oct 2026): pushes to main and pull requests into it,
plus releases, the schedule and a manual start, which GitHub runs from the default branch. Never for pushes to, or
pull requests into, other branches (dev, task branches). A pull request trigger may name its event types, as the e2e
gate's does to re-check an edited description (owner, 14 Nov 2026). The end-to-end tests run on the public repo only,
weekly and by hand, where Linux runners are free."""
import os
import re
import shutil
import subprocess
import tempfile
import sys
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

    @unittest.skipIf(os.name == "nt" or not shutil.which("bash"), "the scan runs under bash on Linux and macOS")
    def test_a_tree_with_no_lockfile_has_no_dependencies_to_check(self):
        # 1.1.0 review: the public repository has no lockfile, osv-scanner says "No package sources found" and
        # exits non-zero, and every security run on it failed. That is nothing to check, not a finding.
        script = (WORKFLOWS.parents[1] / "scripts" / "security-scan.sh").read_text(encoding="utf-8")
        function = re.search(r"^dependencies\(\) \{\n.*?^\}\n", script, re.M | re.S).group(0)
        tools = Path(tempfile.mkdtemp(prefix="osv-"))
        (tools / "bin").mkdir()
        fake = tools / "bin" / "osv-scanner"

        def run(says, code):
            fake.write_text("#!/bin/sh\necho '%s'\nexit %d\n" % (says, code))
            fake.chmod(0o755)
            return subprocess.run(["bash", "-c", function + "dependencies"], env=dict(os.environ, TOOLS=str(tools)),
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT).returncode

        self.assertEqual(run("No package sources found, --help for usage information.", 128), 0)
        self.assertEqual(run("Scanned requirements.txt: 1 vulnerability", 1), 1)  # a finding still fails
        self.assertEqual(run("No issues found", 0), 0)

    def test_the_mac_jobs_can_run_before_a_release(self):
        # 1.1.0 review: the macOS jobs ran only once a release was published, when PyPI was already on its way, so
        # they could not stop a bad tag. A manual start runs them too; they still never run on a push.
        flow = (WORKFLOWS / "test.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch", triggers(flow))
        for job in ("test-macos", "installers-macos"):
            block = re.search(r"^  %s:\n((?:    [^\n]*\n|\n)*)" % job, flow, re.M).group(1)
            self.assertIn("if: ${{ github.event_name == 'release' || github.event_name == 'workflow_dispatch' }}",
                          block, job)

    def test_workflows_run_only_for_main(self):
        for path in workflow_files():
            for event, lines in triggers(path.read_text(encoding="utf-8")).items():
                self.assertIn(event, ALLOWED, "%s: %s" % (path.name, event))
                if event in BRANCH_EVENTS:
                    self.assertEqual([l for l in lines if not re.match(r"^types: \[[a-z_, ]+\]$", l)],
                                     ["branches: [main]"], "%s: %s must be limited to main" % (path.name, event))

    def test_a_pull_request_may_name_its_types_but_not_other_branches(self):
        # The rule above, on the shapes it must tell apart.
        def ok(block):
            lines = triggers("on:\n" + block)["pull_request"]
            return [l for l in lines if not re.match(r"^types: \[[a-z_, ]+\]$", l)] == ["branches: [main]"]
        self.assertTrue(ok("  pull_request:\n    branches: [main]\n    types: [opened, edited]\n"))
        self.assertFalse(ok("  pull_request:\n    branches: [main, dev]\n    types: [opened]\n"))
        self.assertFalse(ok("  pull_request:\n    types: [opened]\n"))

    def test_the_e2e_tests_run_weekly_and_by_hand_on_the_public_repo_only(self):
        # Owner, 14 Nov 2026: crewchat-dev's private minutes run out, the public repo's Linux runners are free.
        flow = (WORKFLOWS / "e2e.yml").read_text(encoding="utf-8")
        self.assertEqual(sorted(triggers(flow)), ["schedule", "workflow_dispatch"])
        jobs = re.findall(r"^  ([\w-]+):\n((?:    [^\n]*\n|\n)*)", flow.split("\njobs:\n", 1)[1], re.M)
        self.assertTrue(jobs)
        for name, block in jobs:
            self.assertIn("    if: github.repository == 'deepankar17/crewchat'\n", block, name)
            self.assertRegex(block, r"timeout-minutes: \d+", name)
        self.assertIn("python e2e/ci.py", flow)
        self.assertIn("CREWCHAT_CHROME=", flow)
        # The public repo has no tag before v1.0.0: the upgrade tests skip there, and say why, rather than fail.
        install = (WORKFLOWS.parents[1] / "e2e" / "test_install.py").read_text(encoding="utf-8")
        self.assertIn('"rev-parse", "-q", "--verify", "refs/tags/" + tag', install)
        self.assertIn('raise unittest.SkipTest("the old release %s is not in this clone', install)
        self.assertIn('CREWCHAT_E2E_NO_OLD_TAGS: "1"', flow)

    def test_no_workflow_puts_what_a_stranger_wrote_into_a_script(self):
        """A pull request's title, description or branch name goes in through env:, never into run: text."""
        risky = re.compile(r"\$\{\{\s*github\.(?:event\.(?:pull_request|issue|comment|review|head_commit)\.(?:title|body|message)"
                           r"|head_ref|event\.pull_request\.head\.ref)")
        for path in workflow_files():
            text = path.read_text(encoding="utf-8")
            for n, line in enumerate(text.splitlines(), 1):
                if risky.search(line):
                    self.assertRegex(line, r"^\s+[A-Z_]+: \$\{\{", "%s:%d: pass it through env:" % (path.name, n))

    def test_an_e2e_run_that_skipped_chrome_or_uv_fails(self):
        sys.path.insert(0, str(WORKFLOWS.parents[1] / "e2e"))
        try:
            import ci
        finally:
            sys.path.pop(0)
        self.assertEqual(ci.missing([]), [])
        self.assertEqual(ci.missing([("t", "the inbox is a named pipe there"), ("t", "needs uv")]), ["needs uv"])
        self.assertEqual(ci.missing([("a", "needs Google Chrome and websocket-client")] * 2 +
                                    [("b", "needs Chrome, websocket-client and cryptography")]),
                         ["needs Chrome, websocket-client and cryptography", "needs Google Chrome and websocket-client"])
        # A run that records the e2e gate must not skip the upgrade tests for want of an old tag; the public run may.
        tag = ("t", "the old release v0.9.2 is not in this clone (git fetch --tags)")
        self.assertEqual(ci.missing([tag]), [tag[1]])
        self.assertEqual(ci.missing([tag], old_tags=False), [])

    def test_a_release_carries_its_checksums_and_pypi_gets_only_packages(self):
        """Path to 10 (Security): SHA256SUMS is attached to every release, and made outside dist/, which the PyPI job
        uploads whole (a stray file there fails the upload)."""
        text = (WORKFLOWS / "release-assets.yml").read_text(encoding="utf-8")
        self.assertIn("(cd dist && sha256sum *.whl *.tar.gz) > SHA256SUMS", text)
        self.assertIn("for f in dist/*.whl dist/*.tar.gz SHA256SUMS; do", text)
        self.assertNotIn("> dist/SHA256SUMS", text)
        security = (WORKFLOWS.parents[1] / "SECURITY.md").read_text(encoding="utf-8")
        self.assertIn("sha256sum -c SHA256SUMS", security)

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
