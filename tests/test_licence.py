"""crewchat's licence: GNU AGPL-3.0-only, with the attribution term in NOTICE, everywhere the licence is named.

1.2.0 shipped the MIT text again and lost NOTICE, so each place that names the licence is checked here."""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return (ROOT / path).read_text(encoding="utf-8")


class Licence(unittest.TestCase):
    def test_license_is_the_agpl_text(self):
        text = read("LICENSE")
        self.assertTrue(text.lstrip().startswith("GNU AFFERO GENERAL PUBLIC LICENSE"), text[:80])
        self.assertIn("Version 3, 19 November 2007", text)
        self.assertNotIn("Permission is hereby granted, free of charge", text)  # the MIT text

    def test_notice_keeps_the_copyright_and_the_attribution_term(self):
        text = read("NOTICE")
        self.assertIn("Copyright (C) 2026 Deepankar Sarkar (deepankar17)", text)
        self.assertIn("section 7(b)", text)
        self.assertIn("Based on crewchat by Deepankar Sarkar (https://github.com/deepankar17/crewchat)", text)
        self.assertIn("crewchat 1.1.0 and earlier were released under the MIT License", text)
        self.assertIn("Fluent Emoji", text)

    def test_the_package_declares_agpl_and_ships_notice(self):
        text = read("pyproject.toml")
        self.assertRegex(text, r'(?m)^license = "AGPL-3\.0-only"$')
        self.assertRegex(text, r'(?m)^license-files = \["LICENSE", "NOTICE"\]$')
        self.assertRegex(text, r'setuptools>=(7[7-9]|[89]\d)')  # an SPDX licence string needs setuptools 77
        self.assertNotIn("MIT", text)

    def test_the_plugin_manifests_say_agpl(self):
        for path in ("plugin/.claude-plugin/plugin.json", "plugin/.cursor-plugin/plugin.json"):
            self.assertEqual(json.loads(read(path))["license"], "AGPL-3.0-only", path)

    def test_the_docker_image_copies_notice(self):
        self.assertRegex(read("glama/Dockerfile"), r"(?m)^COPY .*\bLICENSE NOTICE\b")

    def test_the_readme_names_the_agpl_and_the_credit(self):
        text = read("README.md")
        section = text.split("## Licence", 1)[1].split("\n## ", 1)[0]
        self.assertIn("GNU AGPL-3.0", section)
        self.assertIn("NOTICE", section)
        self.assertIn('"Based on crewchat by Deepankar Sarkar"', section)
        self.assertNotIn('alt="license: MIT"', text)
        self.assertFalse(re.search(r"(?m)^MIT\. See", section))


if __name__ == "__main__":
    unittest.main()
