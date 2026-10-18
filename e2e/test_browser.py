"""The chat page in Google Chrome (headless): what the owner does with it.

    pip install websocket-client      (skipped without it, or without Chrome)

The "Add an agent" form is only opened and closed: starting it would open a real terminal.
"""
import json
import os
import socket
import subprocess
import sys
import threading
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
        self.events = []  # the other events, newest last (ServiceWorker.* when that domain is on)
        for domain in ("Page", "Runtime", "DOM", "Network"):
            self.call(domain + ".enable")

    def call(self, method, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("method") == "Runtime.exceptionThrown":
                self.errors.append(msg["params"]["exceptionDetails"])
            elif msg.get("method"):
                self.events = (self.events + [msg])[-200:]
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
        def check():
            try:
                return self.js(expression)
            except RuntimeError as e:  # a page still loading after a click navigated: its script is not in yet
                if "is not defined" in str(e):
                    return None
                raise
        return eventually(check, timeout, what or expression)

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
    """A machine with one agent, and Chrome signed in to its chat page."""

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

    def shown_though_hidden(self):
        """Elements marked hidden that the page's styles show anyway."""
        return self.chrome.js("[...document.querySelectorAll('[hidden]')].filter(e => getComputedStyle(e).display !== 'none')"
                              ".map(e => e.id || e.className)")


@unittest.skipUnless(websocket and os.path.exists(CHROME), "needs Google Chrome and websocket-client")
class NewPage(Signed):
    """The chat page from 0.14: project tabs, the crew on the left, the space, a side panel."""

    def card(self):
        return "document.querySelector('#agents .agent[data-agent=%s]')" % json.dumps(self.agent.name)

    def tearDown(self):
        self.chrome.js("closePanel(); if (state.agentPane) closeAgent(); showView('chat'); document.body.classList.remove('rail-open')")

    def test_the_shell(self):
        c = self.chrome
        c.until("!!%s" % self.card(), what="the agent in the rail")
        self.assertEqual(c.js("$('project').textContent"), "Demo")
        tabs = c.js("[...document.querySelectorAll('#tabs .tab')].map(t => [t.innerText, t.getAttribute('aria-current')])")
        self.assertIn(["D\nDemo", "page"], tabs)  # (and one for each other project, not current)
        self.assertEqual([t for t in tabs if t[1] == "page"], [["D\nDemo", "page"]])
        self.assertIn("1 agent", c.js("$('space-meta').textContent"))
        self.assertTrue(c.js("$('panel').hidden"))
        self.assertEqual(self.shown_though_hidden(), [])
        self.assertEqual(c.errors, [])

    def test_the_rail_buttons_stay_on_one_line(self):
        c = self.chrome
        was = c.js("$('tasks-count').textContent")
        c.js("$('tasks-count').textContent = '(12)'")  # a busy board's count
        try:
            # Each button's text, the lines it takes (the tops of its text's boxes), and whether it is cut off.
            shown = c.js("[...document.querySelectorAll('.rail-tools button')].map(b => { const r = document.createRange(); "
                         "r.selectNodeContents(b); return [b.innerText, new Set([...r.getClientRects()].filter(x => x.width)"
                         ".map(x => Math.round(x.top))).size, b.scrollWidth > b.clientWidth]; })")
        finally:
            c.js("$('tasks-count').textContent = %s" % json.dumps(was))
        self.assertEqual([t for t, lines, over in shown if lines != 1 or over], [], shown)

    def test_agent_messages_are_shown_as_text(self):
        c = self.chrome
        self.agent.call("hub_send", to="Owner", text='<img src=x onerror="window.pwned=1"><b>bold?</b>')
        c.until("$('log').innerText.includes('onerror=')", what="the message on the page")
        time.sleep(0.5)
        self.assertIsNone(c.js("window.pwned"))
        self.assertEqual(c.js("document.querySelectorAll('#log img[src=x], #log b').length"), 0)
        self.assertEqual(c.errors, [])

    def test_attach_a_file(self):
        c = self.chrome
        c.until("!!%s" % self.card(), what="the agent")
        path = H.TMP / "from-the-page.txt"
        path.write_text("attached from the page\n")
        node = c.call("DOM.querySelector", nodeId=c.call("DOM.getDocument")["root"]["nodeId"], selector="#files")
        c.call("DOM.setFileInputFiles", nodeId=node["nodeId"], files=[str(path)])
        c.until("$('pending').innerText.includes('from-the-page.txt')", what="the file to be attached")
        c.type_into("#text", "@%s see the file" % self.agent.name)
        c.js("$('text').dispatchEvent(new Event('input'))")
        c.click("#send")
        got = eventually(self.agent.stop_hook, what="the agent to get the file")
        fid = got.split("[file ")[1].split(":")[0]
        self.assertIn("attached from the page", self.agent.call("hub_file", id=fid))
        self.assertEqual(c.errors, [])

    def test_a_page_that_was_offline_catches_up_on_everything(self):
        c = self.chrome
        c.until("!!%s" % self.card(), what="the agent")
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
        c.until("!$('agent-pane').hidden && $('agent-pane-title').textContent === %s" % json.dumps(self.agent.name))
        self.assertTrue(c.js("$('panel').hidden"))  # in the rail, under the crew: the side panel is for the project
        self.assertTrue(c.js("$('rail').contains($('agent-pane'))"))
        self.assertTrue(c.js(self.card() + ".classList.contains('on')"))
        texts = c.js("$('agent-pane-body').innerText")
        self.assertEqual(c.js("document.querySelector('#agent-pane input[aria-label^=\"Name for\"]').value"), self.agent.name)
        for word in ("NOW", "SETTINGS", "ACTIONS", "Message", "Remove"):
            self.assertIn(word, texts.upper() if word.isupper() else texts)
        # The picture itself opens the picture menu, and closes it again.
        pic = "document.querySelector('#agent-pane .picbutton')"
        self.assertTrue(c.js("%s.getAttribute('aria-label') === %s" % (pic, json.dumps("Change %s's picture" % self.agent.name))))
        c.js(pic + ".click()")
        c.until("!document.querySelector('#agent-pane .picmenu').hidden && %s.getAttribute('aria-expanded') === 'true'" % pic,
                what="the picture menu to open")
        self.assertGreater(c.js("document.querySelectorAll('#agent-pane .picmenu .picks button.art').length"), 10)
        c.js(pic + ".click()")
        c.until("document.querySelector('#agent-pane .picmenu').hidden", what="the picture menu to close")
        sel = "#agent-pane select[aria-label='Role for %s']" % self.agent.name
        c.js("(() => { const s = document.querySelector(%s); s.value = 'reviewer'; s.dispatchEvent(new Event('change')); })()"
             % json.dumps(sel))
        eventually(lambda: self.m.owner.row(self.agent.name)["role"] == "reviewer", what="the role to be set")
        c.until("%s.innerText.includes('Reviewer')" % self.card(), what="the role in the rail")
        c.js("[...$('agent-pane-body').querySelectorAll('button')].find(b => b.textContent === 'Message').click()")
        self.assertTrue(c.js("$('text').value.startsWith('@%s ')" % self.agent.name))
        c.js("$('text').value = ''")
        c.js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}))")
        c.until("$('agent-pane').hidden && !%s.classList.contains('on')" % self.card(), what="Esc to close the agent's settings")

    def test_keyboard_focus_goes_into_the_panel_and_back(self):
        c = self.chrome
        c.until("!!%s" % self.card(), what="the agent")
        c.js(self.card() + ".focus(); " + self.card() + ".click()")
        c.until("document.activeElement === $('agent-pane-title')", what="focus in the agent's settings")
        c.js("renderAgents(true); renderAgentPane(true)")  # the chat moved on meanwhile
        self.assertTrue(c.js(self.card() + ".classList.contains('on') && %s.getAttribute('aria-current') === 'true'" % self.card()))
        c.js("$('agent-pane-close').click()")
        c.until("document.activeElement === %s" % self.card(), what="focus back on the agent")
        self.assertEqual(c.errors, [])

    def test_commands_open_in_the_side_panel_beside_an_agents_settings(self):
        c = self.chrome
        c.until("!!%s" % self.card(), what="the agent")
        c.js(self.card() + ".click()")
        c.until("!$('agent-pane').hidden", what="the agent's settings")
        c.click("#view-commands")
        c.until("!$('panel').hidden && $('panel-title').textContent === 'Commands' && !$('commands').hidden",
                what="the commands in the side panel")
        c.until("document.querySelectorAll('#panel #cmd-list .cmd').length > 40", what="the commands")
        self.assertFalse(c.js("$('agent-pane').hidden"))  # both at once: the agent on the left, the commands on the right
        self.assertEqual(c.js("$('view-commands').getAttribute('aria-pressed')"), "true")
        self.assertFalse(c.js("$('log').hidden"))  # the chat stays in the middle
        c.click("#view-commands")
        c.until("$('panel').hidden && $('view-commands').getAttribute('aria-pressed') === 'false'", what="Commands to close")
        self.assertEqual(c.errors, [])

    def test_new_agent_browses_makes_and_connects_a_folder(self):
        c = self.chrome
        top = H.TMP / "browse-here"
        (top / "apps" / "web").mkdir(parents=True, exist_ok=True)
        try:
            c.js("openLaunch(); showStep(1)")
            c.js("$('l-path').value = %s" % json.dumps(str(top)))
            c.click("#l-browse")
            c.until("[...document.querySelectorAll('#l-browser .dirs button')].some(b => b.textContent.includes('apps'))",
                    what="the folder list")
            c.js("[...document.querySelectorAll('#l-browser .dirs button')].find(b => b.textContent.includes('apps')).click()")
            c.until("$('l-path').value.endsWith('/apps') && $('l-browser').innerText.includes('web')", what="into apps")
            c.js("document.querySelector('#l-browser .make input').value = 'mobile'")
            c.js("[...document.querySelectorAll('#l-browser .make button')].find(b => b.textContent === 'New folder').click()")
            c.until("$('l-path').value.endsWith('/apps/mobile')", what="into the new folder")
            self.assertTrue((top / "apps" / "mobile").is_dir())
            c.js("[...document.querySelectorAll('#l-browser .make button')].find(b => b.textContent === 'Connect this folder').click()")
            c.until("$('l-folder').value.endsWith('/apps/mobile') && $('l-browser').hidden", timeout=60, what="connected")
            self.assertTrue((top / "apps" / "mobile" / ".mcp.json").is_file())
            # A path that is not there: Connect offers to make it.
            c.js("$('l-path').value = %s" % json.dumps(str(top / "apps" / "desktop")))
            c.click("#l-connect")
            c.until("$('launch-note').innerText.includes('Create it and connect')", what="the offer to make it")
            c.js("[...$('launch-note').querySelectorAll('button')].find(b => b.textContent === 'Create it and connect').click()")
            c.until("$('l-folder').value.endsWith('/apps/desktop')", timeout=60, what="made and connected")
            self.assertEqual(c.errors, [])
        finally:
            c.js("$('launch').close()")

    def test_errors_on_the_task_board_are_shown_there_and_keep_the_task(self):
        c = self.chrome
        c.click("#view-tasks")
        c.type_into("#t-title", "Waits for a task that isn't there")
        c.type_into("#t-depends", "#9999")
        c.js("$('task-form').requestSubmit()")
        c.until("$('tasks-note').textContent.includes('Task not added')", what="the error on the board")
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
        field = "document.querySelector('#agent-pane .namefield input')"
        c.until("!!" + field, what="the name field")
        c.js("(() => { const f = %s; f.value = 'bad name!'; f.dispatchEvent(new FocusEvent('blur')); })()" % field)
        self.assertIn("Not renamed", c.js("$('agent-pane-body').innerText"))
        self.assertEqual(c.js(field + ".value"), self.agent.name)
        c.js("(() => { const f = %s; f.value = 'Nova'; f.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter'})); })()" % field)
        eventually(lambda: "Nova" in [r["agent"] for r in self.m.owner.poll()["agents"]], what="the rename")
        c.until("$('agent-pane-title').textContent === 'Nova'", what="the panel to follow the new name")
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
              row("far-one", {remote: true, device: "laptop", session: null}),
              row("unrecorded-one", {session: null, startable: true, activity: "", seen: state.now - 3600}),
              row("busy-one", {session: null, startable: false})];
            const look = (n) => { openPanel("agent", n); const b = $("agent-pane-body");
              return {buttons: [...b.querySelectorAll("button")].filter(x => !x.disabled).map(x => x.textContent),
                      text: b.innerText, selects: [...b.querySelectorAll("select")].map(s => s.getAttribute("aria-label")),
                      picture: !!b.querySelector(".picmenu"), nameField: !!b.querySelector(".namefield")}; };
            const out = {running: look("running-one"), closed: look("closed-one"), far: look("far-one"),
                         unrecorded: look("unrecorded-one"), busy: look("busy-one")};
            closeAgent(); state.agents = saved; state.models = savedModels; renderAgents(true);
            return out;
        })()""")
        self.assertIn("Stop", out["running"]["buttons"])
        self.assertIn("Switch running-one's model", out["running"]["selects"])
        self.assertIn("Reopen", out["closed"]["buttons"])
        self.assertIn("Session closed 4 min ago", out["closed"]["text"])
        self.assertNotIn("Start again", out["closed"]["buttons"])  # its own conversation reopens instead
        self.assertIn("Start again", out["unrecorded"]["buttons"])  # no conversation recorded: a new session
        self.assertNotIn("Reopen", out["unrecorded"]["buttons"])
        self.assertNotIn("Start again", out["busy"]["buttons"])
        self.assertNotIn("Remove", out["far"]["buttons"])  # it runs on another machine: changed there
        self.assertFalse(out["far"]["nameField"])
        self.assertFalse(out["far"]["picture"])
        self.assertIn("Runs on laptop", out["far"]["text"])
        self.assertEqual(c.errors, [])

    def test_edit_tracking_in_the_project_panel(self):
        c = self.chrome
        c.js("openPanel('project')")
        c.until("$('edits-note').textContent.startsWith('Kept')", what="edit tracking on (a software team)")
        self.assertEqual(c.js("$('edits-mode').value"), "auto")
        c.js("$('edits-mode').value = 'off'; $('edits-mode').dispatchEvent(new Event('change'))")
        c.until("$('edits-note').textContent === 'Not kept.'", what="edit tracking off")
        self.assertIn("Edit tracking: off", self.m.cli("edits"))
        c.js("$('edits-mode').value = 'auto'; $('edits-mode').dispatchEvent(new Event('change'))")
        c.until("$('edits-note').textContent.startsWith('Kept')", what="back to auto")
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
        c.until("!state.roles.some(r => r.name === 'designer')", what="the page's list to follow (the poll's boot tag)")
        self.assertEqual(c.errors, [])

    def test_adding_and_taking_off_tasks_on_the_sheet(self):
        c = self.chrome
        c.click("#view-tasks")
        c.js("$('as-list').click()")
        self.addCleanup(c.js, "$('as-board').click(); showView('chat'); window.confirm = () => true")
        # Add with no title says what is missing, and puts the cursor there.
        c.js("$('t-title').value = ''; $('task-form').requestSubmit()")
        self.assertIn("Write the task first", c.js("$('tasks-note').textContent"))
        self.assertEqual(c.js("document.activeElement.id"), "t-title")
        # An error on adding says it was not added (not "not changed").
        c.type_into("#t-title", "Write the release notes")
        c.type_into("#t-depends", "#99999")
        c.js("$('task-form').requestSubmit()")
        c.until("$('tasks-note').textContent.startsWith('Task not added')", what="the error on adding")
        # Added: the form is cleared and ready for the next one.
        c.js("$('t-depends').value = ''")
        c.js("$('task-form').requestSubmit()")
        c.until("$('sheet').innerText.includes('Write the release notes')", what="the new task")
        self.assertEqual(c.js("[$('t-title').value, document.activeElement.id]"), ["", "t-title"])
        mid = c.js("state.sheet.find(r => r.title === 'Write the release notes').id")
        off = "[...document.querySelectorAll('#sheet button')].find(b => b.title === 'Take task %s off the sheet')" % mid
        # × asks first, as every action that cannot be undone here does.
        c.js("window.asked = ''; window.confirm = (q) => { window.asked = q; return false; }")
        c.js(off + ".click()")
        self.assertIn("off the sheet?", c.js("window.asked"))
        self.assertTrue(c.js("state.sheet.some(r => r.id === %s)" % json.dumps(mid)))  # said no: still there
        c.js("window.confirm = () => true")
        c.js(off + ".click()")
        c.until("!state.sheet.some(r => r.id === %s)" % json.dumps(mid), what="the task off the sheet")
        # Its card in the chat says so, instead of "Open".
        card = "state.nodes.get(%s)[1].querySelector('.state').textContent" % json.dumps(mid)
        c.until("%s === 'Off the sheet'" % card, what="the card to say it is off the sheet")
        self.assertEqual(c.errors, [])

    def test_searching_from_the_board_shows_the_chat(self):
        c = self.chrome
        self.addCleanup(c.js, "$('search').value = ''; applySearch(); showView('chat')")
        c.click("#view-tasks")
        self.assertTrue(c.js("$('log').hidden"))
        c.type_into("#search", "anything")
        self.assertFalse(c.js("$('log').hidden"))  # the search is of the chat: it shows, with what matches
        self.assertTrue(c.js("$('tasks').hidden"))
        self.assertEqual(c.errors, [])

    def test_a_blocked_tasks_answer_shows_in_its_history(self):
        c = self.chrome
        mid = self.m.owner.task("all", "Pick the default theme")
        self.agent.call("hub_task", action="take", id=mid)
        self.agent.call("hub_task", action="update", id=mid, status="blocked", note="Light or dark by default?")
        c.until("state.needs.some(n => n.task === %s)" % json.dumps(mid), what="Needs you")
        c.js("openPanel('task', %s)" % json.dumps(mid))
        c.until("!!document.querySelector('#panel .needs-card textarea')", what="the answer box")
        c.js("const t = document.querySelector('#panel .needs-card textarea'); t.value = 'Follow the system setting.'")
        c.js("[...document.querySelectorAll('#panel .needs-card button')].find(b => b.textContent === 'Send').click()")
        c.until("$('panel-body').innerText.includes('You: Re #%s: Follow the system setting.')" % mid,
                what="the answer in the task's history")
        self.assertEqual(c.errors, [])

    def test_the_agent_settings_have_room_and_its_count_says_what_it_is(self):
        c = self.chrome
        for i in range(2):
            self.m.owner.send(self.agent.name, "Unread %d" % i)
        c.until("(state.agents.find(a => a.agent === %s) || {}).unread > 0" % json.dumps(self.agent.name), what="unread")
        c.until("!!%s.querySelector('.count')" % self.card(), what="the count on its card")
        self.assertIn("has not read yet", c.js("%s.querySelector('.count').title" % self.card()))
        c.js(self.card() + ".click()")
        c.until("!$('agent-pane').hidden", what="the agent's settings")
        widths = c.js("[...$('agent-pane').querySelectorAll('.field select')].map(s => s.getBoundingClientRect().width)")
        self.assertTrue(widths and min(widths) >= 180, widths)  # the menus had 120 px, "As its folder s…"
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

    def test_suggested_files_are_accepted_in_the_task_panel(self):
        # Roadmap C2: when a task goes to review, the folders its holder changed outside its areas are suggested,
        # and the owner adds them with one click.
        c = self.chrome
        mid = self.m.owner.task("all", "Tidy the search box")
        self.agent.call("hub_task", action="take", id=mid)
        status, raw, _ = H.request(self.m.url + "/api/hook", {"key": self.agent.key, "event": "tool", "doing": "Editing",
                                                              "path": "web/search/box.tsx"},
                                   {"Authorization": "Bearer " + self.agent.token})
        self.assertEqual(status, 200, raw)
        self.agent.call("hub_task", action="update", id=mid, status="review", note="ready")
        c.js("openPanel('task', %s)" % json.dumps(mid))
        c.until("$('panel-body').innerText.includes('web/search/')", what="the suggested files")
        self.assertIn("SUGGESTED FILES", c.js("$('panel-body').innerText").upper())
        c.js("[...$('panel-body').querySelectorAll('button')].find(b => b.textContent === 'Accept files').click()")
        c.until("!$('panel-body').innerText.toUpperCase().includes('SUGGESTED FILES') && "
                "$('panel-body').innerText.includes('web/search/')", what="the files accepted")
        self.assertIn("files web/search/", self.agent.call("hub_task", action="view", show="all"))
        self.m.owner.post("/api/task", {"op": "status", "task": mid, "status": "done"})  # nothing left that needs you
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
        self.agent.call("hub_task", action="take", id=tid)
        c.js("Object.defineProperty(document, 'hidden', {value: true, configurable: true})")  # the page goes to the back
        self.agent.call("hub_task", action="update", id=tid, status="blocked", note="Blue or green?")
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
        self.agent.call("hub_task", action="update", id=tid, status="in_progress", note="Green it is")
        c.until("$('needs-you').hidden || !$('needs-list').innerText.includes('Blue or green?')", what="the item to go")
        # A message to you is unread until the page is in front, then read.
        self.agent.call("hub_send", to="Owner", text="Done with the button")
        c.until("$('log').innerText.includes('Done with the button')", what="the message on the page")
        c.js("$('log').scrollTop = $('log').scrollHeight; markRead()")
        c.until("!state.needs.some(n => n.kind === 'message')", what="read while in front")
    def test_allow_or_deny_a_permission_prompt(self):
        c = self.chrome
        c.until("!!%s" % self.card(), what="the agent")

        def ask(command):  # the agent's tool waits on its prompt; its hook asks the page
            out = {}
            t = threading.Thread(target=lambda: out.update(text=self.agent.hook(
                "permission", extra={"tool_name": "Bash", "tool_input": {"command": command}})))
            t.start()
            c.until("$('needs-list').innerText.includes(%s)" % json.dumps(command), what="the prompt in Needs you")
            return t, out

        t, out = ask("npm publish --tag next")
        item = "[...document.querySelectorAll('#needs-list .need.ask')].find(e => e.innerText.includes('npm publish'))"
        self.assertIn("ASKS PERMISSION", c.js(item + ".querySelector('.k').innerText").upper())
        self.assertIn(self.agent.name, c.js(item + ".textContent"))
        self.assertEqual(c.js("$('needs-list').firstElementChild.classList.contains('ask')"), True)  # first of all
        c.js(item + ".querySelector('button:not(.ghost)').click()")
        t.join(30)
        self.assertEqual(json.loads(out["text"])["hookSpecificOutput"]["decision"], {"behavior": "allow"})
        c.until("!$('needs-list').innerText.includes('npm publish')", what="the prompt to go")

        t, out = ask("<img src=x onerror=window.pwned=1>")
        c.js("[...document.querySelectorAll('#needs-list .need.ask button')].find(b => b.textContent === 'Deny').click()")
        t.join(30)
        self.assertEqual(json.loads(out["text"])["hookSpecificOutput"]["decision"]["behavior"], "deny")
        self.assertIsNone(c.js("window.pwned"))
        self.assertEqual(self.shown_though_hidden(), [])
        self.assertEqual(c.errors, [])

    def test_notifications_subscribe_to_push_and_a_push_shows_one(self):
        c = self.chrome
        self.addCleanup(c.js, "try { localStorage.removeItem(notifyKey()); } catch (e) {} showBell()")
        # The page subscribes when notifications are turned on (the push service stubbed: no Google here).
        c.js("""window.pushed = []; window.realPost = postJSON;
                postJSON = (path, body) => { window.pushed.push([path, body]); return window.realPost(path, body); };
                Object.defineProperty(navigator.serviceWorker, 'ready', {configurable: true, value: Promise.resolve({pushManager: {
                  getSubscription: async () => null,
                  subscribe: async (o) => { window.serverKey = o.applicationServerKey.length;
                                            return {endpoint: 'https://fcm.googleapis.com/fcm/send/e2e'}; }}})});
                window.Notification = class { static requestPermission() { return Promise.resolve('granted'); } };
                Notification.permission = 'granted';""")
        self.addCleanup(c.js, "postJSON = window.realPost; delete navigator.serviceWorker.ready; delete window.Notification")
        c.js("document.querySelector('[data-notify=mentions]').click()")
        c.until("window.pushed.some(p => p[1].op === 'mode')", what="the subscription sent")
        self.assertEqual(c.js("window.serverKey"), 65)  # this machine's VAPID key, uncompressed P-256
        self.assertEqual(c.js("window.pushed.find(p => p[1].op === 'mode')[1]"),
                         {"op": "mode", "endpoint": "https://fcm.googleapis.com/fcm/send/e2e", "mode": "mentions"})
        # A push reaches the service worker (as from the push service): it shows a notification.
        c.call("Browser.grantPermissions", permissions=["notifications"], origin=self.m.url)
        c.call("ServiceWorker.enable")
        reg = eventually(lambda: c.js("0") or next((r["registrationId"] for e in c.events
                                                    if e["method"] == "ServiceWorker.workerRegistrationUpdated"
                                                    for r in e["params"]["registrations"]
                                                    if r["scopeURL"].startswith(self.m.url)), None), what="the service worker")
        # Its fetch of what the push is about is signed in, as the page is (its cookie goes with it).
        worker = next(v["targetId"] for e in c.events if e["method"] == "ServiceWorker.workerVersionUpdated"
                      for v in e["params"]["versions"] if v["registrationId"] == reg and v.get("targetId"))
        session = c.call("Target.attachToTarget", targetId=worker, flatten=True)["sessionId"]
        c.n += 1
        c.ws.send(json.dumps({"id": c.n, "sessionId": session, "method": "Runtime.evaluate", "params": {
            "expression": "fetch('/api/push', {method: 'POST', headers: {'Content-Type': 'application/json'},"
                          " body: JSON.stringify({op: 'notices', endpoint: 'x'})}).then(r => r.status)",
            "awaitPromise": True, "returnByValue": True}}))
        while True:
            msg = json.loads(c.ws.recv())
            if msg.get("id") == c.n:
                break
        self.assertEqual(msg["result"]["result"]["value"], 200)
        c.call("Target.detachFromTarget", sessionId=session)
        shown = ("navigator.serviceWorker.getRegistration().then(r => r.getNotifications())"
                 ".then(n => n.some(x => x.body === 'Something new needs you.'))")

        def pushed():  # a push that reaches a worker still starting is dropped: send it again until one shows
            c.call("ServiceWorker.deliverPushMessage", origin=self.m.url, registrationId=reg, data="")
            return any(c.js(shown) or time.sleep(0.2) for _ in range(10))
        c.js("navigator.serviceWorker.ready.then(() => true)")
        eventually(pushed, timeout=30, what="the notification")
        c.call("ServiceWorker.disable")
        self.assertEqual(c.errors, [])

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
        c.js("$('l-tool').value = 'claude'; $('l-tool').dispatchEvent(new Event('change'))")
        self.assertEqual(c.js("[...$('l-advisor').options].map(o => o.value)"), ["", "fable", "opus", "sonnet"])
        self.assertTrue(c.js("$('l-advisor-note').hidden"))
        c.js("$('l-advisor').value = 'opus'; $('l-advisor').dispatchEvent(new Event('change'))")
        self.assertFalse(c.js("$('l-advisor-note').hidden"))  # what Claude Code's own advisor is
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

    def test_what_the_ui_test_found(self):
        c = self.chrome
        # A search that matches nothing says so, and the note goes with the search.
        c.js("$('search').value = 'zzz-nothing'; $('search').dispatchEvent(new Event('input'))")
        self.assertFalse(c.js("$('search-none').hidden"))
        self.agent.call("hub_send", to="Owner", text="zzz-nothing after all")  # a match arriving during the search
        c.until("$('search-none').hidden && $('log').innerText.includes('zzz-nothing after all')", what="the match shown")
        c.js("$('search').value = ''; $('search').dispatchEvent(new Event('input'))")
        self.assertTrue(c.js("$('search-none').hidden"))
        # New agent's templates read as their role's name; no install notes for tools that are not here.
        c.js("openLaunch()")
        c.until("$('l-template').options.length > 1", what="the templates")
        self.assertIn("Lead role", c.js("[...$('l-template').options].map(o => o.text)"))
        self.assertNotIn("Needs", c.js("$('launch-note').textContent"))  # no install notes for tools not here
        c.click("#l-cancel")
        # An agent silent for long is not shown working, whatever its hooks said last.
        self.assertEqual(c.js("doing({activity: 'working', seen: state.now - 1200})"), "Not heard from lately")
        self.assertEqual(c.js("doing({activity: 'working', seen: state.now})"), "Working")
        # A message to you in Needs you: to the message, marked, with a reply to its sender started.
        c.js("Object.defineProperty(document, 'hidden', {value: true, configurable: true})")  # unread while away
        self.addCleanup(c.js, "delete document.hidden")
        self.agent.call("hub_send", to="Owner", text="Can you look at the build?")
        c.until("$('needs-list').innerText.includes('Can you look at the build?')", what="the message in Needs you")
        c.js("""delete document.hidden; $('text').value = '';
                [...document.querySelectorAll('#needs-list .need')].find(b => b.innerText.includes('look at the build')).click()""")
        self.assertEqual(c.js("$('text').value"), "@%s " % self.agent.name)
        self.assertTrue(c.js("[...document.querySelectorAll('#log .msg.flash')].some(m => m.innerText.includes('look at the build'))"))
        self.assertTrue(c.js("$('agent-pane').hidden"))
        c.js("$('text').value = ''")
        self.assertEqual(c.errors, [])

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

    def test_the_analytics_view(self):
        # Roadmap X2: the crew at a glance, live through the chat's own poll; light and dark, desktop and phone.
        c = self.chrome
        try:
            c.click("#view-analytics")
            c.until("window.crewAnalytics && crewAnalytics.stats().report", what="the analytics view and its numbers")
            self.assertTrue(c.js("$('log').hidden && !$('analytics').hidden"))
            self.assertGreaterEqual(c.js("$('ax-numbers').children.length"), 5)
            for panel in ("ax-river", "ax-machines", "ax-lanes", "ax-tokens", "ax-heat"):
                self.assertTrue(c.js("$('%s').children.length > 0" % panel), panel)
            machines = c.js("$('ax-machines').innerText")
            self.assertIn("not linked to other machines", machines)
            self.assertNotIn("this machine only", machines)  # it read "this machine · this machine only"
            self.assertEqual(c.js("crewAnalytics.stats().nodes"), c.js("state.agents.length") + 1)  # and you
            self.assertIn(self.agent.whoami(), c.js("$('ax-canvas').getAttribute('aria-label')"))
            # Live: a message flies along its path, from the poll the page makes anyway.
            sent = c.js("crewAnalytics.stats().sent")
            self.agent.call("hub_send", to="all", text="For the analytics view")
            c.until("crewAnalytics.stats().sent > %d" % sent, what="the message to fly")
            self.assertTrue(c.js("crewAnalytics.stats().animating"))
            # 60 frames a second with 20 agents and messages in flight: a frame draws in well under 16 ms.
            self.assertLess(c.js("crewAnalytics.bench(120, 20)"), 16)
            # Replay: back to the start of the day, then live again.
            c.js("crewAnalytics.replay(Number($('ax-scrub').min))")
            self.assertTrue(c.js("$('ax-when').textContent.startsWith('Replay') && !$('ax-live').hidden"))
            c.click("#ax-live")
            self.assertEqual(c.js("[$('ax-when').textContent, crewAnalytics.stats().replay]"), ["Live", None])
            # Presenting: one panel at a time, under the numbers; Esc ends it.
            c.click("#ax-present")
            self.assertEqual(c.js("document.querySelectorAll('#analytics .ax-current').length"), 1)
            c.js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}))")
            c.until("!document.body.classList.contains('ax-present')", what="the end of presenting")
            # Reduced motion: a message lights its path for a moment; nothing flies.
            c.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-reduced-motion", "value": "reduce"}])
            c.until("crewAnalytics.stats().reduced", what="reduced motion")
            sent = c.js("crewAnalytics.stats().sent")
            self.agent.call("hub_send", to="all", text="Quietly, please")
            c.until("crewAnalytics.stats().sent > %d" % sent, what="the quiet message")
            self.assertEqual(c.js("[crewAnalytics.stats().particles, crewAnalytics.stats().flashes > 0]"), [0, True])
            # Dark, on a phone: the page's own colours, and nothing wider than the screen.
            c.size(375, 812, mobile=True, dark=True)
            c.until("innerWidth === 375 && crewAnalytics.stats().shown", what="a phone")
            self.assertLessEqual(c.js("document.documentElement.scrollWidth"), 375)
            self.assertLessEqual(c.js("$('analytics').scrollWidth"), 375)
            self.assertNotEqual(c.js("getComputedStyle(document.querySelector('.ax-card')).backgroundColor"),
                                "rgb(255, 255, 255)")
            self.assertEqual(c.errors, [])
        finally:
            c.js("sessionStorage.removeItem('crewchat-view')")
            c.size(1280, 900)
            c.open(self.m.url + "/")
            c.until("!!document.getElementById('log')")

    def test_the_code_by_who_holds_it(self):
        # Roadmap C8: up to 500 files, each shows; beyond that, folders, opened with a click.
        c = self.chrome
        try:
            c.click("#view-analytics")
            c.until("window.crewAnalytics && crewAnalytics.stats().report", what="the analytics view")
            graph = lambda n: json.dumps({"on": True, "commit": "abc", "more": 0, "links": [[0, 1, 1], [1, n - 1, 2]],  # noqa: E731
                                          "files": ["mod%d/sub/f%d.py" % (i % 9, i) for i in range(n)]})
            self.assertEqual(c.js("crewAnalytics.code(%s)" % graph(480))["files"], 480)
            shown = c.js("crewAnalytics.code(%s)" % graph(1500))
            self.assertEqual((shown["files"], shown["folders"]), (0, 9))
            x, y = shown["first"]
            c.js("(() => { const cv = $('ax-code').querySelector('canvas'), b = cv.getBoundingClientRect();"
                 " cv.dispatchEvent(new MouseEvent('click', {clientX: b.left + %f, clientY: b.top + %f})); })()" % (x, y))
            opened = c.js("crewAnalytics.code()")
            self.assertEqual((opened["folders"], opened["files"] > 100), (8, True))  # that folder's files show
            self.assertIn("1500 files", c.js("$('ax-code').innerText"))
            self.assertEqual(c.errors, [])
        finally:
            c.js("sessionStorage.removeItem('crewchat-view')")
            c.open(self.m.url + "/")
            c.until("!!document.getElementById('log')")

    def test_the_analytics_sample_data(self):
        # Roadmap X3: the sample the public demo shows, in the app too; it never mixes with the chat.
        c = self.chrome
        try:
            c.click("#view-analytics")
            c.until("window.crewAnalytics && crewAnalytics.stats().report", what="the analytics view")
            crew = c.js("crewAnalytics.stats().nodes")
            name = self.agent.whoami()
            c.click("#ax-sample")
            self.assertEqual(c.js("[crewAnalytics.stats().demo, crewAnalytics.stats().nodes, $('ax-sample').ariaPressed]"),
                             [True, 9, "true"])  # its crew of eight, and you
            self.assertIn("Sample data: a made-up crew", c.js("$('ax-demo-note').textContent"))
            self.assertFalse(c.js("$('ax-demo-note').hidden"))
            sent = c.js("crewAnalytics.stats().sent")
            c.until("crewAnalytics.stats().sent > %d" % sent, what="the sample crew to talk")
            sample = c.js("$('ax-canvas').getAttribute('aria-label')")
            self.agent.call("hub_send", to="all", text="Not one for the sample")
            c.until("$('log').textContent.includes('Not one for the sample')", what="the message in the chat")
            # The chat stays out of it (its agent may share a name with one of the sample's: the crew is the same).
            self.assertEqual((c.js("$('ax-canvas').getAttribute('aria-label')"), c.js("crewAnalytics.stats().nodes")), (sample, 9))
            c.click("#ax-sample")
            c.until("!crewAnalytics.stats().demo && crewAnalytics.stats().nodes === %d" % crew, what="the chat's own crew")
            self.assertIn(name, c.js("$('ax-canvas').getAttribute('aria-label')"))
            self.assertNotEqual(c.js("$('ax-canvas').getAttribute('aria-label')"), sample)
            self.assertTrue(c.js("$('ax-demo-note').hidden"))
            c.click("#ax-sample")  # left on, then the view closed: off again when it reopens
            c.click("#view-analytics")
            c.click("#view-analytics")
            c.until("crewAnalytics.stats().shown && !crewAnalytics.stats().demo", what="the view without the sample")
            self.assertEqual(c.errors, [])
        finally:
            c.js("sessionStorage.removeItem('crewchat-view')")
            c.open(self.m.url + "/")
            c.until("!!document.getElementById('log')")

    def test_liquid_glass_and_solid_when_asked(self):
        # The controls are glass (see-through and blurred) over solid content; the system's "reduce transparency"
        # or "increase contrast" makes them solid again. Nothing starts hidden under the floating header.
        c = self.chrome
        glass = ["#space-head", "#form .pillbox"]
        look = """(sel) => { const s = getComputedStyle(document.querySelector(sel));
            const a = s.backgroundColor.match(/rgba\\([^)]*,\\s*([\\d.]+)\\)/);
            return [a ? parseFloat(a[1]) : 1, (s.backdropFilter || s.webkitBackdropFilter || 'none')]; }"""
        try:
            for sel in glass:
                alpha, blur = c.js("(%s)(%s)" % (look, json.dumps(sel)))
                self.assertLess(alpha, 0.95, sel)
                self.assertIn("blur", blur, sel)
            self.agent.call("hub_send", to="all", text="A message to read, on solid ground")
            c.until("!!document.querySelector('#log .msg:not(.own) .body')", what="a message")
            alpha, blur = c.js("(%s)('#log .msg:not(.own) .body')" % look)
            self.assertEqual((alpha, blur), (1, "none"))  # what is read stays solid
            # Nothing under the header: the thread at its top, the list, the board, the analytics.
            head = "$('space-head').getBoundingClientRect().bottom"
            c.js("$('log').scrollTop = 0")
            self.assertGreaterEqual(c.js("$('log').firstElementChild.getBoundingClientRect().top"), c.js(head) - 1)
            for view, first in (("tasks", "$('tasks').firstElementChild"), ("analytics", "$('analytics').firstElementChild")):
                c.js("showView(%s)" % json.dumps(view))
                for mode in (("as-list", "as-board") if view == "tasks" else ("",)):
                    if mode:
                        c.click("#" + mode)
                    c.until("!!%s && %s.getBoundingClientRect().height > 0" % (first, first), what=view)
                    self.assertGreaterEqual(c.js("%s.getBoundingClientRect().top" % first), c.js(head) - 1, (view, mode))
            c.js("showView('chat')")
            # Asked for less transparency, or more contrast: solid.
            for feature, value in (("prefers-reduced-transparency", "reduce"), ("prefers-contrast", "more")):
                c.call("Emulation.setEmulatedMedia", features=[{"name": feature, "value": value}])
                if not c.js("matchMedia('(%s: %s)').matches" % (feature, value)):
                    continue  # this Chrome cannot pretend so
                for sel in glass:
                    self.assertEqual(c.js("(%s)(%s)" % (look, json.dumps(sel))), [1, "none"], (feature, sel))
            self.assertEqual(c.errors, [])
        finally:
            c.js("showView('chat')")
            c.size(1280, 900)

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
            # The sign-in page fits a phone too (its card once ran off the right edge).
            c.call("Network.clearBrowserCookies")
            try:
                c.open(self.m.url + "/login")
                c.until("!!document.querySelector('main')", what="the sign-in page")
                self.assertLessEqual(c.js("document.querySelector('main').getBoundingClientRect().right"), 375)
                self.assertLessEqual(c.js("document.documentElement.scrollWidth"), 375)
            finally:
                code = self.m.cli("ui", "--print").split(": ")[1].split()[0]
                c.open(self.m.url + "/login?code=" + code.replace("-", ""))
                c.until("location.pathname === '/' && !!%s" % self.card(), what="signed in again")
            c.click("#rail-open")
            c.until("$('rail').getBoundingClientRect().left >= 0", what="the drawer to open")
            c.js(self.card() + ".click()")
            c.until("!$('agent-pane').hidden", what="the agent's settings")
            self.assertTrue(c.js("document.body.classList.contains('rail-open')"))  # in the drawer, under the crew
            self.assertLessEqual(c.js("$('agent-pane').getBoundingClientRect().right"), 375)
            # The task list on a phone: every action on screen, nothing to scroll sideways for.
            mid = self.m.owner.task("all", "Add the dark colour scheme to app/src/main/ui/theme/Color.kt")  # long words
            self.agent.call("hub_task", action="take", id=mid)
            c.js("showView('tasks'); $('as-list').click()")
            c.until("state.sheet.some(r => r.id === %s && r.status === 'doing') && "
                    "$('sheet').querySelectorAll('tbody button').length > 0" % json.dumps(mid), what="the task list")
            right = c.js("Math.max(...[...$('sheet').querySelectorAll('button, select')].map(b => b.getBoundingClientRect().right))")
            self.assertLessEqual(right, 375)
            c.js("$('as-board').click(); showView('chat')")
            c.click("#view-commands")
            c.until("!$('panel').hidden", what="the panel")
            self.assertEqual(c.js("[$('panel').getBoundingClientRect().width, document.body.classList.contains('rail-open')]"),
                             [375, False])  # a full-screen sheet; the drawer closed
            self.assertEqual(c.errors, [])
        finally:
            c.size(1280, 900)
            c.open(self.m.url + "/")
            c.until("!!document.getElementById('log')")


@unittest.skipUnless(websocket and os.path.exists(CHROME), "needs Google Chrome and websocket-client")
class PublicDemo(unittest.TestCase):
    """Roadmap X3: crewchat.dev/demo, the analytics view on its sample data with no crewchat server, served with the
    headers firebase.json gives the site (a policy that allows no inline script or style)."""

    def test_the_demo_runs_in_the_browser_under_the_sites_policy(self):
        import functools
        import http.server
        sys.path.insert(0, str(H.ROOT / "site"))
        import build
        site = build.build(H.TMP / "site")
        policy = json.loads((H.ROOT / "firebase.json").read_text())["hosting"]["headers"][0]["headers"]

        class Site(http.server.SimpleHTTPRequestHandler):
            def end_headers(self):
                for header in policy:
                    self.send_header(header["key"], header["value"])
                super().end_headers()

            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Site, directory=str(site)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        c = Chrome()
        self.addCleanup(c.close)
        c.size(1280, 900)
        # Anything the policy refuses is noted before the page's own scripts run.
        c.call("Page.addScriptToEvaluateOnNewDocument", source="window.refused = []; document.addEventListener("
               "'securitypolicyviolation', (e) => window.refused.push(e.violatedDirective + ' ' + e.blockedURI));")
        c.open("http://127.0.0.1:%d/demo/" % server.server_address[1])
        c.until("window.crewAnalytics && crewAnalytics.stats().demo && crewAnalytics.stats().report", what="the demo")
        self.assertEqual(c.js("crewAnalytics.stats().nodes"), 9)
        self.assertIn("A demo: a made-up crew", c.js("$('ax-demo-note').textContent"))  # it says what it is
        self.assertTrue(c.js("$('ax-sample').hidden"))  # nothing to switch to
        self.assertGreater(c.js("$('ax-numbers').children.length"), 4)
        self.assertGreater(c.js("document.querySelectorAll('#ax-heat .ax-tile').length"), 5)
        code = c.js("crewAnalytics.code()")
        self.assertEqual((code["files"], code["folders"], code["held"] > 0), (28, 0, True))  # coloured by holder
        self.assertTrue(c.js("[...document.querySelectorAll('#analytics .avatar img')].every((i) => i.complete && i.naturalWidth)"))
        sent = c.js("crewAnalytics.stats().sent")
        c.until("crewAnalytics.stats().sent > %d" % sent, what="the sample crew to talk")
        self.assertEqual(c.js("getComputedStyle(document.querySelector('.ax-card')).borderRadius"), "18px")  # styled
        self.assertEqual(c.js("window.refused"), [])
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
