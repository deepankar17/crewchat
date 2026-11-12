"""Windows install instructions never leave a blocked command: Smart App Control blocks the crewchat.exe
uv makes, and only crewchat's installer (install.ps1) adds the crewchat.cmd that runs crewchat by Python."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = "irm https://crewchat.dev/install.ps1"


def windows_part(text, start, end):
    i = text.index(start)
    j = text.index(end, i + len(start)) if end in text[i + len(start):] else len(text)
    return text[i:j]


class WindowsInstall(unittest.TestCase):
    def test_the_guides_install_windows_with_the_installer_not_a_bare_uv(self):
        for name, start, end in (("docs/getting-started.md", "**Windows** (PowerShell)", "With uv already installed"),
                                 ("README.md", "**Windows** (PowerShell)", "**From Claude Code**"),
                                 ("plugin/skills/setup/SKILL.md", "**Windows:**", "**macOS or Linux:**")):
            part = windows_part((ROOT / name).read_text(encoding="utf-8"), start, end)
            self.assertIn(INSTALLER, part, name)
            self.assertNotRegex(part, r"(?m)^\s*`?uv tool install", name)

    def test_the_site_does_not_offer_windows_a_bare_uv_install(self):
        path = ROOT / "site" / "templates" / "home.html"
        if not path.exists():  # the public copy has no website
            self.skipTest("no site here")
        home = path.read_text(encoding="utf-8")
        card = re.search(r"<h3>Windows</h3>(.*?)</article>", home, re.S)
        if card:  # the site's Windows card, where it has one
            self.assertIn("install.ps1", card.group(1))
            self.assertNotIn("uv tool install", card.group(1))


if __name__ == "__main__":
    unittest.main()
