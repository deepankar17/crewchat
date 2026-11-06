"""A feedback channel without telemetry (review finding 7): `crewchat feedback` opens a GitHub issue form with
the version and system filled in, and sends nothing itself."""
import re
import sys
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import crewchat  # noqa: E402

FORMS = ROOT / ".github" / "ISSUE_TEMPLATE"


class Feedback(unittest.TestCase):
    def test_each_kind_has_a_form_with_the_fields_the_link_fills_in(self):
        for kind, form in crewchat.FEEDBACK_KINDS.items():
            text = (FORMS / form).read_text(encoding="utf-8")
            ids = set(re.findall(r"^\s+id: (\S+)$", text, re.M))
            self.assertTrue({"version", "system"} <= ids, (kind, ids))

    def test_the_link_names_the_form_version_and_system(self):
        url = crewchat.feedback_url("bug")
        self.assertTrue(url.startswith("https://github.com/deepankar17/crewchat/issues/new?"))
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(query["template"], ["bug.yml"])
        self.assertEqual(query["version"], [crewchat.__version__])
        self.assertIn("Python", query["system"][0])

    def test_feedback_opens_the_browser_and_sends_nothing(self):
        args = crewchat.build_parser().parse_args(["feedback", "works"])
        with mock.patch.object(crewchat.webbrowser, "open") as opened, \
                mock.patch.object(crewchat.urllib.request, "urlopen") as fetched, \
                mock.patch("sys.stdout"):
            crewchat.cmd_feedback(args)
        opened.assert_called_once()
        self.assertIn("template=works-for-me.yml", opened.call_args[0][0])
        fetched.assert_not_called()

    def test_print_only_prints(self):
        with mock.patch.object(crewchat.webbrowser, "open") as opened, mock.patch("sys.stdout"):
            crewchat.cmd_feedback(mock.Mock(kind="idea", print=True))
        opened.assert_not_called()

    def test_commands_md_lists_it(self):
        self.assertIn("crewchat feedback", (ROOT / "docs" / "commands.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
