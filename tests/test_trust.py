"""The trust model as it is (review finding 6): agents run as the owner's user, so the docs say so and
the guide tells agents not to act as the owner."""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ["CREWCHAT_NO_SHORTCUT"] = "1"  # no test, nor a child process of one, makes a real shortcut
import crewchat  # noqa: E402


class TrustModel(unittest.TestCase):
    def test_the_guide_tells_agents_not_to_act_as_the_owner(self):
        self.assertIn("Never act as the owner", crewchat.RULES)
        self.assertIn("crewchat say", crewchat.RULES)

    def test_security_md_does_not_claim_agents_cannot_post_as_the_owner(self):
        text = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        self.assertNotIn("and cannot post as the owner. Only", text)
        self.assertIn("## Agents run as you", text)
        self.assertNotIn("strict\n  Content-Security-Policy", text)  # it allows unsafe-inline

    def test_security_md_says_windows_has_no_file_modes(self):
        text = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        self.assertIn("Windows has no such file modes", text)

    def test_the_browser_docs_say_the_gate_can_be_got_round(self):
        text = (ROOT / "docs" / "teams.md").read_text(encoding="utf-8")
        self.assertIn("SECURITY.md#agents-run-as-you", text)

    def test_starting_cursor_from_the_page_says_it_trusts_the_folder(self):
        """Review 14 Nov: Cursor agents launched from the page get --trust, and the launch dialog did not say so."""
        notes = [item["text"] for item in crewchat.folder_ready("cursor", str(ROOT))]
        self.assertTrue(any("--trust" in text for text in notes), notes)
        self.assertIn("--trust", crewchat.LAUNCH_BASE["cursor"])

    def test_security_md_covers_the_extras_and_the_names_it_answers(self):
        text = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        self.assertIn("## Jira, Google Chat and the phone", text)
        self.assertIn("DNS rebinding", text)
        self.assertNotIn("must\n  join again", text)


if __name__ == "__main__":
    unittest.main()
