"""Cloud sync between machines: several hubs in one process, sharing an in-memory Firestore.

Needs the `cryptography` library; skipped without it. Google's Firestore library is not needed.
"""
import base64
import contextlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
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

    def test_shared_files_open_on_the_other_machine(self):
        # Roadmap Y4: a file up to 5 MB travels with its message, encrypted, in pieces under Firestore's limit.
        data = os.urandom(cloud.CLOUD_FILE_MAX)  # a 5 MB screenshot: ten pieces
        meta = self.a.hub.add_file("screenshot.png", data, "image/png")
        msg = self.a.hub.send(crewchat.OWNER, "all", "see the screenshot", files=[meta])
        wait_until(lambda: self.b.has("see the screenshot"), message="the message")
        wait_until(lambda: self.store.get("files/%s-9" % meta["id"]), message="the file's last piece")
        got, path = self.b.hub.open_file(meta["id"])
        self.assertEqual((got["name"], path.read_bytes()), ("screenshot.png", data))
        # Firestore holds ciphertext only: neither the bytes, nor the name, nor the message.
        stored = json.dumps(self.store.docs, default=str)
        self.assertNotIn("screenshot", stored)
        self.assertNotIn(base64.b64encode(data[:300]).decode()[:200], stored)
        self.assertEqual(self.store.get("files/%s-0" % meta["id"])["parts"], 10)
        self.assertTrue(all(len(json.dumps(d)) < 1024 * 1024 for k, d in self.store.docs.items() if k.startswith("files/")))
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
        wait_until(lambda: self.store.get("files/%s-0" % small["id"]), message="the notes in the cloud")
        doc = dict(self.store.get("files/%s-0" % small["id"]))
        doc["ct"] = doc["ct"][:-4] + ("AAAA" if not doc["ct"].endswith("AAAA") else "BBBB")
        self.store.set("files/%s-0" % small["id"], doc)
        with self.assertRaises(crewchat.HubError) as e:
            self.b.hub.open_file(small["id"])
        self.assertIn("cannot fetch that file from the cloud", str(e.exception))
        # Delivered everywhere: the pieces go with the messages.
        for m in (self.a, self.b):
            m.sync.marks["synced_to"] = time.time() + 60
            m.sync.housekeeping()
        self.a.sync.housekeeping()
        self.assertFalse([k for k in self.store.docs if k.startswith("files/")])
        self.assertTrue(msg)

    def test_on_a_paid_plan_the_owner_can_let_bigger_files_travel(self):
        for m in (self.a, self.b):
            m.sync.file_max = 7 * 1024 * 1024  # cloud_file_mb = 7
        data = os.urandom(6 * 1024 * 1024)
        meta = self.a.hub.add_file("recording.mov", data, "video/quicktime")
        self.a.hub.send(crewchat.OWNER, "all", "the recording", files=[meta])
        wait_until(lambda: self.b.has("the recording"), message="the message")
        wait_until(lambda: self.store.get("files/%s-11" % meta["id"]), message="the last piece")
        self.assertEqual(self.b.hub.open_file(meta["id"])[1].read_bytes(), data)
        self.assertEqual(cloud.CloudSync(self.a.hub, self.a.link, self.a.device, file_max=10 ** 9).file_max,
                         crewchat.MAX_UPLOAD)  # never past crewchat's own limit for a shared file

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
        self.a.sync.renew_lease()
        self.assertEqual(self.a.sync.coordinator()["term"], term + 2)
        self.assertTrue(self.a.sync.coordinator()["me"])
        self.b.sync.renew_lease()
        self.assertFalse(self.b.sync.coordinator()["me"])
        # Stopping hands it on at once rather than after LEASE_SECS.
        self.a.sync.release_lease()
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
