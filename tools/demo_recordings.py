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
import queue
import shutil
import subprocess
import sys
import threading
import time
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
    find(sel, text) { const e = text == null ? document.querySelector(sel)
        : [...document.querySelectorAll(sel)].find((e) => e.textContent.trim() === text);
      if (!e) throw new Error("no " + sel + (text == null ? "" : " saying " + text)); return e; },
    center(sel, text) { const r = this.find(sel, text).getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]; },
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
        if url.startswith("http"):  # wait for the new page (a sign-in redirects once more)
            loaded = "location.href.startsWith(%s) && !location.pathname.startsWith('/login') && document.readyState === 'complete'" \
                % json.dumps("/".join(url.split("/")[:3]))
            for _ in range(100):
                time.sleep(0.1)
                try:
                    if self.js(loaded):
                        break
                except RuntimeError:  # asked mid-navigation, as the old page went away: ask again
                    pass
            else:
                raise RuntimeError("the page did not load: %s" % url)
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
    def move(self, selector, ms=700, text=None):
        x, y = self.js("demo.center(%s, %s)" % (json.dumps(selector), json.dumps(text)))
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

    def click(self, selector, ms=700, text=None):
        """Move there, show the click, and click (text: the one among selector's matches that says
        it). The click goes through the page itself: synthetic mouse events get lost while
        screenshots are being taken."""
        self.move(selector, ms, text)
        self.js("demo.ripple(); (e => { e.focus(); e.click(); })(demo.find(%s, %s))" % (json.dumps(selector), json.dumps(text)))
        time.sleep(0.35)

    def type(self, text, per_key=0.055):
        for ch in text:
            self.send("Input.insertText", {"text": ch})
            time.sleep(per_key * (2.5 if ch in " ,." else 1))
        time.sleep(0.3)

    def key(self, name, code, text=""):
        """A key: Enter sends; Tab picks the agent the @ list offers."""
        for kind in ("keyDown", "keyUp"):
            self.send("Input.dispatchKeyEvent", {"type": kind, "key": name, "code": name, "windowsVirtualKeyCode": code,
                                                 "text": text if kind == "keyDown" else ""})
        time.sleep(0.4)

    def enter(self):
        self.key("Enter", 13, "\r")

    def tab(self):
        self.key("Tab", 9)

    def choose(self, selector, value, ms=600):
        """Pick an option in a select, as a person would: point at it, then the choice shows."""
        self.point(selector, ms)
        self.js("(() => { const s = document.querySelector(%s); s.value = %s; s.dispatchEvent(new Event('change')); })()"
                % (json.dumps(selector), json.dumps(value)))
        time.sleep(0.3)

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
    # One palette for the whole GIF, from frames across it: frames then differ only where the
    # picture changed, which keeps the file small.
    picks = [small[round(i * (len(small) - 1) / 23)].reduce(2) for i in range(24)]
    board = Image.new("RGB", (picks[0].width, picks[0].height * len(picks)))
    for i, f in enumerate(picks):
        board.paste(f, (0, i * f.height))
    palette = board.quantize(colors=256, method=Image.MEDIANCUT, dither=Image.NONE)
    paletted = [f.quantize(palette=palette, dither=Image.NONE) for f in small]
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
def crew(chat, roles=("lead", "developer", "qa")):
    """Maya, Teo and Wren, waiting for messages; with roles, the lead, a developer and QA."""
    names = (chat.agent(status="Coordinating", activity="listening", name="Maya"),
             chat.agent("laptop", "cursor", status="Settings screen", activity="listening", name="Teo"),
             chat.agent(status="Testing", activity="listening", name="Wren"))
    for name, role in zip(names, roles):
        chat.hub.set_role(O, name, role)
    return names


def post_task(r, text):
    """The composer's + menu, Post as task, and the task."""
    r.click("#plus summary", 600)
    time.sleep(0.5)
    r.click("#as-task", 450)
    r.type(text)
    r.enter()


def scene_talk(r):
    """@ an agent's name to write to it; it answers at once, and the rail shows what it is doing."""
    chat = shots.Chat("Notes App")
    _, teo, wren = crew(chat)
    chat.doing(teo, "working", "Editing SettingsScreen.kt")
    chat.quiet()
    chat.say(teo, "all", "Login screen is done; tests pass locally.")
    chat.read_all()
    r.size(1280, 720)
    r.open(chat.sign_in())
    r.start()
    r.click("#text")
    r.type("@Wr", 0.12)
    time.sleep(0.8)
    r.tab()
    r.type("can you check why the login test fails on CI?")
    r.enter()
    time.sleep(1.0)
    chat.heard(wren)
    chat.doing(wren, "working", "Reading LoginViewModelTest.kt")
    time.sleep(1.4)
    chat.say(wren, O, "On it. It passes locally, so I'll compare the CI logs with my run.")
    chat.hub.set_status(wren, "Flaky login test on CI")
    time.sleep(1.2)
    chat.doing(wren, "working", "Running gradlew")
    time.sleep(2.0)
    chat.say(wren, O, "Found it: CI is slower, and the test waits only 100 ms for the token refresh. Fixed in fix/login-test.")
    chat.doing(wren, "listening")
    frames = r.stop(2.2)
    chat.stop()
    return frames


def scene_task(r):
    """Post as task: every agent bids, exactly one takes it; the board follows it to Done."""
    chat = shots.Chat("Notes App")
    maya, teo, wren = crew(chat, roles=())
    chat.hub.set_status(maya, "Free")
    chat.hub.set_status(wren, "Free")
    chat.quiet()
    r.size(1280, 720)
    r.open(chat.sign_in())
    r.start()
    post_task(r, "Find out why the login test is flaky.")
    time.sleep(1.0)
    task = chat.hub.history(1)[0]["id"]
    for name, text in ((maya, "BID #%s: yes. I wrote that test and I'm free."),
                       (teo, "BID #%s: no. I'm in the middle of the settings screen."),
                       (wren, "BID #%s: yes, but Maya knows that code better.")):
        chat.doing(name, "working")
        time.sleep(0.9)
        chat.say(name, "all", text % task)
        chat.doing(name, "listening")
    time.sleep(0.8)
    chat.hub.take(maya, task)
    chat.doing(maya, "working", "Reading LoginViewModelTest.kt")
    time.sleep(1.2)
    r.click("#view-tasks", 600)
    time.sleep(1.6)
    chat.hub.update(maya, task, "review", "Fixed: the test waited 100 ms for the token refresh, which takes up to 300 ms on CI.")
    chat.doing(maya, "listening")
    time.sleep(1.8)
    r.click("#needs-list .need", 600)
    time.sleep(1.2)
    r.click("#panel-body .needs-card button", 600, text="Mark done")
    time.sleep(1.4)
    r.click("#panel-close", 500)
    frames = r.stop(2.0)
    chat.stop()
    return frames


def scene_roles(r):
    """Give roles in the agent's settings (in the rail); the lead hands out the work; the board shows it move."""
    chat = shots.Chat("Notes App")
    maya, teo, wren = crew(chat, roles=())
    chat.quiet()
    r.size(1280, 720)
    r.open(chat.sign_in())
    r.start()
    for name, role in ((maya, "lead"), (teo, "developer"), (wren, "qa")):
        r.click('#agents .agent[data-agent="%s"]' % name, 600)
        time.sleep(0.7)
        r.choose('#agent-pane-body select[aria-label="Role for %s"]' % name, role)
        time.sleep(0.9)
    r.click("#agent-pane-close", 500)
    post_task(r, "Add a dark theme to the settings screen.")
    time.sleep(1.0)
    task = chat.hub.history(1)[0]["id"]
    chat.hub.take(maya, task)
    chat.doing(maya, "working", "Reading SettingsScreen.kt")
    time.sleep(1.0)
    r.click("#view-tasks", 600)
    time.sleep(0.8)
    build = chat.hub.assign(maya, teo, "Build the theme switch and keep the choice in DataStore.", task)["id"]
    time.sleep(0.6)
    check = chat.hub.assign(maya, wren, "Test the theme switch, also after a restart.", task)["id"]
    chat.doing(maya, "listening")
    time.sleep(1.2)
    chat.doing(teo, "working", "Editing ThemeRepository.kt")
    chat.hub.update(teo, build, "review", "Theme switch is ready to test: Settings > Theme.")
    time.sleep(1.4)
    chat.doing(wren, "working", "Running gradlew")
    time.sleep(1.2)
    chat.hub.update(wren, build, "done", "Passes, including after a restart.")
    chat.hub.update(wren, check, "done", "Tested on Android 14 and 12.")
    chat.doing(teo, "listening")
    chat.doing(wren, "listening")
    time.sleep(1.4)
    chat.hub.update(maya, task, "done", "Switch built by Teo, tested by Wren.")
    time.sleep(1.2)
    r.click("#view-tasks", 600)  # back to the chat
    time.sleep(0.8)
    chat.say(maya, O, "The dark theme is done and tested. Should the default follow the system setting?")
    frames = r.stop(2.4)
    chat.stop()
    return frames


def scene_add_agent(r):
    """+ New agent, in four steps; it joins with its name, role and first task."""
    chat = shots.Chat("Notes App")
    chat.hub.set_role(O, chat.agent(status="Coordinating", activity="listening", name="Maya"), "lead")
    chat.quiet()
    started = {}
    old_options, old_launch, old_folders = crewchat.launch_options, crewchat.launch_agent, crewchat.launch_folders
    old_ready = crewchat.tool_readiness
    crewchat.tool_readiness = lambda tool, *more, **named: [crewchat.ready_item("ok", "Signed in")]  # not this machine's own
    folder = [{"path": "/Users/you/code/notes-app", "name": "notes-app", "place": "macbook"}]
    crewchat.launch_folders = lambda config, root=None, chat="": folder
    crewchat.launch_options = lambda config, root=None, chat="": {"tools": [
        {"id": t, "label": label, "models": crewchat.tool_models(t),
         "permissions": [[k, v[0]] for k, v in crewchat.LAUNCH_PERMISSIONS[t].items()]}
        for t, (label, _) in crewchat.LAUNCH_TOOLS.items()], "folders": folder}
    crewchat.launch_agent = lambda tool, folder, key, *more, **named: started.update(key=key)
    try:
        r.size(1280, 720)
        r.open(chat.sign_in())
        r.start()
        r.click("#add-agent")
        time.sleep(0.9)
        r.click("#l-name", 600)
        r.type("Iris")
        r.choose("#l-role", "docs", 500)
        time.sleep(0.6)
        r.click("#l-next", 500)
        time.sleep(1.0)
        r.click("#l-next", 500)
        time.sleep(0.6)
        r.choose("#l-perms", "edits", 500)
        time.sleep(0.8)
        r.click("#l-next", 500)
        r.click("#l-task", 500)
        r.type("Write a user guide for the settings screen.", 0.04)
        r.click("#l-start", 600)
        time.sleep(2.4)
        session = chat.hub.open_session("macbook", "claude")  # the new terminal window's agent checks in
        chat.hub.start_link(session, started["key"])
        chat.doing("Iris", "working", "Reading SettingsScreen.kt")
        time.sleep(1.8)
        chat.say("Iris", O, "Hi, I'm Iris, on the docs. Reading the settings code, then I'll start the guide.")
        frames = r.stop(2.4)
    finally:
        crewchat.launch_options, crewchat.launch_agent, crewchat.launch_folders = old_options, old_launch, old_folders
        crewchat.tool_readiness = old_ready
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
    _, teo, wren = crew(chat)
    chat.hub.set_status(wren, "Testing the theme switch")
    chat.quiet()
    r.size(1280, 720)
    r.open(chat.sign_in())
    meta = chat.hub.add_file("image.png", shot, "image/png")  # so the page can fetch it from its own server
    r.start()
    r.click("#text")
    r.js("""(async () => { const b = await (await fetch("/files/%s/x")).blob();
        const dt = new DataTransfer(); dt.items.add(new File([b], "image.png", {type: "image/png"}));
        $("text").dispatchEvent(new ClipboardEvent("paste", {clipboardData: dt, bubbles: true, cancelable: true})); })()""" % meta["id"])
    time.sleep(1.0)
    r.type("@Wr", 0.12)
    time.sleep(0.6)
    r.tab()
    r.type("after a restart the theme is Light again. What do you see?")
    r.enter()
    time.sleep(1.2)
    chat.heard(wren)
    chat.doing(wren, "working", "Reading image.png")
    time.sleep(1.8)
    chat.say(wren, O, "I see it: Theme shows Light after the restart. It's the DataStore read; I'll tell Teo.")
    time.sleep(1.0)
    chat.say(wren, teo, "The theme resets to Light after a restart: Owner's screenshot shows it. Is DataStore read too early?")
    chat.doing(wren, "listening")
    frames = r.stop(2.4)
    chat.stop()
    return frames


def scene_phone(r):
    """On the phone: Needs you, an agent's question, and the answer."""
    chat = shots.Chat("Notes App")
    maya, teo, _ = crew(chat)
    chat.doing(teo, "working", "Editing Theme.kt")
    chat.quiet()
    task = chat.hub.add_row(O, "Pick the default theme")["id"]
    chat.hub.add_row(O, "Ship the settings screen", depends=task)
    chat.hub.take(maya, task)
    chat.hub.update(maya, task, "blocked", "Should the default follow the system setting, or always be light?")
    chat.read_all()
    r.size(390, 760, scale=2, mobile=True)
    r.open(chat.sign_in(), wait=2)
    r.start()
    r.click("#rail-open", 600)
    time.sleep(1.2)
    r.click("#needs-list .need", 600)
    time.sleep(1.2)
    r.click("#panel-body .needs-card textarea", 600)
    r.type("Follow the system setting by default.", 0.05)
    r.click("#panel-body .needs-card button", 500, text="Send")
    time.sleep(1.4)
    chat.heard(maya)
    chat.hub.update(maya, task, "in_progress", "The default follows the system setting now.")
    time.sleep(1.0)
    r.click("#panel-close", 500)
    time.sleep(0.6)
    chat.say(maya, O, "Will do: the default follows the system setting. Teo is on it.")
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
