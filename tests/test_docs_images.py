"""The docs' pictures: every image a page shows exists, and each animated diagram's alt text says what
the diagram plays (its title, every step's caption in order, and its summary), so the two stay alike
when a diagram changes (tools/diagrams/specs.py)."""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "diagrams"))

from specs import SPECS  # noqa: E402

PAGES = [ROOT / "README.md"] + sorted((ROOT / "docs").glob("*.md"))
MARKDOWN = re.compile(r"!\[([^\]]*)\]\((?:docs/)?images/([\w.-]+)\)")
TAG = re.compile(r"<img\b[^>]*>")


def sentence(text):
    text = text.strip()
    return text if text[-1:] in ".?!" else text + "."


def diagram_alt(spec):
    """The alt text a diagram's spec makes: its title, then '1: ...' for each step, then its summary."""
    parts = [sentence(spec["title"])] + ["%d: %s" % (i, sentence(s["caption"])) for i, s in enumerate(spec["steps"], 1)]
    return " ".join(parts + ([sentence(spec["summary"])] if spec.get("summary") else []))


def pictures(page):
    """(file name, alt text) of each picture on a page, in Markdown or as an <img> tag."""
    text = page.read_text(encoding="utf-8")
    found = [(m.group(2), m.group(1)) for m in MARKDOWN.finditer(text)]
    for tag in TAG.findall(text):
        src, alt = re.search(r'src="([^"]+)"', tag), re.search(r'alt="([^"]*)"', tag)
        if src and "images/" in src.group(1):
            found.append((src.group(1).split("images/")[-1], alt.group(1).replace("&quot;", '"') if alt else ""))
    return found


class DocsImages(unittest.TestCase):
    def test_every_picture_exists_and_has_alt_text(self):
        for page in PAGES:
            for name, alt in pictures(page):
                self.assertTrue((ROOT / "docs" / "images" / name).exists(), "%s: %s" % (page.name, name))
                self.assertTrue(alt.strip(), "%s: %s has no alt text" % (page.name, name))

    def test_a_diagrams_alt_text_says_what_it_plays(self):
        shown = 0
        for page in PAGES:
            for name, alt in pictures(page):
                if name.startswith("diagram-") and name.endswith(".gif"):
                    shown += 1
                    self.assertEqual(alt, diagram_alt(SPECS[name[len("diagram-"):-len(".gif")]]), "%s: %s" % (page.name, name))
        self.assertGreater(shown, 15)


if __name__ == "__main__":
    unittest.main()
