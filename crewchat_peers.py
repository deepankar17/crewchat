"""crewchat over Tailscale: servers on your own machines share one chat, with no cloud service.

Optional add-on to crewchat.py, standard library only. Every machine runs its own crewchat server
for its local agents, reachable from the others through a private network such as Tailscale
(`tailscale serve`). Servers pull each other's messages and rosters directly, with long-polls, so
a message arrives within a moment and nothing is stored anywhere but on your machines.

How the pieces fit:
- Mesh: this machine's membership, in peers.json next to config.json: its name, its tag (the
  letter in its message ids), the key all members share, the other members and their addresses,
  and how far it has read each one.
- PeerSync: plugs into the Hub (hub.sync). For every other member a thread long-polls
  POST {url}/peer/pull for the messages that member wrote and its roster. A task is settled by
  the machine that posted it: whoever asks it first gets it.

Joining: `crewchat peers invite` on a member prints a single-use code; `crewchat peers join URL
CODE` on the new machine swaps it for the shared key and the member list. Members tell each other
about new members as they pull. Removing a machine changes the shared key.

The private network is the first line of defence: the server only listens on 127.0.0.1, and only
devices on your tailnet reach it through `tailscale serve`. The shared key is the second.
"""
import hashlib
import hmac
import json
import os
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import crewchat

PULL_WAIT = 25  # seconds a pull waits for something new
INVITE_TTL = 600
REKEY_GRACE = 600  # the old key still works this long after a change, while members catch up
OFFLINE_AFTER = 2  # failed pulls in a row before a member shows as offline
BACKOFF = (1, 2, 5, 10, 20, 30)
MAX_BATCH = 500
TAGS = [chr(c) for c in range(ord("A"), ord("Z") + 1)]
TAGS += [a + b for a in TAGS for b in TAGS]


class PeerError(Exception):
    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status  # the HTTP status another member answered with, if any


def log(text):
    sys.stderr.write("%s peers: %s\n" % (crewchat.now_iso(), text))


def machine_name():
    try:
        host = socket.gethostname()
    except OSError:
        host = "machine"
    return crewchat.place_label(host.split(".")[0])


def clean_url(url):
    url = str(url or "").strip().rstrip("/")
    if url.endswith("/mcp"):
        url = url[:-4]
    return url


# --------------------------------------------------------------------------------------------
# Membership
# --------------------------------------------------------------------------------------------
class Mesh:
    """This machine's membership, kept in peers.json (private to the user)."""

    def __init__(self, root=None):
        self.root = Path(root) if root else crewchat.home()
        self.path = self.root / "peers.json"
        self.lock = threading.RLock()
        self.data = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except ValueError:
                raise PeerError("%s is damaged; remove it and join again" % self.path)

    @property
    def exists(self):
        return bool(self.data.get("me"))

    @property
    def me(self):
        return self.data["me"]

    def save(self):
        with self.lock:
            crewchat.write_json(self.path, self.data, private=True)

    def found(self, name, url):
        """Start a new chat between machines, with this one as its first member."""
        with self.lock:
            self.data = {"mesh": secrets.token_hex(8), "secret": secrets.token_urlsafe(32),
                         "me": {"id": secrets.token_hex(8), "name": name, "tag": "A", "url": clean_url(url)},
                         "members": {}, "removed": {}, "cursors": {}, "claims": {}}
            self.save()

    def adopt(self, answer, me):
        """Become a member with what the inviting machine sent back."""
        with self.lock:
            self.data = {"mesh": answer["mesh"], "secret": answer["secret"], "me": me,
                         "members": {}, "removed": dict(answer.get("removed") or {}), "cursors": {}, "claims": {}}
            self.merge(answer.get("members") or {})
            self.save()

    def members(self):
        with self.lock:
            return {k: dict(v) for k, v in self.data.get("members", {}).items()}

    def merge(self, members, removed=None):
        """Add what another member knows. Returns the ids that are new here."""
        new = []
        with self.lock:
            gone = self.data.setdefault("removed", {})
            for mid, tag in (removed or {}).items():
                gone.setdefault(mid, tag)
            mine = self.data.setdefault("members", {})
            for mid, info in (members or {}).items():
                if mid == self.me["id"] or mid in gone or not isinstance(info, dict):
                    continue
                entry = {"name": crewchat.place_label(str(info.get("name") or "machine")),
                         "tag": str(info.get("tag") or ""), "url": clean_url(info.get("url"))}
                if mid not in mine:
                    new.append(mid)
                    mine[mid] = entry
                elif entry["url"] and mine[mid] != entry:
                    mine[mid] = entry
            for mid in list(mine):
                if mid in gone:
                    del mine[mid]
            self.save()
        return new

    def everyone(self):
        """The member list as sent to others: every member including this machine."""
        with self.lock:
            out = self.members()
            out[self.me["id"]] = {"name": self.me["name"], "tag": self.me["tag"], "url": self.me["url"]}
            return out

    def free_tag(self):
        with self.lock:
            used = {m["tag"] for m in self.data["members"].values()} | {self.me["tag"]}
            used |= set(self.data.get("removed", {}).values())
            return next(t for t in TAGS if t not in used)

    def free_name(self, name):
        with self.lock:
            taken = {m["name"] for m in self.data["members"].values()} | {self.me["name"]}
            base, number = name, 2
            while name in taken:
                name, number = "%s-%d" % (base[:13], number), number + 1
            return name

    def key_ok(self, token):
        with self.lock:
            if token and hmac.compare_digest(token, self.data.get("secret", "")):
                return True
            old, until = self.data.get("old_secret"), float(self.data.get("old_until") or 0)
            return bool(token and old and time.time() < until and hmac.compare_digest(token, old))

    def rekey(self, secret=None, removed=None):
        """Change the shared key (to `secret`, or a new one). The old one works a little longer."""
        with self.lock:
            self.data["old_secret"] = self.data["secret"]
            self.data["old_until"] = time.time() + REKEY_GRACE
            self.data["secret"] = secret or secrets.token_urlsafe(32)
            self.merge({}, removed)
            return self.data["secret"]

    def find(self, name):
        with self.lock:
            return next((mid for mid, m in self.data["members"].items() if m["name"] == name), None)

    def cursor(self, mid):
        with self.lock:
            return int(self.data.setdefault("cursors", {}).get(mid, 0))

    def set_cursor(self, mid, seq):
        with self.lock:
            self.data.setdefault("cursors", {})[mid] = int(seq)
            self.save()

    def claim(self, task, agent):
        """Settle a task posted here: the first agent to ask gets it. Returns the holder."""
        with self.lock:
            claims = self.data.setdefault("claims", {})
            if task not in claims:
                claims[task] = agent
                self.save()
            return claims[task]


# --------------------------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------------------------
class PeerSync:
    """Plugs into the Hub as hub.sync: the hub's own messages are served to members that pull
    them; messages and rosters pulled from members are handed to the hub."""

    def __init__(self, hub, mesh, log=None):
        self.hub, self.mesh = hub, mesh
        self.log = log or (lambda text: globals()["log"](text))
        self.device_id = mesh.me["id"]
        self.invites = {}  # single-use join code -> expiry (memory only)
        self.threads = {}  # member id -> its puller thread
        self.failures = {}  # member id -> failed pulls in a row
        self.agents = {}  # member id -> its last roster
        self.stopped = threading.Event()
        self.removed_me = False

    def start(self):
        hub, me = self.hub, self.mesh.me
        with hub.lock:
            hub.prefix, hub.device, hub.sync = me["tag"], me["name"], self
            # Tasks posted here before a restart keep their first taker.
            for task, agent in hub.taken.items():
                if any(m["id"] == task and m.get("origin") == self.device_id for m in hub.messages):
                    self.mesh.data.setdefault("claims", {}).setdefault(task, agent)
        for mid in self.mesh.members():
            self._watch(mid)
        return self

    def stop(self):
        self.stopped.set()

    def _watch(self, mid):
        if mid in self.threads or self.stopped.is_set():
            return
        thread = threading.Thread(target=self._puller, args=(mid,), name="crewchat-peer-" + mid, daemon=True)
        self.threads[mid] = thread
        thread.start()

    # The hub's side --------------------------------------------------------------------------
    def publish(self, msg):
        pass  # members pull; the hub's change notification wakes their waiting pulls

    def state_changed(self):
        pass

    def claim_task(self, task, agent):
        with self.hub.lock:
            msg = next((m for m in self.hub.messages if m["id"] == task), None)
        origin = (msg or {}).get("origin") or self.device_id
        if origin == self.device_id:
            return self.mesh.claim(task, agent)
        member = self.mesh.members().get(origin)
        if member is None:
            raise crewchat.HubError("task #%s came from a machine that has left the chat" % task)
        try:
            return self._call(member, "/peer/claim", {"id": task, "agent": agent}, timeout=15)["holder"]
        except (PeerError, KeyError) as e:
            raise crewchat.HubError("cannot reach %s, the machine task #%s was posted on (%s). Try again when "
                                    "it is back." % (member["name"], task, e))

    # Talking to members ----------------------------------------------------------------------
    def _call(self, member, path, body, timeout=PULL_WAIT + 20, key=None):
        body = dict(body, sender=dict(self.mesh.me, id=self.device_id))
        req = urllib.request.Request(
            member["url"] + path, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + (key or self.mesh.data["secret"]),
                     "X-Crewchat-Device": self.device_id})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as answer:
                return json.loads(answer.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                reason = json.loads(e.read().decode("utf-8")).get("error") or "HTTP %d" % e.code
            except ValueError:
                reason = "HTTP %d" % e.code
            raise PeerError(reason, e.code)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise PeerError(str(getattr(e, "reason", e)))

    def _puller(self, mid):
        version, attempt = "", 0
        while not self.stopped.is_set():
            member = self.mesh.members().get(mid)
            if member is None:  # removed
                self.hub.drop_remote(mid)
                self.threads.pop(mid, None)
                return
            try:
                out = self._call(member, "/peer/pull", {"since": self.mesh.cursor(mid), "v": version,
                                                        "wait": PULL_WAIT})
            except PeerError as e:
                if self.stopped.is_set():
                    return
                if e.status == 403:  # this machine was removed
                    self._removed()
                    return
                attempt += 1
                self.failures[mid] = self.failures.get(mid, 0) + 1
                if self.failures[mid] == OFFLINE_AFTER:
                    self.log("%s is not reachable (%s)" % (member["name"], e))
                    self.hub.set_remote(mid, member["name"], self.agents.get(mid, []), 0)
                self.stopped.wait(BACKOFF[min(attempt, len(BACKOFF)) - 1])
                version = ""
                continue
            if self.stopped.is_set():
                return
            if self.failures.get(mid, 0) >= OFFLINE_AFTER:
                self.log("%s is back" % member["name"])
            self.failures[mid], attempt = 0, 0
            self._apply(mid, member, out)
            version = str(out.get("v", ""))

    def _removed(self):
        """Another member removed this machine: stop syncing; its own chat carries on."""
        with self.hub.lock:
            if self.removed_me:
                return
            self.removed_me = True
            if self.hub.sync is self:
                self.hub.sync = None
        self.stop()
        self.log("this machine was removed from the chat between your machines; its agents now only see "
                 "each other. To rejoin: `crewchat peers leave`, then join with a new code")
        for mid in list(self.hub.remote):
            self.hub.drop_remote(mid)

    def _apply(self, mid, member, out):
        if self.device_id in (out.get("removed") or {}):
            self._removed()
            return
        for new in self.mesh.merge(out.get("members") or {}, out.get("removed") or {}):
            self.log("%s joined" % self.mesh.members().get(new, {}).get("name", new))
            self._watch(new)
        for gone in (out.get("removed") or {}):
            self.hub.drop_remote(gone)
        last = self.mesh.cursor(mid)
        for msg in out.get("messages") or []:
            if not isinstance(msg, dict) or msg.get("origin") != mid:
                continue
            self.hub.ingest(msg)
            last = max(last, int(msg.get("seq") or 0))
        if last != self.mesh.cursor(mid):
            self.mesh.set_cursor(mid, last)
        self.agents[mid] = out.get("agents") or []
        self.hub.set_remote(mid, member["name"], self.agents[mid], time.time())

    # Requests from members -------------------------------------------------------------------
    def peer_request(self, path, token, device, body):
        """A request from another member (or, for /peer/join, a machine with a code).
        Returns (HTTP status, JSON object)."""
        if path == "/peer/join":
            return self._join(body)
        if not self.mesh.key_ok(token):
            return 401, {"error": "wrong key: this machine is not a member, or was left behind when the "
                                  "key changed; join again with a new code"}
        if device in self.mesh.data.get("removed", {}):
            return 403, {"error": "this machine was removed from the chat"}
        sender = body.get("sender") if isinstance(body.get("sender"), dict) else {}
        if device and sender.get("id") == device:
            for new in self.mesh.merge({device: sender}):
                self.log("%s joined" % sender.get("name", new))
                self._watch(new)
        if path == "/peer/pull":
            return 200, self._pull(body)
        if path == "/peer/claim":
            task = crewchat.clean_id(body.get("id", ""))
            with self.hub.lock:
                ours = any(m["id"] == task and m.get("origin") == self.device_id for m in self.hub.messages)
            if not ours:
                return 404, {"error": "task #%s was not posted here" % task}
            agent = str(body.get("agent") or "")
            if not crewchat.NAME_RE.match(agent):
                return 400, {"error": "bad agent name"}
            return 200, {"holder": self.mesh.claim(task, agent)}
        if path == "/peer/rekey":
            secret = str(body.get("secret") or "")
            if len(secret) < 32:
                return 400, {"error": "bad key"}
            self.mesh.rekey(secret, body.get("removed") or {})
            for gone in body.get("removed") or {}:
                self.hub.drop_remote(gone)
            self.mesh.save()
            return 200, {"ok": True}
        if path == "/peer/leave":
            if not device or device not in self.mesh.members():
                return 404, {"error": "not a member"}
            self.remove(device)
            return 200, {"ok": True}
        return 404, {"error": "unknown request"}

    def _join(self, body):
        code = "".join(str(body.get("code") or "").upper().split()).replace("-", "")
        with self.mesh.lock:
            now = time.time()
            self.invites = {c: t for c, t in self.invites.items() if t > now}
            if not code or self.invites.pop(code, None) is None:
                return 401, {"error": "that code is wrong, already used or expired; ask for a new one with "
                                      "`crewchat peers invite`"}
            sender = body.get("device") if isinstance(body.get("device"), dict) else {}
            url = clean_url(sender.get("url"))
            if not url.startswith(("http://", "https://")):
                return 400, {"error": "this machine has no address the others can reach"}
            mid = secrets.token_hex(8)
            info = {"name": self.mesh.free_name(crewchat.place_label(str(sender.get("name") or "machine"))),
                    "tag": self.mesh.free_tag(), "url": url}
            self.mesh.merge({mid: info})
            answer = {"mesh": self.mesh.data["mesh"], "secret": self.mesh.data["secret"],
                      "you": dict(info, id=mid), "members": self.mesh.everyone(),
                      "removed": self.mesh.data.get("removed", {}), "project": self.hub.roster.project}
        self.log("%s joined" % info["name"])
        self._watch(mid)
        return 200, answer

    @staticmethod
    def _state_key(agents):
        """What changed in this machine's roster, ignoring 'last seen' moving within a minute."""
        rows = [dict(r, seen=int(r.get("seen") or 0) // 60) for r in agents]
        return hashlib.sha256(json.dumps(rows, sort_keys=True).encode("utf-8")).hexdigest()[:16]

    def _pull(self, body):
        """Wait until this machine has written messages past `since` or its roster differs from
        the one the caller has (`v`), then send both. Only this machine's own changes wake a
        pull: what it hears from other members is theirs to send, so news never echoes around."""
        try:
            since = int(body.get("since") or 0)
            wait = max(0, min(PULL_WAIT, int(body.get("wait") or 0)))
        except (TypeError, ValueError):
            since, wait = 0, 0
        known = str(body.get("v", ""))
        deadline = time.time() + wait
        hub = self.hub
        with hub.lock:
            while True:
                own = [m for m in hub.messages if m["seq"] > since and m.get("origin") == self.device_id]
                agents = hub.local_state()
                key = self._state_key(agents)
                if own or key != known or time.time() >= deadline or self.stopped.is_set():
                    break
                hub.lock.wait(timeout=max(0.0, deadline - time.time()))
            own = [dict(m) for m in own[:MAX_BATCH]]
        return {"messages": own, "v": key, "agents": agents, "members": self.mesh.everyone(),
                "removed": self.mesh.data.get("removed", {})}

    # Membership changes ----------------------------------------------------------------------
    def new_invite(self):
        code = "".join(secrets.choice(crewchat.CODE_ALPHABET) for _ in range(8))
        with self.mesh.lock:
            self.invites[code] = time.time() + INVITE_TTL
        return code

    def remove(self, mid):
        """Drop a member and change the key so it is shut out. Returns who heard of the change."""
        members = self.mesh.members()
        gone = members.get(mid)
        if gone is None:
            raise PeerError("no member with that id")
        old = self.mesh.data["secret"]
        secret = self.mesh.rekey(removed={mid: gone["tag"]})
        self.hub.drop_remote(mid)
        told, missed = [], []
        for other, member in members.items():
            if other == mid:
                continue
            try:
                self._call(member, "/peer/rekey", {"secret": secret, "removed": {mid: gone["tag"]}}, timeout=15, key=old)
                told.append(member["name"])
            except PeerError:
                missed.append(member["name"])
        self.log("removed %s" % gone["name"])
        return told, missed


def admin(hub, root, op, data):
    """The owner's peer operations, run inside the server (see crewchat.Handler._admin)."""
    sync = hub.sync if isinstance(hub.sync, PeerSync) else None
    if op == "peers-start":
        if sync is None:
            if hub.sync is not None:
                raise PeerError("this server already syncs another way (cloud sync)")
            start(hub, root)
        return {"ok": True}
    if sync is None:
        raise PeerError("this server is not linked to other machines")
    if op == "peer-invite":
        return {"code": sync.new_invite()}
    if op == "peers-status":
        now, states = time.time(), {}
        for mid in sync.mesh.members():
            remote = hub.remote.get(mid)
            if sync.failures.get(mid, 0) >= OFFLINE_AFTER:
                states[mid] = {"state": "offline"}
            elif remote and now - remote["updated"] < PULL_WAIT * 3:
                states[mid] = {"state": "online"}
            else:
                states[mid] = {"state": "connecting"}
        return {"members": states}
    if op == "peer-remove":
        told, missed = sync.remove(str(data.get("id") or ""))
        return {"told": told, "missed": missed}
    if op == "peer-leave":
        told = []
        for member in sync.mesh.members().values():
            try:
                sync._call(member, "/peer/leave", {}, timeout=15)
                told.append(member["name"])
                break
            except PeerError:
                continue
        sync.stop()
        with hub.lock:
            hub.sync = None
            hub.device = ""
        for mid in list(hub.remote):
            hub.drop_remote(mid)
        return {"told": told}
    raise PeerError("unknown operation")


# --------------------------------------------------------------------------------------------
# Server and setup helpers
# --------------------------------------------------------------------------------------------
def configured(root=None):
    return Mesh(root).exists


def start(hub, root=None):
    """Start syncing this server's hub with the other members. Returns the PeerSync."""
    mesh = Mesh(root or hub.home)
    if not mesh.exists:
        raise PeerError("this machine is not linked to other machines; see `crewchat peers --help`")
    sync = PeerSync(hub, mesh).start()
    log("on as %s (tag %s), %d other machine(s)" % (mesh.me["name"], mesh.me["tag"], len(mesh.members())))
    return sync


def tailscale_address(port):
    """This machine's https address on the tailnet for the local server, setting up
    `tailscale serve` for it if needed. Returns (url, None) or (None, why not)."""
    exe = crewchat.find_tailscale()
    if not exe:
        return None, "Tailscale is not installed (https://tailscale.com/download)"
    try:
        status = json.loads(crewchat.run([exe, "status", "--json"]).stdout or "{}")
        host = (status.get("Self") or {}).get("DNSName", "").rstrip(".")
    except (OSError, ValueError):
        host = ""
    if not host or status.get("BackendState") not in (None, "Running"):
        return None, "Tailscale is not signed in on this machine"
    try:
        serve = json.loads(crewchat.run([exe, "serve", "status", "--json"]).stdout or "{}")
    except (OSError, ValueError):
        serve = {}
    targets = ("http://127.0.0.1:%d" % port, "http://localhost:%d" % port, "127.0.0.1:%d" % port,
               "localhost:%d" % port, "%d" % port)
    for site, conf in (serve.get("Web") or {}).items():
        proxy = ((conf.get("Handlers") or {}).get("/") or {}).get("Proxy", "")
        if proxy.rstrip("/") in targets:
            name, _, https = site.rpartition(":")
            return "https://%s%s" % (name, "" if https == "443" else ":" + https), None
    used = set((serve.get("TCP") or {}).keys())
    https = next((p for p in ("443", "8443", "10000", "4443") if p not in used), None)
    if https is None:
        return None, "Tailscale's https ports 443, 8443, 10000 and 4443 are all in use here"
    out = crewchat.run([exe, "serve", "--bg", "--https=%s" % https, "http://127.0.0.1:%d" % port])
    if out.returncode != 0:
        return None, ("`tailscale serve` did not start: %s" % (out.stderr.strip() or out.stdout.strip()))
    return "https://%s%s" % (host, "" if https == "443" else ":" + https), None


def my_address(config, given=None):
    """The address other members use to reach this machine, saved in config.json."""
    url = clean_url(given or config.get("url"))
    if not url:
        url, why = tailscale_address(int(config["port"]))
        if not url:
            raise PeerError("other machines need an address for this one. %s. Or pass --url "
                            "https://<this machine's private address>." % why)
    if url != config.get("url"):
        config["url"] = url
        crewchat.save_config(config)
    return url


def server_call(op, **body):
    """Ask this machine's running server to do something (as the owner)."""
    return crewchat.owner_call("/api/admin", dict(body, op=op))


def turn_on(config):
    """Mark peers on and start them in the running server (no restart needed)."""
    if config.get("cloud"):
        raise PeerError("this machine uses cloud sync; a machine syncs one way. Turn it off first with "
                        "`crewchat cloud logout`")
    config["peers"] = True
    crewchat.save_config(config)
    if crewchat.server_up(config):
        server_call("peers-start")
    else:
        raise PeerError("the server is not running here; start it with `crewchat start` or `crewchat serve`")


# --------------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------------
def cmd_peers(args):
    try:
        _cmd_peers(args)
    except PeerError as e:
        crewchat.die(str(e))


def _cmd_peers(args):
    root = crewchat.home()
    if not (root / "config.json").exists():
        crewchat.setup_host(Path.cwd().name)
    config = crewchat.load_config()
    mesh = Mesh(root)
    action = args.action

    if action == "invite":
        if not mesh.exists:
            url = my_address(config, args.url)
            mesh.found(args.name or machine_name(), url)
            print("Started a chat between your machines, with this one (%s) as the first member." % mesh.me["name"])
        turn_on(config)
        code = server_call("peer-invite")["code"]
        print("On the other machine, after installing crewchat and running `crewchat start` in its project")
        print("folder, run this within %d minutes (the code works once):" % (INVITE_TTL // 60))
        print()
        print("  crewchat peers join %s %s-%s" % (mesh.me["url"], code[:4], code[4:]))
        print()
        print("Both machines must be signed in to the same Tailscale account (or private network).")

    elif action == "join":
        if mesh.exists:
            raise PeerError("this machine is already linked (as %s). To move it, run `crewchat peers leave` first"
                            % mesh.me["name"])
        if not args.target or not args.code:
            raise PeerError("usage: crewchat peers join URL CODE (from `crewchat peers invite` on a member)")
        if config.get("cloud"):
            raise PeerError("this machine uses cloud sync; a machine syncs one way. Turn it off first with "
                            "`crewchat cloud logout`")
        url = my_address(config, args.url)
        target = clean_url(args.target)
        body = json.dumps({"code": args.code, "device": {"name": args.name or machine_name(), "url": url}})
        req = urllib.request.Request(target + "/peer/join", data=body.encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as answer:
                out = json.loads(answer.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                reason = json.loads(e.read().decode("utf-8")).get("error")
            except ValueError:
                reason = None
            raise PeerError(reason or "%s refused (HTTP %d)" % (target, e.code))
        except (urllib.error.URLError, OSError) as e:
            raise PeerError("cannot reach %s (%s). Is that machine on, with Tailscale connected?"
                            % (target, getattr(e, "reason", e)))
        you = out["you"]
        mesh.adopt(out, {"id": you["id"], "name": you["name"], "tag": you["tag"], "url": url})
        turn_on(config)
        others = ", ".join(m["name"] for m in mesh.members().values())
        print("This machine joined as \"%s\". Its agents now share the chat with: %s." % (you["name"], others))
        print("Messages written here are numbered %s1, %s2, ..." % (you["tag"], you["tag"]))

    elif action == "status":
        if not mesh.exists:
            print("Not linked to other machines. To link: `crewchat peers invite` on one machine, then "
                  "`crewchat peers join` on the other.")
            return
        print("This machine: %s (tag %s), reachable at %s" % (mesh.me["name"], mesh.me["tag"], mesh.me["url"]))
        rows = server_call("peers-status").get("members", {}) if crewchat.server_up(config) else {}
        for mid, member in mesh.members().items():
            state = rows.get(mid, {}).get("state", "unknown (server not running)" if not rows else "connecting")
            print("  %-16s tag %-3s %-10s %s" % (member["name"], member["tag"], state, member["url"]))
        if not mesh.members():
            print("  No other machines yet: `crewchat peers invite`.")

    elif action == "remove":
        if not mesh.exists:
            raise PeerError("this machine is not linked to other machines")
        mid = mesh.find(args.target or "")
        if mid is None:
            raise PeerError("usage: crewchat peers remove NAME, where NAME is one of: %s"
                            % (", ".join(m["name"] for m in mesh.members().values()) or "(no other machines)"))
        out = server_call("peer-remove", id=mid)
        print("Removed %s and changed the shared key." % args.target)
        if out.get("missed"):
            print("Not reachable just now, so left behind until they join again: %s" % ", ".join(out["missed"]))

    elif action == "leave":
        if not mesh.exists:
            print("This machine is not linked to other machines.")
            return
        if crewchat.server_up(config):
            out = server_call("peer-leave")
            if not out.get("told"):
                print("No other machine could be told; remove this one there with `crewchat peers remove %s`."
                      % mesh.me["name"])
        mesh.path.unlink()
        config["peers"] = False
        crewchat.save_config(config)
        print("This machine left. Its agents now only see each other. Restart the server to finish: "
              "`crewchat service restart` (or restart `crewchat serve`).")


def add_parser(sub):
    p = sub.add_parser("peers", help="link this machine's crewchat with your other machines over Tailscale",
                       description="Each machine runs its own crewchat; linked machines share one chat over a "
                       "private network such as Tailscale, with no cloud service. Start with `crewchat peers "
                       "invite` on one machine, then run the join command it prints on the other.")
    p.add_argument("action", choices=["invite", "join", "status", "remove", "leave"])
    p.add_argument("target", nargs="?", help="join: the address from the invite; remove: the machine's name")
    p.add_argument("code", nargs="?", help="join: the code from the invite")
    p.add_argument("--name", help="this machine's name in the chat (default: its host name)")
    p.add_argument("--url", help="this machine's address for the others (default: set up with Tailscale)")
    p.set_defaults(fn=cmd_peers)
