"""Make the crewchat tutorial video and its YouTube assets.

    pip install websocket-client
    python3 tools/tutorial/make_video.py

Needs macOS (the `say` voices and AVFoundation, through compose.swift) and Google Chrome. Slides
are drawn from tools/tutorial/script.json and the pictures in docs/images (remake those first with
tools/docs_screenshots.py if the page changed). Everything is written to media/ (not in git):

    crewchat-tutorial.mp4      the video, 1920x1080
    crewchat-tutorial.srt      captions
    youtube.txt                title, description with chapters, tags
    thumbnail.png              1280x720
    channel-picture.png        800x800
    channel-banner.png         2560x1440
"""
import html
import json
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import crewchat  # noqa: E402
import docs_screenshots as shots  # noqa: E402  (its Chrome driver)

OUT = ROOT / "media"
WORK = OUT / "work"
IMAGES = ROOT / "docs" / "images"
FPS = 30
FADE = 0.5  # seconds of crossfade between slides
PAD = 0.9  # seconds of quiet after each scene's narration

LOGO = crewchat.LOGO_SVG % 'xmlns="http://www.w3.org/2000/svg"'
STYLE = """
* { box-sizing: border-box; }
body { margin: 0; width: %(w)dpx; height: %(h)dpx; overflow: hidden; font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
  color: #1d1b18; background: radial-gradient(1200px 700px at 85%% 0%%, #e3ebff 0%%, transparent 60%%), linear-gradient(160deg, #f8f6f1, #eef1f8); }
.brand { position: absolute; left: 110px; top: 64px; display: flex; align-items: center; gap: 14px; font-weight: 700; font-size: 34px; letter-spacing: -0.02em; }
.brand svg { width: 58px; height: 58px; } .brand b, .word b { color: #2b57c4; }
.chip { position: absolute; right: 110px; top: 76px; font-size: 24px; font-weight: 600; color: #2b57c4; background: #fff; border: 2px solid #d7e1fb; border-radius: 99px; padding: 8px 22px; }
h1 { position: absolute; left: 110px; top: 136px; margin: 0; font-size: 58px; letter-spacing: -0.025em; }
.cap { position: absolute; left: 112px; top: 216px; margin: 0; font-size: 28px; color: #6a665e; }
.media { position: absolute; left: 80px; right: 80px; top: 276px; bottom: 36px; display: flex; gap: 40px; align-items: center; justify-content: center; }
.media img { max-height: 100%%; max-width: 100%%; border-radius: 18px; box-shadow: 0 24px 60px rgba(20, 35, 80, .18), 0 2px 8px rgba(20, 35, 80, .10); background: #fff; }
.media.pair img { max-width: calc(50%% - 20px); }
.media.pair.tall img { height: 100%%; width: auto; }
.card { background: #fff; border-radius: 22px; padding: 40px 60px; box-shadow: 0 24px 60px rgba(20, 35, 80, .14); max-width: 100%%; max-height: 100%%; display: flex; align-items: center; justify-content: center; }
.card pre { margin: 0; } .card svg { width: 1400px !important; max-width: 1400px !important; height: auto; max-height: 600px; }
.term { background: #1c1b18; color: #c9c4ba; border-radius: 16px; box-shadow: 0 24px 60px rgba(20, 35, 80, .22); font: 26px/1.5 ui-monospace, Menlo, monospace; max-width: 1560px; }
.term .bar { height: 46px; display: flex; gap: 10px; align-items: center; padding: 0 20px; border-bottom: 1px solid #2f2d28; }
.term .bar i { width: 15px; height: 15px; border-radius: 50%%; } .term pre { margin: 0; padding: 26px 34px 32px; white-space: pre-wrap; font: inherit; }
.term .p { color: #8fb0ff; } .term .c { color: #edeae4; font-weight: 600; }
ul.points { list-style: none; margin: 0; padding: 0; font-size: 40px; line-height: 1.35; }
ul.points li { display: flex; gap: 22px; align-items: flex-start; margin: 0 0 34px; }
ul.points li::before { content: "✓"; color: #fff; background: #2b57c4; border-radius: 50%%; width: 52px; height: 52px; flex: none; display: grid; place-items: center; font-size: 30px; margin-top: 2px; }
.center { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; }
.center svg.big { width: 300px; height: 300px; filter: drop-shadow(0 30px 40px rgba(20, 35, 80, .18)); }
.word { font-weight: 750; font-size: 128px; letter-spacing: -0.04em; margin: 18px 0 8px; }
.tag { font-size: 42px; color: #4a463f; margin: 0; }
.url { font-size: 46px; font-weight: 650; color: #2b57c4; margin: 30px 0 18px; }
.cmd { font: 26px ui-monospace, Menlo, monospace; background: #1c1b18; color: #edeae4; padding: 18px 28px; border-radius: 12px; }
.small { font-size: 28px; color: #6a665e; margin-top: 26px; }
"""


def page(body, w=1920, h=1080, extra=""):
    return ('<!doctype html><meta charset="utf-8"><style>%s</style>%s<body>%s</body>'
            % (STYLE % {"w": w, "h": h}, extra, body))


def brand(chapter):
    out = '<div class="brand">%s<span>crew<b>chat</b></span></div>' % LOGO
    return out + ('<div class="chip">%s</div>' % html.escape(chapter) if chapter else "")


def img(name):
    return '<img src="%s">' % (IMAGES / name).as_uri()


def terminal(runs):
    lines = []
    for command, output in runs:
        lines.append('<span class="p">$</span> <span class="c">%s</span>' % html.escape(command))
        lines.append(html.escape(output))
    return ('<div class="term"><div class="bar"><i style="background:#ff5f57"></i><i style="background:#febc2e">'
            '</i><i style="background:#28c840"></i></div><pre>%s</pre></div>' % "\n".join(lines))


def slide_html(scene, chapter):
    kind = scene["kind"]
    if kind == "title":
        return page('<div class="center"><svg class="big" viewBox="0 0 64 64">%s</svg><div class="word">crew<b>chat</b></div>'
                    '<p class="tag">A group chat for your AI coding agents, and you</p>'
                    '<p class="small">Setup and tour</p></div>' % LOGO.split(">", 1)[1].rsplit("</svg>", 1)[0])
    if kind == "outro":
        return page('<div class="center"><svg class="big" viewBox="0 0 64 64">%s</svg><div class="word">crew<b>chat</b></div>'
                    '<div class="url">github.com/deepankar17/crewchat</div>'
                    '<div class="cmd">curl -LsSf https://raw.githubusercontent.com/deepankar17/crewchat/main/install.sh | sh</div>'
                    '<p class="small">Free and open source, MIT licence</p></div>' % LOGO.split(">", 1)[1].rsplit("</svg>", 1)[0])
    head = brand(chapter) + '<h1>%s</h1><p class="cap">%s</p>' % (html.escape(scene["heading"]), html.escape(scene.get("caption", "")))
    extra = ""
    if kind == "image":
        media = '<div class="media">%s</div>' % img(scene["image"])
    elif kind == "pair":
        media = '<div class="media pair%s">%s</div>' % (" tall" if scene.get("tall") else "", "".join(img(i) for i in scene["images"]))
    elif kind == "terminal":
        media = '<div class="media">%s</div>' % terminal(scene["terminal"])
    elif kind == "points":
        media = '<div class="media" style="justify-content:flex-start;padding-left:20px"><ul class="points">%s</ul></div>' % "".join(
            "<li>%s</li>" % html.escape(p) for p in scene["points"])
    elif kind == "diagram":
        media = '<div class="media"><div class="card"><pre class="mermaid">%s</pre></div></div>' % html.escape(scene["mermaid"])
        extra = ('<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script><script>'
                 'addEventListener("load", async () => { mermaid.initialize({startOnLoad: false, theme: "base",'
                 ' themeVariables: {fontSize: "24px", primaryColor: "#e8eefc", primaryBorderColor: "#2b57c4",'
                 ' lineColor: "#2b57c4", fontFamily: "-apple-system, Helvetica, Arial"}});'
                 ' try { await mermaid.run(); } finally { document.body.dataset.ready = "1"; } });</script>')
    else:
        raise ValueError(kind)
    return page(head + media, extra=extra)


def render(browser, markup, path, w=1920, h=1080):
    source = WORK / (path.stem + ".html")
    source.write_text(markup, encoding="utf-8")
    browser.size(w, h, scale=1)
    browser.open(source.as_uri(), wait=1.0)
    for _ in range(40):  # diagrams draw after load
        if "mermaid" not in markup or browser.js("document.body.dataset.ready") == "1":
            break
        time.sleep(0.25)
    data = browser.call("Page.captureScreenshot", format="png")["data"]
    import base64
    path.write_bytes(base64.b64decode(data))


def narrate(text, path, voice, rate):
    subprocess.run(["say", "-v", voice, "-r", str(rate), "-o", str(path), text], check=True)
    info = subprocess.run(["afinfo", str(path)], capture_output=True, text=True).stdout
    return float(re.search(r"estimated duration: ([\d.]+)", info).group(1))


def stamp(seconds, srt=False):
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    if srt:
        return "%02d:%02d:%06.3f" % (h, m, s)
    return "%d:%02d" % (m, int(s)) if not h else "%d:%02d:%02d" % (h, m, int(s))


def captions(scenes):
    """SRT captions: each scene's narration split into sentences, timed by length."""
    out, n = [], 0
    for scene in scenes:
        parts = []
        for sentence in (p.strip() for p in re.split(r"(?<=[.?!])\s+", scene["say"]) if p.strip()):
            chunk = ""
            for piece in re.split(r"(?<=,)\s+", sentence):  # keep each caption to about two short lines
                if chunk and len(chunk) + len(piece) > 84:
                    parts.append(chunk)
                    chunk = piece
                else:
                    chunk = (chunk + " " + piece).strip()
            parts.append(chunk)
        total = sum(len(p) for p in parts)
        t = scene["start"]
        for p in parts:
            d = scene["voice"] * len(p) / total
            n += 1
            out.append("%d\n%s --> %s\n%s\n" % (n, stamp(t, True).replace(".", ","), stamp(t + d, True).replace(".", ","), p))
            t += d
    return "\n".join(out)


def main():
    script = json.loads((HERE / "script.json").read_text(encoding="utf-8"))
    WORK.mkdir(parents=True, exist_ok=True)
    browser = shots.Browser()
    scenes, t, chapter = [], 0.0, ""
    try:
        for scene in script["scenes"]:
            chapter = scene.get("chapter", chapter)
            png = WORK / ("%02d-%s.png" % (len(scenes), scene["id"]))
            render(browser, slide_html(scene, chapter), png)
            voice = narrate(scene["say"], WORK / ("%02d.aiff" % len(scenes)), script["voice"], script["rate"])
            duration = round(voice + PAD + (FADE if scenes else 0.4), 2)
            scenes.append(dict(scene, image=str(png), audio=str(WORK / ("%02d.aiff" % len(scenes))),
                               start=t + (FADE if scenes else 0.4), voice=voice, duration=duration, at=t))
            t += duration
            print("  %-9s %5.1fs  %s" % (scene["id"], duration, stamp(scenes[-1]["at"])))
        # YouTube pictures
        mark = LOGO.split(">", 1)[1].rsplit("</svg>", 1)[0]
        render(browser, page(
            '<div style="position:absolute;left:96px;top:0;bottom:0;width:860px;display:flex;flex-direction:column;'
            'justify-content:center"><div style="display:flex;align-items:center;gap:22px"><svg viewBox="0 0 64 64" '
            'style="width:150px;height:150px">%s</svg><div class="word" style="font-size:118px;margin:0">crew<b>chat</b>'
            '</div></div><div style="font-weight:800;font-size:92px;line-height:1.02;letter-spacing:-.035em;margin:44px 0 0">'
            'Your AI agents,<br>in <span style="color:#2b57c4">one chat</span></div><div style="margin-top:34px;font-size:44px;'
            'font-weight:650;color:#fff;background:#2b57c4;border-radius:14px;padding:12px 26px;width:fit-content">'
            'Full setup &amp; tour</div></div><img src="%s" style="position:absolute;right:-120px;top:150px;width:1080px;'
            'border-radius:22px;transform:rotate(-5deg);box-shadow:0 40px 90px rgba(20,35,80,.28)">'
            % (mark, (IMAGES / "screenshot-light.png").as_uri())), OUT / "thumbnail-large.png")
        render(browser, page('<div class="center" style="background:linear-gradient(160deg,#f8f6f1,#e3ebff)">'
                             '<svg viewBox="0 0 64 64" style="width:560px;height:560px">%s</svg></div>'
                             % LOGO.split(">", 1)[1].rsplit("</svg>", 1)[0], 800, 800), OUT / "channel-picture.png", 800, 800)
        # Banner: everything inside the 1235x338 middle that YouTube shows on every device.
        render(browser, page('<div class="center" style="flex-direction:row;gap:34px"><svg viewBox="0 0 64 64" '
                             'style="width:250px;height:250px">%s</svg><div style="text-align:left"><div class="word" '
                             'style="font-size:124px;margin:0">crew<b>chat</b></div><p class="tag" style="font-size:40px">'
                             'A group chat for your AI coding agents, and you</p></div></div>'
                             % LOGO.split(">", 1)[1].rsplit("</svg>", 1)[0], 2560, 1440), OUT / "channel-banner.png", 2560, 1440)
    finally:
        browser.close()
        import shutil
        shutil.rmtree(shots.TMP, ignore_errors=True)
    subprocess.run(["sips", "-z", "720", "1280", str(OUT / "thumbnail-large.png"), "--out", str(OUT / "thumbnail.png")],
                   check=True, capture_output=True)
    (OUT / "thumbnail-large.png").unlink()

    timeline = {"width": 1920, "height": 1080, "fps": FPS, "fade": FADE, "output": str(OUT / "crewchat-tutorial.mp4"),
                "scenes": [{"image": s["image"], "duration": s["duration"]} for s in scenes],
                "audio": [{"path": s["audio"], "start": s["start"]} for s in scenes]}
    (WORK / "timeline.json").write_text(json.dumps(timeline, indent=1))
    composer = WORK / "compose"
    subprocess.run(["swiftc", "-O", str(HERE / "compose.swift"), "-o", str(composer)], check=True)
    subprocess.run([str(composer), str(WORK / "timeline.json")], check=True)

    (OUT / "crewchat-tutorial.srt").write_text(captions(scenes), encoding="utf-8")
    chapters = [(s["at"], s["chapter"]) for s in scenes if s.get("chapter")]
    chapters[0] = (0.0, chapters[0][1])
    (OUT / "youtube.txt").write_text(youtube_text(script, chapters, t), encoding="utf-8")
    print("Video: %s (%s)" % (timeline["output"], stamp(t)))


def youtube_text(script, chapters, total):
    lines = ["TITLE", script["title"], "", "DESCRIPTION",
             "crewchat is a free, open-source group chat for your AI coding agents and you. Connect Claude Code, "
             "Cursor and other MCP agents on one or several machines: they join by themselves, talk to each other, "
             "settle who takes each task, and you follow everything live on your computer or phone.",
             "",
             "In this video: installing crewchat, starting a chat, agents joining, how messages reach agents, tasks, "
             "roles with a lead, adding an agent from the chat, sharing screenshots and files, linking several "
             "machines over Tailscale, and using it on your phone.",
             "",
             "Install (macOS, Linux):",
             "curl -LsSf https://raw.githubusercontent.com/deepankar17/crewchat/main/install.sh | sh",
             "",
             "GitHub: https://github.com/deepankar17/crewchat",
             "Guides: https://github.com/deepankar17/crewchat/tree/main/docs",
             "",
             "Chapters:"]
    lines += ["%s %s" % (stamp(at), name) for at, name in chapters]
    lines += ["", "TAGS", "crewchat, AI agents, Claude Code, Cursor, MCP, multi-agent, AI coding, developer tools, "
              "Tailscale, open source, agent collaboration, coding assistant",
              "", "LENGTH", stamp(total)]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
