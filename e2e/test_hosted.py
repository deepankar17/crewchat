"""The hosted page (W13) in Google Chrome (headless): a phone pairs with a machine, reads and sends, and allows a command.

    pip install websocket-client cryptography      (skipped without them, or without Chrome)

The page is the one crewchat publishes, with the browser test's stand-in for Firestore in place of Google's SDK: a
local server over the in-memory store cloud sync's own tests use. So the machine runs in this process, unlike the
rest of this suite. What it proves is that the page's Web Crypto and the machine's Python agree: the page checks the
machine's signed trust list and unwraps the account key, and the machine opens what the page sealed and accepts what
it signed.
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import harness as H
from harness import eventually

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("CREWCHAT_HOME", str(H.TMP / "hosted-unused"))

try:
    import websocket  # noqa: F401
    import cryptography  # noqa: F401
    import crewchat
    import crewchat_cloud as cloud
    from test_browser import CHROME, Chrome
    READY = os.path.exists(CHROME) or sys.platform != "darwin"
except ImportError:
    READY = False


def store_server(store):
    """The stand-in for Firestore the page talks to (hosted.js fakeStore): the store's calls, as JSON over POST, and the
    hosted page's own files."""
    files = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, body, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0].lstrip("/") or "index.html"
            if path not in files:
                return self.reply(404, b"")
            ctype = {"js": "text/javascript", "html": "text/html", "png": "image/png", "svg": "image/svg+xml"}.get(
                path.rsplit(".", 1)[-1], "application/json")
            self.reply(200, files[path], ctype + "; charset=utf-8" if "text" in ctype else ctype)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            op = self.path.rsplit("/", 1)[-1]
            try:
                if op == "get":
                    out = store.get(body["path"])
                elif op == "set":
                    data = {k: cloud.SERVER_TIME if v == {".sv": "time"} else v for k, v in body["data"].items()}
                    store.set(body["path"], data, merge=body.get("merge"))
                    out = {}
                elif op == "delete":
                    store.delete(body["path"])
                    out = {}
                elif op == "list":
                    out = store.list(body["coll"])
                else:
                    return self.reply(404, b"")
            except cloud.CloudError as e:
                return self.reply(403, str(e).encode())
            self.reply(200, json.dumps(out).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    base = "http://127.0.0.1:%d" % server.server_address[1]
    files.update(cloud.hosted_own_files(fake_store=base + "/store"))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, base


class Machine:
    """A machine on the account, in this process (as tests/test_cloud.py's)."""

    def __init__(self, store, name):
        self.root = Path(tempfile.mkdtemp(prefix="hosted-%s-" % name, dir=H.TMP))
        crewchat.save_config({"project": "Demo", "agent_names": "place"}, self.root)
        self.device = cloud.Device(self.root)
        self.device.create(name)
        self.account = cloud.Account(store, self.device)
        self.account.register()
        self.projects = crewchat.Projects(self.root)
        self.hub = self.projects.primary.hub
        self.sync = cloud.CloudSync(self.hub, store, self.device, log=lambda text: None).start()

    def has(self, text, sender=None):
        with self.hub.lock:
            return next((m for m in self.hub.messages if m["text"] == text and (sender is None or m["from"] == sender)), None)


@unittest.skipUnless(READY, "needs Chrome, websocket-client and cryptography")
class HostedPage(unittest.TestCase):
    def setUp(self):
        crewchat.user_home = lambda: H.TMP / "hosted-user"
        crewchat.Handler.log_message = lambda *args: None
        cloud.PAGE_EVERY = 0.5  # the machine's page snapshot follows quickly
        self.store = cloud.MemoryStore()
        self.mac = Machine(self.store, "mac")
        self.server, self.base = store_server(self.store)
        self.chrome = Chrome()
        self.chrome.size(390, 844, mobile=True)

    def tearDown(self):
        self.chrome.close()
        self.mac.sync.stop()
        self.server.shutdown()

    def test_a_phone_pairs_reads_sends_allows_and_unpairs(self):
        c, mac = self.chrome, self.mac
        agent = mac.hub.agent_for(mac.hub.open_session("mac", "claude"))
        code = mac.sync.start_pairing()["code"]
        c.open(self.base + "/#pair=" + code)
        c.until("document.getElementById('hosted-code') && document.getElementById('hosted-code').value === %s" % json.dumps(code),
                what="the pairing form, with the code from the address")
        self.assertEqual(c.js("location.hash"), "")  # the code is off the address bar
        c.type_into("#hosted-name", "pixel")
        c.click("#hosted-pair")
        c.until("document.getElementById('hosted').hidden", timeout=40, what="the phone to pair")
        phone_id = c.js("crewchatHosted.me.id")
        entry = mac.device.trusted(phone_id)
        self.assertEqual((entry["kind"], entry["name"]), ("phone", "pixel"))
        self.assertTrue(c.js("Object.values(crewchatHosted.keys).every(k => k instanceof CryptoKey && !k.extractable)"))
        self.assertFalse(c.js("crewchatHosted.me.pss.extractable || crewchatHosted.me.oaep.extractable"))

        mac.hub.send(agent, "all", "hello phone, from the mac")  # sealed by Python, opened by Web Crypto
        c.until("$('log').innerText.includes('hello phone, from the mac')", what="the machine's message on the phone")

        c.type_into("#text", "hello mac, from the phone")
        c.js("$('text').dispatchEvent(new Event('input'))")
        c.click("#send")
        got = eventually(lambda: mac.has("hello mac, from the phone", crewchat.OWNER), what="the phone's message on the mac")
        self.assertEqual(got["origin"], phone_id)

        out = {}

        def ask():
            with mac.hub.lock:
                out["ask"] = mac.hub.asks.wait_for("permission", agent, "rm -rf build", time.time() + 60, tool="Bash")
        t = threading.Thread(target=ask)
        t.start()
        c.until("$('needs-list').innerText.includes('rm -rf build')", timeout=30, what="the prompt in Needs you on the phone")
        c.js("[...document.querySelectorAll('#needs-list .need.ask')].find(e => e.innerText.includes('rm -rf build'))"
             ".querySelector('button:not(.ghost)').click()")
        t.join(30)
        self.assertEqual(out["ask"]["answer"], "allow")  # signed by the phone, checked by the mac
        c.until("$('hosted-bar').innerText.includes('Awake: mac')", what="the line saying which computer is awake")

        c.js("window.confirm = () => true")
        c.click("#hosted-unpair")
        eventually(lambda: mac.device.trusted(phone_id) is None, what="the phone to be removed")
        self.assertEqual(c.errors, [])


if __name__ == "__main__":
    unittest.main()
