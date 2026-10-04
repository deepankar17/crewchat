"""YouTube thumbnails for the tutorial videos: a bold headline beside a diagram from the video.

    pip install websocket-client
    python3 tools/tutorial/thumbnails.py      # writes media/thumbnail-tour.png, media/thumbnail-firebase.png

The diagrams come from tools/diagrams (their last frame, with every step played).
"""
import base64
import html
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "tools" / "diagrams"))
import diagrams as D  # noqa: E402

OUT = ROOT / "media"
THUMBS = {
    "tour": {"diagram": "architecture", "chip": "Full setup + tour",
             "headline": "All your AI<br>agents, <em>one</em><br>group chat", "foot": "Claude Code · Cursor · any MCP agent"},
    "firebase": {"diagram": "cloud-design", "chip": "End-to-end encrypted",
                 "headline": "Link your<br>machines<br>with <em>Firebase</em>", "foot": "Agents on any network, one chat"},
}

PAGE = """<!doctype html><meta charset="utf-8"><style>
html, body { margin: 0; width: 1280px; height: 720px; overflow: hidden;
  font-family: -apple-system, "SF Pro Display", "Helvetica Neue", Arial, sans-serif; }
body { background: radial-gradient(circle at 78%% 30%%, #2a4fb8 0%%, #142a6b 45%%, #0a1533 100%%); color: #fff; position: relative; }
.dots { position: absolute; inset: 0; background-image: radial-gradient(rgba(255,255,255,.08) 1.5px, transparent 1.5px);
  background-size: 28px 28px; }
.left { position: absolute; left: 60px; top: 56px; width: 540px; }
.brand { display: flex; align-items: center; gap: 16px; font-size: 44px; font-weight: 800; letter-spacing: -1px; }
.brand img { width: 72px; height: 72px; } .brand b { color: #8db2ff; }
.chip { display: inline-block; margin-top: 34px; padding: 10px 22px; border-radius: 999px; background: #ffd23f; color: #1b1300;
  font-size: 26px; font-weight: 800; text-transform: uppercase; letter-spacing: .5px; }
h1 { margin: 28px 0 0; font-size: 82px; line-height: 1.02; font-weight: 900; letter-spacing: -2.5px; }
h1 em { font-style: normal; color: #ffd23f; }
.foot { position: absolute; left: 60px; bottom: 50px; font-size: 28px; font-weight: 600; color: #b9c8ee; }
.card { position: absolute; right: -170px; top: 150px; width: 860px; border-radius: 26px; overflow: hidden; background: #fff;
  transform: rotate(-4deg); box-shadow: 0 40px 80px rgba(0,0,0,.45), 0 0 0 6px rgba(255,255,255,.12); }
.card img { display: block; width: 100%%; }
</style>
<div class="dots"></div>
<div class="left">
  <div class="brand"><img src="%s"><span>crew<b>chat</b></span></div>
  <div class="chip">%s</div>
  <h1>%s</h1>
</div>
<div class="foot">%s</div>
<div class="card"><img src="%s"></div>"""


def main():
    browser = D.shots.Browser()
    logo = "data:image/svg+xml;base64," + base64.b64encode(D.crewchat.LOGO_FILE).decode()
    try:
        for name, t in THUMBS.items():
            # The diagram with every step played, without its title and caption.
            browser.size(1280, 720, scale=2)
            browser.open(D.page(t["diagram"], D.SPECS[t["diagram"]]), wait=0.8)
            total = browser.js("window.TOTAL")
            browser.js("window.STILL_LINES = true; render(%f)" % (total - 0.05))
            shot = browser.call("Page.captureScreenshot", format="png",
                                clip={"x": 20, "y": 100, "width": 1240, "height": 540, "scale": 1})["data"]
            page = D.shots.TMP / ("thumb-%s.html" % name)
            page.write_text(PAGE % (logo, html.escape(t["chip"]), t["headline"], html.escape(t["foot"]),
                                    "data:image/png;base64," + shot), encoding="utf-8")
            browser.size(1280, 720, scale=1)
            browser.open(page.as_uri(), wait=1.0)
            data = base64.b64decode(browser.call("Page.captureScreenshot", format="png")["data"])
            out = OUT / ("thumbnail-%s.png" % name)
            out.write_bytes(data)
            print("  media/%s  %d KB" % (out.name, len(data) // 1024))
    finally:
        browser.close()
        shutil.rmtree(D.shots.TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
