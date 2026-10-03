"""Make the screenshots in docs/images: each scene is a real crewchat server with demo data,
photographed by Google Chrome.

    pip install websocket-client
    python3 tools/docs_screenshots.py [SCENE ...]

Needs Google Chrome. Everything runs in a throwaway folder; nothing touches ~/.crewchat. With no
arguments it makes every scene; name scenes to make only those (see SCENES at the bottom).
"""
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import crewchat  # noqa: E402

try:
    import websocket  # websocket-client
except ImportError:
    sys.exit("needs: pip install websocket-client")

OUT = ROOT / "docs" / "images"
TMP = Path(tempfile.mkdtemp(prefix="crewchat-shots-")).resolve()
CHROME = {"darwin": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"}.get(sys.platform, "google-chrome")
O = crewchat.OWNER
crewchat.Handler.log_message = lambda *args: None


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------------------------------
# A demo chat
# ------------------------------------------------------------------------------------------
class Chat:
    def __init__(self, project="PhonIQ"):
        self.root = TMP / ("chat-%d" % free_port())
        self.root.mkdir()
        self.port = free_port()
        crewchat.save_config({"project": project, "port": self.port, "url": "", "forget_hours": 24}, self.root)
        crewchat.ensure_token(O, self.root)
        self.server = crewchat.make_server(self.port, self.root)
        self.hub = self.server.RequestHandlerClass.hub
        self.url = "http://127.0.0.1:%d" % self.port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def agent(self, place="macbook", client="claude", status="", activity="working"):
        name = self.hub.agent_for(self.hub.open_session(place, client))
        if status:
            self.hub.set_status(name, status)
        self.doing(name, activity)
        return name

    def doing(self, name, activity):
        with self.hub.lock:
            self.hub.waiting.pop(name, None)
            if activity == "listening":
                self.hub.waiting[name] = 1
            self.hub.activity[name] = (activity, time.time())
            self.hub._changed()

    def say(self, sender, to, text, kind="msg", files=None):
        return self.hub.send(sender, to, text, kind, files)

    def quiet(self):
        """Drop the join and role lines, and mark everything read, for a tidy picture."""
        with self.hub.lock:
            self.hub.messages[:] = [m for m in self.hub.messages if m["kind"] not in ("event", "role")]
            self.hub.ids = {m["id"]: m["seq"] for m in self.hub.messages}
            self.read_all()

    def read_all(self):
        with self.hub.lock:
            self.hub.owner_cursor = self.hub.next_seq
            for a in self.hub.agents.values():
                a["cursor"] = self.hub.next_seq
                a["seen"] = time.time()
            self.hub._changed()

    def sign_in(self):
        return self.url + "/login?code=" + self.hub.new_code()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


# ------------------------------------------------------------------------------------------
# Chrome
# ------------------------------------------------------------------------------------------
class Browser:
    def __init__(self):
        self.port = free_port()
        self.proc = subprocess.Popen([CHROME, "--headless=new", "--remote-debugging-port=%d" % self.port,
                                      "--remote-allow-origins=*", "--user-data-dir=%s" % (TMP / "chrome"),
                                      "--hide-scrollbars", "--no-first-run", "about:blank"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                pages = json.load(urllib.request.urlopen("http://127.0.0.1:%d/json" % self.port))
                break
            except OSError:
                time.sleep(0.2)
        page = next(p for p in pages if p["type"] == "page")
        self.ws = websocket.create_connection(page["webSocketDebuggerUrl"], suppress_origin=True)
        self.n = 0
        self.call("Page.enable")

    def call(self, method, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError("%s: %s" % (method, msg["error"]))
                return msg.get("result", {})

    def js(self, expression):
        out = self.call("Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True)
        if out.get("exceptionDetails"):
            raise RuntimeError("page script failed: %s" % out["exceptionDetails"])
        return out.get("result", {}).get("value")

    def size(self, width, height, scale=2, mobile=False, dark=False):
        self.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=scale,
                  mobile=mobile)
        self.call("Emulation.setEmulatedMedia", features=[
            {"name": "prefers-color-scheme", "value": "dark" if dark else "light"}])

    def open(self, url, wait=1.5):
        self.call("Page.navigate", url=url)
        time.sleep(wait)

    def shoot(self, name, selector=None, pad=0):
        clip = None
        if selector:
            box = self.js("(() => { const r = document.querySelector(%s).getBoundingClientRect();"
                          " return [r.x, r.y, r.width, r.height]; })()" % json.dumps(selector))
            x, y, w, h = box
            clip = {"x": max(0, x - pad), "y": max(0, y - pad), "width": w + 2 * pad, "height": h + 2 * pad, "scale": 1}
        params = {"format": "png"}
        if clip:
            params["clip"] = clip
        data = base64.b64decode(self.call("Page.captureScreenshot", **params)["data"])
        (OUT / (name + ".png")).write_bytes(data)
        print("  docs/images/%s.png" % name)

    def close(self):
        self.ws.close()
        self.proc.terminate()


TERMINAL = """<!doctype html><meta charset="utf-8"><style>
body { margin: 0; padding: 24px; background: #f4f2ed; font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }
.win { background: #1c1b18; color: #edeae4; border-radius: 10px; box-shadow: 0 8px 24px rgba(0,0,0,.18); width: fit-content; min-width: 640px; max-width: 980px; }
.bar { height: 30px; display: flex; align-items: center; gap: 7px; padding: 0 12px; border-bottom: 1px solid #2f2d28; }
.bar i { width: 11px; height: 11px; border-radius: 50%%; display: inline-block; }
.bar span { color: #a09b91; font: 12px system-ui, sans-serif; margin-left: 10px; }
pre { margin: 0; padding: 14px 18px 18px; white-space: pre-wrap; }
.p { color: #8fb0ff; } .c { color: #edeae4; font-weight: 600; } .o { color: #c9c4ba; }
</style><div class="win" id="t"><div class="bar"><i style="background:#ff5f57"></i><i style="background:#febc2e"></i><i style="background:#28c840"></i><span>%s</span></div><pre>%s</pre></div>"""


def terminal(browser, name, title, runs):
    """A terminal window showing real commands and what they printed."""
    import html
    body = []
    for command, output in runs:
        body.append('<span class="p">$</span> <span class="c">%s</span>' % html.escape(command))
        if output.strip():
            body.append('<span class="o">%s</span>\n' % html.escape(output.rstrip()))
    page = TMP / (name + ".html")
    page.write_text(TERMINAL % (html.escape(title), "\n".join(body)), encoding="utf-8")
    browser.size(1100, 900)
    browser.open(page.as_uri(), wait=0.5)
    browser.shoot(name, "#t", pad=14)


def run(args, home, cwd, ports=()):
    """Run a crewchat command for real; its output, with throwaway paths and ports made readable."""
    out = subprocess.run([sys.executable, str(ROOT / "crewchat.py")] + args, cwd=str(cwd), capture_output=True,
                         text=True, env=dict(os.environ, CREWCHAT_HOME=str(home)))
    text = (out.stdout + out.stderr).replace(str(cwd), "~/code/phoniq").replace(str(home), "~/.crewchat")
    for port in ports:
        text = text.replace("127.0.0.1:%s" % port, "127.0.0.1:8765")
    return text


# ------------------------------------------------------------------------------------------
# Scenes
# ------------------------------------------------------------------------------------------
def scene_sign_in(b):
    chat = Chat()
    b.size(760, 460)
    b.open(chat.url + "/login")
    b.shoot("sign-in")
    chat.stop()


def scene_empty_chat(b):
    chat = Chat()
    b.size(1100, 640)
    b.open(chat.sign_in())
    b.shoot("empty-chat")
    chat.stop()


def team_chat():
    chat = Chat()
    lead = chat.agent(status="Coordinating the dark theme", activity="listening")
    dev = chat.agent(status="Persisting the theme choice", activity="listening")
    cur = chat.agent("laptop", "cursor", status="Dark colour scheme")
    qa = chat.agent(status="Testing the colours")
    for name, role in ((lead, "lead"), (dev, "developer"), (cur, "developer"), (qa, "qa")):
        chat.hub.set_role(O, name, role)
    chat.quiet()
    task = chat.say(O, "all", "Add a dark theme to the settings screen, with a test.", "task")
    chat.hub.take(lead, task["id"])
    p1 = chat.hub.assign(lead, dev, "Build the theme switch in Settings and keep the choice in DataStore.", task["id"])
    p2 = chat.hub.assign(lead, cur, "Add the dark colour scheme to ui/theme/Color.kt.", task["id"])
    chat.hub.update(cur, p2["id"], "review", "Dark colours are in Color.kt and Theme.kt.")
    chat.say(dev, qa, "Theme switch is ready to test: Settings > Theme, then restart the app.")
    chat.say(qa, dev, "Bug: the choice resets after a restart on Android 14. Pick Dark, force-stop, reopen: it is Light again.")
    chat.say(dev, qa, "Fixed: the choice was read before DataStore had loaded. Ready again.")
    chat.hub.update(qa, p1["id"], "done", "Passes on Android 14 and 12, including after a restart.")
    chat.say(lead, O, "Dark theme: the switch is done and tested; colours are in review with QA now. One question: "
                      "should the default follow the system setting?")
    chat.read_all()
    return chat


def scene_team(b):
    chat = team_chat()
    for dark, name in ((False, "screenshot-light"), (True, "screenshot-dark")):
        b.size(1200, 1000, dark=dark)
        b.open(chat.sign_in())
        b.shoot(name)
    b.size(1200, 1000)
    b.open(chat.sign_in())
    b.shoot("team")
    b.shoot("role-cards", "#agents", pad=8)
    chat.stop()


def scene_agents_joined(b):
    chat = Chat()
    a = chat.agent(status="Fixing the login test")
    c = chat.agent("macbook", "cursor", status="Reviewing the settings screen", activity="listening")
    w = chat.agent("windows", "claude", status="", activity="idle")
    chat.say(a, "all", "Heads up: I'm about to change LoginViewModel.kt and its test.")
    chat.say(c, a, "Fine by me. I'm only in SettingsScreen.kt.")
    chat.say(w, "all", "I'm on the Windows laptop if anything needs checking there.")
    chat.read_all()
    b.size(1100, 700)
    b.open(chat.sign_in())
    b.shoot("agents-joined")
    b.shoot("agent-cards", "#agents", pad=8)
    chat.stop()


def scene_task(b):
    chat = Chat()
    a = chat.agent(status="Free")
    c = chat.agent("macbook", "cursor", status="Dark theme")
    w = chat.agent("windows", "claude", status="Free")
    chat.quiet()
    task = chat.say(O, "all", "Find out why the login test is flaky.", "task")
    chat.say(a, "all", "BID #%s: yes. I wrote that test and I'm free." % task["id"])
    chat.say(c, "all", "BID #%s: no. I'm in the middle of the dark theme." % task["id"])
    chat.say(w, "all", "BID #%s: yes, but claude-macbook knows that code better." % task["id"])
    chat.hub.take(a, task["id"])
    chat.hub.set_status(a, "Flaky login test")
    chat.say(a, O, "Found it: the test waited 100 ms for the token refresh, which takes up to 300 ms on CI. "
                   "It now waits for the refresh to finish. Pushed to fix/login-test.")
    chat.read_all()
    b.size(1100, 760)
    b.open(chat.sign_in())
    b.shoot("task-bids")
    chat.stop()


def scene_add_agent(b):
    chat = Chat()
    chat.agent(status="Coordinating the dark theme", activity="listening")
    chat.hub.set_role(O, list(chat.hub.agents)[0], "lead")
    chat.quiet()
    old = crewchat.launch_options
    crewchat.launch_options = lambda config, root=None: {
        "tools": [{"id": "claude", "label": "Claude Code"}, {"id": "cursor", "label": "Cursor"}],
        "folders": [{"path": "/Users/you/code/phoniq", "name": "phoniq", "place": "macbook"}]}
    try:
        b.size(1100, 760)
        b.open(chat.sign_in())
        b.js("""(async () => { await openLaunch(); await new Promise(r => setTimeout(r, 400));
            $("l-name").value = "docs-writer"; $("l-role").value = "docs";
            $("l-task").value = "Write a user guide for the settings screen, with the new dark theme."; })()""")
        time.sleep(0.4)
        b.shoot("add-agent-form", "#launch", pad=0)
    finally:
        crewchat.launch_options = old
    chat.stop()


def app_screenshot(b):
    """A made-up phone screen to share in the file scenes."""
    page = TMP / "phone.html"
    page.write_text("""<!doctype html><meta charset="utf-8"><style>body{margin:0;font:16px system-ui,sans-serif;
background:#121212;color:#eee} header{padding:18px;font-size:20px;font-weight:600} .row{display:flex;
justify-content:space-between;padding:16px 18px;border-top:1px solid #333} .on{color:#8fb0ff}</style>
<header>Settings</header><div class="row"><span>Theme</span><span class="on">Light</span></div>
<div class="row"><span>Notifications</span><span>On</span></div><div class="row"><span>Language</span>
<span>English</span></div>""", encoding="utf-8")
    b.size(360, 300, scale=1)
    b.open(page.as_uri(), wait=0.3)
    return base64.b64decode(b.call("Page.captureScreenshot", format="png")["data"])


def scene_files(b):
    shot = app_screenshot(b)
    chat = Chat()
    qa = chat.agent(status="Testing the theme switch")
    dev = chat.agent(status="Persisting the theme choice")
    chat.hub.set_role(O, qa, "qa")
    chat.hub.set_role(O, dev, "developer")
    chat.quiet()
    img = chat.hub.add_file("theme-after-restart.png", shot, "image/png")
    log = chat.hub.add_file("logcat.txt", b"E/ThemeRepository: preference read before DataStore loaded\n" * 30)
    chat.say(qa, dev, "After a restart the theme is Light again. Screenshot and log attached.", files=[img, log])
    chat.say(dev, qa, "Thanks, the log shows it: fixing now.")
    chat.read_all()
    b.size(1100, 700)
    b.open(chat.sign_in())
    b.shoot("files-in-chat")
    # Attaching: a pasted screenshot waiting to be sent.
    b.js("""(async () => {
        const res = await fetch("/files/%s/x"); const blob = await res.blob();
        addFiles([new File([blob], "image.png", {type: "image/png"}), new File(["Steps:\\n1. Pick Dark\\n2. Restart"], "steps.md")]);
        $("text").value = "Same on my phone. What I see:"; fit(); })()""" % img["id"])
    time.sleep(0.5)
    b.shoot("attach", "#form", pad=0)
    chat.stop()


def scene_phone(b):
    chat = team_chat()
    b.size(390, 844, scale=2, mobile=True)
    b.open(chat.sign_in(), wait=2)
    b.shoot("phone")
    b.size(390, 640, scale=2, mobile=True)
    b.open(chat.url + "/login")
    b.shoot("phone-sign-in")
    chat.stop()


def scene_terminals(b):
    home, project = TMP / "home", TMP / "phoniq"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)])
    port = str(free_port())
    start = run(["start", "--no-service", "--no-open", "--port", port, "--place", "macbook"], home, project, [port])
    again = run(["start", "--no-service", "--no-open"], home, project, [port])
    terminal(b, "terminal-start", "~/code/phoniq", [("crewchat start", start), ("crewchat start", again)])
    roles = run(["roles"], home, project)
    terminal(b, "terminal-roles", "~/code/phoniq", [("crewchat roles", roles)])
    listen = run(["listen", "status"], home, project)
    status = run(["status"], home, project, [port])
    terminal(b, "terminal-status", "~/code/phoniq", [("crewchat status", status), ("crewchat listen status", listen)])
    # Two machines linked over Tailscale (here: two servers on this machine, with local addresses).
    home2, project2 = TMP / "home2", TMP / "phoniq2"
    project2.mkdir()
    subprocess.run(["git", "init", "-q", str(project2)])
    port2 = str(free_port())
    run(["start", "--no-service", "--no-open", "--port", port2, "--place", "laptop"], home2, project2)
    invite = run(["peers", "invite", "--url", "http://127.0.0.1:%s" % port, "--name", "macbook"], home, project)
    line = next(l for l in invite.splitlines() if "crewchat peers join" in l).split()
    url, code = line[3], line[4]
    shown_url = "https://macbook.your-tailnet.ts.net"
    join = run(["peers", "join", url, code, "--url", "http://127.0.0.1:%s" % port2, "--name", "laptop"],
               home2, project2)
    time.sleep(2)
    status = run(["peers", "status"], home2, project2)
    tidy = lambda t: t.replace(url, shown_url).replace("http://127.0.0.1:%s" % port2, "https://laptop.your-tailnet.ts.net")  # noqa: E731
    terminal(b, "terminal-peers-invite", "macbook: ~/code/phoniq", [("crewchat peers invite", tidy(invite))])
    terminal(b, "terminal-peers-join", "laptop: ~/code/phoniq", [
        ("crewchat peers join %s %s" % (shown_url, code), tidy(join)), ("crewchat peers status", tidy(status))])
    for h in (home, home2):
        subprocess.run(["pkill", "-f", "serve --log %s" % (h / "hub.log")])


SCENES = {
    "sign-in": scene_sign_in, "empty-chat": scene_empty_chat, "team": scene_team,
    "agents-joined": scene_agents_joined, "task": scene_task, "add-agent": scene_add_agent,
    "files": scene_files, "phone": scene_phone, "terminals": scene_terminals,
}


def main(names):
    OUT.mkdir(parents=True, exist_ok=True)
    browser = Browser()
    try:
        for name in names or SCENES:
            print(name)
            SCENES[name](browser)
    finally:
        browser.close()
        shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    main(sys.argv[1:])
