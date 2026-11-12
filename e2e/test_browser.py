"""The chat page in Google Chrome (headless): what the owner does with it.

    pip install websocket-client      (skipped without it, or without Chrome)

The "Add an agent" form is only opened and closed: starting it would open a real terminal.
"""
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
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
        return self.chrome.js("[...document.querySelectorAll('[hidden]')].filter(e => getComputedStyle(e).display !== 'none'"
                              " && getComputedStyle(e).visibility !== 'hidden').map(e => e.id || e.className)")


@unittest.skipUnless(websocket and os.path.exists(CHROME), "needs Google Chrome and websocket-client")
class NewPage(Signed):
    """The chat page from W16: the sidebar (projects, places, the crew), the space, the side panel."""

    def card(self):
        return "document.querySelector('#agents .agent[data-agent=%s]')" % json.dumps(self.agent.name)

    def tearDown(self):
        self.chrome.js("closePanel(); if (state.agentPane) closeAgent(); showView('chat'); document.body.classList.remove('rail-open');"
                       " $('tab-add').open = false")

    def test_the_shell(self):
        c = self.chrome
        c.until("!!%s" % self.card(), what="the agent in the rail")
        self.assertEqual(c.js("$('project').textContent"), "Demo")
        self.assertEqual(c.js("$('switch-name').textContent"), "Demo")  # the switcher at the sidebar's top
        c.js("$('tab-add').open = true")
        tabs = c.js("[...document.querySelectorAll('#tabs .tab')].map(t => [t.innerText, t.getAttribute('aria-current')])")
        c.js("$('tab-add').open = false")
        self.assertIn(["Demo", "page"], tabs)  # (and one for each other project, not current)
        self.assertEqual([t for t in tabs if t[1] == "page"], [["Demo", "page"]])
        # Each project wears its icon, not a letter (11 Nov): the same one in the switcher, the tab and the header.
        icons = c.js("[$('switch-avatar'), $('space-avatar'), document.querySelector('#tabs .tab[aria-current] .pav')]"
                     ".map(e => [e.dataset.icon, !!e.querySelector('svg'), e.textContent])")
        self.assertEqual(len({i[0] for i in icons}), 1, icons)
        self.assertTrue(all(i[1] and i[2] == "" for i in icons), icons)
        self.assertIn("1 agent", c.js("$('space-meta').textContent"))
        self.assertNotIn("machine", c.js("$('space-meta').textContent"))  # one machine: not said
        self.assertTrue(c.js("$('panel').hidden"))
        self.assertEqual(self.shown_though_hidden(), [])
        self.assertEqual(c.errors, [])

    def test_the_sidebars_places(self):
        # W16: the sidebar has the project's places, the crew, and at its foot the settings and You; nothing behind ⋯.
        c = self.chrome
        c.until("!!%s" % self.card(), what="the agent in the sidebar")
        self.assertEqual(c.js("[...document.querySelectorAll('#rail .places .place')].map(b => b.firstChild.textContent.trim())"),
                         ["Chat", "Needs you", "Tasks", "Activity"])
        self.assertEqual(c.js("[...document.querySelectorAll('#rail .rail-foot .place')].map(b => b.textContent.trim().split('\\n')[0])"
                              ".map(t => t.replace(/^\\W+/, '').split(' notifications')[0].trim())"),
                         ["Project settings", "Machines & devices", "You"])
        self.assertIsNone(c.js("document.getElementById('space-menu')"))  # the ⋯ menu is gone
        self.assertEqual(c.js("$('agents-count').textContent"), "1")
        for view, shown in (("needs", "needs-you"), ("tasks", "tasks"), ("analytics", "analytics"), ("chat", "log")):
            c.click("#view-" + view)
            c.until("!$(%s).hidden" % json.dumps(shown), what="the %s place" % view)
            self.assertEqual(c.js("[...document.querySelectorAll('#rail .place[aria-current=page]')].map(b => b.id)"),
                             ["view-" + view])
            self.assertEqual(c.js("['log', 'needs-you', 'tasks', 'analytics'].filter(id => !$(id).hidden)"), [shown])
        c.click("#view-needs")
        c.until("!$('needs-none').hidden", what="nothing needs you, said so")
        c.click("#view-chat")
        c.click('#rail [data-panel="project"]')
        c.until("!$('settings').hidden && $('settings-title').textContent === 'General'"
                " && document.querySelector('#settings-nav [aria-current=page]').dataset.settings === 'project'", what="Project settings")
        c.js("closePanel()")
        self.assertEqual(c.errors, [])

    def test_the_rail_buttons_stay_on_one_line(self):
        c = self.chrome
        was = c.js("$('tasks-count').textContent")
        c.js("$('tasks-count').textContent = '(12)'")  # a busy board's count
        try:
            # Each place's text, whether it takes more than one line's height, and whether it is cut off.
            shown = c.js("[...document.querySelectorAll('#rail .place')].filter(b => b.offsetParent).map(b => "
                         "[b.innerText, b.getBoundingClientRect().height > 40 ? 2 : 1, b.scrollWidth > b.clientWidth])")
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

    @unittest.skipIf(os.name == "nt", "Claude Code's inbox is a named pipe there: the Stop hook's wait it is")
    def test_a_message_wakes_an_idle_agent_and_says_so_under_it(self):
        # W2: the owner writes to a Claude Code agent whose turn is over and whose hook no longer listens. Its session
        # is woken through its inbox, its woken turn gets the message, and the page says "woke the agent" under it.
        c = self.chrome
        agent = self.m.agent()
        agent.link()
        self.addCleanup(self.m.cli, "agent", "remove", agent.name)  # the page's other tests count one agent
        inbox = H.Inbox(agent.session)
        self.addCleanup(inbox.close)
        self.assertEqual(agent.stop_hook(env=inbox.env), "")  # done, not listening
        c.until("!!document.querySelector('#agents .agent[data-agent=%s]')" % json.dumps(agent.name), what="the agent")
        c.type_into("#text", "@%s are you awake?" % agent.name)
        c.js("$('text').dispatchEvent(new Event('input'))")
        c.click("#send")
        eventually(lambda: inbox.taken, what="the wake")
        self.assertEqual(len(inbox.taken), 1)
        self.assertEqual(inbox.taken[0]["type"], "user")
        self.assertNotIn("awake", json.dumps(inbox.taken))  # the frame carries nothing anyone wrote
        c.until("[...document.querySelectorAll('#log .msg.own')].some(m => m.innerText.includes('are you awake?')"
                " && m.querySelector('.receipt').textContent.includes('woke the agent'))", what="woke the agent")
        turn = agent.hook("prompt", extra={"prompt": inbox.taken[0]["message"]["content"]}, env=inbox.env)
        self.assertIn("are you awake?", turn)  # its woken turn has the message
        self.assertEqual(c.errors, [])

    def test_the_agent_panel_opens_sets_a_role_and_closes(self):
        c = self.chrome
        c.until("!!%s" % self.card(), what="the agent")
        c.js(self.card() + ".click()")
        c.until("!$('agent-pane').hidden && $('agent-pane-title').textContent === %s" % json.dumps(self.agent.name))
        self.assertTrue(c.js("$('panel').hidden"))  # the agent alone in the side panel, beside the chat (W16)
        self.assertTrue(c.js("$('side').contains($('agent-pane')) && $('side').getBoundingClientRect().left >= $('space').getBoundingClientRect().right - 1"))
        self.assertFalse(c.js("$('log').hidden"))
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

    def test_the_settings_page_its_sections_search_and_links(self):
        # W16 (D1 to D3): one Settings page, from You or ⌘,: crewchat's sections, then the project's, each with its
        # cards; a search box finds a setting by its words, and every section and card has a link.
        c = self.chrome
        self.addCleanup(c.js, "closePanel()")
        c.click("#bell")
        c.until("!$('settings').hidden && $('settings-title').textContent === 'General'", what="Settings from You")
        nav = "[...document.querySelectorAll('#%s [data-settings]')].map(b => b.firstChild.textContent)"
        self.assertEqual(c.js(nav % "settings-app"), ["General", "Appearance", "Notifications", "Agents & tools", "Machines & devices",
                                                       "Phone & remote access", "Accounts", "Privacy & data", "Advanced", "About"])
        self.assertEqual(c.js(nav % "settings-projects"), ["General", "Crew & roles", "Safety", "Integrations", "Routines",
                                                            "Folders & files"])
        self.assertIn("Demo", c.js("$('settings-projects').textContent"))
        shown = "[...document.querySelectorAll('#settings-main .card[id]')].filter(x => x.offsetParent).map(x => x.id)"
        for kind, cards in (("safety", ["join-card", "approvals-card", "edits-card"]),
                            ("integrations", ["jira-card", "mcp-card", "gchat-card"]), ("routines", ["routines-card"]),
                            ("folders", ["folders-card"]), ("project", ["link-card", "setup"]),
                            ("notifications", ["notify-card"]), ("agents", ["new-agent-card", "crew-card"]),
                            ("accounts", ["accounts-card"]), ("privacy", ["privacy-card"])):
            c.js("document.querySelector('#settings-nav [data-settings=%s]').click()" % kind)
            c.until("location.hash === '#settings/%s'" % kind, what=kind)
            self.assertEqual(c.js(shown), cards, kind)
        c.js("openPanel('accounts')")  # Accounts says whether crewchat is signed in to Atlassian on this machine
        c.until("$('accounts-jira').textContent === 'Not signed in.'", what="the Atlassian sign-in's state in Accounts")
        # Machines & devices: three tabs.
        c.js("openPanel('machines')")
        c.until("!!document.querySelector('#settings-body .m-me')", what="Machines")
        self.assertEqual(c.js("[...$('settings-tabs').children].map(b => b.textContent)"),
                         ["Machines", "Phones & emulators", "Agents' browser"])
        c.js("[...$('settings-tabs').children][1].click()")
        c.until("location.hash === '#settings/devices' && $('settings-tabs').children[1].getAttribute('aria-pressed') === 'true'",
                what="the Phones tab")
        # Find a setting.
        c.type_into("#settings-find", "risky commands")
        c.js("findSetting()")
        found = c.js("[...$('settings-results').querySelectorAll('.hit')].map(b => b.innerText.split('\\n'))")
        self.assertEqual(found[0], ["Approvals", "Safety"], found)
        self.assertTrue(c.js("$('settings-body').hidden"))
        c.js("$('settings-results').querySelector('.hit').click()")
        c.until("location.hash === '#settings/safety/approvals-card' && $('approvals-card').classList.contains('flash')",
                what="the card found, linked and marked")
        self.assertEqual(c.js("$('settings-find').value"), "")
        c.type_into("#settings-find", "google")
        c.js("findSetting()")
        self.assertIn("Google Chat", c.js("$('settings-results').innerText"))
        self.assertTrue(c.js("document.querySelector('#settings-nav [data-settings=privacy]').classList.contains('dim')"))
        c.type_into("#settings-find", "zebra crossing")
        c.js("findSetting()")
        self.assertIn("No setting matches", c.js("$('settings-results').innerText"))
        c.js("$('settings-find').value = ''; findSetting()")
        # ⌘, opens Settings; Esc closes it.
        c.js("closePanel()")
        c.js("document.dispatchEvent(new KeyboardEvent('keydown', {key: ',', metaKey: true}))")
        c.until("!$('settings').hidden && $('settings-title').textContent === 'General'", what="⌘, to open Settings")
        c.js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}))")
        c.until("$('settings').hidden", what="Esc to close Settings")
        self.assertEqual(c.errors, [])

    def test_command_k_finds_places_settings_actions_agents_and_messages(self):
        # W16 (A3): ⌘K (or Ctrl+K, or the sidebar's box) finds everything; ↑ ↓ move, ↵ opens, Esc closes.
        c = self.chrome
        self.addCleanup(c.js, "$('jump').close(); closePanel(); if (state.agentPane) closeAgent(); showView('chat')")
        c.until("!!%s" % self.card(), what="the agent")
        heads = "[...document.querySelectorAll('#jump-list .menu-head')].map(h => h.textContent)"
        rows = "[...document.querySelectorAll('#jump-list .jump-row')].map(r => r.querySelector('.what').textContent)"
        def search(q):
            c.js("$('jump-q').value = %s; $('jump-q').dispatchEvent(new Event('input'))" % json.dumps(q))
        c.js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'k', metaKey: true}))")
        c.until("$('jump').open && document.activeElement === $('jump-q')", what="⌘K to open")
        self.assertEqual(c.js(heads)[:3], ["Places", "Actions", "Agents"])
        self.assertIn("Turn on notifications", c.js(rows))
        c.js("$('jump').close()")
        c.js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'k', ctrlKey: true}))")
        c.until("$('jump').open", what="Ctrl+K too")
        # A setting: the card, found by its words; ↵ opens it, linked and marked.
        search("ask before")
        self.assertEqual(c.js("jump.rows[0].title"), "Approvals")
        c.js("$('jump-q').dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter'}))")
        c.until("!$('jump').open && !$('settings').hidden && location.hash === '#settings/safety/approvals-card'",
                what="Approvals opened from ⌘K")
        c.js("closePanel()")
        # An action, with the arrow keys.
        c.click("#jump-open")
        c.until("$('jump').open", what="the sidebar's box to open it")
        search("project")
        names = c.js(rows)
        c.js("for (let i = 0; i < %d; i++) $('jump-q').dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowDown'}))"
             % names.index("New project"))
        self.assertEqual(c.js("document.querySelector('#jump-list [aria-selected=true] .what').textContent"), "New project")
        self.assertEqual(c.js("$('jump-q').getAttribute('aria-activedescendant')"),
                         c.js("document.querySelector('#jump-list [aria-selected=true]').id"))
        c.js("$('jump-q').dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter'}))")
        c.until("$('project-dialog').open", what="New project from ⌘K")
        c.click("#p-cancel")
        # An agent opens beside the chat.
        c.js("openJump()")
        search(self.agent.name)
        self.assertIn("Agents", c.js(heads))
        c.js("[...document.querySelectorAll('#jump-list .jump-row')].find(r => r.querySelector('.what').textContent === %s).click()"
             % json.dumps(self.agent.name))
        c.until("!$('agent-pane').hidden && $('agent-pane-title').textContent === %s" % json.dumps(self.agent.name),
                what="the agent from ⌘K")
        c.js("closeAgent()")
        # Messages: this page's at once, and every project's from the server.
        self.agent.call("hub_send", to="all", text="The zebra crossing is painted")
        c.until("$('log').innerText.includes('zebra crossing')", what="the message")
        c.js("openJump()")
        search("zebra crossing")
        self.assertIn("Messages", c.js(heads))
        self.assertEqual(c.js("document.querySelectorAll('#jump-list .jump-row mark').length"), 2)
        c.until("jump.found && jump.found.q === 'zebra crossing' && jump.found.hits.length > 0", what="the server's search")
        c.js("[...document.querySelectorAll('#jump-list .jump-row')].find(r => r.textContent.includes('zebra')).click()")
        c.until("[...document.querySelectorAll('#log .msg.flash')].some(m => m.innerText.includes('zebra'))", what="the message shown")
        self.assertEqual(c.errors, [])

    def test_every_theme_keeps_the_same_page_and_changes_only_its_look(self):
        # W16 (D2): a theme is one set of variables. Switching keeps every element and where it sits; the colours,
        # fonts and corners change; messages stay readable; the choice is this browser's, and System follows it.
        c = self.chrome
        self.addCleanup(c.js, "['theme', 'size', 'density', 'heads', 'accent'].forEach(k => look(k, '')); crewTheme(); closePanel()")
        c.until("!!%s" % self.card(), what="the agent")
        self.agent.call("hub_send", to="Owner", text="A message to read in every theme")
        c.until("$('log').innerText.includes('in every theme')", what="the message")
        self.assertEqual(c.js("[document.documentElement.dataset.theme, document.documentElement.dataset.scheme]"),
                         ["paper", "light"])  # System, on a light computer
        ids = c.js("THEMES.map(t => t[0])")
        self.assertEqual(ids, ["system", "paper", "ink", "classic-light", "classic-dark", "lagoon", "ember", "cobalt", "sage",
                               "breeze", "evergreen", "evergreen-dark"])
        probe = """(() => {
            const shape = [...document.body.querySelectorAll('*')].filter(e => !e.closest('#log, #themes'))
              .map(e => e.tagName + '#' + e.id).join('|');
            const box = (id) => { const r = $(id).getBoundingClientRect(); return [r.left, r.right]; };
            const rgb = (c) => (c.match(/[\\d.]+/g) || []).map(Number);
            const lum = ([r, g, b]) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
              return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
            const body = [...document.querySelectorAll('#log .msg:not(.own) .body')].pop(), cs = getComputedStyle(body);
            const fg = lum(rgb(cs.color)), bg = rgb(cs.backgroundColor);
            const ratio = bg.length > 3 && bg[3] < 1 ? null : (Math.max(fg, lum(bg)) + 0.05) / (Math.min(fg, lum(bg)) + 0.05);
            const root = getComputedStyle(document.documentElement);
            return {shape, rail: box('rail'), space: box('space'), side: box('side'),
                    look: [root.getPropertyValue('--accent').trim(), root.getPropertyValue('--page').trim(), root.getPropertyValue('--font').trim()],
                    theme: document.documentElement.dataset.theme, scheme: document.documentElement.dataset.scheme, ratio};
        })()"""
        c.js(self.card() + ".click()")  # the side panel open: all three sections
        c.until("!$('agent-pane').hidden", what="the agent beside the chat")
        seen, first = {}, None
        for theme in ids[1:]:
            c.js("setLook('theme', %s)" % json.dumps(theme))
            out = c.js(probe)
            self.assertEqual(out["theme"], theme)
            self.assertEqual(out["scheme"], "dark" if theme in ("ink", "classic-dark", "lagoon", "evergreen-dark") else "light")
            first = first or out
            self.assertEqual(out["shape"], first["shape"], theme)  # the same elements, in the same order
            self.assertLessEqual(out["rail"][1], out["space"][0], theme)  # sidebar | chat | side panel, in every theme
            self.assertLessEqual(out["space"][1], out["side"][0], theme)
            self.assertGreater(out["side"][1] - out["side"][0], 200, theme)
            if out["ratio"] is not None:
                self.assertGreaterEqual(out["ratio"], 4.5, theme)  # a message reads, WCAG AA
            seen[theme] = tuple(out["look"])
        self.assertEqual(len(set(seen.values())), len(seen))  # each theme its own look
        c.js("closeAgent()")
        # The choice is this browser's: it stays after a reload, before the page draws.
        c.js("setLook('theme', 'ember')")
        self.assertEqual(c.js("localStorage.getItem('crewchat-theme')"), "ember")
        c.open(self.m.url + "/")
        c.until("!!document.getElementById('log')")
        self.assertEqual(c.js("document.documentElement.dataset.theme"), "ember")
        # System: Paper by day, Ink by night, as the computer is set.
        c.js("setLook('theme', '')")
        c.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": "dark"}])
        c.until("document.documentElement.dataset.theme === 'ink'", what="Ink at night")
        c.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": "light"}])
        c.until("document.documentElement.dataset.theme === 'paper'", what="Paper by day")
        # Settings > Appearance: a radio for each theme, arrow keys between them, and the details.
        c.js("openPanel('appearance')")
        c.until("$('themes').children.length === 12", what="the themes")
        self.assertEqual(c.js("[...$('themes').children].filter(b => b.getAttribute('aria-checked') === 'true').map(b => b.dataset.themeId)"),
                         ["system"])
        c.js("[...$('themes').children][0].dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowRight'}))")
        c.until("document.documentElement.dataset.theme === 'paper' && localStorage.getItem('crewchat-theme') === 'paper'",
                what="the arrow key to pick Paper")
        c.js("document.querySelector('[data-look=size][data-value=large]').click()")
        self.assertEqual(c.js("getComputedStyle(document.body).fontSize"), "16.5px")
        self.assertNotIn("Serif", c.js("getComputedStyle($('settings-title')).fontFamily"))  # Paper: one font throughout
        c.js("document.querySelector('[data-look=heads][data-value=serif]').click()")
        self.assertIn("Serif", c.js("getComputedStyle($('settings-title')).fontFamily"))
        c.js("document.querySelector('[data-look=heads][data-value=sans]').click()")
        self.assertNotIn("Serif", c.js("getComputedStyle($('settings-title')).fontFamily"))
        c.js("document.querySelector('[data-look=density][data-value=compact]').click()")
        self.assertEqual(c.js("document.documentElement.dataset.density"), "compact")
        c.js("$('accent-pick').value = '#7c3aed'; $('accent-pick').dispatchEvent(new Event('input'))")
        self.assertEqual(c.js("getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()"), "#7c3aed")
        c.js("$('accent-reset').click()")
        self.assertEqual(c.js("getComputedStyle(document.documentElement).getPropertyValue('--accent').trim().toLowerCase()"), "#2f5fd6")  # Paper's own, the logo's blue
        self.assertEqual(c.errors, [])

    def test_every_themes_cards_pills_and_mentions_read(self):
        # Lagoon (11 Nov): its task card is cream and its messages white, on a dark page, and they wore the page's
        # light text: the task's text, who has it, the done and blocked pills, @mentions, Thread and Reply were hard
        # to read. In every theme, each of them is WCAG AA (4.5:1) against what it sits on.
        c = self.chrome
        self.addCleanup(c.js, "look('theme', ''); crewTheme(); document.querySelectorAll('.contrast-probe').forEach(n => n.remove())")
        c.until("!!%s" % self.card(), what="the agent")
        name = json.dumps(self.agent.name)
        c.js("""(() => {
            const a = %s, ts = new Date().toISOString(), add = (m) => { const n = buildMessage(m); n.classList.add('contrast-probe');
              $('log').append(n); return n; };
            add({id: 'p1', kind: 'task', from: 'Owner', to: 'all', ts, text: 'Probe task for @' + a});
            add({id: 'p2', kind: 'msg', from: a, to: 'Owner', ts, text: 'Probe message for @Owner and @' + a});
            add({id: 'p3', kind: 'msg', from: 'Owner', to: a, ts, text: 'Probe own message for @' + a});
            add({id: 'p4', kind: 'update', from: a, to: 'all', ts, task: 'p1', status: 'done', text: 'Probe done'});
            add({id: 'p5', kind: 'answer', from: a, to: 'all', ts, answers: 'p1', verdict: 'changes', text: 'Probe blocked'});
            add({id: 'p6', kind: 'ask', from: a, to: 'Owner', ts, text: 'Probe question'});
            document.querySelector('.contrast-probe .receipt').textContent = 'Read by all';
        })()""" % name)
        probe = """(() => {
            const rgba = (c) => { const v = (c.match(/[\\d.]+/g) || []).map(Number); return v.length < 3 ? null : [v[0], v[1], v[2], v.length > 3 ? v[3] : 1]; };
            const lum = ([r, g, b]) => { const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
              return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
            const ground = (e) => {  // the solid colour under e's text: null when a picture or see-through glass is under it
              for (; e && e.nodeType === 1; e = e.parentElement) {
                const cs = getComputedStyle(e), bg = rgba(cs.backgroundColor);
                if (bg && bg[3] >= 0.9) return bg;
                if (cs.backgroundImage !== 'none' || (bg && bg[3] > 0.2)) return null;
              }
              return null;
            };
            const ratio = (e) => { const fg = rgba(getComputedStyle(e).color), bg = ground(e); if (!fg || !bg) return null;
              const a = lum(fg), b = lum(bg); return Math.round((Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05) * 100) / 100; };
            const out = {}, q = (s) => document.querySelector(s);
            const task = q('.contrast-probe.task'), st = task.querySelector('.state');
            out['task text'] = ratio(task.querySelector('.body'));
            out['task label'] = ratio(task.querySelector('.label span'));
            st.textContent = 'Taken by someone · done';
            for (let i = 0; i < 8; i++) { st.className = 'state c' + i; out['task state c' + i] = ratio(st); }
            st.className = 'state off'; out['task state off'] = ratio(st);
            out['mention'] = ratio(q('.contrast-probe:not(.own):not(.task) .body .mention'));
            out['own mention'] = ratio(q('.contrast-probe.own .body .mention'));
            out['task mention'] = ratio(task.querySelector('.mention'));
            out['done pill'] = ratio(q('.contrast-probe .pill.done'));
            out['blocked pill'] = ratio(q('.contrast-probe .pill.blocked'));
            out['Thread'] = ratio(q('.contrast-probe .thread-btn'));
            out['Reply'] = ratio(q('.contrast-probe .reply'));
            out['receipt'] = ratio(q('.contrast-probe .receipt'));
            out['banner'] = ratio($('banner'));
            return out;
        })()"""
        mid = self.m.owner.task("all", "Probe a thread's replies")  # the side panel's thread: white replies in Lagoon
        self.addCleanup(self.m.owner.post, "/api/task", {"op": "remove", "task": mid})
        c.until("state.nodes.has(%s)" % json.dumps(mid), what="the task")
        c.js("openPanel('thread', %s)" % json.dumps(mid))
        c.until("!!document.querySelector('.thread-log .tmsg b')", what="the thread")
        probe = probe.replace("return out;", "out['thread reply'] = ratio(q('.thread-log .tmsg'));"
                              " out['thread reply name'] = ratio(q('.thread-log .tmsg b')); return out;")
        ids = c.js("THEMES.map(t => t[0])")[1:]
        failing = {}
        for theme in ids:
            c.js("setLook('theme', %s)" % json.dumps(theme))
            out = c.js(probe)
            self.assertIsNotNone(out["thread reply"], theme)
            self.assertIsNotNone(out["task text"], theme)  # measured, not skipped: the card is solid in every theme
            low = {k: v for k, v in out.items() if v is not None and v < 4.5}
            if low:
                failing[theme] = low
        self.assertEqual(failing, {}, json.dumps(failing, indent=1))

    def test_the_sections_resize_like_the_desktop_app(self):
        # W16: drag the handle between two sections, or focus it and use the arrow keys; double-click resets; dragged
        # narrow enough a section hides and its handle stays; the widths are this browser's, after a reload too.
        c = self.chrome
        self.addCleanup(c.js, "localStorage.removeItem('crewchat-widths'); for (const k in SIZES) setWidth(k, SIZES[k].def, false);"
                              " closePanel(); if (state.agentPane) closeAgent()")
        c.until("!!%s" % self.card(), what="the agent")
        width = lambda id: c.js("Math.round($(%s).getBoundingClientRect().width)" % json.dumps(id))  # noqa: E731
        key = lambda grip, k: c.js("$(%s).dispatchEvent(new KeyboardEvent('keydown', {key: %s, bubbles: true}))"  # noqa: E731
                                   % (json.dumps(grip), json.dumps(k)))
        self.assertEqual(width("rail"), 260)
        self.assertEqual(c.js("[$('grip-rail').getAttribute('role'), $('grip-rail').getAttribute('aria-valuenow')]"), ["separator", "260"])
        key("grip-rail", "ArrowRight"); key("grip-rail", "ArrowRight")
        self.assertEqual(width("rail"), 292)
        self.assertEqual(c.js("JSON.parse(localStorage.getItem('crewchat-widths')).rail"), 292)
        c.js("$('grip-rail').dispatchEvent(new MouseEvent('dblclick', {bubbles: true}))")
        self.assertEqual(width("rail"), 260)  # reset
        # A real drag with the mouse: wider, then narrow enough to hide; the handle stays and opens it again.
        r = c.js("(() => { const b = $('grip-rail').getBoundingClientRect(); return [b.left + b.width / 2, b.top + b.height / 2]; })()")
        def drag(x0, y, x1):
            c.call("Input.dispatchMouseEvent", type="mousePressed", x=x0, y=y, button="left", clickCount=1)
            for i in range(1, 6):
                c.call("Input.dispatchMouseEvent", type="mouseMoved", x=x0 + (x1 - x0) * i / 5, y=y, button="left", buttons=1)
            c.call("Input.dispatchMouseEvent", type="mouseReleased", x=x1, y=y, button="left", clickCount=1)
        drag(r[0], r[1], r[0] + 60)
        self.assertEqual(width("rail"), 320)
        drag(r[0] + 60, r[1], 40)
        self.assertTrue(c.js("document.body.classList.contains('rail-hidden') && getComputedStyle($('rail')).display === 'none'"))
        self.assertEqual(c.js("$('grip-rail').getAttribute('aria-valuetext')"), "The sidebar is hidden")
        self.assertGreater(width("grip-rail"), 0)  # its handle stays
        self.assertGreater(width("space"), 1000)  # the chat takes the room
        key("grip-rail", "ArrowRight")
        self.assertEqual(width("rail"), 200)  # open again, at its narrowest
        # The side panel, from its own handle: the arrows go its way (left is wider).
        c.js(self.card() + ".click()")
        c.until("!$('agent-pane').hidden", what="the agent beside the chat")
        self.assertEqual(width("side"), 320)
        key("grip-side", "ArrowLeft")
        self.assertEqual(width("side"), 336)
        c.js("closeAgent()")
        # The Settings list too.
        c.js("openPanel('general')")
        key("grip-settings", "ArrowRight")
        self.assertEqual(width("settings-nav"), 276)
        c.js("closePanel()")
        # Remembered by this browser.
        c.open(self.m.url + "/")
        c.until("!!document.getElementById('log')")
        self.assertEqual([width("rail"), c.js("widths.side"), c.js("widths.settings")], [200, 336, 276])
        self.assertEqual(c.errors, [])

    def test_the_phone_has_the_places_as_tabs_and_settings_as_one_list(self):
        # W16 (C1 to C3): on a phone, Chat, Needs you, Tasks, Agents and More at the bottom; what needs you said over the
        # chat; More is Settings as one grouped list, this project first, a section a tap away and back.
        c = self.chrome
        tab = lambda name: c.js("document.querySelector('#tabbar [data-tab=%s]').click()" % name)  # noqa: E731
        current = "document.querySelector('#tabbar [aria-current=page]').dataset.tab"
        try:
            self.assertEqual(c.js("getComputedStyle($('tabbar')).display"), "none")  # not on a wide screen
            c.size(390, 844, mobile=True)
            c.open(self.m.url + "/")
            c.until("!!%s" % self.card(), what="the page on a phone")
            self.assertEqual(c.js("[...document.querySelectorAll('#tabbar [data-tab]')].map(b => b.textContent.replace(/\\d+$/, ''))"),
                             ["Chat", "Needs you", "Tasks", "Agents", "More"])
            self.assertEqual(c.js(current), "chat")
            self.assertLessEqual(c.js("document.documentElement.scrollWidth"), 390)
            self.assertLessEqual(c.js("$('form').getBoundingClientRect().bottom"), c.js("$('tabbar').getBoundingClientRect().top") + 1)
            self.assertEqual(self.shown_though_hidden(), [])
            # Something that needs you: counted on its tab, and said over the chat.
            self.agent.call("hub_send", to="Owner", text="Shall I take the login screen?", ask=True)
            c.until("!$('needs-line').hidden && $('tab-needs').textContent !== ''", what="the count and the line")
            self.assertIn("need", c.js("$('needs-line').textContent"))
            c.click("#needs-line")
            c.until("!$('needs-you').hidden && %s === 'needs'" % current, what="Needs you from the line")
            button = c.js("Math.round([...document.querySelectorAll('#needs-list .need-card .acts button')].pop().getBoundingClientRect().height)")
            self.assertGreaterEqual(button, 44)  # a thumb's size
            c.js("[...document.querySelectorAll('#needs-list .need-card')].pop().querySelector('textarea').value = 'Yes'; "
                 "[...[...document.querySelectorAll('#needs-list .need-card')].pop().querySelectorAll('button')].find(b => b.textContent === 'Send').click()")
            c.until("$('needs-line').hidden", what="answered")
            tab("tasks")
            c.until("!$('tasks').hidden && %s === 'tasks'" % current, what="Tasks")
            tab("agents")
            c.until("document.body.classList.contains('rail-open') && %s === 'agents'" % current, what="the crew")
            c.until("$('agents').getBoundingClientRect().left >= 0", what="the crew on screen")
            # More: Settings as one list, this project's sections first.
            tab("more")
            c.until("!$('settings').hidden && $('settings').classList.contains('list') && %s === 'more'" % current, what="More")
            self.assertFalse(c.js("document.body.classList.contains('rail-open')"))
            places = c.js("[...document.querySelectorAll('#settings-nav [data-settings]')].filter(b => b.offsetParent)"
                          ".sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top).map(b => b.firstChild.textContent)")
            self.assertEqual(places[:6], ["General", "Crew & roles", "Safety", "Integrations", "Routines", "Folders & files"])
            self.assertIn("Appearance", places[6:])
            self.assertEqual(c.js("getComputedStyle($('settings-main')).display"), "none")
            c.js("document.querySelector('#settings-nav [data-settings=safety]').click()")
            c.until("$('settings-title').textContent === 'Safety' && getComputedStyle($('settings-nav')).display === 'none'",
                    what="a section, the whole screen")
            self.assertLessEqual(c.js("document.documentElement.scrollWidth"), 390)
            c.click("#settings-up")
            c.until("$('settings').classList.contains('list')", what="back to the list")
            tab("chat")
            c.until("$('settings').hidden && !$('log').hidden && %s === 'chat'" % current, what="the chat again")
            self.assertEqual(c.errors, [])
        finally:
            c.size(1280, 900)
            c.open(self.m.url + "/")
            c.until("!!document.getElementById('log')")

    def test_the_option_switches_stay_one_row_in_their_pill(self):
        # Owner, 12 Nov (Settings, Appearance, a narrow window): Small/Default/Large, Comfortable/Compact and the
        # headings font each wrapped into a tall oval, one option per line, the chosen one sticking out of its border.
        # Every switch in Settings stays one row inside its pill, at any width; a row too tight for it puts the switch
        # under its label instead, and on a phone the long ones (notifications) are a column of rounded rows, not an oval.
        c = self.chrome
        broken = ("[...document.querySelectorAll('#settings .seg')].filter(s => s.offsetParent).flatMap(s => {"
                  " const r = s.getBoundingClientRect(), bs = [...s.children].filter(b => b.offsetParent);"
                  " const rows = new Set(bs.map(b => Math.round(b.getBoundingClientRect().top))).size;"
                  " const out = bs.some(b => { const q = b.getBoundingClientRect();"
                  "   return q.left < r.left - 0.5 || q.right > r.right + 0.5 || q.top < r.top - 0.5 || q.bottom > r.bottom + 0.5; });"
                  " const cut = bs.some(b => b.scrollWidth > b.clientWidth + 1) || r.right > innerWidth + 0.5;"
                  " const column = getComputedStyle(s).display === 'grid' && parseFloat(getComputedStyle(s).borderTopLeftRadius) <= 16"
                  "   && bs.every(b => Math.abs(b.getBoundingClientRect().width - bs[0].getBoundingClientRect().width) < 1);"
                  " return (rows > 1 && !column) || out || cut ? [(s.getAttribute('aria-label') || s.id) + ': ' + rows + ' rows'"
                  "   + (out ? ', out of its pill' : '') + (cut ? ', cut off' : '')] : []; })")
        try:
            for width, height, mobile in ((1280, 900, False), (1000, 800, False), (800, 800, False), (390, 844, True), (320, 640, True)):
                c.size(width, height, mobile=mobile)
                for section in ("appearance", "notifications"):
                    c.js("openPanel(%s)" % json.dumps(section))
                    c.until("!$('settings').hidden && [...document.querySelectorAll('#settings .seg')].some(s => s.offsetParent)",
                            what="the %s section" % section)
                    self.assertEqual(c.js(broken), [], "%s at %dpx" % (section, width))
                    c.js("closePanel()")
            self.assertEqual(c.errors, [])
        finally:
            c.size(1280, 900)
            c.js("closePanel()")

    def test_the_phones_settings_list_says_what_each_row_is_set_to(self):
        # W16 (C3): on a phone each row of the grouped Settings list says, under its name, what it is set to now, from
        # the same state its section shows.
        c = self.chrome
        of = lambda kind: c.js("(document.querySelector('#settings-nav [data-settings=%s] .sum') || {}).textContent || ''" % kind)  # noqa: E731
        shown = "getComputedStyle(document.querySelector('#settings-nav [data-settings=appearance] .sum')).display !== 'none'"
        try:
            c.js("localStorage.setItem('crewchat-theme', 'ink'); localStorage.removeItem('crewchat-density'); "
                 "localStorage.setItem(notifyKey(), 'all')")
            c.js("openPanel('general')")
            c.until("!!document.querySelector('#settings-nav [data-settings=appearance] .sum')", what="the values")
            self.assertFalse(c.js(shown))  # a wide screen: the list has the names only
            c.js("closePanel()")
            c.size(390, 844, mobile=True)
            c.open(self.m.url + "/")
            c.until("!!%s" % self.card(), what="the page on a phone")
            c.js("document.querySelector('#tabbar [data-tab=more]').click()")
            c.until("$('settings').classList.contains('list') && " + shown, what="the list with each row's value")
            self.assertEqual(of("appearance"), "Ink, comfortable")
            self.assertEqual(of("notifications"), "On, when something needs you")
            self.assertEqual(of("machines"), "This machine only")
            self.assertRegex(of("agents"), r"^\d+ agents?$")
            self.assertRegex(of("general"), r"^\d+ projects?")
            self.assertRegex(of("project"), r"\d+ shared skills?")
            self.assertRegex(of("crew"), r"^\d+ roles")
            c.until("/^(Agents you start|Any session joins), /.test(%s)" % "(document.querySelector('#settings-nav [data-settings=safety] .sum') || {}).textContent", what="Safety's value")
            c.until("/^\\d+ folders?$/.test(%s)" % "(document.querySelector('#settings-nav [data-settings=folders] .sum') || {}).textContent", what="the folders")
            self.assertEqual(c.js("document.querySelector('#settings-nav [data-settings=appearance]').firstChild.textContent"), "Appearance")
            # A change in its section shows in the list at once.
            c.js("setLook('density', 'compact')")
            self.assertEqual(of("appearance"), "Ink, compact")
            self.assertLessEqual(c.js("document.documentElement.scrollWidth"), 390)
            self.assertEqual(c.errors, [])
        finally:
            c.js("localStorage.removeItem('crewchat-theme'); localStorage.removeItem('crewchat-density'); "
                 "localStorage.removeItem(notifyKey())")
            c.size(1280, 900)
            c.open(self.m.url + "/")
            c.until("!!document.getElementById('log')")

    def test_a_link_to_a_setting_and_the_old_panel_names(self):
        c = self.chrome
        try:
            c.open(self.m.url + "/#settings/integrations/jira-card")
            c.until("!$('settings').hidden && $('settings-title').textContent === 'Integrations'", what="the linked section")
            c.until("$('jira-card').classList.contains('flash')", what="the linked card marked")
            for old, title in (("machines", "Machines & devices"), ("roles", "Crew & roles"), ("commands", "Advanced"),
                               ("phone", "Phone & remote access"), ("project", "General")):
                c.js("closePanel(); location.hash = '#%s'" % old)  # an old panel's name, for one release
                c.until("!$('settings').hidden && $('settings-title').textContent === %s" % json.dumps(title), what=old)
                c.js("closePanel()")
                c.js("openPanel(%s)" % json.dumps(old))
                c.until("!$('settings').hidden && $('settings-title').textContent === %s" % json.dumps(title), what=old)
            self.assertEqual(c.errors, [])
        finally:
            c.open(self.m.url + "/")
            c.until("!!document.getElementById('log')")

    def test_commands_are_in_settings_advanced_and_the_agent_stays_beside_the_chat(self):
        c = self.chrome
        c.until("!!%s" % self.card(), what="the agent")
        c.js(self.card() + ".click()")
        c.until("!$('agent-pane').hidden", what="the agent's settings")
        c.js("openPanel('commands')")  # the old panel name opens its new place (W16)
        c.until("!$('settings').hidden && $('settings-title').textContent === 'Advanced' && !$('commands').hidden",
                what="the commands in Settings › Advanced")
        c.until("document.querySelectorAll('#settings #cmd-list .cmd').length > 40", what="the commands")
        self.assertEqual(c.js("location.hash"), "#settings/advanced")
        self.assertEqual(c.js("getComputedStyle($('rail')).display"), "none")  # Settings takes the window
        c.click("#settings-back")
        c.until("$('settings').hidden && !$('log').hidden", what="back to the chat")
        self.assertFalse(c.js("$('agent-pane').hidden"))  # the agent is still open beside the chat
        self.assertEqual(c.js("location.hash"), "")
        self.assertEqual(c.errors, [])

    def test_new_agent_browses_makes_and_connects_a_folder(self):
        c = self.chrome
        top = H.TMP / "browse-here"
        (top / "apps" / "web").mkdir(parents=True, exist_ok=True)
        try:
            c.js("openLaunch(); showStep(0)")  # the workspace is the first step (U8)
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
            approve = ("[...$('l-connect-box').querySelectorAll('button')].find(b => /^(Approve and connect|Connect)$/"
                       ".test(b.textContent)).click()")
            c.until("!$('l-connect-box').hidden", what="the tools to connect (W11)")
            c.js(approve)
            c.until("$('l-folder').value.endsWith('/apps/mobile') && $('l-browser').hidden", timeout=60, what="connected")
            self.assertTrue((top / "apps" / "mobile" / ".mcp.json").is_file())
            # A path that is not there: Connect offers to make it.
            c.js("$('l-path').value = %s" % json.dumps(str(top / "apps" / "desktop")))
            c.click("#l-connect")
            c.until("!$('l-connect-box').hidden", what="the tools to connect")
            c.js(approve)
            c.until("$('launch-note').innerText.includes('Create it and connect')", what="the offer to make it")
            c.js("[...$('launch-note').querySelectorAll('button')].find(b => b.textContent === 'Create it and connect').click()")
            c.until("$('l-folder').value.endsWith('/apps/desktop')", timeout=60, what="made and connected")
            self.assertEqual(c.errors, [])
        finally:
            c.js("$('launch').close()")

    def test_the_chat_tab_comes_back_from_any_view(self):
        c = self.chrome
        for view in ("tasks", "analytics"):  # U2
            c.click("#view-" + view)
            c.until("!$('%s').hidden && $('log').hidden" % view, what="the %s view" % view)
            c.click("#view-chat")
            c.until("!$('log').hidden && $('view-chat').getAttribute('aria-current') === 'page'", what="the chat again")
        self.assertEqual(c.errors, [])

    def test_the_board_fades_an_edge_with_more_columns_past_it(self):
        c = self.chrome
        mid = self.m.owner.task("all", "A task to open beside the board")
        try:
            c.size(1100, 800)
            c.js("showView('tasks'); $('as-board').click()")
            c.until("!$('board').hidden && $('board').querySelectorAll('.col').length === 5", what="the board")
            c.js("openPanel('task', %s)" % json.dumps(mid))  # the panel takes room: the board scrolls (U5)
            c.until("$('board').scrollWidth > $('board').clientWidth + 2 && $('board').classList.contains('more-right')",
                    what="a faded right edge")
            c.js("$('board').scrollLeft = $('board').scrollWidth")
            c.until("$('board').classList.contains('more-left') && !$('board').classList.contains('more-right')",
                    what="the fade on the left at the end")
            self.assertEqual(c.errors, [])
        finally:
            c.js("closePanel(); showView('chat')")
            c.size(1280, 900)

    def test_a_leads_pieces_name_the_task_they_are_part_of(self):
        c = self.chrome
        self.m.owner.role(self.agent.name, "lead")
        self.addCleanup(self.m.owner.role, self.agent.name, "none")
        parent = self.m.owner.task("all", "Offline mode for notes")
        dev = self.m.agent()
        dev.link()
        self.addCleanup(self.m.owner.post, "/api/agent", {"op": "remove", "agent": dev.name})
        said = self.agent.call("hub_task", action="assign", to=dev.name, title="Queue edits made offline", parent=parent)
        piece = re.search(r"#(\w+)", said).group(1)
        for mid in (parent, piece):  # off the sheet before the agent goes (cleanups run last first)
            self.addCleanup(self.m.owner.post, "/api/task", {"op": "remove", "task": mid})
        c.js("showView('tasks'); $('as-board').click()")  # U6: the piece says whose part it is; the parent counts them
        c.until("[...$('board').querySelectorAll('.tcard')].some(b => b.dataset.task === %s && "
                "b.textContent.includes('part of #%s'))" % (json.dumps(piece), parent), what="the piece's chip")
        c.until("[...$('board').querySelectorAll('.tcard')].some(b => b.dataset.task === %s && "
                "b.textContent.includes('coordinating 1 piece'))" % json.dumps(parent), what="the parent's count")
        c.js("openPanel('task', %s)" % json.dumps(piece))
        c.until("$('panel-body').textContent.includes('Part of') && $('panel-body').textContent.includes('Offline mode')",
                what="Part of in the piece's panel")
        c.js("closePanel(); showView('chat')")
        self.assertEqual(c.errors, [])

    def test_a_tasks_panel_names_every_action_and_sets_remove_apart(self):
        c = self.chrome
        mid = self.m.owner.task("all", "A task whose actions are read")
        self.addCleanup(self.m.owner.post, "/api/task", {"op": "remove", "task": mid})
        c.js("openPanel('task', %s)" % json.dumps(mid))
        c.until("[...$('panel-body').querySelectorAll('.acts button')].length >= 3", what="the task's actions")
        labels = c.js("[...$('panel-body').querySelectorAll('.acts button')].map(b => b.textContent)")
        self.assertTrue(all(any(ch.isalpha() for ch in label) for label in labels), labels)  # U7: no bare symbol
        self.assertIn("Remove…", labels)
        self.assertEqual(c.js("[...$('panel-body').querySelectorAll('.acts button')].find(b => b.textContent === "
                              "'Remove…').classList.contains('apart')"), True)
        c.js("closePanel()")
        self.assertEqual(c.errors, [])

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
              row("busy-one", {session: null, startable: false}),
              row("stuck-one", {session: null, startable: true, activity: "working", seen: state.now - 10})];
            const look = (n) => { openPanel("agent", n); const b = $("agent-pane-body");
              return {buttons: [...b.querySelectorAll("button")].filter(x => !x.disabled).map(x => x.textContent),
                      text: b.innerText, selects: [...b.querySelectorAll("select")].map(s => s.getAttribute("aria-label")),
                      picture: !!b.querySelector(".picmenu"), nameField: !!b.querySelector(".namefield")}; };
            const out = {running: look("running-one"), closed: look("closed-one"), far: look("far-one"),
                         unrecorded: look("unrecorded-one"), busy: look("busy-one"), stuck: look("stuck-one")};
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
        # It says it is working, but crewchat recorded no session to tell: the owner can still start a new one.
        self.assertIn("Start again", out["stuck"]["buttons"])
        self.assertNotIn("Remove", out["far"]["buttons"])  # it runs on another machine: changed there
        self.assertFalse(out["far"]["nameField"])
        self.assertFalse(out["far"]["picture"])
        self.assertIn("Runs on laptop", out["far"]["text"])
        self.assertEqual(c.errors, [])

    def test_an_agent_in_the_background_on_the_page(self):
        # W9: no window. Its card says what it is doing, shows its terminal, and switches its mode; its tool's
        # first-run questions and a session stuck on its terminal are cards in Needs you.
        c = self.chrome
        c.until("state.agents.length > 0", what="the agents")
        out = c.js("""(() => {
            const saved = state.agents;
            const base = state.agents[0];
            const row = (name, extra) => Object.assign({}, base, {agent: name, remote: false, permissions: "ask"}, extra);
            const bg = {app: "background", since: state.now, tool: "claude", reopen: true, attach: true, modes: ["ask", "plan", "edits"]};
            state.agents = [row("bg-one", {session: Object.assign({}, bg, {running: true, state: "waiting", waiting: "permission prompt"})}),
                            row("bg-closed", {session: Object.assign({}, bg, {running: false, state: "done"})})];
            const look = (n) => { openPanel("agent", n); const b = $("agent-pane-body");
              return {buttons: [...b.querySelectorAll("button")].filter(x => !x.disabled).map(x => x.textContent),
                      text: b.innerText, selects: [...b.querySelectorAll("select")].map(s => s.getAttribute("aria-label")),
                      modes: [...(b.querySelector("select[aria-label^='Switch bg-one']") || {options: []}).options].map(o => o.text)}; };
            const out = {running: look("bg-one"), closed: look("bg-closed")};
            closeAgent(); state.agents = saved; renderAgents(true);
            renderNeeds([
              {kind: "firstrun", key: "firstrun:start-1", id: "start-1", agent: "Ivy", tool: "Claude Code", folder: "/w/site",
               questions: ["Trust the files in /w/site", "Use the MCP servers its .mcp.json lists: jira"], text: "x", ts: 1,
               blocking: 0, choices: ["approve", "cancel"]},
              {kind: "stuck", key: "stuck:Max", id: "Max", agent: "Max", tool: "Claude Code", text: "Its background session waits",
               ts: 1, blocking: 0, choices: ["show", "attach", "stop"]}]);
            const needs = {text: $("needs-list").innerText,
                           buttons: [...$("needs-list").querySelectorAll("button")].map(x => x.textContent)};
            renderNeeds(state.needs || []);
            return Object.assign(out, {needs});
        })()""")
        self.assertIn("Running in the background · Waiting on permission prompt", out["running"]["text"])
        for button in ("Show terminal", "Open in Terminal", "Stop"):
            self.assertIn(button, out["running"]["buttons"])
        self.assertIn("Switch bg-one's mode", out["running"]["selects"])
        self.assertEqual(out["running"]["modes"], ["Switch mode…", "Ask (now)", "Plan", "Accept edits"])
        self.assertIn("Reopen", out["closed"]["buttons"])
        self.assertIn("Show terminal", out["closed"]["buttons"])
        self.assertNotIn("Open in Terminal", out["closed"]["buttons"])
        self.assertIn("Trust the files in /w/site", out["needs"]["text"])
        self.assertIn("jira", out["needs"]["text"])
        for button in ("Approve and start", "Cancel", "Show terminal", "Open in Terminal", "Stop"):
            self.assertIn(button, out["needs"]["buttons"])
        # New agent offers both ways, the background first.
        c.js("openLaunch()")
        c.until("launchTools.length > 0", what="the launch options")
        where = c.js("""(() => { $("l-tool").value = "claude"; showTool();
            const claude = {hidden: $("l-where").hidden, value: $("l-where").value};
            $("l-tool").value = "cursor"; showTool();
            const cursor = {hidden: $("l-where").hidden, value: $("l-where").value};
            $("launch").close(); return {claude, cursor}; })()""")
        self.assertEqual(where["claude"], {"hidden": False, "value": "background"})
        self.assertEqual(where["cursor"], {"hidden": False, "value": "background"})
        self.assertEqual(c.errors, [])

    def test_an_agent_that_did_not_start_is_a_card_with_why_and_its_last_lines(self):
        # Any start that ends or never joins before it links (a crash, a missing sign-in, a link timeout): the owner
        # sees why on the page, with its last lines, and can try again or dismiss it.
        c = self.chrome
        c.until("NEED_LOOK.start_failed !== undefined", what="the kinds of Needs you")
        out = c.js("""(() => {
            const sent = [];
            const saved = window.answerNeed;
            window.answerNeed = async (n, path, body) => { sent.push(body); return {ok: true}; };
            renderNeeds([
              {kind: "start_failed", key: "start_failed:start-1", id: "start-1", agent: "ivy", tool: "Cursor",
               folder: "/w/site", where: "background", ts: 1, blocking: 0, choices: ["retry", "dismiss"],
               text: "Its background session ended before it joined the chat (exit code 1).",
               log: ["No crewchat tools here.", "Error: MCP server crewchat is not approved"]},
              {kind: "start_failed", key: "start_failed:start-2", id: "start-2", agent: "", tool: "Claude Code",
               folder: "/w/api", where: "window", ts: 1, blocking: 0, choices: ["retry", "dismiss"],
               text: "It has not joined the chat 5 minutes after it started.", log: []}]);
            const list = $("needs-list");
            const cards = [...list.querySelectorAll(".need")];
            const look = cards.map((b) => ({text: b.innerText, pre: (b.querySelector("pre.screen") || {}).textContent || "",
                                            buttons: [...b.querySelectorAll("button")].map((x) => x.textContent),
                                            ghost: [...b.querySelectorAll("button.ghost")].map((x) => x.textContent)}));
            cards[0].querySelector("button").click();
            return new Promise((done) => setTimeout(() => {
              const said = cards[0].querySelector(".acts .meta").textContent;
              window.answerNeed = saved;
              renderNeeds(state.needs || []);
              done({look, sent, said});
            }, 50));
        })()""")
        first, second = out["look"]
        self.assertIn("Not started · ivy", first["text"])
        self.assertIn("Cursor in the background in /w/site", first["text"])
        self.assertIn("ended before it joined the chat (exit code 1)", first["text"])
        self.assertIn("Its last lines:", first["text"])
        self.assertEqual(first["pre"], "No crewchat tools here.\nError: MCP server crewchat is not approved")
        self.assertEqual(first["buttons"], ["Try again", "Dismiss"])
        self.assertEqual(first["ghost"], ["Dismiss"])
        self.assertIn("Not started · new agent", second["text"])
        self.assertIn("Claude Code in a window in /w/api", second["text"])
        self.assertEqual(second["pre"], "")  # no lines to show: none shown
        self.assertEqual(out["sent"], [{"key": "start_failed:start-1", "answer": "retry"}])
        self.assertEqual(out["said"], "Starting again")
        self.assertEqual(c.errors, [])

    def test_restart_applies_the_browser_turned_on_while_it_runs(self):
        # Two-machine test (dev 803c0b6): the browser turned on for a running background Claude Code agent said "from its
        # next start or Reopen", but its card had no Reopen. Now it has Restart, which posts op restart, and says so.
        c = self.chrome
        c.until("state.agents.length > 0", what="the agents")
        out = c.js("""(() => {
            const saved = state.agents, base = state.agents[0];
            const bg = {app: "background", since: state.now, tool: "claude", reopen: true, attach: true,
                        modes: ["ask", "plan", "edits"], state: "idle", waiting: ""};
            const row = (name, extra) => Object.assign({}, base, {agent: name, remote: false, client: "claude",
                                                                  permissions: "ask", browser: true, mobile: false}, extra);
            state.agents = [row("due-one", {session: Object.assign({}, bg, {running: true, restart: ["browser"]})}),
                            row("fresh-one", {session: Object.assign({}, bg, {running: true, restart: []})}),
                            row("closed-one", {session: Object.assign({}, bg, {running: false, state: "done", restart: []})})];
            const look = (n) => { openPanel("agent", n); const b = $("agent-pane-body");
              return {buttons: [...b.querySelectorAll("button")].filter(x => !x.disabled).map(x => x.textContent),
                      text: b.innerText}; };
            const out = {due: look("due-one"), fresh: look("fresh-one"), closed: look("closed-one")};
            openPanel("agent", "due-one");
            window.sent = []; window.realFetch = window.realFetch || window.fetch;
            window.fetch = (url, opts) => { window.sent.push([String(url), opts && opts.body || ""]);
                                            return Promise.resolve(new Response('{"ok": true}', {status: 200})); };
            window.asked = ""; window.confirm = (q) => { window.asked = q; return true; };
            [...$("agent-pane-body").querySelectorAll("button")].find(x => x.textContent === "Restart").click();
            out.asked = window.asked;
            window.fetch = window.realFetch; window.confirm = () => true;
            out.sent = window.sent.filter(([u]) => u.includes("/api/agent")).map(([u, b]) => JSON.parse(b));
            closeAgent(); state.agents = saved; renderAgents(true);
            return out;
        })()""")
        self.assertIn("Restart", out["due"]["buttons"])
        self.assertIn("Claude in Chrome once it restarts", out["due"]["text"])
        self.assertNotIn("next start or Reopen", out["due"]["text"])  # it has no Reopen while it runs
        self.assertNotIn("Restart", out["fresh"]["buttons"])  # nothing waits for a restart
        # Running with the browser it started (or was restarted) with: no "next start or Reopen" (Mac retest of #120).
        self.assertNotIn("next start or Reopen", out["fresh"]["text"])
        self.assertNotIn("once it restarts", out["fresh"]["text"])
        self.assertNotIn("Restart", out["closed"]["buttons"])
        self.assertIn("Reopen", out["closed"]["buttons"])  # closed: Reopen starts it with the browser, as it says
        self.assertIn("next start or Reopen", out["closed"]["text"])
        self.assertIn("so its browser applies", out["asked"])
        self.assertEqual(out["sent"], [{"op": "restart", "agent": "due-one"}])
        self.assertEqual(c.errors, [])

    def test_the_agent_card_changes_live_after_a_button(self):
        # Mac retest of #134 and #136: Driving it now, Let go and Restart only showed after a reload. The card was not
        # drawn again while the owner's focus was on one of its buttons (the Turn on just pressed), nor when only the
        # agent's row changed (a hold taken, a session restarted) with no new chat version.
        c = self.chrome
        c.until("state.agents.length > 0", what="the agents")
        out = c.js("""(() => {
            const saved = state.agents, base = state.agents[0];
            const s = {app: "background", since: state.now, tool: "claude", reopen: true, attach: true,
                       modes: ["ask", "plan", "edits"], state: "idle", waiting: "", running: true, restart: []};
            const row = (extra) => Object.assign({}, base, {agent: "live-one", remote: false, client: "claude",
                permissions: "ask", browser: false, browser_held: false, mobile: false, session: s}, extra);
            const buttons = () => [...$("agent-pane-body").querySelectorAll("button")].map((x) => x.textContent);
            const text = () => $("agent-pane-body").innerText;
            const out = {};
            state.agents = [row({})];
            openPanel("agent", "live-one");
            const on = [...$("agent-pane-body").querySelectorAll("button")].find((x) => x.getAttribute("aria-label") === "The browser for live-one");
            on.focus();  // the owner just pressed Turn on
            // The server turned it on while it runs: its card offers Restart (same chat version, the poll's new row).
            state.agents = [row({browser: true, session: Object.assign({}, s, {restart: ["browser"]})})];
            renderAgentPane();
            out.restart = buttons().includes("Restart");
            out.focus = document.activeElement.getAttribute("aria-label");
            // Restarted, then it took the browser: Driving it now, with Let go.
            state.agents = [row({browser: true, browser_held: true})];
            renderAgentPane();
            out.driving = text().includes("Driving it now") && buttons().includes("Let go");
            out.gone = !buttons().includes("Restart");
            // Let go: the hold is gone, and so are the label and its button.
            [...$("agent-pane-body").querySelectorAll("button")].find((x) => x.textContent === "Let go").focus();
            state.agents = [row({browser: true, browser_held: false})];
            renderAgentPane();
            out.letGo = !text().includes("Driving it now") && !buttons().includes("Let go");
            // Typing in its name still keeps the card as it is.
            const name = $("agent-pane-body").querySelector("input");
            name.focus();
            state.agents = [row({browser: true, browser_held: true})];
            renderAgentPane();
            out.typing = !text().includes("Driving it now");
            closeAgent(); state.agents = saved; renderAgents(true);
            return out;
        })()""")
        self.assertEqual(out, {"restart": True, "focus": "The browser for live-one", "driving": True, "gone": True,
                               "letGo": True, "typing": True})
        self.assertEqual(c.errors, [])

    def test_agents_on_linked_machines_on_the_page(self):
        # W10: a linked machine's agent has its session's controls here, through crewchat's own link (not cloud sync);
        # Fork for a Claude Code one; the crew's buttons; and New agent's machine.
        c = self.chrome
        c.until("state.agents.length > 0", what="the agents")
        out = c.js("""(() => {
            const saved = state.agents, base = state.agents[0];
            const s = {app: "background", running: true, since: state.now, tool: "claude", reopen: true, attach: false,
                       modes: ["ask", "plan", "edits"], state: "idle", waiting: ""};
            state.agents = [Object.assign({}, base, {agent: "far-linked", remote: true, device: "laptop", machine: "dev-b", session: s}),
                            Object.assign({}, base, {agent: "far-cloud", remote: true, device: "desk", machine: "", session: s})];
            const look = (n) => { openPanel("agent", n);
              return [...$("agent-pane-body").querySelectorAll("button")].filter(x => !x.disabled).map(x => x.textContent); };
            const out = {linked: look("far-linked"), cloud: look("far-cloud")};
            closeAgent(); state.agents = saved; renderAgents(true);
            out.crew = ["crew-save", "crew-start", "crew-stop"].map((id) => !!$(id) && $("crew-card").contains($(id)) && $("crew-card").dataset.section === "agents");
            return out;
        })()""")
        for button in ("Stop", "Fork", "Show terminal"):
            self.assertIn(button, out["linked"])
        self.assertNotIn("Open in Terminal", out["linked"])  # its window would open on the other machine
        self.assertNotIn("Stop", out["cloud"])  # cloud sync carries no requests
        self.assertEqual(out["crew"], [True, True, True])
        c.js("openLaunch()")
        c.until("$('l-machine').options.length > 0", what="the machines")
        machines = c.js("(() => { const m = [...$('l-machine').options].map(o => [o.value, o.text]); $('launch').close(); return m; })()")
        self.assertEqual(machines[0][0], "")
        self.assertIn("(this machine)", machines[0][1])
        self.assertEqual(c.errors, [])

    def test_connect_a_folder_shows_its_tools_and_their_questions(self):
        # W11: Connect a folder… opens New agent on its folder; Connect lists the tools found on this machine, each with
        # what it would ask once there, before anything is connected.
        c = self.chrome
        folder = tempfile.mkdtemp(prefix="w11-page-")
        c.js("$('tab-add').open = true; $('connect-project').click()")
        c.until("$('launch').open && document.activeElement === $('l-path')", what="New agent on its folder step")
        c.js("$('l-path').value = %s; $('l-connect').click()" % json.dumps(folder))
        c.until("!$('l-connect-box').hidden", what="the tools to connect")
        out = c.js("""(() => { const b = $('l-connect-box');
            const r = {text: b.innerText, ticks: b.querySelectorAll('input[type=checkbox]').length,
                       buttons: [...b.querySelectorAll('button')].map(x => x.textContent)};
            $('launch').close(); return r; })()""")
        self.assertIn("Connect " + os.path.realpath(folder), out["text"])
        self.assertGreater(out["ticks"], 0)
        self.assertTrue({"Approve and connect", "Connect"} & set(out["buttons"]))
        self.assertIn("Cancel", out["buttons"])
        self.assertFalse(os.path.exists(os.path.join(folder, ".mcp.json")))  # nothing connected yet
        self.assertEqual(c.errors, [])

    def test_the_claude_code_plugin_card(self):
        # W11: the Machines panel's card installs crewchat's Claude Code plugin, moves the folders still holding
        # crewchat's files, or uninstalls it.
        c = self.chrome
        out = c.js("""(() => {
            const look = (p) => { const b = machPluginCard(p);
              return {text: b.innerText, buttons: [...b.querySelectorAll("button")].map(x => x.textContent)}; };
            return {off: look({on: false, folders: ["/a", "/b"], claude: true}),
                    on: look({on: true, folders: ["/a"], claude: true}), done: look({on: true, folders: [], claude: true})};
        })()""")
        self.assertIn("Install the plugin", out["off"]["buttons"])
        self.assertIn("(2)", out["off"]["text"])
        self.assertEqual(out["on"]["buttons"], ["Move them", "Uninstall"])
        self.assertIn("1 connected folder still has crewchat's files", out["on"]["text"])
        self.assertEqual(out["done"]["buttons"], ["Uninstall"])
        self.assertEqual(c.errors, [])

    def test_the_cloud_cards_firebase_agent_and_phone_from_anywhere(self):
        # W13: Set it up with an agent needs the database's location first; Phone from anywhere publishes, pairs, removes.
        c = self.chrome
        out = c.js("""(() => {
            const cloud = machCloudCard({firebase_locations: {"nam5": "United States (multi-region)"}, local: true});
            const options = [...cloud.querySelectorAll("#m-firebase-location option")].map(o => o.value);
            cloud.querySelector("#m-firebase-agent").click();
            mach.phones = {phones: [{id: "p1", name: "pixel", tag: "C", fp: "aaaa bbbb", seen: 0}], url: "https://x.firebaseapp.com/",
                           days: 7, published: "", build: "b1"};
            const phone = machPhoneCard({local: true});
            return {options, error: mach.error, buttons: [...phone.querySelectorAll("button")].map(b => b.textContent),
                    text: phone.innerText};
        })()""")
        self.assertEqual(out["options"], ["", "nam5"])
        self.assertIn("Pick where your database lives first", out["error"])  # nothing started without it
        self.assertEqual(out["buttons"], ["Publish the page", "Pair a phone", "Remove", "Remove and clear"])
        self.assertIn("pixel", out["text"])
        c.js("mach.error = ''; mach.phones = null")
        self.assertEqual(c.errors, [])

    def test_edit_tracking_in_the_project_panel(self):
        c = self.chrome
        c.js("openPanel('safety')")
        c.until("$('edits-note').textContent.startsWith('Kept')", what="edit tracking on (a software team)")
        self.assertEqual(c.js("$('edits-mode').value"), "auto")
        c.js("$('edits-mode').value = 'off'; $('edits-mode').dispatchEvent(new Event('change'))")
        c.until("$('edits-note').textContent === 'Not kept.'", what="edit tracking off")
        self.assertIn("Edit tracking: off", self.m.cli("edits"))
        c.js("$('edits-mode').value = 'auto'; $('edits-mode').dispatchEvent(new Event('change'))")
        c.until("$('edits-note').textContent.startsWith('Kept')", what="back to auto")
        self.assertEqual(c.errors, [])

    def test_mcp_servers_in_the_project_panel(self):
        c = self.chrome
        c.js("openPanel('integrations')")
        key = "Bearer e2e-key-0123456789abcdef"
        block = {"mcpServers": {"my-server": {"type": "http", "url": "http://localhost:9/mcp",
                                              "headers": {"Authorization": "Bearer <YOUR_API_KEY>"}}}}
        c.js("$('mcp-json').value = %s; $('mcp-add-json').click()" % json.dumps(json.dumps(block)))
        c.until("$('mcp-note').textContent.includes('<YOUR_API_KEY>')", what="the placeholder refused")
        block["mcpServers"]["my-server"]["headers"]["Authorization"] = key
        c.js("$('mcp-json').value = %s; $('mcp-add-json').click()" % json.dumps(json.dumps(block)))
        c.until("$('mcp-list').textContent.includes('my-server')", what="the server listed")
        self.assertNotIn("e2e-key", c.js("$('mcp-list').textContent"))  # a key never comes back to the page
        self.assertIn("key: Authorization", c.js("$('mcp-list').textContent"))
        listed = self.m.cli("mcp", "list")
        self.assertIn("my-server", listed)
        self.assertNotIn("e2e-key", listed)
        c.js("window.confirm = () => true; [...$('mcp-list').querySelectorAll('button')].find(b => b.textContent === 'Remove').click()")
        c.until("!$('mcp-list').textContent.includes('my-server')", what="the server removed")
        self.assertEqual(c.errors, [])

    def test_add_an_mcp_server_from_a_tool(self):
        # E4: a server set up by hand in Cursor is offered without its key, and once added reaches Claude Code too,
        # with its key.
        cursor = self.m.dir / "user-home" / ".cursor"
        cursor.mkdir(parents=True, exist_ok=True)
        (cursor / "mcp.json").write_text(json.dumps({"mcpServers": {"e2e-from-cursor": {
            "command": "npx", "args": ["-y", "e2e-mcp"], "env": {"E2E_TOKEN": "e2e-cursor-secret-0123"}}}}))
        self.addCleanup((cursor / "mcp.json").unlink)
        c = self.chrome
        c.js("openPanel('integrations')")
        c.js("$('mcp-from-tool').click()")
        c.until("$('mcp-found').textContent.includes('e2e-from-cursor')", what="the Cursor server offered")
        found = c.js("$('mcp-found').textContent")
        self.assertIn("in Cursor", found)
        self.assertIn("key: E2E_TOKEN", found)
        self.assertNotIn("e2e-cursor-secret", found)
        c.js("[...$('mcp-found').querySelectorAll('li')].find(li => li.textContent.includes('e2e-from-cursor'))"
             ".querySelector('button').click()")
        c.until("$('mcp-list').textContent.includes('e2e-from-cursor')", what="the server added")
        self.assertNotIn("e2e-cursor-secret", c.js("$('mcp-list').textContent"))
        entry = json.loads((self.m.folder / ".mcp.json").read_text())["mcpServers"]["e2e-from-cursor"]
        self.assertEqual(entry["env"], {"E2E_TOKEN": "e2e-cursor-secret-0123"})
        # A localhost address is this machine's: the box for the linked machines unticks itself.
        c.js("$('mcp-share').checked = true; $('mcp-where').value = 'http://localhost:9/mcp';"
             "$('mcp-where').dispatchEvent(new Event('input'))")
        self.assertFalse(c.js("$('mcp-share').checked"))
        c.js("$('mcp-where').value = ''; $('mcp-share').checked = true")
        c.js("window.confirm = () => true; [...$('mcp-list').querySelectorAll('li')].find(li => "
             "li.textContent.includes('e2e-from-cursor')).querySelectorAll('button')[2].click()")
        c.until("!$('mcp-list').textContent.includes('e2e-from-cursor')", what="the server removed")
        self.assertEqual(c.errors, [])

    def test_mirror_into_google_chat_in_the_project_panel(self):
        # W12: a space added, the project mirrored into it from the Google Chat card, and turned off again. A webhook
        # space without the Google sign-in only posts: the card says what is missing, and its button opens the sign-in
        # setup. No message is sent while it is on, so nothing goes to Google.
        c = self.chrome
        hook = "https://chat.googleapis.com/v1/spaces/AAAAe2e/messages?key=AIza" + "k" * 35 + "&token=" + "t" * 43
        self.m.owner.post("/api/gchat", {"op": "add", "title": "Team", "webhook": hook})
        self.addCleanup(self.m.owner.post, "/api/gchat", {"op": "remove", "title": "Team"})
        c.js("openPanel('integrations')")
        c.until("!$('gchat-mirror-box').hidden && $('gchat-mirror').options.length === 2", what="the mirror's choices")
        self.assertIn("leave this machine", c.js("$('gchat-mirror-note').textContent"))
        self.assertTrue(c.js("$('gchat-send').disabled"))
        c.js("$('gchat-mirror').value = 'Team'; $('gchat-mirror').dispatchEvent(new Event('change'))")
        c.until("$('gchat-mirror-note').textContent.startsWith('Posting only')", what="the mirror on, posting only")
        self.assertIn("Replies from Google Chat need sign-in with Google (read messages)",
                      c.js("$('gchat-mirror-note').textContent"))
        c.js("$('gchat-mirror-signin').click()")
        self.assertTrue(c.js("$('gchat-signin').open && document.activeElement === $('gchat-client')"))
        c.js("$('gchat-send').value = 'owner'; $('gchat-send').dispatchEvent(new Event('change'))")
        mirror = lambda: json.loads(self.m.owner.post("/api/gchat", {"op": "state"})[1])["mirror"]  # noqa: E731
        until = time.time() + 10
        while mirror()["send"] != "owner" and time.time() < until:  # the page's request is on its way
            time.sleep(0.1)
        self.assertEqual((mirror()["space"], mirror()["send"]), ("Team", "owner"))
        c.js("$('gchat-mirror').value = ''; $('gchat-mirror').dispatchEvent(new Event('change'))")
        c.until("$('gchat-send').disabled", what="the mirror off")
        self.assertIsNone(json.loads(self.m.owner.post("/api/gchat", {"op": "state"})[1])["mirror"])
        self.assertEqual(c.errors, [])

    def test_open_folder_for_the_projects_folders_and_an_agents_folder(self):
        c = self.chrome
        folder = json.loads(self.m.owner.post("/api/folders", {"op": "project"})[1])["folders"][0]["path"]
        self.assertTrue(folder.endswith("notes-app"))
        # The page's request is caught here: the server must not open a real file manager window during a test.
        c.js("window.__opened = []; window.__fetch = window.__fetch || window.fetch;"
             "window.fetch = (url, init) => String(url).includes('/api/open-folder')"
             " ? (window.__opened.push(JSON.parse(init.body)), Promise.resolve(new Response('{\"ok\": true}', { status: 200 })))"
             " : window.__fetch(url, init)")
        try:
            c.js("openPanel('folders')")
            c.until("[...document.querySelectorAll('#folders-list li')].some(li => li.innerText.includes(%s))" % json.dumps(folder),
                    what="the project's folder")
            c.js("document.querySelector('#folders-list button').click()")
            c.until("window.__opened.length === 1", what="the request")
            self.assertEqual(c.js("window.__opened"), [{"folder": folder}])
            c.until("$('folders-note').textContent.startsWith('Opened ')", what="its note")
            c.js("closePanel()")
            # The agent's panel offers its own folder (the one it is joined from), not any other.
            c.js(self.card() + ".click()")
            c.until("!!document.querySelector('#agent-pane-body button[aria-label^=\"Open notes-app\"]')", what="the agent's folder")
            self.assertIn(folder, c.js("$('agent-pane-body').innerText"))
            c.js("document.querySelector('#agent-pane-body button[aria-label^=\"Open notes-app\"]').click()")
            c.until("window.__opened.length === 2", what="the second request")
            self.assertEqual(c.js("window.__opened[1]"), {"folder": folder})
            c.until("$('agent-pane-body').querySelector('.panel-note').textContent.startsWith('Opened ')", what="its note")
            self.assertEqual(c.errors, [])
        finally:
            c.js("window.fetch = window.__fetch || window.fetch")

    def test_roles_in_the_panel(self):
        c = self.chrome
        c.js("openPanel('roles')")
        c.until("$('settings-body').innerText.includes('Software team') && $('settings-body').innerText.includes('Lead')",
                what="the roles")
        c.js("[...$('settings-body').querySelectorAll('button')].find(b => b.textContent === 'New role').click()")
        c.until("!!document.querySelector('#settings-body form.roleform')", what="the role form")
        c.js("""(() => { const f = document.querySelector('#settings-body form');
            f.querySelector('[aria-label=Name]').value = 'designer';
            f.querySelector('[aria-label=Label]').value = 'Designer';
            f.querySelector('textarea').value = 'You design the screens.';
            f.querySelector('[aria-label=Picture]').value = 'preset:palette';
            f.requestSubmit(); })()""")
        c.until("$('settings-title').textContent === 'Designer'", what="the new role's panel")
        self.assertIn("You design the screens.", c.js("$('settings-body').innerText"))
        c.until("state.roles.some(r => r.name === 'designer')", what="the role in the page's list")
        c.js("window.confirm = () => true")
        c.js("[...$('settings-body').querySelectorAll('button')].find(b => b.textContent === 'Delete').click()")
        c.until("$('settings-title').textContent === 'Crew & roles' && !$('settings-body').innerText.includes('Designer')",
                what="the role gone")
        c.until("!state.roles.some(r => r.name === 'designer')", what="the page's list to follow (the poll's boot tag)")
        self.assertEqual(c.errors, [])

    def test_a_role_under_another_in_the_panel(self):
        # Roles as a tree (roadmap H3): a role made under the lead, with the assign power, sits under it in the list.
        c = self.chrome
        c.js("openPanel('roles')")
        c.until("$('settings-body').innerText.includes('Software team')", what="the roles")
        c.js("[...$('settings-body').querySelectorAll('button')].find(b => b.textContent === 'New role').click()")
        c.until("!!document.querySelector('#settings-body form.roleform')", what="the role form")
        c.js("""(() => { const f = document.querySelector('#settings-body form');
            f.querySelector('[aria-label=Name]').value = 'ux-lead';
            f.querySelector('[aria-label=Label]').value = 'UX lead';
            f.querySelector('textarea').value = 'You lead the designers.';
            f.querySelector('[aria-label="Reports to"]').value = 'lead';
            [...f.querySelectorAll('.powers label')].find(l => l.textContent.includes('Hands out work'))
              .querySelector('input').checked = true;
            f.requestSubmit(); })()""")
        c.until("$('settings-title').textContent === 'UX lead'", what="the new role's panel")
        text = c.js("$('settings-body').innerText")
        self.assertIn("Lead", text.split("Reports to")[1][:40])
        self.assertIn("Hands out work", text)
        c.js("openPanel('roles')")
        c.until("[...document.querySelectorAll('.rolelist li.under')].some(li => li.innerText.includes('UX lead'))",
                what="the role under the lead")
        order = c.js("[...document.querySelectorAll('.rolelist li')].map(li => li.innerText.split('\\n')[0])")
        self.assertEqual(order[0], "Lead")
        c.js("openPanel('role', 'ux-lead')")
        c.until("$('settings-title').textContent === 'UX lead'", what="its panel")
        c.js("window.confirm = () => true")
        c.js("[...$('settings-body').querySelectorAll('button')].find(b => b.textContent === 'Delete').click()")
        c.until("$('settings-title').textContent === 'Crew & roles' && !$('settings-body').innerText.includes('UX lead')",
                what="the role gone")
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

    def test_reply_keeps_clear_of_a_messages_pills_on_a_phone(self):
        # At phone width a task update's pill ("#11 done") ran under the Reply button at the top right.
        c = self.chrome
        mid = self.m.owner.task("all", "Build the theme switch")
        try:
            self.m.cli("role", self.agent.name, "developer")
            self.agent.call("hub_task", action="take", id=mid)
            self.agent.call("hub_task", action="update", id=mid, status="review", note="Passes on Android 14 and 12")
            c.size(390, 844, mobile=True)
            c.until("innerWidth === 390 && [...document.querySelectorAll('#log .msg .meta .pill')].length > 0",
                    what="a phone, and the update's pill")
            overlaps = c.js("""[...document.querySelectorAll('#log .msg > .reply, #log .msg > .thread-btn')]
                .flatMap(btn => { const r = btn.getBoundingClientRect(), m = btn.parentElement;
                  return [...m.querySelectorAll(':scope > .meta > *')].filter(e => { const b = e.getBoundingClientRect();
                    return b.width && b.right > r.left && b.left < r.right && b.bottom > r.top && b.top < r.bottom; })
                  .map(e => e.textContent); })""")  # Reply, and Thread beside it (W7)
            self.assertEqual(overlaps, [])
            # Whatever the width, the meta's pills wrap before they reach the button.
            crowded = c.js("""[...document.querySelectorAll('#log .msg')].filter(m => m.querySelector(':scope > .reply'))
                .filter(m => { const meta = m.querySelector(':scope > .meta'), b = meta.getBoundingClientRect();
                  return b.right - parseFloat(getComputedStyle(meta).paddingRight) >
                    m.querySelector(':scope > .reply').getBoundingClientRect().left; })
                .map(m => m.querySelector(':scope > .meta').innerText)""")
            self.assertEqual(crowded, [])
            self.assertEqual(c.errors, [])
        finally:
            self.m.cli("role", self.agent.name, "none")
            self.m.cli("tasks", "remove", mid)  # nothing left in Needs you for the tests after
            c.size(1280, 900)

    def test_a_routines_links_read_as_separate_words(self):
        c = self.chrome
        self.m.cli("routines", "add", "jira-review", "Go through my Jira tickets", "--when", "weekdays 09:00")
        try:
            c.js("openPanel('routines')")
            c.until("$('routines-list').innerText.includes('jira-review')", what="the routine in Project settings")
            self.assertIn("Run now Pause Edit Remove", c.js("$('routines-list').innerText"))
            self.assertEqual(c.errors, [])
        finally:
            self.m.cli("routines", "remove", "jira-review")

    def test_a_routine_is_set_to_start_an_agent_and_the_daily_cap_from_the_page(self):
        # T4: on the Routines card the owner picks the agent a routine starts when nobody who could do it is here (a
        # tool, a folder and what it may do without asking, as New agent offers them) and the cap a day. Not run.
        c = self.chrome
        folder = json.loads(self.m.owner.post("/api/folders", {"op": "project"})[1])["folders"][0]["path"]
        try:
            c.js("openPanel('routines')")
            c.until("state.routineLaunch && $('routine-cap').value !== ''", what="the Routines card with its cap")
            tool = c.js("(state.routineLaunch.tools.find(t => !t.missing) || {}).id || ''")
            if not tool:
                self.skipTest("no agent tool is installed on this machine")
            c.js("$('routine-form-box').open = true; $('routine-name').value = 'nightly';"
                 "$('routine-what').value = 'Fix the flaky tests'; $('routine-when').value = 'every 2h'")
            c.js("$('routine-start-tool').value = %s; $('routine-start-tool').dispatchEvent(new Event('change'))"
                 % json.dumps(tool))
            self.assertFalse(c.js("$('routine-start-box').hidden"))
            perms = c.js("[...$('routine-start-permissions').options].map(o => o.value)")
            self.assertIn("all", perms)  # the owner's decision: any mode New agent offers
            c.js("$('routine-start-folder').value = %s; $('routine-start-permissions').value = 'all';"
                 "$('routine-save').click()" % json.dumps(folder))
            c.until("$('routines-list').innerText.includes('starts ')", what="the routine with the agent it starts")
            self.assertIn(os.path.basename(folder), c.js("$('routines-list').innerText"))
            routine = json.loads(self.m.owner.post("/api/routines", {"op": "view"})[1])["routines"][0]
            self.assertEqual((routine["start"]["tool"], routine["start"]["permissions"]), (tool, "all"))
            c.js("[...$('routines-list').querySelectorAll('button')].find(b => b.textContent === 'Edit').click()")
            self.assertEqual(c.js("$('routine-start-tool').value"), tool)  # Edit shows what it starts
            c.js("$('routine-cap').value = '4'; $('routine-cap').dispatchEvent(new Event('change'))")
            c.until("$('routine-cap-note').textContent.includes('of 4')", what="the new cap")
            self.assertEqual(c.errors, [])
        finally:
            self.m.owner.post("/api/routines", {"op": "remove", "name": "nightly"})
            self.m.owner.post("/api/routines", {"op": "cap", "cap": 10})

    def test_a_blocked_tasks_answer_shows_in_its_history(self):
        c = self.chrome
        mid = self.m.owner.task("all", "Pick the default theme")
        self.agent.call("hub_task", action="take", id=mid)
        self.agent.call("hub_task", action="update", id=mid, status="blocked", note="Light or dark by default?")
        c.until("state.needs.some(n => n.task === %s)" % json.dumps(mid), what="Needs you")
        c.js("openPanel('task', %s)" % json.dumps(mid))
        c.until("!!document.querySelector('#panel .needs-card textarea')", what="the answer box")
        self.assertEqual(c.js("getComputedStyle(document.querySelector('#panel .needs-card')).borderLeftColor"),
                         c.js("(() => { const i = document.createElement('i'); i.style.color = 'var(--err)'; document.body.append(i);"
                              " const v = getComputedStyle(i).color; i.remove(); return v; })()"))  # its red edge
        c.js("const t = document.querySelector('#panel .needs-card textarea'); t.value = 'Follow the system setting.'")
        c.js("[...document.querySelectorAll('#panel .needs-card button')].find(b => b.textContent === 'Send').click()")
        c.until("$('panel-body').innerText.includes('You: Re #%s: Follow the system setting.')" % mid,
                what="the answer in the task's history")
        c.until("!state.needs.some(n => n.key === 'blocked:' + %s)" % json.dumps(mid),
                what="the answered task to leave Needs you")  # R8: answered by the reply's re field
        self.assertEqual(c.errors, [])

    def test_review_answered_from_the_chat_box_leaves_needs_you(self):
        c = self.chrome
        mid = self.m.owner.task("all", "Write the upgrade notes")
        self.agent.call("hub_task", action="take", id=mid)
        self.agent.call("hub_task", action="update", id=mid, status="review", note="Ready for a look")
        c.until("state.needs.some(n => n.key === 'review:' + %s)" % json.dumps(mid), what="the review in Needs you")
        c.js("postJSON('/api/send', { to: %s, text: 'Re #' + %s + ': looks fine, go ahead', kind: 'msg', files: [] })"
             % (json.dumps(self.agent.name), json.dumps(mid)))  # as the chat box sends what the owner typed
        c.until("!state.needs.some(n => n.key === 'review:' + %s)" % json.dumps(mid), what="the review to leave Needs you")
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
        c.js("openPanel('notifications')")  # Settings › Notifications (W16)
        c.js("document.querySelector('[data-notify=all]').click()")
        c.until("Notification.permission === 'granted' && !$('bell').classList.contains('off')", what="the bell on")
        self.assertEqual(c.js("$('you-note').textContent"), "notifications on")
        c.js("closePanel()")
        tid = c.js("postJSON('/api/task', {op: 'add', title: 'Pick a colour for the login button'}).then(r => r.id)")
        self.agent.call("hub_task", action="take", id=tid)
        c.js("Object.defineProperty(document, 'hidden', {value: true, configurable: true})")  # the page goes to the back
        self.agent.call("hub_task", action="update", id=tid, status="blocked", note="Blue or green?")
        c.until("$('needs-list').textContent.includes('Blue or green?')", what="the item")
        c.until("window.notes.some(n => n[0].includes('Needs you') && n[1].includes('Blue or green?'))", what="a notification")
        self.assertGreaterEqual(int(c.js("$('needs-count').textContent")), 1)  # counted in the sidebar
        self.assertTrue(c.js("[...document.querySelectorAll('#tabs .tab')].some(t => t.classList.contains('needs'))"))
        c.js("delete document.hidden")
        c.click("#view-needs")
        # The item opens its task, with the question and a way to answer.
        c.js("[...document.querySelectorAll('#needs-list .need')].find(b => b.innerText.includes('Blue or green?')).click()")
        c.until("!!document.querySelector('#panel-body .needs-card textarea')", what="the answer box")
        c.js("""(() => { const t = document.querySelector('#panel-body .needs-card textarea'); t.value = 'Green, please';
                  [...document.querySelectorAll('#panel-body .needs-card button')].find(b => b.textContent === 'Send').click(); })()""")
        self.assertIn("Re #%s: Green, please" % tid, eventually(self.agent.stop_hook, what="the agent to get the answer"))
        self.agent.call("hub_task", action="update", id=tid, status="in_progress", note="Green it is")
        c.until("!$('needs-list').textContent.includes('Blue or green?')", what="the item to go")
        c.click("#view-chat")
        # A question to you is read once the page is in front, and stays in Needs you until answered or dismissed
        # (Mac retest, 12 Nov: the chat scrolled it into view, marked it read, and it left Needs you unanswered).
        self.agent.call("hub_send", to="Owner", text="Done with the button?", ask=True)
        c.until("$('log').innerText.includes('Done with the button?')", what="the message on the page")
        seq = c.js("state.needs.find(n => n.kind === 'message').seq")
        c.js("$('log').scrollTop = $('log').scrollHeight; markRead()")
        c.until("state.readPosted >= %d" % seq, what="read while in front")
        time.sleep(1)
        self.assertTrue(c.js("state.needs.some(n => n.kind === 'message')"))  # read is not answered
        item = "[...document.querySelectorAll('#needs-list .need')].find(n => n.textContent.includes('Done with the button?'))"
        c.js("[...%s.querySelectorAll('button')].find(b => b.textContent === 'Dismiss').click()" % item)
        c.until("!state.needs.some(n => n.kind === 'message')", what="dismissed")
    def test_the_park_after_field_shows_what_is_saved_and_says_when_it_is_refused(self):
        # From the e2e retest of C11 (11 Nov 2026): 1 typed into "Park after (min)" was not kept while the field showed
        # it, and 5 typed next was saved while the field still showed 1. The field shows what the server keeps.
        c = self.chrome
        self.addCleanup(lambda: c.js("postJSON('/api/roles', {op: 'crew', park: 30}).then(() => true)"))
        c.js("openPanel('role', 'lead')")
        c.until("!!$('crew-park')", what="the lead's park field")
        park = "postJSON('/api/roles', {op: 'list'}).then(r => r.crew.park)"
        def enter(value):
            c.js("(() => { const f = $('crew-park'); f.focus(); f.value = %s; f.dispatchEvent(new Event('change')); })()"
                 % json.dumps(value))
        enter("1")
        c.until("$('crew-note').textContent === 'Saved.'", what="saved")
        self.assertEqual(c.js(park), 1)
        self.assertEqual(c.js("$('crew-park').value"), "1")
        self.assertEqual(c.js("$('crew-park').step"), "1")  # any minute is allowed: 1 is not marked invalid
        self.assertTrue(c.js("$('crew-park').validity.valid"))
        enter("5")
        c.until("$('crew-note').textContent === 'Saved.'", what="saved again")
        c.js("$('crew-park').blur(); renderPanel(true)")  # drawn again, as the page's poll does
        c.until("$('crew-park').value === '5'", what="the field to show 5")
        self.assertEqual(c.js(park), 5)
        enter("99999")  # more than a day: refused, and the field goes back to what is kept
        c.until("$('crew-note').textContent.startsWith('Not saved')", what="the refusal said")
        self.assertIn("Still 5", c.js("$('crew-note').textContent"))
        self.assertEqual(c.js("$('crew-park').value"), "5")
        self.assertEqual(c.js(park), 5)
        enter("")  # emptied: nothing is sent, and the field shows the saved value
        c.until("$('crew-note').textContent.includes('needs a number')", what="the empty field said")
        self.assertEqual(c.js("$('crew-park').value"), "5")
        self.assertEqual(c.errors, [])

    def test_parked_tasks_are_answered_on_their_card_in_needs_you(self):
        # From the e2e retest of C11: a Parked card had no reply box until its task's panel was opened.
        c = self.chrome
        tid = c.js("postJSON('/api/task', {op: 'add', title: 'Rotate the keys'}).then(r => r.id)")
        self.agent.call("hub_task", action="take", id=tid)
        self.agent.call("hub_task", action="update", id=tid, status="blocked", note="Staging or prod vault?")
        c.js("postJSON('/api/task', {op: 'park', task: %s})" % json.dumps(tid))
        c.click("#view-needs")
        card = "[...document.querySelectorAll('#needs-list .need')].find(n => n.innerText.includes('Staging or prod vault?'))"
        c.until("(%s || {}).dataset?.key === 'parked:%s'" % (card, tid), what="the parked card")
        self.assertTrue(c.js("!!%s.querySelector('textarea')" % card), "the reply box is on the card")
        c.js("(() => { const n = %s; n.querySelector('textarea').value = 'Staging first';"
             " [...n.querySelectorAll('button')].find(b => b.textContent === 'Send').click(); })()" % card)
        self.assertIn("Re #%s: Staging first" % tid, eventually(self.agent.stop_hook, what="the agent to get the answer"))
        c.until("!$('needs-list').innerText.includes('Staging or prod vault?')", what="the card to go")
        self.agent.call("hub_task", action="update", id=tid, status="done", note="Rotated")
        c.click("#view-chat")
        self.assertEqual(c.errors, [])

    def test_empty_messages_are_not_drawn(self):
        # An empty record (no words, no files) would be a name over an empty body.
        c = self.chrome
        shown = "document.querySelectorAll('#log .msg, #log .sys').length"
        before = c.js(shown)
        c.js("addMessages([{id: 'x-empty', seq: state.after, ts: Date.now() / 1000, from: %s, to: 'all', kind: 'msg', text: '  '}])"
             % json.dumps(self.agent.name))
        self.assertEqual(c.js(shown), before)
        self.assertFalse(c.js("!!document.querySelector('#log [data-id=\"x-empty\"]')"))

    def test_the_side_panel_lies_over_the_chat_in_a_narrow_window(self):
        # From the e2e retest: at about 900px with a task's panel open, the chat was crushed to ~200px and its header
        # wrapped a word a line. Between a phone and a wide screen the panel lies over the chat instead.
        c = self.chrome
        tid = c.js("postJSON('/api/task', {op: 'add', title: 'A task to open'}).then(r => r.id)")
        self.addCleanup(c.size, 1280, 900)
        c.size(900, 800)
        c.js("showView('chat'); openPanel('task', %s)" % json.dumps(tid))
        c.until("document.body.classList.contains('panel-open') && !$('panel').hidden", what="the task's panel")
        space = c.js("$('space').getBoundingClientRect().width")
        self.assertGreater(space, 500, "the chat keeps its width")
        self.assertEqual(c.js("getComputedStyle($('side')).position"), "fixed")
        self.assertGreater(c.js("$('side').getBoundingClientRect().width"), 250)
        line = c.js("parseFloat(getComputedStyle($('space-meta')).lineHeight) || 20")
        self.assertLess(c.js("$('space-meta').getBoundingClientRect().height"), line * 1.6, "the header's line does not wrap")
        c.size(1280, 900)
        c.until("getComputedStyle($('side')).position !== 'fixed'", what="a column again on a wide screen")
        c.js("postJSON('/api/task', {op: 'remove', task: %s})" % json.dumps(tid))
        self.assertEqual(c.errors, [])

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

    def test_a_new_ask_draws_the_eye_until_seen_and_is_answered_in_the_chat(self):
        # Owner, 12 Nov 2026: what lands in Needs you draws the eye until it is seen, and an agent's ask shows in the
        # chat too, answered there, the same item as in Needs you.
        c = self.chrome
        c.until("!!%s" % self.card(), what="the agent")
        c.js("document.hasFocus = () => true; showView('tasks')")  # in front, on the board: the chat is not in view
        self.addCleanup(c.js, "delete document.hasFocus; showView('chat'); localStorage.removeItem('crewchat-chime'); showChime()")
        c.js("openPanel('notifications')")
        c.js("document.querySelector('[data-chime=on]').click()")  # the chime, on for this browser
        self.assertEqual(c.js("document.querySelector('[data-chime=on]').getAttribute('aria-pressed')"), "true")
        c.js("closePanel(); chimeAudio = null; window.chimes = 0; navigator.vibrate = () => { window.chimes++; return true; }")
        out = {}
        t = threading.Thread(target=lambda: out.update(text=self.agent.hook(
            "permission", extra={"tool_name": "Bash", "tool_input": {"command": "rm -rf build/cache"}})))
        t.start()
        chat = "[...document.querySelectorAll('#chat-needs .need-card')].find(e => e.innerText.includes('rm -rf build/cache'))"
        c.until("!!" + chat, what="the ask in the chat")
        self.assertEqual(c.js("window.chimes"), 1)  # one buzz for the new item
        # Not seen yet: Needs you pulses in the sidebar, and the card has a ring.
        self.assertTrue(c.js("document.body.classList.contains('needs-new')"))
        self.assertEqual(c.js("getComputedStyle($('view-needs')).animationName"), "need-pulse")
        self.assertTrue(c.js(chat + ".classList.contains('need-new')"))
        self.assertIn("ASKS PERMISSION", c.js(chat + ".querySelector('.k').innerText").upper())
        self.assertEqual(c.js("$('log').lastElementChild.id"), "chat-needs")
        # With reduced motion the ring stays and nothing moves.
        c.call("Emulation.setEmulatedMedia", features=[{"name": "prefers-reduced-motion", "value": "reduce"}])
        self.assertEqual(c.js("getComputedStyle($('view-needs')).animationName"), "none")
        self.assertEqual(c.js("getComputedStyle($('view-needs')).outlineStyle"), "solid")
        c.call("Emulation.setEmulatedMedia", features=[])
        # A message that comes now goes before the ask, which stays after the newest message.
        self.agent.call("hub_send", to="all", text="Clearing the build cache first")
        c.until("$('log').innerText.includes('Clearing the build cache first')", what="the message")
        self.assertEqual(c.js("$('log').lastElementChild.id"), "chat-needs")
        self.assertTrue(c.js("document.body.classList.contains('needs-new')"))  # still not seen
        # Seen once the chat shows it with the page in front: the pulse stops.
        c.js("showView('chat'); $('log').scrollTop = $('log').scrollHeight")
        c.until("!document.body.classList.contains('needs-new')", what="seen in the chat")
        self.assertFalse(c.js(chat + ".classList.contains('need-new')"))
        self.assertEqual(c.js("getComputedStyle($('view-needs')).animationName"), "none")
        # Answered in the chat: the agent gets it, and the item leaves the chat and Needs you.
        c.js(chat + ".querySelector('button:not(.ghost)').click()")
        t.join(30)
        self.assertEqual(json.loads(out["text"])["hookSpecificOutput"]["decision"], {"behavior": "allow"})
        c.until("!document.querySelector('#chat-needs .need-card') && !$('needs-list').innerText.includes('rm -rf')",
                what="the ask to go from both")
        self.assertEqual(c.js("window.chimes"), 1)
        self.assertEqual(self.shown_though_hidden(), [])
        self.assertEqual(c.errors, [])

    def test_a_new_item_is_seen_when_needs_you_opens(self):
        c = self.chrome
        c.until("!!%s" % self.card(), what="the agent")
        c.js("document.hasFocus = () => true; showView('tasks')")
        self.addCleanup(c.js, "delete document.hasFocus; showView('chat')")
        tid = c.js("postJSON('/api/task', {op: 'add', title: 'Choose the font'}).then(r => r.id)")
        self.addCleanup(c.js, "postJSON('/api/task', {op: 'remove', task: %s})" % json.dumps(tid))
        self.agent.call("hub_task", action="take", id=tid)
        self.agent.call("hub_task", action="update", id=tid, status="blocked", note="Serif or sans?")
        c.until("$('needs-list').textContent.includes('Serif or sans?')", what="the item")
        card = "[...document.querySelectorAll('#needs-list .need-card')].find(e => e.innerText.includes('Serif or sans?'))"
        self.assertTrue(c.js("document.body.classList.contains('needs-new')"))
        self.assertTrue(c.js(card + ".classList.contains('need-new')"))
        self.assertIsNone(c.js("[...document.querySelectorAll('#chat-needs .need-card')].find(e => e.innerText.includes('Serif')) || null"))
        c.click("#view-needs")
        c.until("!document.body.classList.contains('needs-new')", what="seen")
        self.assertTrue(c.js(card + ".classList.contains('flash')"))  # it glows once
        self.assertFalse(c.js(card + ".classList.contains('need-new')"))
        self.agent.call("hub_task", action="update", id=tid, status="in_progress", note="Sans")
        self.assertEqual(c.errors, [])

    def test_what_needs_you_counts_on_the_tab_and_the_app_icon(self):
        # W4: the title and the tab's icon carry the count, and the installed app's badge follows it.
        c = self.chrome
        c.js("""window.badges = []; navigator.setAppBadge = n => { window.badges.push(n); return Promise.resolve(); };
                navigator.clearAppBadge = () => { window.badges.push(0); return Promise.resolve(); };""")
        tid = c.js("postJSON('/api/task', {op: 'add', title: 'Count me'}).then(r => r.id)")
        self.agent.call("hub_task", action="take", id=tid)
        self.agent.call("hub_task", action="update", id=tid, status="blocked", note="Left or right?")
        c.until("/^\\(\\d+\\) /.test(document.title)", what="the count in the title")
        n = int(c.js("state.needs.length"))
        self.assertTrue(c.js("document.title").startswith("(%d) " % n))
        c.until("window.badges.includes(%d)" % n, what="the app badge")
        c.until("document.querySelector('link[rel=\"icon\"][sizes]').href.startsWith('data:image/png')", what="the icon's count")
        self.agent.call("hub_task", action="update", id=tid, status="in_progress", note="Left")
        c.until("state.needs.length < %d" % n, what="the item to go")
        c.until("!document.title.startsWith('(%d) ')" % n, what="the title's count to drop")
        self.assertEqual(c.errors, [])

    def test_approve_another_tools_command_on_the_page(self):
        # W5: Approvals set in Project settings; a Cursor agent's risky command waits in Needs you; Deny with a note
        # reaches it, and Always allow keeps a rule that Project settings lists with Remove.
        c = self.chrome
        c.js("openPanel('safety')")
        c.until("$('approvals-mode').value === 'off'", what="Approvals off at first")
        c.js("$('approvals-mode').value = 'risky'; $('approvals-mode').dispatchEvent(new Event('change'))")
        eventually(lambda: c.js("postJSON('/api/approvals', {}).then(r => r.mode)") == "risky", what="Approvals on the server")
        cursor = self.m.agent(client="cursor")
        out = json.loads(cursor.hook("stop"))
        cursor.call("hub_link", key=re.search(r'key "([^"]+)"', out["followup_message"]).group(1))
        self.addCleanup(self.m.cli, "agent", "remove", cursor.whoami())  # the page's other tests count one agent

        def run(command):
            out = {}
            t = threading.Thread(target=lambda: out.update(text=cursor.hook("write", extra={"command": command})))
            t.start()
            c.until("$('needs-list').innerText.includes(%s)" % json.dumps(command), what="the command in Needs you")
            return t, out

        t, out = run("git push --force origin main")
        item = "[...document.querySelectorAll('#needs-list .need.ask')].find(e => e.innerText.includes('git push --force'))"
        c.js(item + ".querySelector('button.ghost:last-of-type').click()")  # Deny…
        # On its own card: an agent's question still waiting in Needs you has a reply box too.
        c.js(item + ".querySelector('textarea').value = 'Open a PR.'")
        c.js("[...%s.querySelectorAll('button')].find(b => b.textContent === 'Deny').click()" % item)
        t.join(60)
        said = json.loads(out["text"])
        self.assertEqual(said["permission"], "deny")
        self.assertIn("Open a PR.", said["agent_message"])
        t, out = run("git push origin feature")
        c.js("[...document.querySelectorAll('#needs-list .need button')].find(b => b.textContent === 'Always allow').click()")
        t.join(60)
        self.assertEqual(out["text"].strip(), "")  # allowed: the command goes on
        c.js("loadApprovals()")
        c.until("$('approvals-allow').innerText.includes('git push origin feature')", what="the rule in Project settings")
        c.js("[...$('approvals-allow').querySelectorAll('button')].find(b => b.textContent === 'Remove').click()")
        c.until("!$('approvals-allow').innerText.includes('git push origin feature')", what="the rule removed")
        c.js("$('approvals-mode').value = 'off'; $('approvals-mode').dispatchEvent(new Event('change'))")
        self.assertEqual(c.errors, [])

    def test_enter_searches_all_history_and_a_result_shows_its_message(self):
        # W7: the box filters what is shown as you type; Enter searches every message in every project.
        c = self.chrome
        self.agent.call("hub_send", to="all", text="The outbox drains through SyncWorker <b>now</b>")
        c.until("$('log').innerText.includes('drains through SyncWorker')", what="the message")
        c.type_into("#search", "syncworker drains")
        c.call("Input.dispatchKeyEvent", type="keyDown", key="Enter", code="Enter", windowsVirtualKeyCode=13)
        c.until("$('panel-title').textContent === 'Search: syncworker drains' && !!document.querySelector('.found .hit')",
                what="the results")
        hit = "document.querySelector('.found .hit')"
        self.assertEqual(c.js("[...%s.querySelectorAll('mark')].map(m => m.textContent).join(' ')" % hit), "drains SyncWorker")
        self.assertIsNone(c.js(hit + ".querySelector('b')"))  # the message's text, never its markup
        c.js(hit + ".click()")
        c.until("$('panel').hidden && !!document.querySelector('#log .msg.flash')", what="the message shown")
        self.assertEqual(c.errors, [])

    def test_reply_in_a_thread_and_the_agent_answers_there(self):
        # W7: Thread opens the panel; a reply there goes to the agent marked as in the thread, and the agent's answer
        # with hub_send's thread lands in the panel, not in the main chat, which counts the replies.
        c = self.chrome
        self.agent.call("hub_send", to="Owner", text="Which backoff for the outbox?")
        root = "[...document.querySelectorAll('#log .msg')].find(e => e.innerText.includes('Which backoff for the outbox?'))"
        c.until("!!" + root, what="the message")
        c.js(root + ".querySelector('.thread-btn').click()")
        c.until("$('panel-title').textContent === 'Thread' && !!$('thread-text')", what="the Thread panel")
        c.js("$('thread-text').value = 'Cap retries at 5.'")
        c.js("[...$('panel-body').querySelectorAll('button')].find(b => b.textContent === 'Reply in thread').click()")
        said = eventually(lambda: [l for l in self.agent.stop_hook().splitlines() if "Cap retries at 5." in l], what="the reply")
        self.assertIn("[in thread #", said[0])
        root_id = c.js(root + ".dataset.id")
        self.agent.call("hub_send", to="Owner", text="Done, capped at 5.", thread=root_id)
        c.until("$('panel-body').innerText.includes('Done, capped at 5.')", what="the answer in the thread")
        c.until("%s.querySelector('.replies') && %s.querySelector('.replies .rep-count').textContent === '2 replies'" % (root, root),
                what="the count on the first message")
        self.assertFalse(c.js("[...document.querySelectorAll('#log .msg > .body')].some(e => e.innerText.includes('Done, capped at 5.'))"))
        c.js("closePanel()")
        self.assertEqual(c.errors, [])

    def test_a_reply_in_a_thread_says_who_replied_and_opens_roomy(self):
        # Owner, 12 Nov: "1 reply" did not say that Bea had answered, and the thread opened small in the corner under
        # an agent's settings. The first message says who replied, with the last reply's first line; the thread opens
        # with the side to itself, wide enough to read.
        c = self.chrome
        self.agent.call("hub_send", to="Owner", text="Shall I open the page on the laptop?")
        root = "[...document.querySelectorAll('#log .msg')].find(e => e.innerText.includes('Shall I open the page on the laptop?'))"
        c.until("!!" + root, what="the message")
        self.agent.call("hub_send", to="Owner", text="Opened it. Page title: Example Domain.", thread=c.js(root + ".dataset.id"))
        rep = root + ".querySelector('.replies')"
        c.until("!!" + rep, what="the replies line")
        self.assertEqual(c.js(rep + ".querySelector('.rep-count').textContent"), self.agent.name + " replied")
        self.assertEqual(c.js(rep + ".querySelector('.rep-peek').textContent"), "Opened it. Page title: Example Domain.")
        self.assertEqual(c.js(rep + ".querySelectorAll('.faces .avatar').length"), 1)
        try:
            c.size(1440, 900)
            c.js("openPanel('agent', %s)" % json.dumps(self.agent.name))
            c.until("document.body.classList.contains('agent-open')", what="the agent's settings")
            c.js(rep + ".click()")
            c.until("$('panel-title').textContent === 'Thread'", what="the thread")
            self.assertTrue(c.js("$('agent-pane').hidden"))  # the thread has the side to itself
            self.assertGreaterEqual(c.js("$('side').getBoundingClientRect().width"), 440)
            self.assertEqual(c.js("[...document.querySelectorAll('.thread-log .tmsg')].map(r => r.querySelector('b').textContent)"),
                             [self.agent.name, self.agent.name])
            self.assertIn("1 reply from " + self.agent.name, c.js("document.querySelector('.thread-log .tsplit').textContent"))
            c.js("closePanel()")
            self.assertFalse(c.js("document.body.classList.contains('thread-open')"))
        finally:
            c.size(1280, 900)
        self.assertEqual(c.errors, [])

    def test_a_question_that_needs_a_reply_shows_where_it_is(self):
        # W6: + → Needs a reply; the message's receipt follows it: sent, handed over, answered.
        c = self.chrome
        c.until("!!%s" % self.card(), what="the agent")
        c.js("$('plus').open = true; $('as-ask').click()")
        self.assertFalse(c.js("$('ask-chip').hidden"))
        c.type_into("#text", "@%s Ready to ship the outbox?" % self.agent.name)
        c.js("$('send').click()")
        node = "[...document.querySelectorAll('#log .msg')].find(e => e.innerText.includes('Ready to ship the outbox?'))"
        c.until("!!%s && %s.querySelector('.receipt').textContent === 'Needs a reply · sent'" % (node, node), what="sent")
        self.assertTrue(c.js("$('ask-chip').hidden"))  # one message only
        self.assertIn("Ready to ship the outbox?", self.agent.stop_hook())
        c.until("%s.querySelector('.receipt').textContent === 'Needs a reply · handed over'" % node, what="handed over")
        self.agent.call("hub_send", to="Owner", text="Yes, once CI is green.")
        c.until("%s.querySelector('.receipt').textContent === 'Answered'" % node, what="answered")
        self.assertEqual(c.errors, [])

    def test_an_agents_receipt_and_reply_count_sit_under_its_bubble(self):
        # An agent's Needs a reply receipt, and the reply count of its thread, line up under its bubble, left-aligned,
        # not squeezed into the avatar's narrow column a word or two per line.
        c = self.chrome
        self.agent.call("hub_send", to="Owner", text="Can I drop the old outbox table?", ask=True)
        node = "[...document.querySelectorAll('#log .msg')].find(e => e.innerText.includes('Can I drop the old outbox table?'))"
        c.until("!!%s && %s.querySelector('.receipt').textContent.startsWith('Needs a reply')" % (node, node), what="the ask")
        self.agent.call("hub_send", to="Owner", text="Context: it is empty since 1.2.", thread=c.js(node + ".dataset.id"))
        c.until("!!%s.querySelector('.replies')" % node, what="the reply count")
        try:
            for width, mobile in ((1280, False), (904, False), (390, True)):
                c.size(width, 844, mobile=mobile)
                box = c.js("(() => { const m = %s, r = (s) => m.querySelector(s).getBoundingClientRect();"
                           " const b = r('.body'), t = r('.receipt'), n = r('.replies');"
                           " return [b.left, t.left, t.height, n.left, n.height,"
                           " getComputedStyle(m.querySelector('.receipt')).textAlign]; })()" % node)
                body_left, rec_left, rec_height, rep_left, rep_height, align = box
                self.assertAlmostEqual(rec_left, body_left, delta=1, msg=width)
                self.assertLess(rec_height, 20, width)  # one line
                self.assertAlmostEqual(rep_left, body_left, delta=1, msg=width)
                self.assertLess(rep_height, 60, width)  # who replied, and the last reply's first line
                self.assertEqual(align, "left", width)
        finally:
            c.size(1280, 900)
        self.assertEqual(c.errors, [])

    def test_review_a_plan_on_the_page(self):
        # W3, W16 (B2): the card says what the plan is; Review plan opens it in full, where it reads as a plan (headings,
        # a list, code), its Markdown never becomes markup, and Ask for changes sends the owner's note back with the Deny.
        c = self.chrome
        c.until("!!%s" % self.card(), what="the agent")
        plan = ("## Offline queue\n\n1. Add `OutboxStore`\n2. Drain it from **SyncWorker**\n\n"
                "<img src=x onerror=window.pwned=1>")

        def ask():
            # The page drops the plan just answered at its next read: the card waited for is the new one, not that.
            c.until("!state.needs.some(n => n.plan)", what="the last plan to go")
            out = {}
            t = threading.Thread(target=lambda: out.update(text=self.agent.hook(
                "permission", extra={"tool_name": "ExitPlanMode", "tool_input": {"plan": plan}})))
            t.start()
            c.until("[...document.querySelectorAll('#needs-list .need')].some(n => n.textContent.includes('Offline queue'))",
                    what="the plan in Needs you")
            return t, out

        t, out = ask()
        item = "[...document.querySelectorAll('#needs-list .need')].find(n => n.textContent.includes('Offline queue'))"
        self.assertIn("2 steps", c.js(item + ".textContent"))  # the card: its title and size
        c.js("showView('needs'); [...%s.querySelectorAll('button')].find(b => b.textContent === 'Review plan').click()" % item)
        c.until("!$('review').hidden && $('review-title').textContent === %s" % json.dumps(self.agent.name + "'s plan"),
                what="the plan's review")
        self.assertEqual(c.js("$('view-needs').getAttribute('aria-current')"), "page")  # still in Needs you
        card = "document.querySelector('#review .plan')"
        self.assertEqual(c.js(card + ".querySelector('h4').textContent"), "Offline queue")
        self.assertEqual(c.js(card + ".querySelectorAll('ol li').length"), 2)
        self.assertEqual(c.js(card + ".querySelector('code').textContent"), "OutboxStore")
        self.assertIsNone(c.js(card + ".querySelector('img')"))  # the plan's text, never its markup
        c.js("document.querySelector('#review textarea').value = 'Cap retries at 5.'")
        c.js("[...document.querySelectorAll('#review button')].find(b => b.textContent === %s).click()"
             % json.dumps("Send back to " + self.agent.name))
        t.join(30)
        decision = json.loads(out["text"])["hookSpecificOutput"]["decision"]
        self.assertEqual(decision["behavior"], "deny")
        self.assertIn("Cap retries at 5.", decision["message"])
        c.until("!%s && $('review').hidden" % item, what="the plan to go, and back to Needs you")

        t, out = ask()
        c.js("openReview(state.needs.find(n => n.plan).key)")
        c.until("!$('review').hidden && !!document.querySelector('#review .plan')", what="the review")
        c.js("[...document.querySelectorAll('#review button')].find(b => b.textContent === 'Approve, and let it edit without asking').click()")
        t.join(30)
        decision = json.loads(out["text"])["hookSpecificOutput"]["decision"]
        self.assertEqual(decision["updatedPermissions"][0]["mode"], "acceptEdits")

        t, out = ask()  # the A key approves, asking before each edit
        c.js("openReview(state.needs.find(n => n.plan).key); document.activeElement.blur()")
        c.until("!!document.querySelector('#review .plan')", what="the review")
        c.js("document.dispatchEvent(new KeyboardEvent('keydown', {key: 'a'}))")
        t.join(30)
        decision = json.loads(out["text"])["hookSpecificOutput"]["decision"]
        self.assertEqual(decision["behavior"], "allow")
        self.assertNotIn("updatedPermissions", decision)
        self.assertIsNone(c.js("window.pwned"))
        c.js("showView('chat')")
        self.assertEqual(c.errors, [])

    def test_a_message_to_you_is_answered_on_its_card(self):
        # W16 (B1): Needs you shows a message to you as a card with a reply box; Send writes to its sender, and the
        # card goes, the message read.
        c = self.chrome
        c.until("!!%s" % self.card(), what="the agent")
        c.js("Object.defineProperty(document, 'hidden', {value: true, configurable: true})")  # unread while away
        self.addCleanup(c.js, "delete document.hidden; showView('chat')")
        self.agent.call("hub_send", to="Owner", text="Tabs or spaces in the new module?", ask=True)
        item = "[...document.querySelectorAll('#needs-list .need')].find(n => n.textContent.includes('Tabs or spaces'))"
        c.until("!!" + item, what="the message in Needs you")
        c.js("delete document.hidden; showView('needs')")
        self.assertEqual(c.js(item + ".querySelector('.need-head .k').textContent"), "Question for you from " + self.agent.name)
        self.assertTrue(c.js("!!%s.querySelector('.need-head .avatar') && !!%s.querySelector('.need-head .age')" % (item, item)))
        c.js("%s.querySelector('textarea').value = 'Spaces, four'" % item)
        c.js("[...%s.querySelectorAll('button')].find(b => b.textContent === 'Send').click()" % item)
        self.assertIn("Spaces, four", eventually(self.agent.stop_hook, what="the reply to reach the agent"))
        c.until("!%s" % item, what="the card to go")
        self.assertEqual(c.errors, [])

    def test_the_browser_panel_and_a_browser_ask_in_needs_you(self):
        c = self.chrome
        c.until("!!%s" % self.card(), what="the agent")
        c.js("postJSON('/api/agent', {op: 'browser', agent: %s, mode: 'on'})" % json.dumps(self.agent.name))
        out = {}
        call = {"tool_name": "mcp__claude-in-chrome__navigate",
                "tool_input": {"url": "https://jenkins.example.test/job/build"}}
        t = threading.Thread(target=lambda: out.update(text=self.agent.hook("browser", extra=call)))
        t.start()  # a new site: the agent's call waits on the owner
        c.until("$('needs-list').innerText.includes('jenkins.example.test')", what="the browser ask in Needs you")
        c.js("[...document.querySelectorAll('#needs-list .need.ask button')].find(b => b.textContent === 'Allow the site').click()")
        t.join(30)
        # allowed: the hook allows the call, so Claude Code does not ask about it again (one approval, 55eb490)
        self.assertEqual(json.loads(out["text"])["hookSpecificOutput"]["permissionDecision"], "allow")
        c.js("openPanel('browser')")
        c.until("$('settings-body').innerText.includes('jenkins.example.test')", what="the site in the Browser panel")
        self.assertIn(self.agent.name, c.js("$('settings-body').innerText"))  # it drives now
        c.js("[...$('settings-body').querySelectorAll('button')].find(b => b.textContent === 'Trust').click()")
        c.until("$('settings-body').innerText.includes('trusted')", what="the site trusted")
        c.js("[...$('settings-body').querySelectorAll('button')].find(b => b.textContent === 'Let go').click()")
        c.until("$('settings-body').innerText.includes('No agent is using the browser')", what="the browser let go")
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
        self.assertEqual(c.js(step), 0)  # the workspace first (U8)
        self.assertEqual(c.js("document.querySelector('#l-steps li.on').textContent"), "Workspace")
        self.assertEqual(c.js("[$('l-back').hidden, $('l-next').hidden, $('l-start').hidden]"), [True, False, False])
        self.assertFalse(c.js("$('l-start').disabled"))  # a folder and a tool: it can start from here
        c.js("$('l-template').value = 'qa'; $('l-template').dispatchEvent(new Event('change'))")
        self.assertEqual(c.js(step), 0)  # a template fills in the identity, wherever it is picked
        # A built-in role keeps its picture to itself: the new agent gets a face of its own ("").
        self.assertEqual(c.js("[$('l-role').value, $('l-picture').value]"), ["qa", ""])
        self.assertEqual(c.js("$('l-picture').selectedOptions[0].text"), "A new face, made for it")
        c.click("#l-next")
        c.click("#l-next")
        c.click("#l-next")
        self.assertEqual(c.js(step), 3)
        self.assertEqual(c.js("[$('l-back').hidden, $('l-next').hidden, $('l-start').hidden]"), [False, True, False])
        self.assertEqual(c.js("document.querySelector('#l-steps li.on').textContent"), "Briefing (optional)")
        heights = c.js("[0, 1, 2, 3].map(n => (showStep(n), $('launch').getBoundingClientRect().height))")
        self.assertEqual(len(set(heights)), 1, heights)  # one height, whatever the step
        try:
            # A laptop's window: every step fits, nothing scrolls. A small one: only the steps scroll, the buttons stay.
            scrolls = "[0, 1, 2, 3].map(n => (showStep(n), [$('launch'), document.querySelector('#launch .step-body')]" \
                      ".map(e => e.scrollHeight > e.clientHeight + 1)))"
            c.size(1568, 715)
            self.assertEqual(c.js(scrolls), [[False, False]] * 4)
            c.size(1280, 480)
            self.assertEqual(c.js(scrolls), [[False, True]] * 4)
            self.assertLessEqual(c.js("$('l-start').getBoundingClientRect().bottom"), 480)
        finally:
            c.size(1280, 900)
        c.js("showStep(3)")
        c.click("#l-back")
        self.assertEqual(c.js(step), 2)
        c.js("$('l-tool').value = 'claude'; $('l-tool').dispatchEvent(new Event('change'))")
        self.assertEqual(self.shown_though_hidden(), [])
        c.click("#l-cancel")
        self.assertEqual(c.errors, [])

    def test_use_the_browser_in_new_agent_is_sent(self):
        # The checkbox shared its id with the folder browser (l-browser), which came first: it never reached the
        # launch request or the readiness check.
        c = self.chrome
        c.click("#add-agent")
        c.until("$('launch').open && $('l-folder').options.length > 0", what="the dialog and its folders")
        c.js("""window.sent = []; window.realFetch = window.realFetch || window.fetch;
                window.fetch = (url, opts) => { window.sent.push([String(url), opts && opts.body || ""]);
                  if (String(url).includes('/api/launch') && opts && opts.method === 'POST')
                    return Promise.resolve(new Response(JSON.stringify({error: 'stopped by the test'}), {status: 400}));
                  return window.realFetch(url, opts); };""")
        c.js("$('l-template').value = 'qa'; $('l-template').dispatchEvent(new Event('change'))")
        c.click("#l-next")
        c.js("$('l-tool').value = 'claude'; $('l-tool').dispatchEvent(new Event('change'))")
        box = "[...document.querySelectorAll('#launch input[type=checkbox]')].find(i => i.closest('label') && " \
              "i.closest('label').textContent.includes('Use the browser'))"
        c.js(box + ".click()")
        c.until("window.sent.some(([u]) => u.includes('/api/tool-check') && u.includes('browser=1'))",
                what="the readiness check with the browser")
        c.click("#l-next")
        c.click("#l-start")
        c.until("window.sent.some(([u, b]) => u.includes('/api/launch') && b.includes('\"browser\":true'))",
                what="the launch request with the browser")
        c.js("window.fetch = window.realFetch")
        c.click("#l-cancel")

    def test_the_new_agent_form_has_no_advisor_box(self):
        c = self.chrome
        c.click("#add-agent")
        c.until("$('launch').open && $('l-folder').options.length > 0", what="the dialog and its folders")
        c.js("$('l-template').value = 'qa'; $('l-template').dispatchEvent(new Event('change'))")
        c.click("#l-next")
        c.js("$('l-tool').value = 'claude'; $('l-tool').dispatchEvent(new Event('change'))")
        self.assertEqual(c.js("[$('l-model'), $('l-advisor'), $('l-advisor-note')].map(e => e === null)"),
                         [False, True, True])  # the model stays; Claude Code's own advisor is gone
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
        self.agent.call("hub_send", to="Owner", text="Can you look at the build?", ask=True)
        c.until("$('needs-list').innerText.includes('Can you look at the build?')", what="the message in Needs you")
        c.js("""delete document.hidden; $('text').value = '';
                [...[...document.querySelectorAll('#needs-list .need')].find(b => b.textContent.includes('look at the build'))
                  .querySelectorAll('button')].find(b => b.textContent === 'Show in chat').click()""")
        self.assertEqual(c.js("$('text').value"), "@%s " % self.agent.name)
        self.assertTrue(c.js("[...document.querySelectorAll('#log .msg.flash')].some(m => m.innerText.includes('look at the build'))"))
        self.assertTrue(c.js("$('agent-pane').hidden"))
        c.js("$('text').value = ''")
        self.assertEqual(c.errors, [])

    def test_a_new_project_from_the_tab_menu(self):
        c = self.chrome
        c.js("$('tab-add').open = true")  # the switcher at the sidebar's top
        c.click("#new-project")
        c.until("$('project-dialog').open && $('p-pack').options.length > 3", what="the dialog and the packs")
        c.type_into("#p-name", "Cooking channel")
        c.js("$('p-pack').value = 'youtube'")
        c.js("$('project-form').requestSubmit()")
        c.until("location.search.includes('chat=cooking-channel') && $('project').textContent === 'Cooking channel'",
                what="the new project's page")
        c.js("$('tab-add').open = true")
        c.until("[...document.querySelectorAll('#tabs .tab')].length === 2", what="a row for each project")
        c.until("state.crew === 'general' && state.roles.some(r => r.title === 'Producer')", what="the YouTube pack")
        c.js("[...document.querySelectorAll('#tabs .tab')].find(t => t.textContent.includes('Demo')).click()")
        c.until("$('project').textContent === 'Demo' && state.agents.length > 0", what="back to Demo")
        self.assertEqual(c.errors, [])

    def test_close_a_tab_and_remove_a_project(self):
        c = self.chrome
        tabs = "[...document.querySelectorAll('#tabs .tab .pname')].map(t => t.textContent)"
        self.addCleanup(c.js, "$('tab-add').open = false")
        c.js("postJSON('/api/projects', {op: 'create', name: 'Scratch pad'})")
        c.open(c.js("location.href"))  # a new project shows on the next answer that changes; this page sees it now
        c.until("%s.includes('Scratch pad')" % tabs, what="a tab for the new project")
        # Close tab: off the bar, still in the + menu, and back on the bar once opened from there.
        c.js("[...document.querySelectorAll('#tabs .tab')].find(t => t.textContent.includes('Scratch')).querySelector('.tab-x').click()")
        c.until("$('close-dialog').open", what="the close dialog")
        self.assertEqual(c.js("$('close-title').textContent"), "Close Scratch pad?")
        c.js("$('close-form').requestSubmit()")
        c.until("!%s.includes('Scratch pad')" % tabs, what="the tab closed")
        self.assertIn("Scratch pad", c.js("$('tab-menu').textContent"))
        c.js("[...$('tab-menu').children].find(b => b.textContent === 'Scratch pad').click()")
        c.until("$('project').textContent === 'Scratch pad' && %s.includes('Scratch pad')" % tabs, what="opened again")
        # Remove: the first project cannot be; this one only once its name is typed, and then the page goes to Demo.
        c.js("document.querySelector('#tabs .tab:first-child .tab-x').click()")
        c.until("$('close-dialog').open")
        self.assertTrue(c.js("$('close-remove').hidden"))
        self.assertIn("first project", c.js("$('close-note').textContent"))
        c.click("#close-cancel")
        c.js("document.querySelector('#tabs .tab[aria-current=page] .tab-x').click()")
        c.until("$('close-dialog').open && !$('close-remove').hidden")
        c.click("#close-remove")
        self.assertFalse(c.js("$('remove-part').hidden"))
        self.assertTrue(c.js("$('close-tab').hidden"))
        self.assertEqual(self.shown_though_hidden(), [])
        c.type_into("#remove-name", "scratch")
        c.click("#close-remove")
        self.assertIn("Type Scratch pad", c.js("$('close-note').textContent"))
        self.assertIn("Scratch pad", c.js("%s.join()" % tabs))
        c.type_into("#remove-name", "scratch pad")
        c.js("$('close-form').requestSubmit()")  # Enter in the name box removes too
        c.until("$('project').textContent === 'Demo' && !%s.includes('Scratch pad')" % tabs, what="removed, back on Demo")
        self.assertNotIn("Scratch pad", c.js("$('tab-menu').textContent"))
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
            # Full screen, with no header over it: no room left for one, and nothing faded under it.
            style = "getComputedStyle($('analytics'))"
            self.assertEqual(c.js("[%s.paddingTop, %s.maskImage]" % (style, style)), ["22px", "none"])
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
            c.click("#view-chat")
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
        c.js("setLook('theme', 'classic-light')")  # the glass is Classic's (W16)
        self.addCleanup(c.js, "setLook('theme', '')")
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

    def test_an_agent_wears_a_face_of_its_own_and_the_dice_change_it(self):
        c = self.chrome
        img = "%s.querySelector('img')" % self.card()
        c.until("!!%s && %s.src.includes('/pictures/mix/')" % (img, img), what="its face on its card")
        c.until("%s.complete && %s.naturalWidth > 0" % (img, img), what="the face drawn")
        before = c.js("%s.src" % img)
        c.js(self.card() + ".click()")
        c.until("!$('agent-pane').hidden", what="the agent's settings")
        c.js("[...$('agent-pane').querySelectorAll('.picmenu button')].find(b => b.textContent.includes('New face')).click()")
        c.until("%s.src !== %s && %s.src.includes('/pictures/mix/')" % (img, json.dumps(before), img), what="a new face")
        c.js("closeAgent()")
        self.assertEqual(c.errors, [])

    def test_design_review_details(self):
        # What a design review of the page found, kept: menus readable over glass, the scroll edge under the header,
        # the board without a clipped shadow band, task actions on one line in a fixed order, the add form in two
        # rows, a role's text as paragraphs, and on a phone the header's room and inputs that do not zoom the page.
        c = self.chrome
        try:
            c.js("$('tab-add').open = true")
            self.assertGreaterEqual(c.js("parseFloat((getComputedStyle(document.querySelector('#tab-add .menu-list'))"
                                         ".backgroundColor.match(/[\\d.]+(?=\\)$)/) || [1])[0])"), 0.95)
            c.js("$('tab-add').open = false")
            self.assertIn("gradient", c.js("getComputedStyle($('log')).maskImage || getComputedStyle($('log')).webkitMaskImage"))
            mid = self.m.owner.task("all", "A task for the design review")
            self.m.owner.task("all", "One that waits on it, with a long enough title to need room in the list")
            c.js("showView('tasks'); $('as-board').click()")
            c.until("!!document.querySelector('#board .col')", what="the board")
            self.assertNotIn("30px", c.js("getComputedStyle(document.querySelector('#board .col')).boxShadow"))
            c.js("$('as-list').click(); openPanel('task', %s)" % json.dumps(mid))
            c.until("!$('panel').hidden && $('sheet').querySelectorAll('tbody .acts').length > 1", what="the list beside the panel")
            rows = c.js("[...$('sheet').querySelectorAll('tbody .acts')].map(a => [[...a.children].map(b => b.textContent || b.tagName),"
                        " [...a.children].map(b => b.getBoundingClientRect().top).sort((x, y) => x - y)"
                        ".filter((t, i, all) => i === 0 || t - all[i - 1] > 10).length])")  # a new line: lower by over 10px
            for labels, lines in rows:
                self.assertEqual(lines, 1, labels)  # one line, even beside the panel
                self.assertEqual(labels[:2], ["↑", "↓"], labels)  # the same order in every row, so they line up
            self.assertEqual(c.js("Math.round($('t-add').getBoundingClientRect().top)"),
                             c.js("Math.round($('t-title').getBoundingClientRect().top)"))  # Add beside the task
            c.js("openPanel('role', 'developer')")
            c.until("!!document.querySelector('#settings .prompt-text')", what="the role's text")
            self.assertNotRegex(c.js("document.querySelector('#settings .prompt-text').textContent"), r"\n[ \t]+\S")
            self.assertLessEqual(c.js("Math.max(...[...$('edits-mode').options].map(o => o.text.length))"), 30)
            c.js("closePanel(); showView('chat')")
            # A phone.
            c.size(375, 812, mobile=True)
            c.open(self.m.url + "/")
            c.until("!!%s" % self.card(), what="the page on a phone")
            self.assertLessEqual(c.js("$('search').getBoundingClientRect().width"), 44)  # a magnifier until tapped
            self.assertEqual(c.js("getComputedStyle($('text')).fontSize"), "16px")
            c.js("showView('tasks')")
            gap = c.js("$('tasks').firstElementChild.getBoundingClientRect().top - $('space-head').getBoundingClientRect().bottom")
            self.assertTrue(0 <= gap < 30, gap)  # right under the header, not 80px below it
            self.assertEqual(c.errors, [])
        finally:
            c.js("closePanel(); showView('chat')")
            c.size(1280, 900)
            c.open(self.m.url + "/")
            c.until("!!document.getElementById('log')")

    def test_phone_and_dark_mode(self):
        c = self.chrome
        try:
            c.size(375, 812, mobile=True, dark=True)
            c.open(self.m.url + "/")
            c.until("!!%s" % self.card(), what="the page on a phone")
            self.assertLessEqual(c.js("document.documentElement.scrollWidth"), 375)
            self.assertNotEqual(c.js("getComputedStyle(document.body).backgroundColor"), "rgb(255, 255, 255)")
            self.assertEqual(c.js("getComputedStyle($('tab-add')).display"), "none")  # a picker instead
            self.assertNotEqual(c.js("getComputedStyle($('projects')).display"), "none")
            # One header (U3): the project's name is the picker, in the bar with search.
            self.assertTrue(c.js("$('space-head').contains($('projects'))"))
            self.assertLessEqual(c.js("$('space-head').getBoundingClientRect().height"), 64)
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
            # Something that needs the owner: a tap on the header's count opens Needs you (U3, W16).
            stuck = self.m.owner.task("all", "Pick the default theme")
            self.agent.call("hub_task", action="take", id=stuck)
            self.agent.call("hub_task", action="update", id=stuck, status="blocked", note="Follow the system setting?")
            c.until("!$('needs-open').hidden && getComputedStyle($('needs-open')).display !== 'none'", what="the count")
            self.assertIn("Needs you:", c.js("$('needs-open').getAttribute('aria-label')"))
            c.click("#needs-open")
            c.until("!$('needs-you').hidden && document.activeElement === $('needs-you')", what="Needs you")
            self.assertIn("Pick the default theme", c.js("$('needs-list').textContent"))
            self.agent.call("hub_task", action="update", id=stuck, status="done", note="Follows the system")
            c.js("showView('chat'); setDrawer(false)")
            c.click("#rail-open")
            c.until("$('rail').getBoundingClientRect().left >= 0", what="the drawer to open")
            c.js(self.card() + ".click()")
            c.until("!$('agent-pane').hidden", what="the agent's settings")
            self.assertFalse(c.js("document.body.classList.contains('rail-open')"))  # a sheet over the page
            self.assertEqual(c.js("[$('side').getBoundingClientRect().left, $('side').getBoundingClientRect().width]"), [0, 375])
            c.js("closeAgent()")
            # The task list on a phone: every action on screen, nothing to scroll sideways for.
            mid = self.m.owner.task("all", "Add the dark colour scheme to app/src/main/ui/theme/Color.kt")  # long words
            self.agent.call("hub_task", action="take", id=mid)
            c.js("showView('tasks'); $('as-list').click()")
            c.until("state.sheet.some(r => r.id === %s && r.status === 'doing') && "
                    "$('sheet').querySelectorAll('tbody button').length > 0" % json.dumps(mid), what="the task list")
            right = c.js("Math.max(...[...$('sheet').querySelectorAll('button, select')].map(b => b.getBoundingClientRect().right))")
            self.assertLessEqual(right, 375)
            c.js("$('as-board').click(); showView('chat')")
            c.click("#bell")
            c.until("!$('settings').hidden", what="Settings")
            self.assertEqual(c.js("[$('settings').getBoundingClientRect().width, document.body.classList.contains('rail-open')]"),
                             [375, False])  # the whole screen; the drawer closed
            self.assertEqual(c.errors, [])
        finally:
            c.size(1280, 900)
            c.open(self.m.url + "/")
            c.until("!!document.getElementById('log')")


@unittest.skipUnless(websocket and os.path.exists(CHROME), "needs Google Chrome and websocket-client")
class NeedsYouEveryProject(Signed):
    """W16: Needs you from every project in one list, answered in place; on a machine of its own (a second project
    would change the other page tests' tabs)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        side = cls.m.dir / "side-gig"
        side.mkdir()
        subprocess.run(["git", "init", "-q", str(side)], check=True)
        cls.m.cli("start", "--no-service", "--no-open", cwd=side)
        cls.other = cls.m.agent(folder=side)
        cls.other.link()
        cls.other.stop_hook()

    def card(self):
        return "document.querySelector('#agents .agent[data-agent=%s]')" % json.dumps(self.agent.name)

    def test_one_list_for_every_project_each_item_answered_in_place(self):
        c = self.chrome
        text = "$('needs-list').innerText"
        cards = "[...document.querySelectorAll('#needs-list .need-card')]"
        try:
            self.agent.call("hub_send", to="Owner", text="Which colour for the button?", ask=True)
            self.other.call("hub_send", to="Owner", text="Shall I ship the side gig?", ask=True)
            c.open(self.m.url + "/")
            c.until("(state.projects || []).length === 2 && (state.needsElsewhere || []).length > 0", what="both projects")
            c.js("showView('needs')")
            c.until("%s.includes('Which colour')" % text, what="this project's item")
            self.assertNotIn("side gig", c.js(text))  # this project's list, as before
            self.assertFalse(c.js("$('needs-scope').hidden"))
            self.assertEqual(c.js("document.querySelector('#needs-scope [aria-pressed=true]').dataset.scope"), "here")
            c.click("#needs-scope [data-scope=all]")
            c.until("%s.includes('Shall I ship the side gig?')" % text, what="the other project's item")
            self.assertIn("Which colour", c.js(text))
            self.assertEqual(c.js("[...new Set(%s.map(x => x.querySelector('.need-project').textContent))].sort()" % cards),
                             ["Demo", "side-gig"])
            self.assertIn("2 projects", c.js("$('needs-sub').textContent"))
            self.assertEqual(c.js("$('needs-count').textContent"), "1")  # the sidebar still counts this project's
            # Answered on its card: the reply goes to the other project's agent, and the item leaves the list.
            c.js("const x = %s.find(x => x.innerText.includes('side gig')); x.querySelector('textarea').value = 'Yes, ship it'; "
                 "[...x.querySelectorAll('button')].find(b => b.textContent === 'Send').click()" % cards)
            c.until("!%s.includes('Shall I ship the side gig?')" % text, what="answered and gone")
            self.assertIn("Yes, ship it", self.other.stop_hook())
            self.assertIn("Which colour", c.js(text))
            # The choice is this browser's, and the phone has it too.
            self.other.call("hub_send", to="Owner", text="One more from the side gig", ask=True)
            c.size(390, 844, mobile=True)
            c.open(self.m.url + "/")
            c.until("!!%s" % self.card(), what="the page on a phone")
            c.js("document.querySelector('#tabbar [data-tab=needs]').click()")
            c.until("!$('needs-you').hidden && %s.includes('One more from the side gig')" % text, what="all projects on a phone")
            self.assertEqual(c.js("document.querySelector('#needs-scope [aria-pressed=true]').dataset.scope"), "all")
            self.assertLessEqual(c.js("document.documentElement.scrollWidth"), 390)
            self.assertEqual(self.shown_though_hidden(), [])
            c.click("#needs-scope [data-scope=here]")
            c.until("!%s.includes('side gig')" % text, what="this project's only again")
            self.assertEqual(c.errors, [])
        finally:
            c.js("localStorage.removeItem('crewchat-needs-scope')")
            c.size(1280, 900)
            c.open(self.m.url + "/")
            c.until("!!document.getElementById('log')")


@unittest.skipUnless(websocket and os.path.exists(CHROME), "needs Google Chrome and websocket-client")
class AdvisorOnThePage(Signed):
    """A2, on a machine of its own (its agents would change the other page tests' counts)."""

    def test_the_question_shows_the_conversation_for_the_advisor(self):
        c = self.chrome
        transcript = self.m.dir / "user-home" / ".claude" / "projects" / "notes-app" / "s.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "npm test"}}]}}) + "\n")
        self.agent.stop_hook(extra={"transcript_path": str(transcript)})  # its hook says where its conversation is
        advisor = self.m.agent()
        advisor.link()
        self.m.owner.role(advisor.name, "advisor")
        asked = {}
        thread = threading.Thread(target=lambda: asked.update(text=self.agent.call(
            "hub_ask", question="Is npm test enough here?", to=advisor.name)))
        thread.start()
        try:
            c.until("[...document.querySelectorAll('#log .msg')].some(m => m.innerText.includes('Is npm test enough here?')"
                    " && m.innerText.includes('Conversation, for the advisor'))", what="the question with its file")
            href = c.js("[...document.querySelectorAll('#log a.file')].find(a => a.innerText.includes("
                        "'Conversation, for the advisor')).getAttribute('href')")
            self.assertIn("conversation-%s.txt" % self.agent.name, href)
            opened = c.js("fetch(%s).then(r => r.status + ' ' + r.statusText).catch(e => String(e))" % json.dumps(href))
            self.assertTrue(opened.startswith("200"), opened)
            self.assertIn("tool Bash: npm test", c.js("fetch(%s).then(r => r.text())" % json.dumps(href)))
            self.assertEqual(c.errors, [])
        finally:
            ask_id = eventually(lambda: next((m["id"] for m in self.m.owner.messages() if m["kind"] == "ask"), None),
                                what="the question")
            advisor.call("hub_answer", id=ask_id, text="Yes.")
            thread.join(30)
        self.assertIn("Yes.", asked.get("text", ""))


@unittest.skipUnless(websocket and os.path.exists(CHROME), "needs Google Chrome and websocket-client")
class CommitsAndLeasesOnThePage(Signed):
    """W8 on a machine of its own, whose folder is a git repository: an agent's commit shows in its task's thread with
    Why; another agent's lease shows on its task with Release, and stops the first agent's commit, with Ask the holder
    and Release in Needs you."""
    PEOPLE = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        (cls.m.folder / "README.md").write_text("x\n")
        for args in (["init", "-q"], ["add", "README.md"], ["commit", "-qm", "one"]):
            subprocess.run(["git"] + args, cwd=cls.m.folder, env=dict(cls.m.env, **cls.PEOPLE), check=True,
                           capture_output=True)
        cls.m.cli("hooks", "refresh")  # what the server does at its next start: crewchat's git hooks in the folder

    def commit(self, agent, rel, message):
        (self.m.folder / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.m.folder / rel).write_text("%s\n" % time.time())
        env = dict(self.m.env, CREWCHAT_LINK_KEY=agent.key, **self.PEOPLE)
        subprocess.run(["git", "add", rel], cwd=self.m.folder, env=env, check=True)
        return subprocess.run(["git", "commit", "-qm", message], cwd=self.m.folder, env=env, capture_output=True,
                              text=True, timeout=60)

    def test_a_commit_in_its_thread_with_why_and_a_lease_released_from_the_page(self):
        c = self.chrome
        luca = self.agent
        task = re.search(r"#(\w+)", luca.call("hub_task", action="add", title="Offline queue for messages",
                                               take=True)).group(1)
        luca.call("hub_task", action="update", id=task, status="in_progress", note="A Room table, then a worker")
        done = self.commit(luca, "app/outbox.py", "Clear the outbox on logout")
        self.assertEqual(done.returncode, 0, done.stderr)
        root = "document.querySelector('#log .msg[data-id=\"%s\"]')" % task
        c.until("!!%s && %s.querySelector('.replies') && %s.querySelector('.replies .rep-count').textContent === %s"
                % (root, root, root, json.dumps(luca.name + " replied")), what="the commit counted in the task's thread")
        c.js(root + ".querySelector('.replies').click()")
        c.until("$('panel-title').textContent === 'Thread' && $('panel-body').innerText.includes('Clear the outbox on logout')",
                what="the commit in the thread")
        self.assertIn("for #" + task, c.js("$('panel-body').innerText"))
        c.js("$('panel-body').querySelector('.why-btn').click()")
        c.until("$('panel-title').textContent.startsWith('Why ') && $('panel-body').textContent.includes('Decisions')",
                what="the Why panel")
        why = c.js("$('panel-body').textContent")
        for part in ("Task #" + task, "Offline queue for messages", "A Room table, then a worker", "app/outbox.py"):
            self.assertIn(part, why)
        c.js("closePanel()")
        # Its work in the agent's panel beside the chat (W16, A1): the commit, a link to it in the chat.
        c.js("openPanel('agent', %s)" % json.dumps(luca.name))
        c.until("$('agent-pane-body').textContent.includes('Its work') && $('agent-pane-body').textContent.includes('Clear the outbox on logout')",
                what="the commit in the agent's panel")
        c.js("[...$('agent-pane-body').querySelectorAll('.work button')].find(b => b.textContent.includes('Clear the outbox')).click()")
        c.until("$('panel-title').textContent === 'Thread' && $('panel-body').innerText.includes('Clear the outbox on logout')",
                what="the commit in its task's thread")
        c.js("closePanel(); closeAgent()")

        ravi = self.m.agent()
        ravi.link()
        mine = re.search(r"#(\w+)", ravi.call("hub_task", action="add", title="Rename the outbox column", take=True)).group(1)
        self.assertIn("Leased app/**/*.py", ravi.call("hub_task", action="lease", areas="app/**/*.py", minutes=45))
        c.js("showView('tasks'); showBoard(true)")
        card = "document.querySelector('#board .tcard[data-task=\"%s\"]')" % mine
        c.until("!!%s && %s.innerText.includes('app/**/*.py leased to %s')" % (card, card, ravi.name),
                what="the lease on its task's card")
        stopped = self.commit(luca, "app/outbox.py", "Retry on logout")
        self.assertNotEqual(stopped.returncode, 0)
        self.assertIn("leased to " + ravi.name, stopped.stderr)
        item = "[...document.querySelectorAll('#needs-list .need')].find(n => n.textContent.includes('Commit stopped'))"
        c.until("!!(%s)" % item, what="the stopped commit in Needs you")
        self.assertIn("%s is blocked by %s's lease" % (luca.name, ravi.name), c.js("(%s).textContent" % item))
        c.js("[...(%s).querySelectorAll('button')].find(b => b.textContent === 'Ask %s').click()" % (item, ravi.name))
        asked = eventually(lambda: [l for l in ravi.stop_hook().splitlines() if "Can you end it?" in l], what="the ask")
        self.assertIn("app/**/*.py", asked[0])
        c.until("[...(%s).querySelectorAll('button')].some(b => b.textContent === 'Asked %s' && b.disabled)"
                % (item, ravi.name), what="Asked")
        c.js("openPanel('task', %s)" % json.dumps(mine))
        c.until("!!$('panel-body').querySelector('.lease-row')", what="the lease in the task's panel")
        c.js("[...$('panel-body').querySelectorAll('.lease-row button')].find(b => b.textContent === 'Release').click()")
        c.until("!(%s)" % item, what="Needs you emptied")
        c.js("closePanel()")
        self.assertIn("you may commit", luca.stop_hook())
        again = self.commit(luca, "app/outbox.py", "Retry on logout")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(c.errors, [])


class MobileBridge:
    """`crewchat device-proxy claude` in the agent's folder, as its tool would run it, with a stand-in for Mobile MCP
    (tests/fake_mobile_mcp.py, run by an `npx` of the test's own) whose devices are one real Android phone."""
    PHONE = "R58M123ABC"

    def __init__(self, machine):
        bin_dir = H.TMP / "fake-npx"
        bin_dir.mkdir(exist_ok=True)
        npx = bin_dir / "npx"
        npx.write_text("#!/bin/sh\nexec %s %s\n" % (sys.executable, H.ROOT / "tests" / "fake_mobile_mcp.py"))
        npx.chmod(0o755)
        env = dict(machine.env, PATH=str(bin_dir) + os.pathsep + os.environ.get("PATH", ""),
                   FAKE_DEVICES=json.dumps([{"id": self.PHONE, "name": "Galaxy", "platform": "android", "type": "real",
                                             "state": "online"}]))
        self.proc = subprocess.Popen(H.crewchat_command() + ["device-proxy", "claude"], cwd=str(machine.folder), env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.n = 0
        self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}})
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.proc.stdin.flush()

    def rpc(self, method, params):
        self.n += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()
        while True:
            msg = json.loads(self.proc.stdout.readline())
            if msg.get("id") == self.n:
                return msg

    def call(self, tool, **args):
        out = self.rpc("tools/call", {"name": tool, "arguments": dict(args, device=self.PHONE)})["result"]
        return out.get("isError", False), out["content"][0]["text"]

    def close(self):
        self.proc.stdin.close()
        self.proc.terminate()
        self.proc.wait(10)
        self.proc.stdout.close()


@unittest.skipUnless(websocket and os.path.exists(CHROME), "needs Google Chrome and websocket-client")
class MobileDevicesOnThePage(Signed):
    """D4: mobile devices from the owner's side, with the device proxy and a stand-in for Mobile MCP."""

    def test_turn_on_pick_a_phone_allow_an_app_and_see_it_in_the_panel(self):
        c, name = self.chrome, self.agent.name
        card = "document.querySelector('#agents .agent[data-agent=%s]')" % json.dumps(name)
        c.until("!!%s" % card, what="the agent")
        # 1. Mobile devices on, from the agent's card: its folder gets crewchat's "mobile" server.
        c.js(card + ".click()")
        c.until("$('agent-pane-body').innerText.includes('Mobile devices')", what="the agent's settings")
        c.js("[...$('agent-pane-body').querySelectorAll('button')].find(b => b.title === %s).click()"
             % json.dumps("Mobile devices for " + name))
        eventually(lambda: "mobile" in json.loads((self.m.folder / ".mcp.json").read_text())["mcpServers"],
                   what="the mobile server in the folder")
        c.js("closeAgent()")
        bridge = MobileBridge(self.m)
        self.addCleanup(bridge.close)
        # A real phone the owner has not let agents use: it asks in Needs you (owner, 11 Nov 2026), and Let agents use
        # it lets the call through and picks the phone in the Devices panel.
        out = {}
        t = threading.Thread(target=lambda: out.update(said=bridge.call("mobile_take_screenshot")))
        t.start()
        c.until("$('needs-list').textContent.includes(%s)" % json.dumps(bridge.PHONE), what="the phone ask in Needs you")
        c.js("[...document.querySelectorAll('#needs-list .need.ask button')].find(b => b.textContent === "
             "'Let agents use it').click()")
        t.join(60)
        self.assertFalse(out["said"][0])
        c.js("openPanel('devices')")
        c.until("$('settings-body').innerText.includes(%s)" % json.dumps(bridge.PHONE), what="the phone in the panel")
        c.until("$('settings-body').innerText.includes('Agents may use it')", what="the phone picked")
        # 2. Its first app on the phone asks in Needs you; 3. Allow it, and the call goes through.
        out = {}
        t = threading.Thread(target=lambda: out.update(said=bridge.call("mobile_launch_app",
                                                                        packageName="com.example.shop")))
        t.start()
        c.until("$('needs-list').textContent.includes('com.example.shop')", what="the app ask in Needs you")
        c.js("[...document.querySelectorAll('#needs-list .need.ask button')].find(b => b.textContent === 'Allow the app')"
             ".click()")
        t.join(60)
        self.assertEqual(out["said"], (False, "Launched app com.example.shop"))
        # 4. The panel shows who holds the phone and the app in front.
        c.js("loadDevices()")
        c.until("$('settings-body').innerText.includes('Used by %s') && $('settings-body').innerText.includes('in com.example.shop')"
                % name, what="the holder and the app in the panel")
        self.assertIn("com.example.shop", c.js("$('settings-body').innerText"))  # and the app is on the allowed list
        c.js("[...$('settings-body').querySelectorAll('button')].find(b => b.textContent === 'Let go').click()")
        c.until("!$('settings-body').innerText.includes('Used by')", what="the phone let go")
        self.assertEqual(c.errors, [])


@unittest.skipUnless(websocket and os.path.exists(CHROME), "needs Google Chrome and websocket-client")
class Machines(unittest.TestCase):
    """M1: two machines linked from their chat pages alone, then one removed."""

    def sign_in(self, m):
        code = m.cli("ui", "--print").split(": ")[1].split()[0]
        self.chrome.open(m.url + "/")
        self.chrome.until("location.pathname === '/login'")
        self.chrome.type_into("input[name=code]", code)
        self.chrome.js("document.querySelector('form').submit()")
        self.chrome.until("location.pathname === '/' && !!document.getElementById('log')", what="the chat page")
        self.chrome.js("window.confirm = () => true")  # Remove and Unlink ask first

    def open_panel(self):
        self.chrome.click('[data-panel="machines"]')
        self.chrome.until("!!document.querySelector('#settings:not([hidden]) .m-me')", what="Settings › Machines & devices")

    def machine_states(self):
        return self.chrome.js("[...document.querySelectorAll('.m-machine')].map(c => c.querySelector('.m-state').textContent)")

    def test_link_two_machines_from_the_page(self):
        a, b = H.Machine("panel-a"), H.Machine("panel-b")
        self.chrome = Chrome()
        try:
            for m in (a, b):
                m.start()
                m.cli("url", m.url)  # its address for the other (in real use: tailscale serve's)
            self.sign_in(a)
            self.open_panel()
            self.chrome.until("!!document.querySelector('#m-invite')", what="Invite on a machine not linked yet")
            self.chrome.click("#m-invite")
            command = self.chrome.until("(document.querySelector('#m-invite-command') || {}).textContent",
                                        what="the join command")
            self.assertTrue(command.startswith("crewchat peers join %s " % a.url), command)
            self.sign_in(b)
            self.open_panel()
            self.chrome.type_into("#m-join-text", command)
            self.chrome.click("#m-join")
            self.chrome.until("document.querySelectorAll('.m-machine').length === 1 && "
                              "document.querySelector('.m-machine .m-state').textContent === 'online'", timeout=30,
                              what="b's panel to show a online")
            self.sign_in(a)
            self.open_panel()
            self.chrome.until("document.querySelector('.m-machine .m-version') && "
                              "document.querySelector('.m-machine .m-state').textContent === 'online'", timeout=30,
                              what="a's panel to show b online, with its version")
            self.chrome.click(".m-machine .m-remove")
            self.chrome.until("document.querySelectorAll('.m-machine').length === 0", timeout=30, what="b removed")
            self.assertEqual(self.chrome.errors, [])
        finally:
            self.chrome.close()
            for m in (a, b):
                m.stop()


    def test_set_up_here_from_the_banner(self):
        # M2: a project made on one machine shows on the other's page, which sets it up there in a click.
        a, b = H.Machine("offer-a"), H.Machine("offer-b")
        self.chrome = Chrome()
        try:
            for m in (a, b):
                m.start()
            b.link_to(a)
            a.owner.post("/api/projects", {"op": "create", "name": "video-app", "pack": "software"})
            self.sign_in(b)
            self.chrome.until("!!document.querySelector('#offered:not([hidden]) .m-setup')", timeout=40,
                              what="b's page to offer video-app")
            self.assertIn("video-app", self.chrome.js("document.getElementById('offered').textContent"))
            self.chrome.click("#offered .m-setup")
            self.chrome.until("location.search.includes('chat=') && !!document.getElementById('log')", timeout=20,
                              what="the new project's page")
            self.chrome.until("document.getElementById('launch').open", what="the folder picker, to connect its folder")
            self.assertIn("video-app", self.chrome.js("[...document.getElementById('projects').options].map(o => o.text).join(' ')"))
            self.assertEqual(self.chrome.errors, [])
        finally:
            self.chrome.close()
            for m in (a, b):
                m.stop()


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
        c.until("[...document.querySelectorAll('#analytics .avatar img')].every((i) => i.complete && i.naturalWidth)",
                what="the avatars to load")  # they may still be loading when the view is ready
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
