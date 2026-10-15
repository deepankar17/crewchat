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


class Signed(unittest.TestCase):
    """A machine with one agent, and Chrome signed in to its chat page at PATH."""
    PATH = "/"

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
        if cls.PATH != "/":
            cls.chrome.open(cls.m.url + cls.PATH)
            cls.chrome.until("!!document.getElementById('log')", what="the page at " + cls.PATH)

    @classmethod
    def tearDownClass(cls):
        cls.chrome.close()
        cls.m.stop()

    def log_text(self):
        return self.chrome.js("document.getElementById('log').innerText")

    def shown_though_hidden(self):
        """Elements marked hidden that the page's styles show anyway."""
        return self.chrome.js("[...document.querySelectorAll('[hidden]')].filter(e => getComputedStyle(e).display !== 'none')"
                              ".map(e => e.id || e.className)")


@unittest.skipUnless(websocket and os.path.exists(CHROME), "needs Google Chrome and websocket-client")
class Page(Signed):
    """The page before 0.14, at /classic for one release."""
    PATH = "/classic"

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

    def test_message_avatars_survive_roster_changes(self):
        c = self.chrome
        c.until("state.agents.length > 0 && state.tools", what="the agents")
        out = c.js("""(() => {
            const saved = state.agents;
            const base = state.agents[0];
            const node = buildMessage({id: "av-2", seq: 2, kind: "msg", from: base.agent, to: "all", text: "kept", ts: state.now});
            $("log").append(node);
            state.agents = saved.concat([Object.assign({}, base, {agent: "newcomer", session: null})]);  // someone joins
            renderAgents();
            const kept = node.querySelector('[data-who].avatar, .meta > span:first-child').className;
            state.agents = saved.concat([Object.assign({}, base, {agent: "odd", client: "constructor", session: null}),
                                         Object.assign({}, base, {agent: "odder", client: "__proto__", colour: "zz",
                                                                  session: null})]);
            let threw = "";
            try { renderAgents(); } catch (e) { threw = String(e); }
            const odd = [...document.querySelectorAll('#agents .agent')].find(e => e.innerText.includes("odd"));
            const oddColour = odd ? odd.querySelector('.avatar').style.getPropertyValue("--av") : "";
            node.remove(); state.agents = saved; state.names = ""; renderAgents();
            return {kept, threw, oddColour};
        })()""")
        self.assertIn("avatar", out["kept"])  # still drawn as an avatar after the names changed
        self.assertEqual(out["threw"], "")  # a tool called "constructor" is just an unknown tool
        self.assertEqual(out["oddColour"], "#6b7280")
        self.assertEqual(c.errors, [])

    def test_closing_the_picture_menu_shows_what_changed(self):
        c = self.chrome
        c.until("state.agents.length > 0 && state.tools", what="the agents")
        c.js("""(() => { window.savedAgents = state.agents;
                 state.agents = [Object.assign({}, state.agents[0], {agent: "menu-one", remote: false, session: null})];
                 renderAgents(); })()""")
        c.js("document.querySelector('#agents .picmenu').open = true")
        c.js("""(() => { state.agents = [Object.assign({}, state.agents[0], {status: "changed while open"})];
                 renderAgents(); })()""")
        self.assertNotIn("changed while open", c.js("$('agents').innerText"))  # frozen while choosing
        c.js("document.querySelector('#agents .picmenu').open = false")
        c.until("$('agents').innerText.includes('changed while open')", what="the cards to catch up")
        c.js("state.agents = window.savedAgents; renderAgents()")

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
        self.assertEqual(self.shown_though_hidden(), [])
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
            c.open(self.m.url + self.PATH)
            c.until("!!document.getElementById('log')")
            self.assertLessEqual(c.js("document.documentElement.scrollWidth"), 375)
            bg = c.js("getComputedStyle(document.body).backgroundColor")
            self.assertNotEqual(bg, "rgb(255, 255, 255)")
            self.assertEqual(c.errors, [])
        finally:
            c.size(1280, 900)
            c.open(self.m.url + self.PATH)
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
class NewPage(Signed):
    """The chat page from 0.14: project tabs, the crew on the left, the space, a side panel."""

    def card(self):
        return "document.querySelector('#agents .agent[data-agent=%s]')" % json.dumps(self.agent.name)

    def tearDown(self):
        self.chrome.js("closePanel(); showView('chat'); document.body.classList.remove('rail-open')")

    def test_the_shell(self):
        c = self.chrome
        c.until("!!%s" % self.card(), what="the agent in the rail")
        self.assertEqual(c.js("$('project').textContent"), "Demo")
        tabs = c.js("[...document.querySelectorAll('#tabs .tab')].map(t => [t.innerText, t.getAttribute('aria-current')])")
        self.assertIn(["D\nDemo", "page"], tabs)  # (and one for each other project, not current)
        self.assertEqual([t for t in tabs if t[1] == "page"], [["D\nDemo", "page"]])
        self.assertIn("1 agent", c.js("$('space-meta').textContent"))
        self.assertTrue(c.js("$('classic-link').href").endswith("/classic"))
        self.assertTrue(c.js("$('panel').hidden"))
        self.assertEqual(self.shown_though_hidden(), [])
        self.assertEqual(c.errors, [])

    def test_mention_writes_to_one_agent_and_post_as_task(self):
        c = self.chrome
        c.until("!!%s" % self.card(), what="the agent")
        c.type_into("#text", "@%s hello from the new page" % self.agent.name)
        c.js("$('text').dispatchEvent(new Event('input'))")
        c.click("#send")
        msg = self.m.owner.wait_for("hello from the new page")
        self.assertEqual(msg["to"], self.agent.name)
        self.assertIn("hello from the new page", eventually(self.agent.stop_hook, what="the agent to get it"))
        c.until("!!document.querySelector('#log .msg.own .mention')", what="the mention as a chip")
        name = json.dumps(self.agent.name)
        self.assertEqual(c.js("[recipient('@' + %s + ': hi'), recipient('@' + %s + '. hi'), recipient('@' + %s + 'x hi'),"
                              " recipient('hi @' + %s)]" % (name, name, name, name)),
                         [self.agent.name, self.agent.name, "all", "all"])
        c.click("#as-task")
        self.assertFalse(c.js("$('task-chip').hidden"))
        c.type_into("#text", "a task from the new page")
        c.js("$('text').dispatchEvent(new Event('input'))")
        c.click("#send")
        task = self.m.owner.wait_for("a task from the new page")
        self.assertEqual((task["kind"], task["to"]), ("task", "all"))
        self.assertTrue(c.js("$('task-chip').hidden"))  # back to a message after sending
        self.assertEqual(c.errors, [])

    def test_the_agent_panel_opens_sets_a_role_and_closes(self):
        c = self.chrome
        c.until("!!%s" % self.card(), what="the agent")
        c.js(self.card() + ".click()")
        c.until("!$('panel').hidden && $('panel-title').textContent === %s" % json.dumps(self.agent.name))
        self.assertTrue(c.js("document.body.classList.contains('panel-open')"))
        self.assertTrue(c.js(self.card() + ".classList.contains('on')"))
        texts = c.js("$('panel-body').innerText")
        self.assertEqual(c.js("document.querySelector('#panel input[aria-label^=\"Name for\"]').value"), self.agent.name)
        for word in ("NOW", "SETTINGS", "ACTIONS", "Message", "Remove", "Change picture"):
            self.assertIn(word, texts.upper() if word.isupper() else texts)
        sel = "#panel select[aria-label='Role for %s']" % self.agent.name
        c.js("(() => { const s = document.querySelector(%s); s.value = 'reviewer'; s.dispatchEvent(new Event('change')); })()"
             % json.dumps(sel))
        eventually(lambda: self.m.owner.row(self.agent.name)["role"] == "reviewer", what="the role to be set")
        c.until("%s.innerText.includes('Reviewer')" % self.card(), what="the role in the rail")
        c.js("[...$('panel-body').querySelectorAll('button')].find(b => b.textContent === 'Message').click()")
        self.assertTrue(c.js("$('text').value.startsWith('@%s ')" % self.agent.name))
        c.js("$('text').value = ''")
        c.js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}))")
        c.until("$('panel').hidden && !document.body.classList.contains('panel-open')", what="Esc to close the panel")

    def test_keyboard_focus_goes_into_the_panel_and_back(self):
        c = self.chrome
        c.until("!!%s" % self.card(), what="the agent")
        c.js(self.card() + ".focus(); " + self.card() + ".click()")
        c.until("document.activeElement === $('panel-title')", what="focus in the panel")
        c.js("renderAgents(true); renderPanel(true)")  # the chat moved on meanwhile
        self.assertTrue(c.js(self.card() + ".classList.contains('on') && %s.getAttribute('aria-current') === 'true'" % self.card()))
        c.js("$('panel-close').click()")
        c.until("document.activeElement === %s" % self.card(), what="focus back on the agent")
        self.assertEqual(c.errors, [])

    def test_errors_on_the_task_board_are_shown_there_and_keep_the_task(self):
        c = self.chrome
        c.click("#view-tasks")
        c.type_into("#t-title", "Waits for a task that isn't there")
        c.type_into("#t-depends", "#9999")
        c.js("$('task-form').requestSubmit()")
        c.until("$('tasks-note').textContent.includes('Task not changed')", what="the error on the board")
        self.assertEqual(c.js("$('t-title').value"), "Waits for a task that isn't there")  # kept to fix and retry
        c.js("for (const id of ['t-title', 't-depends']) $(id).value = ''")
        self.assertEqual(c.errors, [])

    def test_a_project_that_is_gone(self):
        c = self.chrome
        try:
            c.open(self.m.url + "/?chat=no-such-project")
            c.until("$('log').innerText.includes('not on this machine any more')", what="the page to say so")
            self.assertFalse(c.js("$('banner').classList.contains('show')"))  # not "can't reach the server"
        finally:
            c.open(self.m.url + "/")
            c.until("!!document.getElementById('log')")
        self.assertEqual(c.errors, [])

    def test_rename_in_the_panel(self):
        c = self.chrome
        c.until("!!%s" % self.card(), what="the agent")
        c.js(self.card() + ".click()")
        field = "document.querySelector('#panel .namefield input')"
        c.until("!!" + field, what="the name field")
        c.js("(() => { const f = %s; f.value = 'bad name!'; f.dispatchEvent(new FocusEvent('blur')); })()" % field)
        self.assertIn("Not renamed", c.js("$('panel-body').innerText"))
        self.assertEqual(c.js(field + ".value"), self.agent.name)
        c.js("(() => { const f = %s; f.value = 'Nova'; f.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter'})); })()" % field)
        eventually(lambda: "Nova" in [r["agent"] for r in self.m.owner.poll()["agents"]], what="the rename")
        c.until("$('panel-title').textContent === 'Nova'", what="the panel to follow the new name")
        c.js("(() => { const f = %s; f.value = %s; f.dispatchEvent(new FocusEvent('blur')); })()" % (field, json.dumps(self.agent.name)))
        eventually(lambda: self.agent.name in [r["agent"] for r in self.m.owner.poll()["agents"]], what="the name back")
        self.assertEqual(c.errors, [])

    def test_session_controls_in_the_panel(self):
        c = self.chrome
        c.until("state.agents.length > 0", what="the agents")
        out = c.js("""(() => {
            const saved = state.agents, savedModels = state.models;
            state.models = {claude: [["opus", "Opus (latest)"]]};
            const base = state.agents[0];
            const row = (name, extra) => Object.assign({}, base, {agent: name, remote: false}, extra);
            state.agents = [
              row("running-one", {session: {app: "terminal", running: true, since: state.now, tool: "claude", reopen: true}}),
              row("closed-one", {session: {app: "terminal", running: false, since: state.now - 240, tool: "claude", reopen: true}}),
              row("far-one", {remote: true, device: "laptop", session: null})];
            const look = (n) => { openPanel("agent", n); const b = $("panel-body");
              return {buttons: [...b.querySelectorAll("button")].filter(x => !x.disabled).map(x => x.textContent),
                      text: b.innerText, selects: [...b.querySelectorAll("select")].map(s => s.getAttribute("aria-label")),
                      picture: !!b.querySelector(".picmenu"), nameField: !!b.querySelector(".namefield")}; };
            const out = {running: look("running-one"), closed: look("closed-one"), far: look("far-one")};
            closePanel(); state.agents = saved; state.models = savedModels; renderAgents(true);
            return out;
        })()""")
        self.assertIn("Stop", out["running"]["buttons"])
        self.assertIn("Switch running-one's model", out["running"]["selects"])
        self.assertIn("Reopen", out["closed"]["buttons"])
        self.assertIn("Session closed 4 min ago", out["closed"]["text"])
        self.assertNotIn("Remove", out["far"]["buttons"])  # it runs on another machine: changed there
        self.assertFalse(out["far"]["nameField"])
        self.assertFalse(out["far"]["picture"])
        self.assertIn("Runs on laptop", out["far"]["text"])
        self.assertEqual(c.errors, [])

    def test_roles_in_the_panel(self):
        c = self.chrome
        c.js("openPanel('roles')")
        c.until("$('panel-body').innerText.includes('Software team') && $('panel-body').innerText.includes('Lead')",
                what="the roles")
        c.js("[...$('panel-body').querySelectorAll('button')].find(b => b.textContent === 'New role').click()")
        c.until("!!document.querySelector('#panel-body form.roleform')", what="the role form")
        c.js("""(() => { const f = document.querySelector('#panel-body form');
            f.querySelector('[aria-label=Name]').value = 'designer';
            f.querySelector('[aria-label=Label]').value = 'Designer';
            f.querySelector('textarea').value = 'You design the screens.';
            f.querySelector('[aria-label=Picture]').value = 'preset:palette';
            f.requestSubmit(); })()""")
        c.until("$('panel-title').textContent === 'Designer'", what="the new role's panel")
        self.assertIn("You design the screens.", c.js("$('panel-body').innerText"))
        c.until("state.roles.some(r => r.name === 'designer')", what="the role in the page's list")
        c.js("window.confirm = () => true")
        c.js("[...$('panel-body').querySelectorAll('button')].find(b => b.textContent === 'Delete').click()")
        c.until("$('panel-title').textContent === 'Roles' && !$('panel-body').innerText.includes('Designer')",
                what="the role gone")
        self.assertEqual(c.errors, [])

    def test_the_task_board_and_a_task_in_the_panel(self):
        c = self.chrome
        c.click("#view-tasks")
        self.assertFalse(c.js("$('tasks').hidden"))
        self.assertTrue(c.js("$('form').hidden"))
        c.js("$('as-list').click()")
        self.addCleanup(c.js, "$('as-board').click()")
        c.type_into("#t-title", "Write the welcome screen")
        c.js("$('task-form').requestSubmit()")
        c.until("$('sheet').innerText.includes('Write the welcome screen')", what="the new task")
        c.js("[...document.querySelectorAll('#sheet .open-task')].find(b => b.textContent === 'Write the welcome screen').click()")
        c.until("$('panel-title').textContent.startsWith('Task #')", what="the task panel")
        self.assertIn("Write the welcome screen", c.js("$('panel-body').innerText"))
        self.assertIn("HISTORY", c.js("$('panel-body').innerText").upper())
        c.click("#tasks .back")
        self.assertFalse(c.js("$('log').hidden"))
        self.assertEqual(c.errors, [])

    def test_back_from_the_board_shows_what_came_in_meanwhile(self):
        c = self.chrome
        last_in_view = """(() => { const log = $('log'), r = log.getBoundingClientRect();
            const b = log.lastElementChild.getBoundingClientRect();
            return b.bottom > r.top && b.top < r.bottom; })()"""
        for i in range(12):
            self.agent.call("hub_send", to="all", text="Earlier note %d. " % i + "Some words to fill the chat. " * 8)
        c.until("$('log').innerText.includes('Earlier note 11')", what="the earlier notes")
        c.until("$('log').scrollHeight > $('log').clientHeight + 200", what="a chat taller than the page")
        c.until(last_in_view, what="the chat at its newest message")
        # At the bottom, then the board: what agents post meanwhile is shown on the way back.
        c.click("#view-tasks")
        for i in range(4):
            self.agent.call("hub_send", to="all", text="While on the board %d. " % i + "More words. " * 10)
        c.until("$('log').textContent.includes('While on the board 3')", what="the new messages")
        c.click("#tasks .back")
        self.assertTrue(c.js(last_in_view), "the newest message is out of view")
        # Scrolled up to read, then the board: the chat stays where the owner left it.
        c.js("$('log').scrollTop = 0")
        c.click("#view-tasks")
        self.agent.call("hub_send", to="all", text="While reading back")
        c.until("$('log').textContent.includes('While reading back')", what="the message")
        c.click("#tasks .back")
        self.assertEqual(c.js("$('log').scrollTop"), 0)
        self.assertEqual(c.errors, [])

    def test_the_task_board(self):
        c = self.chrome
        c.click("#view-tasks")
        c.js("$('as-board').click()")
        c.type_into("#t-title", "Board task one")
        c.js("$('task-form').requestSubmit()")
        c.until("!!document.querySelector('#board .col[data-col=todo] .tcard')", what="a card in To do")
        c.type_into("#t-title", "Board task two")
        c.js("$('task-form').requestSubmit()")
        c.until("[...document.querySelectorAll('#board .tcard')].some(b => b.innerText.includes('Board task two'))",
                what="the second card")
        self.assertEqual(c.js("[...document.querySelectorAll('#board .col')].map(c => c.dataset.col)"),
                         ["todo", "doing", "blocked", "review", "done"])
        ids = c.js("[...document.querySelectorAll('#board .tcard')].filter(b => b.innerText.includes('Board task'))"
                   ".map(b => b.dataset.task)")
        # The drop rules, as the page applies them.
        rules = c.js("""(() => { const r = state.sheet.find(x => x.id === %s);
            return ['todo', 'doing', 'blocked', 'review', 'done'].map(col => JSON.stringify(dropAction(r, col, ''))); })()"""
                     % json.dumps(ids[0]))
        self.assertIn('"where":"end"', rules[0])  # a move within its own column
        self.assertEqual(c.js("JSON.stringify(dropAction(state.sheet.find(x => x.id === %s), 'todo', %s))"
                              % (json.dumps(ids[0]), json.dumps(ids[0]))), "null")  # its own place: nothing
        self.assertIn('"give"', rules[1])  # Doing asks who takes it
        self.assertEqual(rules[2:4], ["null", "null"])  # Blocked and Review are the agents' to set
        self.assertIn('"status":"done"', rules[4])
        # A drop on Done marks it done; a drop before another card reorders.
        c.js("taskOp(dropAction(state.sheet.find(x => x.id === %s), 'done', ''))" % json.dumps(ids[1]))
        c.until("!!document.querySelector('#board .col[data-col=done] .tcard[data-task=\"%s\"]')" % ids[1], what="done")
        # Doing asks for an agent, then gives it the task.
        c.js("askGive(state.sheet.find(x => x.id === %s))" % json.dumps(ids[0]))
        self.assertFalse(c.js("$('give').hidden"))
        self.assertIn("Give #%s to" % ids[0], c.js("$('give-label').textContent"))
        c.js("$('give-to').value = %s; $('give-ok').click()" % json.dumps(self.agent.name))
        c.until("!!document.querySelector('#board .col[data-col=doing] .tcard[data-task=\"%s\"]')" % ids[0],
                what="the task given to the agent")
        # Done and Doing tasks can't be given from the board: only one to do.
        self.assertEqual(c.js("JSON.stringify(dropAction(state.sheet.find(x => x.id === %s), 'doing', ''))" % json.dumps(ids[1])), "null")
        # A filter by agent, and the list view.
        c.js("$('f-agent').value = %s; $('f-agent').dispatchEvent(new Event('change'))" % json.dumps(self.agent.name))
        shown = c.js("[...document.querySelectorAll('#board .tcard')].map(b => b.innerText)")
        self.assertTrue(shown and all(self.agent.name in t for t in shown), shown)
        c.js("$('f-agent').value = ''; $('f-agent').dispatchEvent(new Event('change'))")
        c.js("$('as-list').click()")
        self.assertTrue(c.js("$('board').hidden && !$('sheet').hidden"))
        c.js("$('as-board').click()")
        # A card opens its task in the panel.
        c.js("document.querySelector('#board .tcard').click()")
        c.until("$('panel-title').textContent.startsWith('Task #')", what="the task panel")
        self.assertEqual(c.errors, [])

    def test_needs_you_and_notifications(self):
        c = self.chrome
        self.addCleanup(c.js, "delete document.hidden; try { localStorage.removeItem(notifyKey()); } catch (e) {} showBell()")
        # Notifications, stubbed: the page asks once, and shows them only while it is in the background.
        c.js("""window.notes = []; window.Notification = class { constructor(t, o) { window.notes.push([t, o.body]); }
                  close() {} static requestPermission() { Notification.permission = 'granted'; return Promise.resolve('granted'); } };
                Notification.permission = 'default';""")
        c.js("$('bell').open = true")
        c.js("document.querySelector('[data-notify=all]').click()")
        c.until("Notification.permission === 'granted' && !$('bell').classList.contains('off')", what="the bell on")
        c.js("$('bell').open = false")
        tid = c.js("postJSON('/api/task', {op: 'add', title: 'Pick a colour for the login button'}).then(r => r.id)")
        self.agent.call("hub_take", id=tid)
        c.js("Object.defineProperty(document, 'hidden', {value: true, configurable: true})")  # the page goes to the back
        self.agent.call("hub_update", id=tid, status="blocked", note="Blue or green?")
        c.until("!$('needs-you').hidden && $('needs-list').innerText.includes('Blue or green?')", what="the item")
        c.until("window.notes.some(n => n[0].includes('Needs you') && n[1].includes('Blue or green?'))", what="a notification")
        self.assertGreaterEqual(int(c.js("$('needs-count').textContent")), 1)
        self.assertTrue(c.js("[...document.querySelectorAll('#tabs .tab')].some(t => t.classList.contains('needs'))"))
        c.js("delete document.hidden")
        # The item opens its task, with the question and a way to answer.
        c.js("[...document.querySelectorAll('#needs-list .need')].find(b => b.innerText.includes('Blue or green?')).click()")
        c.until("!!document.querySelector('#panel-body .needs-card textarea')", what="the answer box")
        c.js("""(() => { const t = document.querySelector('#panel-body .needs-card textarea'); t.value = 'Green, please';
                  [...document.querySelectorAll('#panel-body .needs-card button')].find(b => b.textContent === 'Send').click(); })()""")
        self.assertIn("Re #%s: Green, please" % tid, eventually(self.agent.stop_hook, what="the agent to get the answer"))
        self.agent.call("hub_update", id=tid, status="in_progress", note="Green it is")
        c.until("$('needs-you').hidden || !$('needs-list').innerText.includes('Blue or green?')", what="the item to go")
        # A message to you is unread until the page is in front, then read.
        self.agent.call("hub_send", to="Owner", text="Done with the button")
        c.until("$('log').innerText.includes('Done with the button')", what="the message on the page")
        c.js("$('log').scrollTop = $('log').scrollHeight; markRead()")
        c.until("!state.needs.some(n => n.kind === 'message')", what="read while in front")
    def test_dragging_a_card_with_the_mouse(self):
        c = self.chrome
        c.click("#view-tasks")
        c.js("$('as-board').click()")
        ids = [c.js("postJSON('/api/task', {op: 'add', title: %s}).then(r => r.id)" % json.dumps("Drag %d" % i)) for i in range(3)]
        card = "document.querySelector('#board .tcard[data-task=\"%s\"]')"
        c.until("!!" + card % ids[2], what="the cards")
        # Drag the last of the three to just above the first: the page reads where the pointer is.
        c.js("""(() => { const from = %s, onto = %s, col = onto.closest('.col'), dt = new DataTransfer();
            const box = onto.getBoundingClientRect(), y = box.top + 2;
            from.dispatchEvent(new DragEvent('dragstart', {dataTransfer: dt, bubbles: true}));
            col.dispatchEvent(new DragEvent('dragover', {dataTransfer: dt, bubbles: true, cancelable: true, clientY: y}));
            onto.dispatchEvent(new DragEvent('drop', {dataTransfer: dt, bubbles: true, cancelable: true, clientY: y}));
            from.dispatchEvent(new DragEvent('dragend', {dataTransfer: dt, bubbles: true})); })()""" % (card % ids[2], card % ids[0]))
        order = "state.sheet.map(r => r.id).filter(id => %s.includes(id))" % json.dumps(ids)
        c.until("JSON.stringify(%s) === %s" % (order, json.dumps(json.dumps([ids[2], ids[0], ids[1]]).replace(" ", ""))),
                what="the card moved above the first")
        self.assertEqual(c.errors, [])

    def test_mention_suggestions_reply_and_grouping(self):
        c = self.chrome
        c.until("!!%s" % self.card(), what="the agent")
        first = self.agent.name[:2]
        c.js("const t = $('text'); t.focus(); t.value = 'hi @%s'; t.setSelectionRange(t.value.length, t.value.length);"
             "t.dispatchEvent(new Event('input'))" % first)
        c.until("!$('mentions').hidden", what="the suggestions")
        self.assertIn(self.agent.name, c.js("$('mentions').innerText"))
        c.js("$('text').dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter'}))")
        self.assertEqual(c.js("$('text').value"), "hi @%s " % self.agent.name)
        self.assertTrue(c.js("$('mentions').hidden"))
        c.js("$('text').value = ''")
        # Two messages in a row from one agent: the second joins the first; Reply writes to it.
        self.agent.call("hub_send", to="Owner", text="first of two")
        self.agent.call("hub_send", to="Owner", text="second of two")
        c.until("$('log').innerText.includes('second of two')", what="the messages")
        last = "[...document.querySelectorAll('#log .msg')].pop()"
        self.assertTrue(c.js(last + ".classList.contains('cont')"))
        c.js(last + ".querySelector('.reply').click()")
        self.assertEqual(c.js("$('text').value"), "@%s " % self.agent.name)
        c.js("$('text').value = ''")
        self.assertEqual(c.errors, [])

    def test_new_agent_in_four_steps(self):
        c = self.chrome
        c.click("#add-agent")
        c.until("$('launch').open && $('l-folder').options.length > 0", what="the dialog and its folders")
        self.assertIn("notes-app", c.js("$('l-folder').innerText"))
        step = "[...document.querySelectorAll('#launch .step')].findIndex(f => !f.hidden)"
        self.assertEqual(c.js(step), 0)
        self.assertEqual(c.js("[$('l-back').hidden, $('l-next').hidden, $('l-start').hidden]"), [True, False, True])
        c.js("$('l-template').value = 'qa'; $('l-template').dispatchEvent(new Event('change'))")
        self.assertEqual(c.js(step), 1)  # a template skips to the workspace
        self.assertEqual(c.js("[$('l-role').value, $('l-picture').value]"), ["qa", "art:scientist"])
        c.click("#l-next")
        c.click("#l-next")
        self.assertEqual(c.js(step), 3)
        self.assertEqual(c.js("[$('l-back').hidden, $('l-next').hidden, $('l-start').hidden]"), [False, True, False])
        self.assertEqual(c.js("document.querySelector('#l-steps li.on').textContent"), "Briefing")
        c.click("#l-back")
        self.assertEqual(c.js(step), 2)
        self.assertEqual(self.shown_though_hidden(), [])
        c.click("#l-cancel")
        self.assertEqual(c.errors, [])

    def test_search_hides_what_does_not_match(self):
        c = self.chrome
        self.agent.call("hub_send", to="Owner", text="the blue elephant report")
        c.until("$('log').innerText.includes('blue elephant')", what="the message")
        c.type_into("#search", "elephant")
        c.js("$('search').dispatchEvent(new Event('input'))")
        shown = c.js("[...$('log').children].filter(n => !n.hidden).map(n => n.innerText).join('|')")
        self.assertIn("elephant", shown)
        self.assertNotIn("hello", shown.lower().replace("elephant", ""))
        c.js("$('search').value = ''; $('search').dispatchEvent(new Event('input'))")
        self.assertEqual(c.js("[...$('log').children].filter(n => n.hidden).length"), 0)

    def test_a_new_project_from_the_tab_menu(self):
        c = self.chrome
        c.js("$('tab-add').open = true")
        c.click("#new-project")
        c.until("$('project-dialog').open && $('p-pack').options.length > 3", what="the dialog and the packs")
        c.type_into("#p-name", "Cooking channel")
        c.js("$('p-pack').value = 'youtube'")
        c.js("$('project-form').requestSubmit()")
        c.until("location.search.includes('chat=cooking-channel') && $('project').textContent === 'Cooking channel'",
                what="the new project's page")
        c.until("[...document.querySelectorAll('#tabs .tab')].length === 2", what="a tab for each project")
        c.until("state.crew === 'general' && state.roles.some(r => r.title === 'Producer')", what="the YouTube pack")
        c.js("[...document.querySelectorAll('#tabs .tab')].find(t => t.innerText.includes('Demo')).click()")
        c.until("$('project').textContent === 'Demo' && state.agents.length > 0", what="back to Demo")
        self.assertEqual(c.errors, [])

    def test_phone_and_dark_mode(self):
        c = self.chrome
        try:
            c.size(375, 812, mobile=True, dark=True)
            c.open(self.m.url + "/")
            c.until("!!%s" % self.card(), what="the page on a phone")
            self.assertLessEqual(c.js("document.documentElement.scrollWidth"), 375)
            self.assertNotEqual(c.js("getComputedStyle(document.body).backgroundColor"), "rgb(255, 255, 255)")
            self.assertEqual(c.js("getComputedStyle($('tabs')).display"), "none")  # a picker instead
            self.assertNotEqual(c.js("getComputedStyle($('projects')).display"), "none")
            c.click("#rail-open")
            c.until("$('rail').getBoundingClientRect().left >= 0", what="the drawer to open")
            c.js(self.card() + ".click()")
            c.until("!$('panel').hidden", what="the panel")
            self.assertEqual(c.js("[$('panel').getBoundingClientRect().width, document.body.classList.contains('rail-open')]"),
                             [375, False])  # a full-screen sheet; the drawer closed
            self.assertEqual(c.errors, [])
        finally:
            c.size(1280, 900)
            c.open(self.m.url + "/")
            c.until("!!document.getElementById('log')")


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
