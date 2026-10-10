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
