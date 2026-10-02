"""crewchat cloud sync: machines signed in to the same Google account share one chat.

Optional add-on to crewchat.py. Every machine runs its own crewchat server for its local agents;
servers exchange messages through the owner's own Firebase project (Firestore), pushed in real
time. Everything is encrypted on the machine before it is sent. Design: docs/cloud-sync-design.md.

Needs:  pip install google-cloud-firestore cryptography   (Python 3.10+ recommended)

How the pieces fit:
- Device: this machine's identity. An RSA key pair (the private key never leaves the machine),
  the account keys it has been given, and the list of devices it trusts.
- Store: the database. FirestoreStore talks to Firestore as the signed-in person; MemoryStore is
  the same thing in memory, for tests.
- Account: the protocol on top: the first device founds the account, later devices are approved
  by a trusted one, removing a device rotates the account key.
- CloudSync: plugs into the Hub. Publishes what happens here, applies what arrives from elsewhere.

Firestore layout, all under users/{uid}/ (the security rules confine an account to its own):
  meta/account      {founder, key_id}                 the account key currently in use
  meta/trust        {devices, version, signer, sig}   the trusted devices, signed by one of them
  devices/{id}      {name, tag, pub, fp, keys, key_sigs, wrapped_by, seen, synced_to}
  messages/{msgid}  {dev, key_id, nonce, ct, created}  one chat message, encrypted
  state/{id}        {dev, key_id, nonce, ct, updated}  one machine's agents, encrypted
  tasks/{msgid}     {dev, key_id, nonce, ct, created}  who took a task: the first create wins
"""
import base64
import hashlib
import json
import os
import queue
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import crewchat

warnings.filterwarnings("ignore", module="google")
warnings.filterwarnings("ignore", message=".*OpenSSL.*")
warnings.filterwarnings("ignore", message=".*Python version.*")

TTL_HOURS = 24  # messages are deleted from Firestore once delivered, and after this at the latest
STATE_DEBOUNCE = 5  # seconds between publishing roster changes
SEEN_EVERY = 300  # "last seen" alone is published at most this often
HEARTBEAT = 15 * 60  # an otherwise quiet machine with agents says it is alive this often
HOUSEKEEPING = 10 * 60  # delete delivered messages this often
CLAIM_TIMEOUT = 20


class CloudError(Exception):
    pass


def need_crypto():
    try:
        import cryptography  # noqa: F401
    except ImportError:
        raise CloudError("cloud sync needs the cryptography library: pip install cryptography")


# --------------------------------------------------------------------------------------------
# Cryptography (all primitives from the `cryptography` library)
# --------------------------------------------------------------------------------------------
def b64(data):
    return base64.b64encode(data).decode("ascii")


def unb64(text):
    return base64.b64decode(text.encode("ascii"))


def new_private_key():
    from cryptography.hazmat.primitives.asymmetric import rsa
    return rsa.generate_private_key(public_exponent=65537, key_size=3072)


def private_pem(key):
    from cryptography.hazmat.primitives import serialization
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode("ascii")


def load_private(pem):
    from cryptography.hazmat.primitives import serialization
    return serialization.load_pem_private_key(pem.encode("ascii"), password=None)


def public_pem(key):
    from cryptography.hazmat.primitives import serialization
    return key.public_key().public_bytes(serialization.Encoding.PEM,
                                         serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")


def load_public(pem):
    from cryptography.hazmat.primitives import serialization
    return serialization.load_pem_public_key(pem.encode("ascii"))


def fingerprint(pub_pem):
    """What a person compares between two machines: 16 hex digits in groups of four."""
    digest = hashlib.sha256(pub_pem.strip().encode("ascii")).hexdigest()[:16]
    return " ".join(digest[i:i + 4] for i in range(0, 16, 4))


def key_id(key):
    return hashlib.sha256(key).hexdigest()[:16]


def wrap(pub_pem, key):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    return b64(load_public(pub_pem).encrypt(key, padding.OAEP(
        mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)))


def unwrap(private_key, wrapped):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    return private_key.decrypt(unb64(wrapped), padding.OAEP(
        mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))


def sign(private_key, data):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    return b64(private_key.sign(data, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                  salt_length=padding.PSS.MAX_LENGTH), hashes.SHA256()))


def verify(pub_pem, signature, data):
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    try:
        load_public(pub_pem).verify(unb64(signature), data, padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH), hashes.SHA256())
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def seal(key, aad, payload):
    """Encrypt a JSON-able payload with AES-256-GCM. Returns (nonce, ciphertext) in base64."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return b64(nonce), b64(AESGCM(key).encrypt(nonce, data, aad.encode("utf-8")))


def unseal(key, aad, nonce, ciphertext):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    data = AESGCM(key).decrypt(unb64(nonce), unb64(ciphertext), aad.encode("utf-8"))
    return json.loads(data.decode("utf-8"))


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


# --------------------------------------------------------------------------------------------
# This machine's identity
# --------------------------------------------------------------------------------------------
def cloud_dir(root=None):
    return Path(root or crewchat.home()) / "cloud"


def write_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path.parent, 0o700)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


class Device:
    """This machine: its keys, the account keys it holds, and the devices it trusts."""

    def __init__(self, root=None):
        self.dir = cloud_dir(root)
        info = read_json(self.dir / "device.json", {})
        self.id = info.get("id", "")
        self.name = info.get("name", "")
        self.tag = info.get("tag", "")
        self.private = None
        self.pub = ""
        if (self.dir / "device_key.pem").exists():
            self.private = load_private((self.dir / "device_key.pem").read_text(encoding="ascii"))
            self.pub = public_pem(self.private)
        keys = read_json(self.dir / "keys.json", {})
        self.keys = {k: unb64(v) for k, v in keys.get("keys", {}).items()}
        self.current = keys.get("current", "")
        self.trust = read_json(self.dir / "trust.json", {"devices": {}, "version": 0})
        self.lock = threading.RLock()

    @property
    def fp(self):
        return fingerprint(self.pub) if self.pub else ""

    @property
    def exists(self):
        return bool(self.id and self.private)

    @property
    def ready(self):
        """Approved and holding the account key in use."""
        return self.exists and self.id in self.trust.get("devices", {}) and self.current in self.keys

    def create(self, name):
        self.id = secrets.token_hex(6)
        self.name = name
        self.private = new_private_key()
        self.pub = public_pem(self.private)
        write_private(self.dir / "device_key.pem", private_pem(self.private))
        self.save()

    def save(self):
        with self.lock:
            write_private(self.dir / "device.json",
                          json.dumps({"id": self.id, "name": self.name, "tag": self.tag}, indent=2))
            write_private(self.dir / "keys.json", json.dumps(
                {"keys": {k: b64(v) for k, v in self.keys.items()}, "current": self.current}, indent=2))
            write_private(self.dir / "trust.json", json.dumps(self.trust, indent=2))

    def trusted(self, device_id):
        return self.trust.get("devices", {}).get(device_id)


# --------------------------------------------------------------------------------------------
# The database
# --------------------------------------------------------------------------------------------
SERVER_TIME = object()  # stands for "the database's own clock" in a write


class MemoryStore:
    """Firestore's behaviour for crewchat, in memory: one account, shared by devices in a test.

    Listeners are called on a background thread, as Firestore does. `online` can be switched off
    to simulate a machine without network: every call then raises CloudError.
    """

    def __init__(self):
        self.docs = {}
        self.lock = threading.RLock()
        self.listeners = []  # (collection, callback, since)
        self.events = queue.Queue()
        self.online = True
        self.clock = 0.0
        threading.Thread(target=self._dispatch, daemon=True).start()

    def _dispatch(self):
        while True:
            callback, changes = self.events.get()
            try:
                callback(changes)
            except Exception:  # a listener's bug must not stop the others
                import traceback
                traceback.print_exc()

    def _check(self):
        if not self.online:
            raise CloudError("offline")

    def _now(self):
        self.clock = max(self.clock + 0.001, time.time())
        return self.clock

    def _fill(self, data, old=None):
        out = {}
        for k, v in data.items():
            if v is SERVER_TIME:
                out[k] = self._now()
            elif isinstance(v, dict) and isinstance((old or {}).get(k), dict):
                merged = dict(old[k])
                merged.update(v)
                out[k] = merged
            else:
                out[k] = v
        return out

    def _notify(self, path, kind, data):
        collection, doc_id = path.rsplit("/", 1)
        for coll, callback, since in list(self.listeners):
            if coll != collection:
                continue
            if since and kind != "removed" and data.get(since[0], 0) < since[1]:
                continue
            self.events.put((callback, [(kind, doc_id, dict(data))]))

    def get(self, path):
        self._check()
        with self.lock:
            doc = self.docs.get(path)
            return dict(doc) if doc is not None else None

    def set(self, path, data, merge=False):
        self._check()
        with self.lock:
            old = self.docs.get(path)
            new = dict(old) if (merge and old) else {}
            new.update(self._fill(data, old if merge else None))
            self.docs[path] = new
            self._notify(path, "modified" if old is not None else "added", new)

    def create(self, path, data):
        self._check()
        with self.lock:
            if path in self.docs:
                return False
            self.docs[path] = self._fill(data)
            self._notify(path, "added", self.docs[path])
            return True

    def delete(self, path):
        self._check()
        with self.lock:
            old = self.docs.pop(path, None)
            if old is not None:
                self._notify(path, "removed", old)

    def list(self, collection, where=None):
        self._check()
        with self.lock:
            out = []
            for path, doc in self.docs.items():
                coll, doc_id = path.rsplit("/", 1)
                if coll == collection and (not where or doc.get(where[0]) == where[1]):
                    out.append((doc_id, dict(doc)))
            return out

    def listen(self, collection, callback, since=None):
        with self.lock:
            entry = (collection, callback, since)
            self.listeners.append(entry)
            initial = [("added", doc_id, doc) for doc_id, doc in self.list(collection)
                       if not since or doc.get(since[0], 0) >= since[1]]
            if initial:
                self.events.put((callback, initial))
        return lambda: self.listeners.remove(entry) if entry in self.listeners else None

    def transact(self, path, fn):
        self._check()
        with self.lock:
            new = fn(self.get(path))
            if new is None:
                return False
            self.set(path, new)
            return True

    def close(self):
        self.listeners.clear()


class FirestoreStore:
    """Firestore, as the signed-in person: the security rules apply to everything it does."""

    def __init__(self, auth):
        from google.auth.credentials import Credentials
        from google.cloud import firestore

        self.firestore = firestore
        self.auth = auth

        class UserCredentials(Credentials):
            def refresh(self, request):
                self.token = auth.id_token()
                self.expiry = None

            @property
            def valid(self):
                return bool(self.token) and auth.fresh()

        creds = UserCredentials()
        creds.token = auth.id_token()
        self.db = firestore.Client(project=auth.project, credentials=creds)
        self.root = "users/%s/" % auth.uid
        self.watches = []

    def _ref(self, path):
        return self.db.document(self.root + path)

    def _out(self, data):
        return {k: (self.firestore.SERVER_TIMESTAMP if v is SERVER_TIME else v) for k, v in data.items()}

    @staticmethod
    def _in(snapshot_dict):
        out = {}
        for k, v in (snapshot_dict or {}).items():
            out[k] = v.timestamp() if hasattr(v, "timestamp") and not isinstance(v, (int, float)) else v
        return out

    def _call(self, fn, *args, **kwargs):
        from google.api_core import exceptions
        try:
            return fn(*args, **kwargs)
        except exceptions.PermissionDenied as e:
            raise CloudError("Firestore refused: check the security rules and the sign-in (%s)" % e.message)
        except (exceptions.ServiceUnavailable, exceptions.DeadlineExceeded, exceptions.RetryError) as e:
            raise CloudError("cannot reach Firestore (%s)" % type(e).__name__)

    def get(self, path):
        snap = self._call(self._ref(path).get, timeout=CLAIM_TIMEOUT)
        return self._in(snap.to_dict()) if snap.exists else None

    def set(self, path, data, merge=False):
        self._call(self._ref(path).set, self._out(data), merge=merge, timeout=CLAIM_TIMEOUT)

    def create(self, path, data):
        from google.api_core import exceptions
        try:
            self._call(self._ref(path).create, self._out(data), timeout=CLAIM_TIMEOUT)
            return True
        except (exceptions.Conflict, exceptions.AlreadyExists):
            return False

    def delete(self, path):
        self._call(self._ref(path).delete, timeout=CLAIM_TIMEOUT)

    def list(self, collection, where=None):
        query = self.db.collection(self.root + collection)
        if where:
            query = query.where(where[0], "==", where[1])
        return [(d.id, self._in(d.to_dict())) for d in self._call(lambda: list(query.stream(timeout=CLAIM_TIMEOUT)))]

    def listen(self, collection, callback, since=None):
        import datetime
        query = self.db.collection(self.root + collection)
        if since:
            query = query.where(since[0], ">=", datetime.datetime.fromtimestamp(since[1], datetime.timezone.utc))

        def on_snapshot(docs, changes, read_time):
            out = []
            for change in changes:
                kind = change.type.name.lower()
                out.append((kind, change.document.id, self._in(change.document.to_dict())))
            if out:
                callback(out)

        watch = query.on_snapshot(on_snapshot)
        self.watches.append(watch)
        return watch.unsubscribe

    def transact(self, path, fn):
        firestore = self.firestore
        ref = self._ref(path)
        transaction = self.db.transaction()

        @firestore.transactional
        def run(tx):
            snap = ref.get(transaction=tx)
            new = fn(self._in(snap.to_dict()) if snap.exists else None)
            if new is None:
                return False
            tx.set(ref, self._out(new))
            return True

        return self._call(run, transaction)

    def close(self):
        for watch in self.watches:
            try:
                watch.unsubscribe()
            except Exception:
                pass


# --------------------------------------------------------------------------------------------
# The account protocol: founding, approving, receiving keys, removing
# --------------------------------------------------------------------------------------------
def trust_payload(devices, version):
    return canonical({"devices": devices, "version": version})


def sign_trust(device, devices, version):
    return {"devices": devices, "version": version, "signer": device.id,
            "sig": sign(device.private, trust_payload(devices, version))}


def wrap_for(device, target_id, target_pub, kid):
    wrapped = wrap(target_pub, device.keys[kid])
    sig = sign(device.private, ("%s|%s|%s" % (target_id, kid, wrapped)).encode("ascii"))
    return wrapped, sig


class Account:
    """The protocol between devices of one account, on top of a store."""

    def __init__(self, store, device):
        self.store = store
        self.device = device

    # Joining -------------------------------------------------------------------------------
    def _free_tag(self, docs):
        used = {d.get("tag") for i, d in docs if i != self.device.id}
        letters = "ABCDEFGHJKLMNPQRSTUVWXYZ"
        for size in (1, 2, 3):
            for n in range(len(letters) ** size):
                tag, k = "", n
                for _ in range(size):
                    tag, k = letters[k % len(letters)] + tag, k // len(letters)
                if tag not in used:
                    return tag
        raise CloudError("no free device tag")

    def register(self):
        """Publish this device's public key. Returns "founded", "ready" or "pending"."""
        device = self.device
        docs = self.store.list("devices")
        if not device.tag:
            device.tag = self._free_tag(docs)
            device.save()
        self.store.set("devices/" + device.id, {
            "name": device.name, "tag": device.tag, "pub": device.pub, "fp": device.fp,
            "seen": SERVER_TIME}, merge=True)
        if self.store.create("meta/account", {"founder": device.id, "created": SERVER_TIME}):
            self._found()
            return "founded"
        self.refresh()
        return "ready" if device.ready else "pending"

    def _found(self):
        device = self.device
        key = os.urandom(32)
        kid = key_id(key)
        with device.lock:
            device.keys[kid] = key
            device.current = kid
            entry = {"fp": device.fp, "name": device.name, "tag": device.tag, "pub": device.pub}
            device.trust = sign_trust(device, {device.id: entry}, 1)
            device.save()
        wrapped, sig = wrap_for(device, device.id, device.pub, kid)
        self.store.set("devices/" + device.id, {"keys": {kid: wrapped}, "key_sigs": {kid: sig},
                                               "wrapped_by": {kid: device.id}}, merge=True)
        self.store.set("meta/trust", device.trust)
        self.store.set("meta/account", {"key_id": kid}, merge=True)

    # Keeping up to date ----------------------------------------------------------------------
    def accept_trust(self, doc):
        """Adopt a newer trusted-device list if it is signed by a device this machine trusts.

        A device that trusts nobody yet (it is waiting for approval) accepts the first list that
        includes its own key and is correctly signed by one of the devices in it. The person
        confirms that step by comparing fingerprints (see `crewchat cloud status`).
        """
        device = self.device
        if not doc or not isinstance(doc.get("devices"), dict):
            return False
        version, signer = int(doc.get("version", 0)), doc.get("signer")
        with device.lock:
            mine = device.trust.get("devices", {})
            if version <= int(device.trust.get("version", 0)):
                return False
            if mine:
                signer_entry = mine.get(signer)
            else:
                ours = doc["devices"].get(device.id)
                if not ours or ours.get("pub", "").strip() != device.pub.strip():
                    return False
                signer_entry = doc["devices"].get(signer)
            if not signer_entry or not verify(signer_entry["pub"], doc.get("sig", ""),
                                              trust_payload(doc["devices"], version)):
                return False
            device.trust = {"devices": doc["devices"], "version": version, "signer": signer,
                            "sig": doc["sig"]}
            device.save()
            return True

    def accept_keys(self, doc):
        """Take account keys wrapped for this device by a trusted device. True if any were new."""
        device = self.device
        new = False
        if not doc:
            return False
        for kid, wrapped in (doc.get("keys") or {}).items():
            if kid in device.keys:
                continue
            by = (doc.get("wrapped_by") or {}).get(kid)
            entry = device.trusted(by) if by else None
            sig = (doc.get("key_sigs") or {}).get(kid, "")
            if not entry or not verify(entry["pub"], sig, ("%s|%s|%s" % (device.id, kid, wrapped)).encode("ascii")):
                continue
            try:
                key = unwrap(device.private, wrapped)
            except ValueError:
                continue
            if key_id(key) != kid:
                continue
            with device.lock:
                device.keys[kid] = key
                new = True
        if new:
            device.save()
        return new

    def accept_account(self, doc):
        """Switch to the account key in use, once this device holds it."""
        device = self.device
        kid = (doc or {}).get("key_id")
        if kid and kid in device.keys and kid != device.current:
            with device.lock:
                device.current = kid
                device.save()
            return True
        return False

    def refresh(self):
        self.accept_trust(self.store.get("meta/trust"))
        self.accept_keys(self.store.get("devices/" + self.device.id))
        self.accept_account(self.store.get("meta/account"))

    # Changing who is trusted -----------------------------------------------------------------
    def devices(self):
        """Every registered device: (id, doc, trusted entry or None)."""
        return [(i, d, self.device.trusted(i)) for i, d in self.store.list("devices")]

    def find(self, name_or_id):
        matches = [(i, d) for i, d in self.store.list("devices")
                   if i == name_or_id or d.get("name") == name_or_id or d.get("tag") == name_or_id]
        if not matches:
            raise CloudError("no device called %s; `crewchat cloud devices` lists them" % name_or_id)
        if len(matches) > 1:
            raise CloudError("several devices are called %s; use its id from `crewchat cloud devices`" % name_or_id)
        return matches[0]

    def _change_trust(self, change):
        device = self.device

        def update(current):
            base = current if current and self._valid_trust(current) else device.trust
            devices = change(dict(base.get("devices", {})))
            return sign_trust(device, devices, max(int(base.get("version", 0)), int(device.trust.get("version", 0))) + 1)

        if not self.store.transact("meta/trust", update):
            raise CloudError("could not update the trusted devices")
        self.accept_trust(self.store.get("meta/trust"))

    def _valid_trust(self, doc):
        entry = self.device.trusted(doc.get("signer"))
        return bool(entry) and verify(entry["pub"], doc.get("sig", ""),
                                      trust_payload(doc.get("devices", {}), int(doc.get("version", 0))))

    def approve(self, name_or_id, expect_fp=None):
        device = self.device
        if not device.ready:
            raise CloudError("this machine is not approved itself yet")
        target_id, doc = self.find(name_or_id)
        pub = doc.get("pub", "")
        fp = fingerprint(pub) if pub else ""
        if not pub or fp != doc.get("fp"):
            raise CloudError("that device's key is damaged; ask it to run `crewchat cloud login` again")
        if expect_fp is not None and "".join(expect_fp.split()).lower() != fp.replace(" ", ""):
            raise CloudError("fingerprint mismatch: the device shows %s. Do not approve it if that is "
                             "not what the new machine displays." % fp)
        entry = {"fp": fp, "name": doc.get("name", ""), "tag": doc.get("tag", ""), "pub": pub}
        self._change_trust(lambda devices: dict(devices, **{target_id: entry}))
        keys, sigs, by = {}, {}, {}
        for kid in device.keys:
            keys[kid], sigs[kid] = wrap_for(device, target_id, pub, kid)
            by[kid] = device.id
        self.store.set("devices/" + target_id, {"keys": keys, "key_sigs": sigs, "wrapped_by": by}, merge=True)
        return doc.get("name", target_id)

    def remove(self, name_or_id):
        """Drop a device and switch everyone else to a new account key it never sees."""
        device = self.device
        if not device.ready:
            raise CloudError("this machine is not approved itself yet")
        target_id, doc = self.find(name_or_id)
        self._change_trust(lambda devices: {k: v for k, v in devices.items() if k != target_id})
        self.store.delete("devices/" + target_id)
        self.store.delete("state/" + target_id)
        if target_id != device.id:
            self.rotate()
        return doc.get("name", target_id)

    def rotate(self):
        device = self.device
        key = os.urandom(32)
        kid = key_id(key)
        with device.lock:
            device.keys[kid] = key
            device.save()
        for other_id, entry in device.trust.get("devices", {}).items():
            wrapped, sig = wrap_for(device, other_id, entry["pub"], kid)
            self.store.set("devices/" + other_id, {"keys": {kid: wrapped}, "key_sigs": {kid: sig},
                                                  "wrapped_by": {kid: device.id}}, merge=True)
        self.store.set("meta/account", {"key_id": kid}, merge=True)
        self.accept_account({"key_id": kid})


# --------------------------------------------------------------------------------------------
# Sync: the Hub on one side, the store on the other
# --------------------------------------------------------------------------------------------
class CloudSync:
    def __init__(self, hub, store, device, ttl_hours=TTL_HOURS, log=None):
        self.hub = hub
        self.store = store
        self.device = device
        self.account = Account(store, device)
        self.device_id = device.id
        self.ttl = ttl_hours * 3600
        self.log = log or (lambda text: sys.stderr.write("%s cloud %s\n" % (crewchat.now_iso(), text)))
        self.outbox = queue.Queue()
        self.stop_event = threading.Event()
        self.dirty = threading.Event()
        self.held = []  # messages whose key has not arrived yet
        self.state_sig = None
        self.state_seen_at = 0
        self.state_sent_at = 0
        self.online = True
        self.unsubscribe = []
        self.marks = read_json(device.dir / "sync.json", {"published": 0, "synced_to": 0})
        hub.prefix = device.tag
        hub.device = device.name
        hub.sync = self

    # Wiring ----------------------------------------------------------------------------------
    def start(self):
        self.account.refresh()
        since = max(time.time() - self.ttl, float(self.marks.get("synced_to", 0)) - 120)
        self.unsubscribe = [
            self.store.listen("messages", self._on_messages, since=("created", since)),
            self.store.listen("state", self._on_state),
            self.store.listen("devices", self._on_devices),
            self.store.listen("meta", self._on_meta),
        ]
        self._republish()
        for target in (self._worker, self._ticker):
            threading.Thread(target=target, daemon=True).start()
        self.dirty.set()
        if not self.device.ready:
            self.log("waiting for this machine to be approved: on a trusted machine run "
                     "`crewchat cloud approve %s` (fingerprint %s)" % (self.device.name, self.device.fp))
        return self

    def stop(self):
        self.stop_event.set()
        for unsubscribe in self.unsubscribe:
            try:
                unsubscribe()
            except Exception:
                pass

    def _save_marks(self):
        write_private(self.device.dir / "sync.json", json.dumps(self.marks))

    # Hub -> cloud ----------------------------------------------------------------------------
    def publish(self, msg):
        """Called by the Hub (with its lock held) for every message written on this machine."""
        self.outbox.put(dict(msg))

    def state_changed(self):
        self.dirty.set()

    def _republish(self):
        """Messages written here that never reached Firestore (offline, or not yet approved)."""
        with self.hub.lock:
            backlog = [dict(m) for m in self.hub.messages
                       if m.get("origin") == self.device_id and m["seq"] > int(self.marks.get("published", 0))]
        for msg in backlog:
            self.outbox.put(msg)

    def _seal(self, aad, payload):
        kid = self.device.current
        nonce, ct = seal(self.device.keys[kid], aad % kid, payload)
        return {"dev": self.device_id, "key_id": kid, "nonce": nonce, "ct": ct}

    def _uid(self):
        return getattr(self.store, "root", "users/memory/")

    def _send_message(self, msg):
        payload = {k: msg[k] for k in ("id", "ts", "from", "to", "text", "kind") if k in msg}
        if msg.get("task"):
            payload["task"] = msg["task"]
        doc = self._seal("crewchat|%s|msg|%s|%%s|%s" % (self._uid(), msg["id"], self.device_id), payload)
        doc["created"] = SERVER_TIME
        self.store.set("messages/" + msg["id"], doc)
        if msg["seq"] > int(self.marks.get("published", 0)):
            self.marks["published"] = msg["seq"]
            self._save_marks()

    def _send_state(self):
        rows = self.hub.local_state()
        signature = json.dumps([{k: v for k, v in r.items() if k != "seen"} for r in rows], sort_keys=True)
        now = time.time()
        changed = signature != self.state_sig
        seen_due = rows and now - self.state_seen_at >= SEEN_EVERY
        heartbeat_due = rows and now - self.state_sent_at >= HEARTBEAT
        if not (changed or seen_due or heartbeat_due) or now - self.state_sent_at < STATE_DEBOUNCE:
            return changed  # try again later if something is pending
        doc = self._seal("crewchat|%s|state|%s|%%s" % (self._uid(), self.device_id),
                         {"device": self.device.name, "agents": rows})
        doc["updated"] = SERVER_TIME
        self.store.set("state/" + self.device_id, doc)
        self.state_sig, self.state_sent_at, self.state_seen_at = signature, now, now
        return False

    def _worker(self):
        backoff = 0
        while not self.stop_event.is_set():
            try:
                msg = self.outbox.get(timeout=1)
            except queue.Empty:
                msg = None
            try:
                if msg is not None:
                    if not self.device.ready:
                        self.outbox.put(msg)
                        time.sleep(2)
                        continue
                    self._send_message(msg)
                if self.device.ready and self.dirty.is_set():
                    self.dirty.clear()
                    if self._send_state():
                        self.dirty.set()
                if not self.online:
                    self.online = True
                    self.log("connected again")
                backoff = 0
            except Exception as e:  # offline, or Firestore refused: keep the message and retry
                if msg is not None:
                    self.outbox.put(msg)
                if self.online:
                    self.online = False
                    self.log("cannot send, will retry (%s)" % e)
                backoff = min(60, max(2, backoff * 2))
                self.stop_event.wait(backoff)

    def _ticker(self):
        last_housekeeping = 0
        while not self.stop_event.wait(30):
            self.dirty.set()
            if time.time() - last_housekeeping >= HOUSEKEEPING:
                last_housekeeping = time.time()
                try:
                    self.housekeeping()
                except Exception as e:
                    self.log("housekeeping skipped (%s)" % e)

    def housekeeping(self):
        """Tell the others how far this machine has read, and delete what everyone has."""
        self.store.set("devices/" + self.device_id, {"seen": SERVER_TIME,
                                                    "synced_to": float(self.marks.get("synced_to", 0))}, merge=True)
        now = time.time()
        trusted = self.device.trust.get("devices", {})
        docs = dict(self.store.list("devices"))
        reach = [float(docs[i].get("synced_to") or 0) for i in trusted
                 if i in docs and now - float(docs[i].get("seen") or 0) < self.ttl]
        everyone_has = min(reach) if reach else 0
        for doc_id, doc in self.store.list("messages", where=("dev", self.device_id)):
            created = float(doc.get("created") or now)
            if created <= everyone_has or now - created > self.ttl:
                self.store.delete("messages/" + doc_id)
        for doc_id, doc in self.store.list("tasks", where=("dev", self.device_id)):
            if now - float(doc.get("created") or now) > self.ttl:
                self.store.delete("tasks/" + doc_id)

    # Tasks -------------------------------------------------------------------------------------
    def claim_task(self, task_id, agent):
        """Settle who takes a task, across machines: the first create of tasks/{id} wins."""
        if not self.device.ready:
            raise crewchat.HubError("this machine is not approved for cloud sync yet")
        aad = "crewchat|%s|task|%s|%%s" % (self._uid(), task_id)
        try:
            doc = self._seal(aad, {"agent": agent})
            doc["created"] = SERVER_TIME
            if self.store.create("tasks/" + task_id, doc):
                return agent
            existing = self.store.get("tasks/" + task_id)
        except Exception as e:
            raise crewchat.HubError("cannot take a task while this machine cannot reach the cloud (%s)" % e)
        try:
            return str(self._open(aad, existing)["agent"])
        except Exception:
            raise crewchat.HubError("task #%s was taken on a machine this one cannot read yet" % task_id)

    def _open(self, aad, doc):
        kid = doc.get("key_id", "")
        if kid not in self.device.keys:
            raise KeyError(kid)
        return unseal(self.device.keys[kid], aad % kid, doc["nonce"], doc["ct"])

    # Cloud -> hub ------------------------------------------------------------------------------
    def _on_messages(self, changes):
        for kind, doc_id, doc in changes:
            if kind == "removed":
                continue
            created = float(doc.get("created") or 0)
            if doc.get("dev") != self.device_id:
                aad = "crewchat|%s|msg|%s|%%s|%s" % (self._uid(), doc_id, doc.get("dev"))
                try:
                    payload = self._open(aad, doc)
                except KeyError:
                    self.held.append((doc_id, doc))  # its key is still on its way
                    continue
                except Exception:
                    self.log("dropped message %s: it does not decrypt (tampered, or from an untrusted device)" % doc_id)
                    continue
                if payload.get("id") != doc_id:
                    self.log("dropped message %s: its content does not match its name" % doc_id)
                    continue
                payload["origin"] = doc.get("dev", "")
                self.hub.ingest(payload)
            if created > float(self.marks.get("synced_to", 0)):
                self.marks["synced_to"] = created
                self._save_marks()

    def _retry_held(self):
        held, self.held = self.held, []
        if held:
            self._on_messages([("added", i, d) for i, d in held])

    def _on_state(self, changes):
        for kind, device_id, doc in changes:
            if device_id == self.device_id:
                continue
            if kind == "removed" or not self.device.trusted(device_id):
                self.hub.drop_remote(device_id)
                continue
            try:
                payload = self._open("crewchat|%s|state|%s|%%s" % (self._uid(), device_id), doc)
            except Exception:
                continue
            self.hub.set_remote(device_id, payload.get("device", ""), payload.get("agents", []),
                                doc.get("updated") or time.time())

    def _on_devices(self, changes):
        for kind, device_id, doc in changes:
            if device_id == self.device_id and kind != "removed":
                if self.account.accept_keys(doc):
                    self.account.accept_account(self.store.get("meta/account"))
                    self._after_keys()
            elif kind == "removed":
                self.hub.drop_remote(device_id)

    def _on_meta(self, changes):
        for kind, doc_id, doc in changes:
            if doc_id == "trust" and self.account.accept_trust(doc):
                # Keys may have arrived before the approval did, when they could not be checked yet.
                self.account.refresh()
                self._after_keys()
                trusted = self.device.trust.get("devices", {})
                for device_id in list(self.hub.remote):
                    if device_id not in trusted:
                        self.hub.drop_remote(device_id)
                if self.device.id not in trusted and self.device.keys:
                    self.log("this machine was removed from the account; cloud sync stops")
                    self.stop()
            elif doc_id == "account" and self.account.accept_account(doc):
                self._after_keys()

    def _after_keys(self):
        if self.device.ready:
            self.log("approved: syncing with the other machines")
            self._retry_held()
            self.dirty.set()


# --------------------------------------------------------------------------------------------
# Signing in with Google
# --------------------------------------------------------------------------------------------
def post(url, data, as_json=False):
    body = json.dumps(data).encode() if as_json else urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json" if as_json else "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300]
        raise CloudError("sign-in failed at %s (HTTP %d): %s" % (url.split("?")[0], e.code, detail))
    except (urllib.error.URLError, OSError) as e:
        raise CloudError("cannot reach Google (%s)" % getattr(e, "reason", e))


class Auth:
    """The Firebase sign-in for this machine, renewed as needed."""

    def __init__(self, root=None):
        self.dir = cloud_dir(root)
        self.config = read_json(self.dir / "firebase.json")
        if not self.config:
            raise CloudError("cloud sync is not set up here; run `crewchat cloud setup` (see docs/firebase-setup.md)")
        self.project = self.config["projectId"]
        self.data = read_json(self.dir / "auth.json") or {}
        self.lock = threading.Lock()

    @property
    def signed_in(self):
        return bool(self.data.get("refresh_token"))

    @property
    def uid(self):
        return self.data.get("uid", "")

    @property
    def email(self):
        return self.data.get("email", "")

    def fresh(self):
        return self.data.get("expires", 0) - time.time() > 300

    def id_token(self):
        with self.lock:
            if not self.signed_in:
                raise CloudError("not signed in; run `crewchat cloud login`")
            if not self.fresh():
                out = post("https://securetoken.googleapis.com/v1/token?key=" + self.config["apiKey"],
                           {"grant_type": "refresh_token", "refresh_token": self.data["refresh_token"]})
                self.data.update(id_token=out["id_token"], refresh_token=out["refresh_token"],
                                 expires=time.time() + int(out["expires_in"]))
                write_private(self.dir / "auth.json", json.dumps(self.data))
            return self.data["id_token"]

    def login(self, timeout=300):
        """Google sign-in in the browser (OAuth for desktop apps, with PKCE), then Firebase."""
        client = self.config.get("oauth") or {}
        if not client.get("client_id"):
            raise CloudError("the setup has no OAuth client; run `crewchat cloud setup` again with --oauth-client")
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        got = {}

        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                if query.get("state", [""])[0] == state:
                    got.update(code=query.get("code", [""])[0], error=query.get("error", [""])[0])
                page = b"<h3>crewchat: you are signed in. You can close this tab.</h3>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)

        server = HTTPServer(("127.0.0.1", 0), Callback)
        redirect = "http://127.0.0.1:%d" % server.server_address[1]
        url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
            "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code",
            "scope": "openid email", "state": state, "code_challenge": challenge,
            "code_challenge_method": "S256", "prompt": "select_account"})
        print("Opening your browser to sign in with Google. If it does not open, visit:\n  %s" % url, flush=True)
        webbrowser.open(url)
        deadline = time.time() + timeout
        server.timeout = 5
        while "code" not in got and time.time() < deadline:
            server.handle_request()
        server.server_close()
        if not got.get("code"):
            raise CloudError("sign-in did not finish (%s)" % (got.get("error") or "timed out"))
        google = post("https://oauth2.googleapis.com/token", {
            "code": got["code"], "client_id": client["client_id"], "client_secret": client.get("client_secret", ""),
            "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier})
        firebase = post("https://identitytoolkit.googleapis.com/v1/accounts:signInWithIdp?key=" + self.config["apiKey"], {
            "postBody": "id_token=%s&providerId=google.com" % google["id_token"],
            "requestUri": "http://localhost", "returnSecureToken": True}, as_json=True)
        self.data = {"uid": firebase["localId"], "email": firebase.get("email", ""),
                     "id_token": firebase["idToken"], "refresh_token": firebase["refreshToken"],
                     "expires": time.time() + int(firebase["expiresIn"])}
        write_private(self.dir / "auth.json", json.dumps(self.data))


def firestore_store(root=None):
    try:
        import google.cloud.firestore  # noqa: F401
    except ImportError:
        raise CloudError("cloud sync needs Google's Firestore library: pip install google-cloud-firestore")
    need_crypto()
    auth = Auth(root)
    if not auth.signed_in:
        raise CloudError("not signed in; run `crewchat cloud login`")
    return FirestoreStore(auth), auth


# --------------------------------------------------------------------------------------------
# Called by crewchat.py
# --------------------------------------------------------------------------------------------
def configured(root=None):
    folder = cloud_dir(root)
    return (folder / "firebase.json").exists() and (folder / "auth.json").exists() and (folder / "device.json").exists()


def start(hub, root=None, ttl_hours=None):
    """Start syncing this server's hub. Returns the CloudSync, or raises CloudError."""
    store, auth = firestore_store(root)
    device = Device(root)
    if not device.exists:
        raise CloudError("this machine has no cloud identity; run `crewchat cloud login`")
    config = crewchat.load_config(root)
    ttl = ttl_hours or float(config.get("cloud_ttl_hours", TTL_HOURS))
    sync = CloudSync(hub, store, device, ttl_hours=ttl)
    sync.start()
    sys.stderr.write("%s cloud sync on as %s (device %s, tag %s), account %s\n" % (
        crewchat.now_iso(), auth.email, device.name, device.tag, "ready" if device.ready else "waiting for approval"))
    return sync


RULES = """rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {
    // Each Google account reads and writes only its own crewchat data.
    function owner(uid) {
      return request.auth != null && request.auth.uid == uid;
    }
    // Writes stay small, so one account cannot fill the project.
    function small() {
      return request.resource.data.keys().size() <= 16
        && (!('ct' in request.resource.data) || request.resource.data.ct.size() <= 60000)
        && (!('pub' in request.resource.data) || request.resource.data.pub.size() <= 2000);
    }
    match /users/{uid}/{document=**} {
      allow read, delete: if owner(uid);
      allow create, update: if owner(uid) && small();
    }
  }
}
"""


def cmd_cloud(args):
    try:
        _cmd_cloud(args)
    except CloudError as e:
        crewchat.die(str(e))


def _cmd_cloud(args):
    root = crewchat.home()
    folder = cloud_dir(root)
    action = args.action
    if action == "rules":
        print(RULES, end="")
        return
    if action == "setup":
        web = read_json(args.web_config) if args.web_config else {}
        if args.web_config and not web:
            raise CloudError("%s is not a readable JSON file" % args.web_config)
        project = args.project_id or web.get("projectId")
        api_key = args.api_key or web.get("apiKey")
        oauth = {}
        if args.oauth_client:
            data = read_json(args.oauth_client)
            if not data:
                raise CloudError("%s is not a readable JSON file" % args.oauth_client)
            data = data.get("installed") or data.get("web") or data
            oauth = {"client_id": data.get("client_id", ""), "client_secret": data.get("client_secret", "")}
        if args.client_id:
            oauth = {"client_id": args.client_id, "client_secret": args.client_secret or ""}
        if not (project and api_key and oauth.get("client_id")):
            raise CloudError("need the Firebase project id, its web API key and a Desktop OAuth client: see "
                             "docs/firebase-setup.md")
        write_private(folder / "firebase.json", json.dumps(
            {"projectId": project, "apiKey": api_key, "oauth": oauth}, indent=2))
        print("Cloud sync is set up for Firebase project %s. Next: crewchat cloud login" % project)
        return
    if action == "login":
        need_crypto()
        auth = Auth(root)
        if not auth.signed_in or args.again:
            auth.login()
        print("Signed in as %s." % auth.email)
        store, auth = firestore_store(root)
        device = Device(root)
        if not device.exists:
            names = {d.get("name") for _, d in store.list("devices")}
            name = crewchat.place_label(args.device or crewchat.socket.gethostname())
            base, number = name, 2
            while name in names:
                name, number = "%s-%d" % (base[:13], number), number + 1
            device.create(name)
        result = Account(store, device).register()
        config = crewchat.load_config(root)
        config["cloud"] = True
        crewchat.save_config(config, root)
        print("This machine is device \"%s\" (tag %s). Its fingerprint: %s" % (device.name, device.tag, device.fp))
        if result == "founded":
            print("It is the first device on this account, so it holds the account key.")
        elif result == "ready":
            print("It is approved and ready.")
        else:
            print()
            print("Before it can read or send anything, approve it from a machine that is already set up:")
            print("  crewchat cloud approve %s" % device.name)
            print("That command shows a fingerprint: it must be %s." % device.fp)
        print()
        print("Restart the server so it starts syncing: crewchat service restart (or crewchat serve).")
        return
    store, auth = firestore_store(root)
    device = Device(root)
    account = Account(store, device)
    if action == "status":
        account.refresh()
        print("Signed in as %s (Firebase project %s)." % (auth.email, auth.project))
        if not device.exists:
            print("This machine has no device identity yet: crewchat cloud login")
            return
        print("This machine: \"%s\", tag %s, fingerprint %s" % (device.name, device.tag, device.fp))
        if device.ready:
            print("Approved. Trusted devices: %d. Account key %s." % (len(device.trust["devices"]), device.current[:8]))
            signer = device.trust.get("devices", {}).get(device.trust.get("signer"), {})
            if signer and device.trust.get("signer") != device.id:
                print("Last change to the trusted list was signed by \"%s\", fingerprint %s." % (
                    signer.get("name"), signer.get("fp")))
        else:
            print("Waiting for approval: on a trusted machine run `crewchat cloud approve %s`." % device.name)
        return
    if action == "devices":
        account.refresh()
        for device_id, doc, trusted in sorted(account.devices(), key=lambda x: x[1].get("tag", "")):
            mark = " (this machine)" if device_id == device.id else ""
            state = "trusted" if trusted else "WAITING FOR APPROVAL"
            seen = crewchat.now_iso(doc["seen"]) if doc.get("seen") else "never"
            print("%-3s %-18s %-22s fingerprint %s; last seen %s%s" % (
                doc.get("tag", "?"), doc.get("name", device_id), state, doc.get("fp", "?"), seen, mark))
        return
    if action == "approve":
        account.refresh()
        if not args.name:
            raise CloudError("usage: crewchat cloud approve DEVICE")
        target_id, doc = account.find(args.name)
        if device.trusted(target_id):
            print("%s is already trusted." % doc.get("name"))
            return
        fp = doc.get("fp", "")
        expect = args.fingerprint
        if expect is None:
            if not sys.stdin.isatty():
                raise CloudError("pass --fingerprint with what the new machine shows (from `crewchat cloud status` there)")
            print("Device \"%s\" shows the fingerprint:\n  %s" % (doc.get("name"), fp))
            answer = input("Does the new machine show exactly this? Type yes to approve: ").strip().lower()
            if answer != "yes":
                print("Not approved.")
                return
            expect = fp
        name = account.approve(target_id, expect_fp=expect)
        print("Approved %s. It receives the account key and starts syncing within a few seconds." % name)
        return
    if action == "remove":
        account.refresh()
        if not args.name:
            raise CloudError("usage: crewchat cloud remove DEVICE")
        name = account.remove(args.name)
        print("Removed %s and switched the others to a new account key it never sees." % name)
        return
    if action == "logout":
        if device.ready:
            try:
                account.remove(device.id)
            except CloudError:
                pass
        for name in ("auth.json", "device.json", "device_key.pem", "keys.json", "trust.json", "sync.json"):
            try:
                (folder / name).unlink()
            except OSError:
                pass
        config = crewchat.load_config(root)
        config["cloud"] = False
        crewchat.save_config(config, root)
        print("Signed out and removed this machine's cloud identity. Restart the server.")
        return
    raise CloudError("unknown action %s" % action)


def add_parser(sub):
    p = sub.add_parser("cloud", help="sync with other machines on the same Google account (Firestore)",
                       description="Cloud sync: each machine runs its own crewchat; machines signed in to "
                       "the same Google account share one chat through your own Firebase project. "
                       "Setup: docs/firebase-setup.md")
    p.add_argument("action", choices=["setup", "login", "status", "devices", "approve", "remove", "logout", "rules"])
    p.add_argument("name", nargs="?", help="device, for approve and remove")
    p.add_argument("--web-config", help="setup: file with the Firebase web app config (projectId, apiKey)")
    p.add_argument("--oauth-client", help="setup: the Desktop OAuth client JSON downloaded from Google Cloud")
    p.add_argument("--project-id")
    p.add_argument("--api-key")
    p.add_argument("--client-id")
    p.add_argument("--client-secret")
    p.add_argument("--device", help="login: this machine's device name (default: its host name)")
    p.add_argument("--again", action="store_true", help="login: sign in again even if signed in")
    p.add_argument("--fingerprint", help="approve: the fingerprint the new machine shows")
    p.set_defaults(fn=cmd_cloud)
