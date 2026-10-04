"""Record the animated demos in docs/images (GIF) and media/clips (MP4, for the tutorial video).

    pip install websocket-client pillow
    python3 tools/demo_recordings.py [SCENE ...]

Each scene runs a real crewchat server with scripted agents, and drives the chat page in Google
Chrome the way a person would: a visible pointer moves and clicks, text is typed key by key, Enter
sends. Chrome's screencast records every change with its timing. MP4 clips need macOS (they are
written by tools/tutorial/clip.swift).
"""
import base64
import html
import io
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import crewchat  # noqa: E402
import docs_screenshots as shots  # noqa: E402  (its demo chats)

import websocket  # noqa: E402
from PIL import Image  # noqa: E402

GIFS = ROOT / "docs" / "images"
CLIPS = ROOT / "media" / "clips"
O = crewchat.OWNER

CURSOR_JS = r"""
(() => {
  if (window.demo) return;
  const c = document.createElement("div");
  c.id = "demo-cursor";
  c.innerHTML = '<svg width="26" height="26" viewBox="0 0 24 24"><path d="M4 2l15 9.5-6.6 1.4 4 7.6-3 1.6-4-7.7L4 19z" fill="#111" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>';
  Object.assign(c.style, {position: "fixed", left: "0px", top: "0px", zIndex: 99999, pointerEvents: "none",
    transition: "transform 0.7s cubic-bezier(.4,.1,.2,1)", transform: "translate(-40px,-40px)", filter: "drop-shadow(0 2px 3px rgba(0,0,0,.3))"});
  document.body.append(c);
  const ring = document.createElement("div");
  Object.assign(ring.style, {position: "fixed", width: "34px", height: "34px", marginLeft: "-17px", marginTop: "-17px",
    borderRadius: "50%", border: "3px solid #2b57c4", opacity: 0, zIndex: 99998, pointerEvents: "none"});
  document.body.append(ring);
  window.demo = {
    at: [-40, -40],
    move(x, y, ms) { c.style.transition = "transform " + (ms / 1000) + "s cubic-bezier(.4,.1,.2,1)"; c.style.transform = "translate(" + x + "px," + y + "px)"; this.at = [x, y]; },
    ripple() { const [x, y] = this.at; ring.style.left = x + "px"; ring.style.top = y + "px";
      ring.animate([{opacity: .9, transform: "scale(.4)"}, {opacity: 0, transform: "scale(1.6)"}], {duration: 450}); },
    center(sel) { const r = document.querySelector(sel).getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]; },
  };
})();
"""


class Recorder:
    """Chrome over the DevTools protocol, with a reader thread so the screencast keeps flowing
    while the scene sleeps between actions."""

    def __init__(self):
        self.browser = shots.Browser()  # starts Chrome; we take over its socket
        self.ws = self.browser.ws
        self.n = 1000
        self.waiting = {}
        self.frames = None
        self.lock = threading.Lock()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        while True:
            try:
                msg = json.loads(self.ws.recv())
            except Exception:
                return
            if msg.get("method") == "Page.screencastFrame":
                p = msg["params"]
                if self.frames is not None:
                    self.frames.append((p["metadata"]["timestamp"], p["data"]))
                self.send("Page.screencastFrameAck", {"sessionId": p["sessionId"]}, wait=False)
            elif "id" in msg and msg["id"] in self.waiting:
                self.waiting.pop(msg["id"]).put(msg)

    def send(self, method, params=None, wait=True):
        with self.lock:
            self.n += 1
            rid = self.n
            box = queue.Queue()
            if wait:
                self.waiting[rid] = box
            self.ws.send(json.dumps({"id": rid, "method": method, "params": params or {}}))
        if not wait:
            return None
        msg = box.get(timeout=60)
        if "error" in msg:
            raise RuntimeError("%s: %s" % (method, msg["error"]))
        return msg.get("result", {})

    def js(self, expression):
        out = self.send("Runtime.evaluate", {"expression": expression, "awaitPromise": True, "returnByValue": True})
        if out.get("exceptionDetails"):
            raise RuntimeError("page script failed: %s" % out["exceptionDetails"])
        return out.get("result", {}).get("value")

    def size(self, width, height, scale=1.5, mobile=False):
        self.width, self.height, self.scale = width, height, scale
        self.send("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": scale,
                                                         "mobile": mobile})
        self.send("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-color-scheme", "value": "light"}]})

    def open(self, url, wait=1.5):
        self.send("Page.navigate", {"url": url})
        time.sleep(wait)
        self.js(CURSOR_JS)

    # Recording -----------------------------------------------------------------------------
    def start(self):
        """Capture full-resolution screenshots as fast as Chrome gives them (about 12 a second),
        keeping only frames that changed. (The screencast would be smoother, but headless Chrome
        sends it at the page's size, not at the device's pixels.)"""
        self.frames = []
        self.capturing = True
        self.capture = threading.Thread(target=self._capture, daemon=True)
        self.capture.start()
        time.sleep(0.6)

    def _capture(self):
        last = None
        while self.capturing:
            t = time.time()
            data = self.send("Page.captureScreenshot", {"format": "png"})["data"]
            if data != last:
                self.frames.append((t, data))
                last = data

    def stop(self, hold=1.6):
        time.sleep(hold)
        self.capturing = False
        self.capture.join()
        self.frames.append((time.time(), self.frames[-1][1]))  # the last picture lasts to the end
        frames, self.frames = self.frames, None
        return frames

    # A person at the keyboard ------------------------------------------------------------------
    def move(self, selector, ms=700):
        x, y = self.js("demo.center(%s)" % json.dumps(selector))
        self.js("demo.move(%f, %f, %d)" % (x, y, ms))
        time.sleep(ms / 1000 + 0.15)
        return x, y

    def point(self, selector, ms=700):
        """Move to an element and show a click, without clicking (for menus, set by the scene)."""
        self.move(selector, ms)
        self.js("demo.ripple()")
        time.sleep(0.3)

    def snapshot(self, url, width, height):
        """A plain screenshot of a page, outside any recording."""
        self.size(width, height, scale=1)
        self.send("Page.navigate", {"url": url})
        time.sleep(0.5)
        return base64.b64decode(self.send("Page.captureScreenshot", {"format": "png"})["data"])

    def click(self, selector, ms=700):
        """Move there, show the click, and click. The click goes through the page itself:
        synthetic mouse events get lost while screenshots are being taken."""
        self.move(selector, ms)
        self.js("demo.ripple(); (e => { e.focus(); e.click(); })(document.querySelector(%s))" % json.dumps(selector))
        time.sleep(0.35)

    def type(self, text, per_key=0.055):
        for ch in text:
            self.send("Input.insertText", {"text": ch})
            time.sleep(per_key * (2.5 if ch in " ,." else 1))
        time.sleep(0.3)

    def enter(self):
        for kind in ("keyDown", "keyUp"):
            self.send("Input.dispatchKeyEvent", {"type": kind, "key": "Enter", "code": "Enter",
                                                 "windowsVirtualKeyCode": 13, "text": "\r" if kind == "keyDown" else ""})
        time.sleep(0.4)

    def close(self):
        self.browser.close()


# ------------------------------------------------------------------------------------------
# Writing the results
# ------------------------------------------------------------------------------------------
def save(name, frames, gif_width=960, video=True):
    """frames: [(timestamp, base64 png)] as recorded. Writes docs/images/<name>.gif and
    media/clips/<name>.mp4."""
    print("  %d frames recorded" % len(frames))
    times = [t for t, _ in frames]
    images = [Image.open(io.BytesIO(base64.b64decode(d))).convert("RGB") for _, d in frames]
    durations = [max(0.02, b - a) for a, b in zip(times, times[1:])] + [1.8]
    # GIF: smaller, at most 15 frames a second, and only what changed in each frame.
    small, delays, last = [], [], None
    for img, d in zip(images, durations):
        frame = img.resize((gif_width, round(img.height * gif_width / img.width)), Image.LANCZOS)
        if last is not None and delays[-1] < 0.066:
            small[-1], delays[-1] = frame, delays[-1] + d  # merge very quick frames into the next
            continue
        small.append(frame)
        delays.append(d)
        last = frame
    paletted = [f.quantize(colors=128, method=Image.MEDIANCUT, dither=Image.NONE) for f in small]
    path = GIFS / (name + ".gif")
    paletted[0].save(path, save_all=True, append_images=paletted[1:], duration=[round(d * 1000) for d in delays],
                     loop=0, optimize=True, disposal=1)
    print("  docs/images/%s.gif  %.1fs  %d frames  %d KB" % (name, sum(delays), len(small), path.stat().st_size // 1024))
    if not video:
        return
    CLIPS.mkdir(parents=True, exist_ok=True)
    work = CLIPS / (name + "-frames")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    listing = []
    for i, (img, d) in enumerate(zip(images, durations)):
        p = work / ("%05d.png" % i)
        img.save(p)
        listing.append({"image": str(p), "duration": d})
    (work / "frames.json").write_text(json.dumps({"frames": listing, "output": str(CLIPS / (name + ".mp4"))}))
    binary = ROOT / "media" / "work" / "clip"
    if not binary.exists():
        binary.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["swiftc", "-O", str(ROOT / "tools" / "tutorial" / "clip.swift"), "-o", str(binary)], check=True)
    subprocess.run([str(binary), str(work / "frames.json")], check=True)
    shutil.rmtree(work, ignore_errors=True)


# ------------------------------------------------------------------------------------------
# Scenes
# ------------------------------------------------------------------------------------------
def team(chat):
    lead = chat.agent(status="Coordinating", activity="listening")
    dev = chat.agent(status="Persisting the theme choice", activity="listening")
    qa = chat.agent("laptop", "cursor", status="Testing", activity="listening")
    return lead, dev, qa


def scene_talk(r):
    """You write; an agent waiting for messages answers at once."""
    chat = shots.Chat("Notes App")
    a = chat.agent(status="Free", activity="listening")
    b = chat.agent("laptop", "cursor", status="Settings screen", activity="working")
    chat.say(a, "all", "Login screen is done; tests pass locally.")
    chat.read_all()
    r.size(1280, 720)
    r.open(chat.sign_in())
    r.start()
    r.click("#text")
    r.type("Can someone check why the login test fails on CI?")
    r.enter()
    time.sleep(1.2)
    chat.doing(a, "working")
    with chat.hub.lock:
        chat.hub.agents[a]["cursor"] = chat.hub.next_seq - 1
        chat.hub._changed()
    time.sleep(1.6)
    chat.say(a, O, "On it. It passes locally, so I'll compare the CI logs with my run.")
    chat.hub.set_status(a, "Flaky login test on CI")
    time.sleep(2.4)
    chat.say(a, O, "Found it: CI is slower, and the test waits only 100 ms for the token refresh. Fixed in fix/login-test.")
    chat.doing(a, "listening")
    frames = r.stop(2.2)
    chat.stop()
    return frames


def scene_task(r):
    """A task: every agent bids, exactly one takes it."""
    chat = shots.Chat("Notes App")
    a = chat.agent(status="Free", activity="listening")
    b = chat.agent("macbook", "cursor", status="Dark theme", activity="listening")
    c = chat.agent("windows", "claude", status="Free", activity="listening")
    chat.read_all()
    r.size(1280, 720)
    r.open(chat.sign_in())
    r.start()
    r.click("#task")
    r.click("#text", 500)
    r.type("Find out why the login test is flaky.")
    r.enter()
    time.sleep(1.0)
    task = chat.hub.history(1)[0]["id"]
    for name, text in ((a, "BID #%s: yes. I wrote that test and I'm free."),
                       (b, "BID #%s: no. I'm in the middle of the dark theme."),
                       (c, "BID #%s: yes, but claude-macbook knows that code better.")):
        chat.doing(name, "working")
        time.sleep(0.9)
        chat.say(name, "all", text % task)
    time.sleep(1.0)
    chat.hub.take(a, task)
    chat.hub.set_status(a, "Flaky login test")
    for name in (b, c):
        chat.doing(name, "listening")
    time.sleep(2.0)
    chat.say(a, O, "Found it: the test waited 100 ms for the token refresh, which takes up to 300 ms on CI. Fixed.")
    frames = r.stop(2.4)
    chat.stop()
    return frames


def scene_roles(r):
    """Give roles from the cards; the lead hands out the work; progress flows back."""
    chat = shots.Chat("Notes App")
    lead, dev, qa = team(chat)
    chat.read_all()
    r.size(1280, 720)
    r.open(chat.sign_in())
    r.start()
    for i, (name, role) in enumerate(((lead, "lead"), (dev, "developer"), (qa, "qa"))):
        sel = '#agents .agent:nth-child(%d) .rolepick' % (i + 1)
        r.point(sel, 600)
        r.js("(() => { const s = document.querySelector(%s); s.value = %s; s.dispatchEvent(new Event('change')); })()"
             % (json.dumps(sel), json.dumps(role)))
        time.sleep(1.0)
    r.click("#task", 600)
    r.click("#text", 400)
    r.type("Add a dark theme to the settings screen.")
    r.enter()
    time.sleep(1.2)
    task = chat.hub.history(1)[0]["id"]
    chat.hub.take(lead, task)
    time.sleep(1.0)
    piece = chat.hub.assign(lead, dev, "Build the theme switch and keep the choice in DataStore.", task)
    time.sleep(1.4)
    chat.hub.update(dev, piece["id"], "in_progress", "Started on the switch.")
    time.sleep(1.4)
    chat.say(dev, qa, "Theme switch is ready to test: Settings > Theme.")
    chat.hub.update(dev, piece["id"], "review", "Ready for QA.")
    time.sleep(1.6)
    chat.hub.update(qa, piece["id"], "done", "Passes, including after a restart.")
    time.sleep(1.2)
    chat.say(lead, O, "The dark theme is done and tested.")
    frames = r.stop(2.4)
    chat.stop()
    return frames


def scene_add_agent(r):
    """Add an agent from the page; it joins with its name, role and task."""
    chat = shots.Chat("Notes App")
    lead = chat.agent(status="Coordinating", activity="listening")
    chat.hub.set_role(O, lead, "lead")
    chat.quiet()
    started = {}
    old_options, old_launch, old_folders = crewchat.launch_options, crewchat.launch_agent, crewchat.launch_folders
    folder = [{"path": "/Users/you/code/notes-app", "name": "notes-app", "place": "macbook"}]
    crewchat.launch_folders = lambda config, root=None: folder
    crewchat.launch_options = lambda config, root=None: {
        "tools": [{"id": "claude", "label": "Claude Code"}, {"id": "cursor", "label": "Cursor"}], "folders": folder}
    crewchat.launch_agent = lambda tool, folder, key, edits=False, root=None: started.update(key=key)
    try:
        r.size(1280, 720)
        r.open(chat.sign_in())
        r.start()
        r.click("#add-agent")
        time.sleep(0.8)
        r.click("#l-name", 600)
        r.type("docs-writer")
        r.point("#l-role", 500)
        r.js("(() => { const s = $('l-role'); s.value = 'docs'; s.dispatchEvent(new Event('change')); })()")
        r.click("#l-task", 500)
        r.type("Write a user guide for the settings screen.", 0.04)
        r.click("#l-start", 600)
        time.sleep(2.4)
        session = chat.hub.open_session("macbook", "claude")  # the new terminal window's agent checks in
        chat.hub.start_link(session, started["key"])
        chat.doing("docs-writer", "working")
        time.sleep(2.0)
        chat.say("docs-writer", O, "Hi, I'm docs-writer. Reading the settings code, then I'll start the guide.")
        frames = r.stop(2.4)
    finally:
        crewchat.launch_options, crewchat.launch_agent, crewchat.launch_folders = old_options, old_launch, old_folders
        chat.stop()
    return frames


def scene_files(r):
    """Paste a screenshot, send it; the agent looks at it and answers."""
    page = shots.TMP / "phone.html"
    page.write_text("<!doctype html><meta charset=utf-8><style>body{margin:0;font:16px system-ui;background:#121212;color:#eee}"
                    "header{padding:18px;font-size:20px;font-weight:600}.row{display:flex;justify-content:space-between;"
                    "padding:16px 18px;border-top:1px solid #333}.on{color:#8fb0ff}</style><header>Settings</header>"
                    "<div class=row><span>Theme</span><span class=on>Light</span></div><div class=row><span>"
                    "Notifications</span><span>On</span></div><div class=row><span>Language</span><span>English</span></div>")
    shot = r.snapshot(page.as_uri(), 360, 300)
    chat = shots.Chat("Notes App")
    qa = chat.agent(status="Testing the theme switch", activity="listening")
    chat.read_all()
    r.size(1280, 720)
    r.open(chat.sign_in())
    meta = chat.hub.add_file("image.png", shot, "image/png")  # so the page can fetch it from its own server
    r.start()
    r.click("#text")
    r.js("""(async () => { const b = await (await fetch("/files/%s/x")).blob();
        const dt = new DataTransfer(); dt.items.add(new File([b], "image.png", {type: "image/png"}));
        $("text").dispatchEvent(new ClipboardEvent("paste", {clipboardData: dt, bubbles: true, cancelable: true})); })()""" % meta["id"])
    time.sleep(1.0)
    r.type("After a restart the theme is Light again. What do you see?")
    r.enter()
    time.sleep(1.4)
    chat.doing(qa, "working")
    time.sleep(1.8)
    chat.say(qa, O, "I see it: Theme shows Light after the restart. It's the DataStore read; I'll tell the developer.")
    frames = r.stop(2.4)
    chat.stop()
    return frames


def scene_phone(r):
    chat = shots.Chat("Notes App")
    lead, dev, qa = team(chat)
    chat.hub.set_role(O, lead, "lead")
    chat.quiet()
    chat.say(lead, O, "Dark theme: done and tested. Should the default follow the system setting?")
    chat.read_all()
    r.size(390, 760, scale=2, mobile=True)
    r.open(chat.sign_in(), wait=2)
    r.start()
    r.click("#text", 600)
    r.type("Yes, follow the system setting by default.", 0.05)
    r.enter()
    time.sleep(1.5)
    chat.say(lead, O, "Will do. claude-macbook-2 is on it.")
    frames = r.stop(2.2)
    chat.stop()
    return frames


TERMINAL = """<!doctype html><meta charset="utf-8"><style>
body { margin: 0; height: 100vh; display: grid; place-items: center; background: linear-gradient(160deg, #f8f6f1, #e9eef9);
  font: 21px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }
.wins { display: flex; gap: 26px; }
.win { background: #1c1b18; color: #c9c4ba; border-radius: 12px; box-shadow: 0 18px 50px rgba(20,35,80,.25); width: %dpx; height: %dpx; overflow: hidden; }
.bar { height: 36px; display: flex; align-items: center; gap: 8px; padding: 0 14px; border-bottom: 1px solid #2f2d28; }
.bar i { width: 12px; height: 12px; border-radius: 50%%; } .bar span { color: #a09b91; font: 14px system-ui; margin-left: 10px; }
pre { margin: 0; padding: 16px 22px; white-space: pre-wrap; font: inherit; }
.p { color: #8fb0ff; } .c { color: #edeae4; font-weight: 600; } .k { display: inline-block; width: 11px; height: 22px; background: #edeae4; vertical-align: -4px; animation: b 1s steps(1) infinite; }
@keyframes b { 50%% { opacity: 0; } }
</style><div class="wins">%s</div>
<script>
window.term = {
  start(i) { this.pre(i).innerHTML = '<span class="p">$</span> <span class="c"></span><span class="k"></span>'; },
  pre(i) { return document.querySelectorAll("pre")[i]; },
  key(i, ch) { const c = this.pre(i).querySelectorAll(".c"); c[c.length - 1].textContent += ch; },
  out(i, text) { const k = this.pre(i).querySelector(".k"); k.remove(); this.pre(i).append(document.createTextNode("\\n" + text)); this.pre(i).append(k); },
  prompt(i) { const k = this.pre(i).querySelector(".k"); k.remove();
    this.pre(i).insertAdjacentHTML("beforeend", '\\n\\n<span class="p">$</span> <span class="c"></span>'); this.pre(i).append(k); },
  answer(i, ch) { const k = this.pre(i).querySelector(".k"); k.before(document.createTextNode(ch)); },
};
</script>"""


def terminal_page(titles, width=1180, height=620, font=21):
    wins = "".join('<div class="win"><div class="bar"><i style="background:#ff5f57"></i><i style="background:#febc2e">'
                   '</i><i style="background:#28c840"></i><span>%s</span></div><pre></pre></div>' % html.escape(t)
                   for t in titles)
    page = shots.TMP / "terminal.html"
    page.write_text((TERMINAL % (width, height, wins)).replace("font: 21px/", "font: %dpx/" % font), encoding="utf-8")
    return page.as_uri()


def terminal_type(r, i, command, output, per_key=0.045):
    for ch in command:
        r.js("term.key(%d, %s)" % (i, json.dumps(ch)))
        time.sleep(per_key * (2 if ch == " " else 1))
    time.sleep(0.4)
    for line in output.rstrip().splitlines():
        r.js("term.out(%d, %s)" % (i, json.dumps(line)))
        time.sleep(0.09)


def scene_start(r):
    home, project = shots.TMP / "home", shots.TMP / "notes-app"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)])
    port = str(shots.free_port())
    out = shots.run(["start", "--no-service", "--no-open", "--port", port, "--place", "macbook"], home, project, [port])
    subprocess.run(["pkill", "-f", "serve --log %s" % (home / "hub.log")])
    r.size(1280, 720)
    r.open(terminal_page(["~/code/notes-app"]), wait=0.5)
    r.js("term.start(0)")
    r.start()
    terminal_type(r, 0, "crewchat start", out)
    return r.stop(2.6)


def scene_peers(r):
    home, project = shots.TMP / "home-a", shots.TMP / "notes-a"
    home2, project2 = shots.TMP / "home-b", shots.TMP / "notes-b"
    for p in (project, project2):
        p.mkdir()
        subprocess.run(["git", "init", "-q", str(p)])
    port, port2 = str(shots.free_port()), str(shots.free_port())
    shots.run(["start", "--no-service", "--no-open", "--port", port, "--place", "macbook"], home, project)
    shots.run(["start", "--no-service", "--no-open", "--port", port2, "--place", "laptop"], home2, project2)
    invite = shots.run(["peers", "invite", "--url", "http://127.0.0.1:%s" % port, "--name", "macbook"], home, project)
    line = next(l for l in invite.splitlines() if "crewchat peers join" in l).split()
    url, code = line[3], line[4]
    join = shots.run(["peers", "join", url, code, "--url", "http://127.0.0.1:%s" % port2, "--name", "laptop"], home2, project2)
    time.sleep(2)
    status = shots.run(["peers", "status"], home2, project2)
    for h in (home, home2):
        subprocess.run(["pkill", "-f", "serve --log %s" % (h / "hub.log")])
    shown = "https://macbook.your-tailnet.ts.net"
    tidy = lambda t: t.replace(url, shown).replace("http://127.0.0.1:%s" % port2, "https://laptop.your-tailnet.ts.net")  # noqa: E731
    r.size(1280, 720)
    r.open(terminal_page(["macbook: ~/code/notes-app", "laptop: ~/code/notes-app"], 600, 560, 15), wait=0.5)
    r.js("term.start(0); term.start(1)")
    r.start()
    terminal_type(r, 0, "crewchat peers invite", tidy(invite))
    time.sleep(0.8)
    terminal_type(r, 1, "crewchat peers join %s %s" % (shown, code), tidy(join), 0.03)
    r.js("term.prompt(1)")
    terminal_type(r, 1, "crewchat peers status", tidy(status))
    return r.stop(2.8)


# Cloud sync talks to a real Firebase project and a Google sign-in, so these two scenes replay
# what the commands print (the same messages, from crewchat_cloud.py) rather than running them.
SIGN_IN = ("Opening your browser to sign in with Google. If it does not open, visit:\n"
           "  https://accounts.google.com/o/oauth2/v2/auth?client_id=...\nSigned in as you@example.com.")
SETUP_CMD = "crewchat cloud setup --web-config firebase-web.json --oauth-client oauth-client.json"
SETUP_OUT = "Cloud sync is set up for Firebase project crewchat-demo. Next: crewchat cloud login"
RESTART = "Restart the server so it starts syncing: crewchat service restart (or crewchat serve)."


def scene_cloud_setup(r):
    r.size(1280, 720)
    r.open(terminal_page(["macbook: ~/code/notes-app"], 1180, 620, 19), wait=0.5)
    r.js("term.start(0)")
    r.start()
    terminal_type(r, 0, SETUP_CMD, SETUP_OUT, 0.03)
    r.js("term.prompt(0)")
    terminal_type(r, 0, "crewchat cloud login", SIGN_IN)
    time.sleep(0.8)
    terminal_type(r, 0, "", 'This machine is device "macbook" (tag A). Its fingerprint: 3f9a 1c07 b2e4 58d1\n'
                  "It is the first device on this account, so it holds the account key.\n \n" + RESTART)
    r.js("term.prompt(0)")
    terminal_type(r, 0, "crewchat service restart", "Restarted.")
    return r.stop(2.6)


def scene_cloud_approve(r):
    fp = "7c2e 90ab 4d13 e6f5"
    r.size(1280, 720)
    r.open(terminal_page(["laptop: ~/code/notes-app", "macbook: ~/code/notes-app"], 600, 560, 15), wait=0.5)
    r.js("term.start(0); term.start(1)")
    r.start()
    terminal_type(r, 0, "crewchat cloud login", SIGN_IN + '\nThis machine is device "laptop" (tag B). Its fingerprint: '
                  + fp + "\n \nBefore it can read or send anything, approve it from a machine that is already set up:\n"
                  "  crewchat cloud approve laptop\nThat command shows a fingerprint: it must be " + fp + ".")
    time.sleep(1.0)
    terminal_type(r, 1, "crewchat cloud approve laptop", 'Device "laptop" shows the fingerprint:\n  ' + fp +
                  "\nDoes the new machine show exactly this? Type yes to approve: ")
    time.sleep(0.8)
    for ch in " yes":
        r.js("term.answer(1, %s)" % json.dumps(ch))
        time.sleep(0.12)
    time.sleep(0.5)
    terminal_type(r, 1, "", "Approved laptop. It receives the account key and starts syncing within a few seconds.")
    time.sleep(0.6)
    r.js("term.prompt(0)")
    terminal_type(r, 0, "crewchat service restart", "Restarted.")
    return r.stop(2.8)


SCENES = {
    "talk": (scene_talk, 960), "task": (scene_task, 960), "roles": (scene_roles, 960),
    "add-agent": (scene_add_agent, 960), "files": (scene_files, 960), "phone": (scene_phone, 360),
    "start": (scene_start, 860), "peers": (scene_peers, 960),
    "cloud-setup": (scene_cloud_setup, 860), "cloud-approve": (scene_cloud_approve, 960),
}


def main(names):
    rec = Recorder()
    try:
        for name in names or SCENES:
            print(name)
            scene, width = SCENES[name]
            save("demo-" + name, scene(rec), gif_width=width)
    finally:
        rec.close()
        shutil.rmtree(shots.TMP, ignore_errors=True)


if __name__ == "__main__":
    main(sys.argv[1:])
