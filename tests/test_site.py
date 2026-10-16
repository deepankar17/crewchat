"""The website (site/ builds hosting/public): it builds, every page links only to what exists,
nothing inline breaks under its Content-Security-Policy, and what is committed is what the build
makes."""
import filecmp
import html.parser
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "site"))

import build  # noqa: E402  (site/build.py)


class Page(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.inline, self.ids, self.title = [], [], set(), ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.add(attrs["id"])
        for key in ("href", "src", "poster", "srcset"):
            if attrs.get(key):
                self.links.append(attrs[key])
        if tag == "script" and not attrs.get("src") and attrs.get("type") != "application/ld+json":
            self.inline.append("<script> without src")
        if tag == "style" or "style" in attrs or any(k.startswith("on") for k in attrs):
            self.inline.append("<%s %s>" % (tag, " ".join(attrs)))
        self._in_title = tag == "title"

    def handle_data(self, data):
        if self._in_title:
            self.title += data

    def handle_endtag(self, tag):
        self._in_title = False


class Website(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = Path(tempfile.mkdtemp(prefix="crewchat-site-")) / "public"
        build.build(cls.out)
        cls.pages = {}
        for _, path, _, _ in build.PAGES:
            parser = Page()
            parser.feed((cls.out / path).read_text(encoding="utf-8"))
            cls.pages[path] = parser

    def test_every_page_builds_with_a_title(self):
        self.assertEqual(sorted(self.pages), ["404.html", "index.html", "privacy.html"])
        for path, page in self.pages.items():
            self.assertIn("crewchat", page.title, path)
            self.assertNotIn("{{", (self.out / path).read_text(encoding="utf-8"), path)

    def test_local_links_and_files_exist(self):
        for path, page in self.pages.items():
            for link in page.links:
                url = urlparse(link)
                if url.scheme in ("http", "https"):
                    continue
                self.assertEqual(url.scheme, "", "%s: %s" % (path, link))
                if url.path in ("", "/") and url.fragment:
                    self.assertIn(url.fragment, self.pages["index.html"].ids, "%s: %s" % (path, link))
                    continue
                target = self.out / url.path.lstrip("/")
                if url.path.endswith("/"):
                    target = target / "index.html"
                self.assertTrue(target.is_file(), "%s links to %s, which the build did not make" % (path, link))

    def test_external_links_go_only_where_expected(self):
        hosts = {"github.com", "raw.githubusercontent.com", "pypi.org", "www.youtube.com", "docs.astral.sh",
                 "crewchat-chat.web.app"}
        for path, page in self.pages.items():
            for link in page.links:
                host = urlparse(link).hostname
                if host:
                    self.assertIn(host, hosts, "%s: %s" % (path, link))
                    self.assertTrue(link.startswith("https://"), link)

    def test_nothing_inline_that_the_security_policy_blocks(self):
        policy = json.loads((ROOT / "firebase.json").read_text())["hosting"]["headers"][0]["headers"]
        csp = next(h["value"] for h in policy if h["key"] == "Content-Security-Policy")
        self.assertNotIn("unsafe-inline", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        for path, page in self.pages.items():
            self.assertEqual(page.inline, [], path)

    def test_pages_stay_small(self):
        for path in self.pages:
            self.assertLess((self.out / path).stat().st_size, 40_000, path)
        media = sum(p.stat().st_size for p in (self.out / "assets" / "media").iterdir())
        self.assertLess(media, 4_000_000)  # GIFs as small MP4s: run site/media.py, not copies of docs/images

    def test_the_committed_site_is_what_the_build_makes(self):
        # Edit site/, run python3 site/build.py, and commit hosting/public with it.
        committed = ROOT / "hosting" / "public"
        if not committed.is_dir():
            self.skipTest("hosting/public is not built in this checkout")
        made = {p.relative_to(self.out) for p in self.out.rglob("*") if p.is_file()}
        have = {p.relative_to(committed) for p in committed.rglob("*") if p.is_file()}
        self.assertEqual(made, have)
        for rel in made:
            if rel.suffix in (".html", ".xml", ".txt"):  # the version and year shown move on by themselves
                strip = lambda t: re.sub(r"© \d{4}|crewchat \d[\w.]*", "", t)  # noqa: E731
                self.assertEqual(strip((self.out / rel).read_text(encoding="utf-8")),
                                 strip((committed / rel).read_text(encoding="utf-8")), str(rel))
            else:
                self.assertTrue(filecmp.cmp(self.out / rel, committed / rel, shallow=False), str(rel))

    def test_the_version_shown_is_this_release(self):
        text = (self.out / "index.html").read_text(encoding="utf-8")
        self.assertIn("crewchat %s" % build.version(), text)


if __name__ == "__main__":
    unittest.main()
