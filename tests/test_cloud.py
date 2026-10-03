"""Cloud sync between machines: several hubs in one process, sharing an in-memory Firestore.

Needs the `cryptography` library; skipped without it. Google's Firestore library is not needed.
"""
import contextlib
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
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
        crewchat.save_config({"project": "Demo"}, self.root)
        self.link = Link(store)
        self.device = cloud.Device(self.root)
        self.device.create(name)
        self.account = cloud.Account(self.link, self.device)
        self.result = self.account.register()
        self.hub = crewchat.Hub(crewchat.Roster(self.root))
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


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
