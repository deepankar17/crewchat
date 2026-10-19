"""Build the crewchat website into hosting/public: the pages from site/templates, the assets from
site/assets. Standard library only. `python3 site/build.py`, then `firebase deploy --only hosting`
from the repository root. Never edit hosting/public by hand: the build replaces it.

Templates are plain HTML with a few tags:
  {{name}}             a value from site/config.json (or version, year, canonical...), HTML-escaped
  {{> main}}           in base.html: the page itself
  {{clip:NAME|ALT}}    a looping demo video from site/assets/media (NAME.mp4 with NAME.jpg as poster)
  {{still:NAME|ALT}}   a picture from site/assets/media (NAME.jpg)
  {{shot:ALT}}         the chat page screenshot, light or dark as the reader's system is
A template that is a whole page (it starts with <!DOCTYPE) is used as it is, without base.html: the analytics
demo, whose page is the chat page's view. Its script, styles and pictures come from crewchat.py.
The site is built into hosting/public, which git ignores: a deploy builds it first (firebase.json).
"""
import base64
import datetime
import html
import json
import re
import shutil
import sys
from pathlib import Path

SITE = Path(__file__).resolve().parent
ROOT = SITE.parent
OUT = ROOT / "hosting" / "public"
PAGES = [  # (template, output, title, description)
    ("home.html", "index.html", "crewchat: a group chat for your AI coding agents",
     "crewchat gives Claude Code, Cursor, Codex, Gemini CLI, Qwen Code, OpenCode and any MCP agent a shared chat: "
     "they join by themselves, talk to each other and share out work, and you read and write to all of them from "
     "one page. Free and open source."),
    ("privacy.html", "privacy.html", "Privacy · crewchat",
     "crewchat collects nothing: no account, no analytics, no telemetry. What it connects to, and only that."),
    ("404.html", "404.html", "Page not found · crewchat", "This page is not on the crewchat site."),
    ("demo.html", "demo/index.html", "Live demo: the analytics view · crewchat",
     "A made-up crew of AI coding agents and a day of their work in crewchat's analytics view: who writes to whom, "
     "tasks flowing to done, each agent's day, tokens and cost. It runs in your browser, on sample data."),
]
DEMO_PICTURES = ("office", "coder", "technologist", "scientist", "mechanic", "detective", "teacher", "artist")


def version():
    text = (ROOT / "crewchat.py").read_text(encoding="utf-8")
    return re.search(r'^__version__ = "([^"]+)"', text, re.M).group(1)


def media(name, sizes):
    if name not in sizes:
        sys.exit("site/build.py: %s is not in site/media.json; run python3 site/media.py" % name)
    return sizes[name]


def clip(name, alt, sizes):
    width, height = media(name + ".mp4", sizes)
    # No autoplay: that would fetch every video at once. site.js plays each one while it is on screen
    # (muted and playsinline so it may); the poster shows until then, or always without JavaScript.
    return ('<video class="clip%s" src="/assets/media/%s.mp4" poster="/assets/media/%s.jpg" width="%d" height="%d" '
            'muted loop playsinline preload="none" aria-label="%s"></video>'
            % (" tall" if height > width else "", name, name, width, height, html.escape(alt)))


def still(name, alt, sizes):
    width, height = media(name + ".jpg", sizes)
    return ('<img class="still" src="/assets/media/%s.jpg" width="%d" height="%d" alt="%s" loading="lazy" decoding="async">'
            % (name, width, height, html.escape(alt)))


def shot(alt, sizes):
    (dark_w, dark_h), (width, height) = media("screenshot-dark.jpg", sizes), media("screenshot-light.jpg", sizes)
    return ('<picture class="shot"><source srcset="/assets/media/screenshot-dark.jpg" width="%d" height="%d" '
            'media="(prefers-color-scheme: dark)"><img src="/assets/media/screenshot-light.jpg" width="%d" height="%d" '
            'alt="%s" fetchpriority="high"></picture>' % (dark_w, dark_h, width, height, html.escape(alt)))


TAGS = {"clip": clip, "still": still}


def render(text, values, sizes):
    text = re.sub(r"\{\{(clip|still):([\w-]+)\|([^}]*)\}\}", lambda m: TAGS[m.group(1)](m.group(2), m.group(3), sizes), text)
    text = re.sub(r"\{\{shot:([^}]*)\}\}", lambda m: shot(m.group(1), sizes), text)

    def value(m):
        if m.group(1) not in values:
            sys.exit("site/build.py: no value for {{%s}}" % m.group(1))
        return html.escape(str(values[m.group(1)]))
    text = re.sub(r"\{\{(\w+)\}\}", value, text)
    if "{{" in text:
        sys.exit("site/build.py: a tag was left unfilled: %s" % re.search(r"\{\{[^}]*\}\}", text).group(0))
    return text


def url(base_url, path):
    return base_url + "/" + (path[:-len("index.html")] if path.endswith("index.html") else path)


def chat_parts():
    """From crewchat.py: the analytics view's script; its styles after the chat page's colours (light and dark), so
    it looks as it does in the app; and the built-in pictures, by name."""
    text = (ROOT / "crewchat.py").read_text(encoding="utf-8")
    script = re.search(r'^ANALYTICS_JS = r"""(.*?)^"""$', text, re.M | re.S).group(1)
    styles = re.search(r"^  const CSS = `(.*?)`;$", script, re.M | re.S).group(1)
    colours = re.search(r"^(:root \{\n  color-scheme: light dark;.*?^\}\n@media \(prefers-color-scheme: dark\) \{.*?^\})$",
                        text, re.M | re.S).group(1)
    block = text[text.index("\nART_DATA = {"):]
    pictures = dict(re.findall(r'^    "(\w+)": "([A-Za-z0-9+/=]+)",?$', block[:block.index("\n}\n")], re.M))
    return script, colours + "\n" + styles, pictures


def demo(out):
    """The analytics demo's files: the view's script and styles, and the pictures its sample crew wears."""
    script, styles, pictures = chat_parts()
    folder = out / "demo"
    (folder / "pictures").mkdir(parents=True, exist_ok=True)
    (folder / "analytics.js").write_text(script, encoding="utf-8")
    (folder / "analytics.css").write_text(styles, encoding="utf-8")
    for name in DEMO_PICTURES:
        (folder / "pictures" / (name + ".webp")).write_bytes(base64.b64decode(pictures[name]))


def build(out=OUT):
    config = json.loads((SITE / "config.json").read_text(encoding="utf-8"))
    sizes = json.loads((SITE / "media.json").read_text(encoding="utf-8"))
    base = (SITE / "templates" / "base.html").read_text(encoding="utf-8")
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(SITE / "assets", out / "assets")
    shutil.copyfile(ROOT / "docs" / "images" / "logo.svg", out / "favicon.svg")  # the logo, in the header too
    base_url = config["base_url"].rstrip("/")
    common = dict(config, version=version(), year=datetime.date.today().year)
    for template, path, title, description in PAGES:
        page = (SITE / "templates" / template).read_text(encoding="utf-8")
        values = dict(common, title=title, description=description, canonical=url(base_url, path),
                      robots="noindex" if path == "404.html" else "index, follow")
        whole = page if page.lstrip().startswith("<!DOCTYPE") else base.replace("{{> main}}", page)
        (out / path).parent.mkdir(parents=True, exist_ok=True)
        (out / path).write_text(render(whole, values, sizes), encoding="utf-8")
    demo(out)
    (out / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "".join("  <url><loc>%s</loc></url>\n" % url(base_url, p) for _, p, _, _ in PAGES if p != "404.html")
        + "</urlset>\n", encoding="utf-8")
    (out / "robots.txt").write_text("User-agent: *\nAllow: /\nSitemap: %s/sitemap.xml\n" % base_url, encoding="utf-8")
    return out


if __name__ == "__main__":
    out = build()
    print("Built %d pages into %s." % (len(PAGES), out.relative_to(ROOT)))
