"""The website (site/ builds hosting/public): it builds, every page links only to what exists, and
nothing inline breaks under its Content-Security-Policy."""
import html.parser
import json
import re
import shutil
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
        if tag == "script" and not attrs.get("src"):
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
        temp = tempfile.mkdtemp(prefix="crewchat-site-")
        cls.addClassCleanup(shutil.rmtree, temp, True)
        cls.out = Path(temp) / "public"
        build.build(cls.out)
        cls.pages = {}
        for _, path, _, _ in build.PAGES:
            parser = Page()
            parser.feed((cls.out / path).read_text(encoding="utf-8"))
            cls.pages[path] = parser

    def test_every_page_builds_with_a_title(self):
        for path, page in self.pages.items():
            self.assertIn("crewchat", page.title, path)

    def test_local_links_and_files_exist(self):
        for path, page in self.pages.items():
            for link in page.links:
                url = urlparse(link)
                if url.scheme in ("http", "https"):
                    continue
                self.assertEqual(url.scheme, "", "%s: %s" % (path, link))
                if url.fragment and url.path in ("", "/"):  # "#x" on this page, "/#x" on the home page
                    ids = page.ids if url.path == "" else self.pages["index.html"].ids
                    self.assertIn(url.fragment, ids, "%s: %s" % (path, link))
                    continue
                target = self.out / url.path.lstrip("/")
                if url.path.endswith("/"):
                    target = target / "index.html"
                self.assertTrue(target.is_file(), "%s links to %s, which the build did not make" % (path, link))

    def test_external_links_go_only_where_expected(self):
        config = json.loads((ROOT / "site" / "config.json").read_text())
        hosts = {urlparse(v).hostname for v in config.values()} | {"raw.githubusercontent.com", "docs.astral.sh"}
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

    def test_the_privacy_page_says_what_docs_privacy_says(self):
        # privacy.html is written from docs/privacy.md: each thing crewchat connects to is on both.
        doc = (ROOT / "docs" / "privacy.md").read_text(encoding="utf-8")
        page = (self.out / "privacy.html").read_text(encoding="utf-8")
        points = re.findall(r"^- \*\*([^*]+)\*\*", doc, re.M)
        self.assertGreaterEqual(len(points), 4)
        for point in points:
            self.assertIn("<b>%s</b>" % point, page, "docs/privacy.md lists it; the site's privacy page should too")

    def test_the_demo_is_the_apps_own_view_on_its_sample(self):
        # Roadmap X3: crewchat.dev/demo runs the analytics view from crewchat.py itself, as plain files (the site's
        # policy allows no inline script or style), in the chat page's colours, with the pictures its sample crew
        # wears; the page says it is a demo, and the home page links to it.
        script, styles, pictures = build.chat_parts()
        demo = self.out / "demo"
        self.assertEqual((demo / "analytics.js").read_text(encoding="utf-8"), script)
        self.assertIn("window.crewAnalytics = {", script)
        css = (demo / "analytics.css").read_text(encoding="utf-8")
        for needed in ("--a7:", "@media (prefers-color-scheme: dark)", ".ax-card", ".ax-demo-note"):
            self.assertIn(needed, css)
        worn = re.findall(r'\["\w+", "\w+", "(\w+)", "[\w-]+", "[\w-]+", "\w*"\]', script)
        self.assertEqual(len(worn), 8)
        for name in worn:
            self.assertEqual((demo / "pictures" / (name + ".webp")).read_bytes()[:4], b"RIFF", name)
        page = (demo / "index.html").read_text(encoding="utf-8")
        self.assertIn("with sample data", page)
        self.assertIn('id="ax-style"', page)  # so the view does not add its styles inline
        self.assertIn('href="/demo/"', (self.out / "index.html").read_text(encoding="utf-8"))
        self.assertIn("https://crewchat.dev/demo/", (self.out / "sitemap.xml").read_text(encoding="utf-8"))

    def test_the_error_page_is_not_indexed(self):
        self.assertIn('<meta name="robots" content="noindex">', (self.out / "404.html").read_text(encoding="utf-8"))
        self.assertNotIn("404", (self.out / "sitemap.xml").read_text(encoding="utf-8"))

    def test_the_version_shown_is_this_release(self):
        text = (self.out / "index.html").read_text(encoding="utf-8")
        import crewchat  # noqa: E402  (build.py reads the version without importing it)
        self.assertEqual(build.version(), crewchat.__version__)
        self.assertIn("crewchat %s" % crewchat.__version__, text)


if __name__ == "__main__":
    unittest.main()
