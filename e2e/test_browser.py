"""The chat page in Google Chrome (headless): what the owner does with it.

    pip install websocket-client      (skipped without it, or without Chrome)

The "Add an agent" form is only opened and closed: starting it would open a real terminal.
"""
import json
import os
import socket
import subprocess
import sys
import time
import unittest
import urllib.request

import harness as H
from harness import eventually

try:
    import websocket
except ImportError:
    websocket = None

CHROME = {"darwin": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"}.get(sys.platform, "google-chrome")


class Chrome:
    def __init__(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.proc = subprocess.Popen([CHROME, "--headless=new", "--remote-debugging-port=%d" % self.port,
                                      "--remote-allow-origins=*", "--user-data-dir=%s" % (H.TMP / "chrome"),
                                      "--no-first-run", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pages = eventually(lambda: json.load(urllib.request.urlopen("http://127.0.0.1:%d/json" % self.port)),
                           what="Chrome to start")
        page = next(p for p in pages if p["type"] == "page")
        self.ws = websocket.create_connection(page["webSocketDebuggerUrl"], suppress_origin=True)
        self.n = 0
        self.errors = []
        for domain in ("Page", "Runtime", "DOM", "Network"):
            self.call(domain + ".enable")

    def call(self, method, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("method") == "Runtime.exceptionThrown":
                self.errors.append(msg["params"]["exceptionDetails"])
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise RuntimeError("%s: %s" % (method, msg["error"]))
                return msg.get("result", {})

    def js(self, expression):
        out = self.call("Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True)
        if out.get("exceptionDetails"):
            raise RuntimeError("page script failed: %s" % out["exceptionDetails"])
        return out.get("result", {}).get("value")

    def until(self, expression, timeout=15, what=None):
        return eventually(lambda: self.js(expression), timeout, what or expression)

    def open(self, url):
        self.call("Page.navigate", url=url)
        self.until("document.readyState === 'complete'")

    def size(self, width, height, mobile=False, dark=False):
        self.call("Emulation.setDeviceMetricsOverride", width=width, height=height, deviceScaleFactor=1, mobile=mobile)
        self.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": "dark" if dark else "light"}])

    def type_into(self, selector, text):
        self.js("(() => { const e = document.querySelector(%s); e.focus(); e.value = ''; })()" % json.dumps(selector))
        self.call("Input.insertText", text=text)

    def click(self, selector):
        self.js("document.querySelector(%s).click()" % json.dumps(selector))

    def close(self):
        self.ws.close()
        self.proc.terminate()
        self.proc.wait(10)


@unittest.skipUnless(websocket and os.path.exists(CHROME), "needs Google Chrome and websocket-client")
class Page(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = H.Machine("page")
        cls.m.start()
        cls.agent = cls.m.agent()
        cls.agent.link()
        cls.agent.stop_hook()
        cls.chrome = Chrome()
        cls.chrome.size(1280, 900)
        code = cls.m.cli("ui", "--print").split(": ")[1].split()[0]
        cls.chrome.open(cls.m.url + "/")  # signed out: the sign-in page
        cls.chrome.until("location.pathname === '/login'")
        cls.chrome.type_into("input[name=code]", code.lower())  # as typed on a phone
        cls.chrome.js("document.querySelector('form').submit()")
        cls.chrome.until("location.pathname === '/' && !!document.getElementById('log')", what="the chat page")

    @classmethod
    def tearDownClass(cls):
        cls.chrome.close()
        cls.m.stop()

    def log_text(self):
        return self.chrome.js("document.getElementById('log').innerText")

    def test_the_page_shows_the_chat_and_the_agent(self):
        c = self.chrome
        self.assertEqual(c.js("document.getElementById('project').textContent"), "Demo")
        c.until("document.getElementById('agents').innerText.includes(%s)" % json.dumps(self.agent.name))
        self.assertIn(self.agent.name, c.js("[...document.getElementById('to').options].map(o => o.value).join(' ')"))
        self.assertEqual(c.errors, [])

    def test_send_a_message_and_a_task_from_the_page(self):
        c = self.chrome
        c.until("[...document.getElementById('to').options].some(o => o.value === %s)" % json.dumps(self.agent.name))
        c.js("document.getElementById('to').value = %s" % json.dumps(self.agent.name))
        c.type_into("#text", "hello from the page")
        c.js("document.getElementById('text').dispatchEvent(new Event('input'))")
        c.click("#send")
        self.assertIn("hello from the page", eventually(self.agent.stop_hook, what="the agent to get it"))
        c.js("document.getElementById('to').value = 'all'")
        c.js("document.getElementById('task').checked = true")
        c.type_into("#text", "a task from the page")
        c.js("document.getElementById('text').dispatchEvent(new Event('input'))")
        c.click("#send")
        task = self.m.owner.wait_for("a task from the page")
        self.assertEqual(task["kind"], "task")
        c.js("document.getElementById('task').checked = false")
        self.agent.call("hub_take", id=task["id"])
        c.until("document.getElementById('log').innerText.includes('took task #%s')" % task["id"])

    def test_agent_messages_are_shown_as_text(self):
        c = self.chrome
        self.agent.call("hub_send", to="Owner", text='<img src=x onerror="window.pwned=1"><b>bold?</b>')
        c.until("document.getElementById('log').innerText.includes('onerror=')", what="the message on the page")
        time.sleep(0.5)
        self.assertIsNone(c.js("window.pwned"))
        self.assertEqual(c.js("document.querySelectorAll('#log img[src=x], #log b.bold').length"), 0)
        self.assertEqual(c.errors, [])

    def test_set_a_role_from_the_agent_card(self):
        c = self.chrome
        sel = "select[aria-label='Role for %s']" % self.agent.name
        c.until("!!document.querySelector(%s)" % json.dumps(sel))
        c.js("(() => { const s = document.querySelector(%s); s.value = 'qa'; s.dispatchEvent(new Event('change')); })()"
             % json.dumps(sel))
        eventually(lambda: self.m.owner.row(self.agent.name)["role"] == "qa", what="the role to be set")
        self.assertIn("Your role in this chat is now: QA", self.agent.stop_hook())
        c.until("document.getElementById('agents').innerText.includes('QA')")

    def test_session_controls_follow_its_state(self):
        c = self.chrome
        c.until("state.agents.length > 0", what="the agents")
        # Rows as the server sends them (the page's own data), drawn by the page's own code.
        out = c.js("""(() => {
            const saved = state.agents, savedModels = toolModels;
            toolModels = {claude: [["opus", "Opus (latest)"], ["sonnet", "Sonnet (latest)"]]};
            const base = state.agents[0];
            const row = (name, extra) => Object.assign({}, base, {agent: name, remote: false}, extra);
            state.agents = [
              row("running-one", {session: {app: "terminal", running: true, since: state.now - 60, tool: "claude", reopen: true}}),
              row("in-the-app", {session: {app: "claude-app", running: true, since: state.now, tool: "claude", reopen: true}}),
              row("closed-one", {session: {app: "terminal", running: false, since: state.now - 240, tool: "claude", reopen: true}}),
              row("closed-cursor", {session: {app: "terminal", running: false, since: state.now, tool: "cursor", reopen: false}}),
              row("far-one", {remote: true, device: "laptop", session: null}),
              row("never-reported", {session: null})];
            state.names = "";
            renderAgents();
            const card = (n) => [...document.querySelectorAll('#agents .agent')].find(e => e.innerText.includes(n));
            const buttons = (n) => [...card(n).querySelectorAll('button')].map(b => b.textContent);
            const out = {
              running: buttons("running-one"), app: card("in-the-app").innerText,
              closed: buttons("closed-one"), closedText: card("closed-one").innerText,
              far: buttons("far-one"), never: buttons("never-reported"), cursor: buttons("closed-cursor"),
              models: [...card("running-one").querySelectorAll("select")].map(s => s.getAttribute("aria-label")),
            };
            state.agents = saved; toolModels = savedModels; state.names = ""; renderAgents();  // as the server says
            return out;
        })()""")
        self.assertIn("Stop", out["running"])
        self.assertNotIn("Reopen", out["running"])
        self.assertIn("Running in the Claude app", out["app"])
        self.assertIn("Reopen", out["closed"])
        self.assertNotIn("Stop", out["closed"])
        self.assertIn("Session closed 4 min ago", out["closedText"])
        for name in ("far", "never", "cursor"):  # no session it could act on here (Cursor: not yet)
            self.assertNotIn("Stop", out[name])
            self.assertNotIn("Reopen", out[name])
        self.assertIn("Switch running-one's model", out["models"])
        self.assertEqual(c.errors, [])

    def test_avatars_follow_the_profile(self):
        c = self.chrome
        c.until("state.agents.length > 0 && state.tools", what="the agents")
        c.js("""(() => {
            window.savedAgents = state.agents;
            const base = state.agents[0];
            const row = (name, extra) => Object.assign({}, base, {agent: name, remote: false, session: null}, extra);
            state.agents = [
              row("rocket-cursor", {client: "cursor", picture: "preset:rocket", colour: ""}),
              row("lost-picture", {client: "claude", picture: "file:" + "0".repeat(16), colour: "#000000",
                                   remote: true, device: "pc", os: "windows"})];
            state.names = "";
            renderAgents();
            buildMessage({id: "av-1", seq: 1, kind: "msg", from: "rocket-cursor", to: "all", text: "hi", ts: state.now})
              .setAttribute("id", "avatar-probe");
        })()""")
        c.until("""(() => { const a = [...document.querySelectorAll('#agents .agent')]
                     .find(e => e.innerText.includes('lost-picture')); return a && !a.querySelector('.avatar img'); })()""",
                what="the missing picture to fall back to initials")
        c.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": "dark"}])
        self.addCleanup(c.call, "Emulation.setEmulatedMedia", features=[])
        self.assertTrue(c.js("matchMedia('(prefers-color-scheme: dark)').matches"))
        out = c.js("""(() => {
            const card = (n) => [...document.querySelectorAll('#agents .agent')].find(e => e.innerText.includes(n));
            const av = (n) => card(n).querySelector('.avatar');
            const probe = buildMessage({id: "av-1", seq: 1, kind: "msg", from: "rocket-cursor", to: "all", text: "hi",
                                        ts: state.now});
            const out = {
              rocket: av("rocket-cursor").innerText, rocketColour: av("rocket-cursor").style.getPropertyValue("--av"),
              mark: av("rocket-cursor").querySelector('.mark').textContent,
              lost: av("lost-picture").querySelector('.initials').textContent,
              lostInk: getComputedStyle(av("lost-picture").querySelector('.initials')).color,
              ring: getComputedStyle(av("lost-picture")).boxShadow,
              os: !!av("lost-picture").querySelector('.os'), osTitle: av("lost-picture").querySelector('.os').title,
              inMessage: !!probe.querySelector('.avatar'),
              pictureAction: [...card("rocket-cursor").querySelectorAll('summary')].map(s => s.textContent),
              remoteAction: [...card("lost-picture").querySelectorAll('summary')].map(s => s.textContent),
            };
            state.agents = window.savedAgents; state.names = ""; renderAgents();  // as the server says
            return out;
        })()""")
        self.assertIn("\U0001F680", out["rocket"])
        self.assertEqual(out["rocketColour"], "#1a1a1a")  # Cursor's own colour
        self.assertEqual(out["mark"], "Cu")
        self.assertEqual(out["lost"], "LP")  # its picture is on another machine: initials, not a broken image
        self.assertEqual(out["lostInk"], "rgb(255, 255, 255)")  # white on black
        self.assertNotEqual(out["ring"], "none")
        self.assertTrue(out["os"])  # two machines: each agent says which OS it runs on
        self.assertEqual(out["osTitle"], "Windows")
        self.assertTrue(out["inMessage"])
        self.assertIn("Picture", out["pictureAction"])
        self.assertNotIn("Picture", out["remoteAction"])  # change it on its own machine
        self.assertEqual(c.errors, [])

    def test_one_machine_shows_no_os_badge(self):
        c = self.chrome
        c.until("state.agents.length > 0 && state.tools", what="the agents")
        self.assertGreater(c.js("document.querySelectorAll('#agents .avatar').length"), 0)
        self.assertEqual(c.js("document.querySelectorAll('#agents .avatar .os').length"), 0)

    def test_attach_a_file(self):
        c = self.chrome
        path = H.TMP / "from-the-page.txt"
        path.write_text("attached from the page\n")
        node = c.call("DOM.querySelector", nodeId=c.call("DOM.getDocument")["root"]["nodeId"], selector="#files")
        c.call("DOM.setFileInputFiles", nodeId=node["nodeId"], files=[str(path)])
        c.until("document.getElementById('pending').innerText.includes('from-the-page.txt')", what="the file to be attached")
        c.js("document.getElementById('to').value = %s" % json.dumps(self.agent.name))
        c.type_into("#text", "see the file")
        c.js("document.getElementById('text').dispatchEvent(new Event('input'))")
        c.click("#send")
        got = eventually(self.agent.stop_hook, what="the agent to get the file")
        fid = got.split("[file ")[1].split(":")[0]
        self.assertIn("attached from the page", self.agent.call("hub_file", id=fid))

    def test_a_page_that_was_offline_catches_up_on_everything(self):
        c = self.chrome
        c.until("document.querySelectorAll('#log .msg').length > 0 || true")
        before = c.js("document.querySelectorAll('#log .msg').length")
        c.call("Network.emulateNetworkConditions", offline=True, latency=0, downloadThroughput=-1, uploadThroughput=-1)
        time.sleep(1)
        for i in range(600):
            self.agent.call("hub_send", to="Owner", text="overnight %d" % i)
        c.call("Network.emulateNetworkConditions", offline=False, latency=0, downloadThroughput=-1, uploadThroughput=-1)
        c.js("window.dispatchEvent(new Event('online'))")
        c.until("document.querySelectorAll('#log .msg').length >= %d" % (before + 600), timeout=60,
                what="all 600 messages on the page")
        texts = c.js("[...document.querySelectorAll('#log .msg .body')].map(e => e.innerText)"
                     ".filter(t => t.startsWith('overnight '))")
        self.assertEqual(texts, ["overnight %d" % i for i in range(600)])

    def test_the_task_sheet_on_the_page(self):
        c = self.chrome
        c.click("#view-tasks")
        c.type_into("#t-title", "Write the release notes")
        c.type_into("#t-areas", "docs/")
        c.js("document.getElementById('task-form').requestSubmit()")
        c.until("document.getElementById('sheet').innerText.includes('Write the release notes')", what="the new task")
        self.assertIn("Write the release notes", self.agent.call("hub_tasks"))
        row = c.js("[...document.querySelectorAll('#sheet tbody tr')].findIndex(r => r.innerText.includes('release notes'))")
        c.js("document.querySelectorAll('#sheet tbody tr')[%d].querySelector('button[title^=\"Mark\"]').click()" % row)
        c.until("document.querySelectorAll('#sheet tbody tr')[%d].className === 'done'" % row, what="done")
        c.click("#view-chat")
        self.assertEqual(c.errors, [])

    def test_the_commands_tab_and_the_connection_line(self):
        c = self.chrome
        c.until("!document.getElementById('link').hidden", what="the connection line")
        self.assertIn("This machine only", c.js("document.getElementById('link').innerText"))
        c.click("#view-commands")
        c.until("document.querySelectorAll('#cmd-list .cmd').length > 40", what="the commands")
        row = "[...document.querySelectorAll('#cmd-list .cmd')].find(r => r.querySelector('code').innerText === 'crewchat projects')"
        c.js(row + ".querySelector('button.run').click()")
        c.until("!%s.querySelector('pre').hidden && %s.querySelector('pre').innerText.length > 0" % (row, row),
                timeout=60, what="what projects printed")
        self.assertEqual(c.js(row + ".querySelector('pre').className"), "", c.js(row + ".querySelector('pre').innerText"))
        # Searching hides rows; clearing the search brings them back with their output still there.
        shown = "[...document.querySelectorAll('#cmd-list .cmd')].filter(r => !r.hidden).length"
        everything = c.js(shown)
        c.type_into("#cmd-filter", "task sheet")
        c.until("%s < %d" % (shown, everything), what="the search to hide rows")
        c.js("const f = document.getElementById('cmd-filter'); f.value = ''; f.dispatchEvent(new Event('input'))")
        self.assertEqual(c.js(shown), everything)
        self.assertFalse(c.js(row + ".querySelector('pre').hidden"))
        c.click("#view-chat")
        self.assertEqual(c.errors, [])

    def test_what_is_hidden_is_not_shown(self):
        c = self.chrome
        # What the agent may do without asking depends on the tool: "Also edit files" is Claude Code's only.
        c.click("#add-agent")
        c.until("document.getElementById('l-tool').options.length > 1", what="the tool list")
        c.js("const t = document.getElementById('l-tool'); t.value = [...t.options].find(o => o.value !== 'claude').value;"
             "t.dispatchEvent(new Event('change'))")
        self.assertNotIn("edits", c.js("[...document.getElementById('l-perms').options].map(o => o.value)"))
        self.assertEqual(c.js("[...document.querySelectorAll('[hidden]')].filter(e => getComputedStyle(e).display !== 'none')"
                              ".map(e => e.id || e.className)"), [])
        c.click("#l-cancel")
        # Opened again, the list starts on its first tool: the box shows exactly when that is Claude Code.
        c.js("document.getElementById('l-tool').replaceChildren()")  # so that the wait below sees the new list
        c.click("#add-agent")
        c.until("document.getElementById('l-tool').options.length > 1", what="the tool list again")
        self.assertEqual("edits" in c.js("[...document.getElementById('l-perms').options].map(o => o.value)"),
                         c.js("document.getElementById('l-tool').value === 'claude'"))
        c.click("#l-cancel")
        self.assertEqual(c.errors, [])

    def test_phone_and_dark_mode(self):
        c = self.chrome
        try:
            c.size(375, 812, mobile=True, dark=True)
            c.open(self.m.url + "/")
            c.until("!!document.getElementById('log')")
            self.assertLessEqual(c.js("document.documentElement.scrollWidth"), 375)
            bg = c.js("getComputedStyle(document.body).backgroundColor")
            self.assertNotEqual(bg, "rgb(255, 255, 255)")
            self.assertEqual(c.errors, [])
        finally:
            c.size(1280, 900)
            c.open(self.m.url + "/")
            c.until("!!document.getElementById('log')")

    def test_add_an_agent_form_opens_and_closes(self):
        c = self.chrome
        c.click("#add-agent")
        c.until("!document.getElementById('launch').hidden || getComputedStyle(document.getElementById('launch')).display !== 'none'",
                what="the form to open")
        c.until("document.getElementById('l-folder').options.length > 0", what="the folder list")
        self.assertIn("notes-app", c.js("document.getElementById('l-folder').innerText"))
        c.click("#l-cancel")
        self.assertEqual(c.errors, [])


@unittest.skipUnless(websocket and os.path.exists(CHROME), "needs Google Chrome and websocket-client")
class UpdateNoticeOnThePage(unittest.TestCase):
    def test_the_page_says_a_newer_crewchat_is_out_until_dismissed(self):
        from test_lifecycle import FakeGitHub
        github = FakeGitHub("v99.0.0")
        m = H.Machine("notice-page")
        del m.env["CREWCHAT_NO_UPDATE_CHECK"]
        m.env["CREWCHAT_UPDATE_URL"] = github.url
        chrome = None
        try:
            m.start()
            eventually(lambda: m.owner.poll()["update"] == "99.0.0", what="the check")
            chrome = Chrome()
            chrome.size(1280, 900)
            code = m.cli("ui", "--print").split(": ")[1].split()[0]
            chrome.open(m.url + "/login?code=" + code.replace("-", ""))
            chrome.until("document.getElementById('update').classList.contains('show')", what="the notice")
            text = chrome.js("document.getElementById('update').innerText")
            self.assertIn("99.0.0", text)
            self.assertIn("crewchat update", text)
            self.assertTrue(chrome.js("document.getElementById('update-notes').href").endswith(
                "/releases/tag/v99.0.0"))
            chrome.click("#update-close")
            self.assertFalse(chrome.js("document.getElementById('update').classList.contains('show')"))
            chrome.open(m.url + "/")
            chrome.until("!!document.getElementById('log') && document.getElementById('project').textContent === 'Demo'")
            time.sleep(1)
            self.assertFalse(chrome.js("document.getElementById('update').classList.contains('show')"))
            self.assertEqual(chrome.errors, [])
        finally:
            if chrome:
                chrome.close()
            m.stop()
            github.close()


if __name__ == "__main__":
    unittest.main()
