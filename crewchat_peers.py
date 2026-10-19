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
import re
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
MISSING_WAIT = 30  # seconds before asking again a member that has no project by this name
INVITE_TTL = 600
REKEY_GRACE = 600  # the old key still works this long after a change, while members catch up
OFFLINE_AFTER = 2  # failed pulls in a row before a member shows as offline
BACKOFF = (1, 2, 5, 10, 20, 30)
MAX_BATCH = 500
RELAY_EVERY = 300  # seconds between asking others for an unreachable member's messages (Y3)
TAGS = [chr(c) for c in range(ord("A"), ord("Z") + 1)]
TAGS += [a + b for a in TAGS for b in TAGS]


MAX_DECISIONS = 500  # claim decisions kept, newest first; a pull sends those the asker has not had
DECIDED = "decided:"  # the cursor of the decisions had from a member: its key is this and the member's id


def tag_rank(tag):
    """A machine tag's place in TAGS (A, B, ... Z, AA, ...): the lowest coordinates when nothing else decides."""
    return TAGS.index(tag) if tag in TAGS else len(TAGS)


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
            try:  # on Windows the server may be replacing it this moment
                self.data = crewchat.retry_on_windows(lambda: json.loads(self.path.read_text(encoding="utf-8")))
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

    def claim(self, task, agent, origin=None, term=0):
        """Settle a claim: the first agent to ask gets it. Returns the holder. Each decision is recorded with
        the machine it was for (origin: this one, unless it settles for a machine that is off, as the
        coordinator), its term, and a Lamport clock and this machine's tag, so that two decisions on one claim
        made while machines could not reach each other can be ordered afterwards (merge_decisions)."""
        with self.lock:
            claims = self.data.setdefault("claims", {})
            if task not in claims:
                claims[task] = agent
                clock = self.data["clock"] = int(self.data.get("clock") or 0) + 1
                decided = self.data.setdefault("decided", {})
                decided[task] = {"agent": agent, "clock": clock, "tag": self.me["tag"], "term": int(term),
                                 "origin": origin or self.me["id"], "n": self._next_n()}
                for old in sorted(decided, key=lambda k: decided[k]["clock"])[:-MAX_DECISIONS]:
                    del decided[old]
                self.save()
            return claims[task]

    def _next_n(self):
        """The number of a change to the decisions here, counted up on this machine: a pull asks for those after
        the last it got (decisions_after), not for all of them each time. Caller holds the lock."""
        n = self.data["decided_n"] = int(self.data.get("decided_n") or 0) + 1
        return n

    def decisions(self):
        with self.lock:
            return {k: dict(v) for k, v in self.data.get("decided", {}).items()}

    def decisions_after(self, n):
        """(the decisions made or adopted here after change number n, the number of the last change): what a member
        that has had those up to n has not. All of them when n is not one of this machine's (0, or from before its
        peers.json was made again)."""
        with self.lock:
            last = int(self.data.get("decided_n") or 0)
            decided = self.data.get("decided", {})
            if not 0 < n <= last:
                return {k: dict(v) for k, v in decided.items()}, last
            return {k: dict(v) for k, v in decided.items() if int(v.get("n") or 0) > n}, last

    def merge_decisions(self, decisions):
        """Another machine's claim decisions. A claim decided only there is adopted; one decided both here and
        there for two agents keeps the earlier (Lamport clock, then tag). Returns [(key, winner, loser)] for
        the claims whose holder changed here from a decision made here: this machine tells the others. One that
        only adopted the losing decision switches quietly, so the news is given once, by the machine whose
        decision lost."""
        changed, touched = [], False
        with self.lock:
            claims = self.data.setdefault("claims", {})
            decided = self.data.setdefault("decided", {})
            clock = start = int(self.data.get("clock") or 0)
            for key, theirs in (decisions or {}).items():
                try:
                    theirs = {"agent": str(theirs["agent"]), "clock": int(theirs["clock"]), "tag": str(theirs["tag"]),
                              "term": int(theirs.get("term") or 0), "origin": str(theirs.get("origin") or "")}
                except (KeyError, TypeError, ValueError):
                    continue
                if not isinstance(key, str) or not crewchat.NAME_RE.match(theirs["agent"]):
                    continue
                clock = max(clock, theirs["clock"])  # Lamport: the next decision here (+1) comes after all seen
                holder = claims.get(key)
                if holder is None:
                    claims[key], decided[key], touched = theirs["agent"], dict(theirs, n=self._next_n()), True
                elif holder != theirs["agent"]:
                    mine = decided.get(key) or {"clock": 0, "tag": ""}  # settled before decisions were kept: first
                    if (theirs["clock"], tag_rank(theirs["tag"])) < (int(mine["clock"]), tag_rank(mine["tag"])):
                        claims[key], decided[key], touched = theirs["agent"], dict(theirs, n=self._next_n()), True
                        if mine["tag"] in ("", self.me["tag"]):
                            changed.append((key, theirs["agent"], holder))
            if touched or clock != start:  # most pulls bring nothing new: peers.json is not rewritten for them
                self.data["clock"] = clock
                self.save()
        return changed


# --------------------------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------------------------
class PeerSync(crewchat.ProjectLink):
    """Plugs into the Hub as hub.sync: the hub's own messages are served to members that pull
    them; messages and rosters pulled from members are handed to the hub.

    Projects: the first project's PeerSync is the machine's link (members, invites, keys). Every
    other project has a PeerSync of its own (chat = its name), over the same members and key, that
    pulls and serves only that project: projects with the same name on linked machines are one
    chat, and a project one machine lacks stays on the other."""

    def __init__(self, hub, mesh, log=None, chat=None):
        self._init_projects(hub, chat)
        self.mesh = mesh
        self.wake = {}  # member id -> Event: it asked us for this project, so ask it again now
        self.log = log or (lambda text: globals()["log"](text))
        self.device_id = mesh.me["id"]
        self.invites = {}  # single-use join code -> expiry (memory only)
        self.threads = {}  # member id -> its puller thread
        self.failures = {}  # member id -> failed pulls in a row
        self.relayed = {}  # member id -> when its messages were last asked of the others (Y3)
        self.relaying = set()  # members whose messages are being fetched from the others now
        self.log_clear = float("inf")  # pulls from at least this point need not read the log: nothing of ours there
        self.agents = {}  # member id -> its last roster
        self.stopped = threading.Event()
        self.removed_me = False
        self._coordinator, self._term = None, 0  # who this machine last saw coordinating, and how many changes

    def coordinator(self, exclude=()):
        """Which machine coordinates now (roadmap Y1): the owner's preferred machine if this one can reach it,
        else the machine with the lowest tag among those it can reach (itself included). Every machine applies
        the same rule to what it can reach, so they agree while they can reach each other; the term goes up
        at every change this machine sees. The coordinator never orders messages: they stay leaderless."""
        me, members = self.mesh.me, self.mesh.members()
        preferred = str((self.mesh.data.get("preferred") or {}).get("id") or "")
        here = {me["id"]: me}
        here.update({mid: m for mid, m in members.items() if self.reachable(mid) and mid not in exclude})
        pick = preferred if preferred in here else min(here, key=lambda mid: (tag_rank(here[mid].get("tag")), mid))
        with self.mesh.lock:
            if pick != self._coordinator:
                if self._coordinator is not None:
                    self.log("%s coordinates now" % here[pick]["name"])
                self._coordinator, self._term = pick, self._term + 1
            term = self._term
        named = dict(members, **{me["id"]: me}).get(preferred) or {}
        return {"name": here[pick]["name"], "me": pick == me["id"], "term": term, "preferred": named.get("name", ""),
                "id": pick}

    def set_preferred(self, name):
        """The owner's preferred coordinator (a machine's name), or none. It travels to the others with the next
        pull; the newest choice wins."""
        name = str(name or "").strip()
        if name.lower() in ("", "none"):
            mid = ""
        elif name == self.mesh.me["name"]:
            mid = self.mesh.me["id"]
        else:
            mid = self.mesh.find(name)
            if mid is None:
                raise PeerError("no machine called %s; `crewchat peers status` lists them" % name)
        with self.mesh.lock:
            self.mesh.data["preferred"] = {"id": mid, "at": time.time()}
            self.mesh.save()
        with self.hub.lock:
            self.hub._changed()  # wakes the others' waiting pulls, so they hear of it now
        return self.coordinator()

    def link_status(self):
        members = self.mesh.members()
        machines = sorted(({"name": m.get("name", ""), "online": self.reachable(mid)}
                           for mid, m in members.items() if mid != self.device_id), key=lambda m: m["name"].lower())
        return {"via": "tailscale", "me": self.mesh.me.get("name", ""), "machines": machines}

    def reachable(self, mid):
        """Is this member answering: it has a puller and its last pulls worked (the chat page's dot)."""
        return mid in self.threads and self.failures.get(mid, 0) < OFFLINE_AFTER

    def _key(self, key):
        """Cursors and claims are kept per project: the first project's under the plain key."""
        return key if self.chat is None else "%s|%s" % (self.chat, key)

    def start(self):
        hub, me = self.hub, self.mesh.me
        first_time = False
        if self.chat is not None:
            with self.mesh.lock:  # said once per project, not at every start
                announced = self.mesh.data.setdefault("announced", [])
                if self.chat not in announced:
                    announced.append(self.chat)
                    self.mesh.save()
                    first_time = True
        adopted = hub.adopt_open_tasks(me["tag"], self.device_id) if hasattr(hub, "adopt_open_tasks") else {}
        if first_time:
            hub.announce_shared(self.chat, adopted)
        with hub.lock:
            hub.prefix, hub.device, hub.sync = me["tag"], me["name"], self
            # Tasks posted here before a restart keep their first taker.
            own = {m["id"] for m in hub.messages if m.get("origin") == self.device_id}
            for task, agent in hub.taken.items():
                if task in own:
                    self.mesh.data.setdefault("claims", {}).setdefault(self._key(task), agent)
        for mid in self.mesh.members():
            self._watch(mid)
        return self

    def stop(self):
        self.stopped.set()
        for event in list(self.wake.values()):
            event.set()
        with self.projects_lock:
            projects, self.projects = list(self.projects.values()), {}
        for project in projects:
            project.detach()

    def _wake(self, mid):
        """The Event that wakes this project's puller for a member (made on first use)."""
        event = self.wake.get(mid)
        if event is None:
            event = self.wake[mid] = threading.Event()
        return event

    def stopping(self):
        return self.stopped.is_set()

    def _project_link(self, hub, name):
        return PeerSync(hub, self.mesh, self.log, chat=name)

    def _member_joined(self, mid, name):
        """A machine joined the link (by code, by its own request, or heard of from another member):
        the link and every shared project start following it."""
        self.log("%s joined" % name)
        self._watch(mid)
        with self.projects_lock:
            projects = list(self.projects.values())
        for project in projects:
            project._watch(mid)

    def _watch(self, mid):
        if mid in self.threads or self.stopped.is_set():
            return
        thread = threading.Thread(target=self._puller, args=(mid,), name="crewchat-peer-" + mid, daemon=True)
        self.threads[mid] = thread
        thread.start()

    # The hub's side --------------------------------------------------------------------------
    def publish(self, msg):
        # Members pull; the hub's change notification wakes their waiting pulls. A message of its own: once out
        # of memory, it is in the log only.
        self.log_clear = float("inf")

    def state_changed(self):
        pass

    def claim_task(self, task, agent, key=None):
        """Who gets a task: settled by the machine it was posted on, under its claim key (the task's
        id, or a new key each time it was given back)."""
        with self.hub.lock:
            msg = self.hub.by_id.get(task)
            row = getattr(self.hub, "sheet", {}).get(task) or {}
        origin = (msg or {}).get("origin") or row.get("origin") or self.device_id
        if origin == self.device_id:
            return self.mesh.claim(self._key(key or task), agent)
        members = self.mesh.members()
        member = members.get(origin)
        if member is None:
            raise crewchat.HubError("task #%s came from a machine that has left the chat" % task)
        try:
            return self._call(member, "/peer/claim", {"id": task, "agent": agent, "key": key or task},
                              timeout=15)["holder"]
        except (PeerError, KeyError) as e:
            unreachable = e
        # The machine it was posted on is off: the coordinator settles it, under its term (roadmap Y2); that
        # machine adopts the decision when it is back.
        coordinator = self.coordinator(exclude={origin})
        if coordinator["me"]:
            return self.mesh.claim(self._key(key or task), agent, origin, coordinator["term"])
        try:
            return self._call(members[coordinator["id"]], "/peer/claim", {
                "id": task, "agent": agent, "key": key or task, "for": origin, "term": coordinator["term"]},
                timeout=15)["holder"]
        except (PeerError, KeyError) as e:
            raise crewchat.HubError("cannot reach %s, the machine task #%s was posted on (%s), nor %s, which "
                                    "coordinates (%s). Try again when one is back." % (
                                        member["name"], task, unreachable, coordinator["name"], e))

    def fetch_file(self, origin, fid):
        """The bytes of a file shared on another member (Hub.open_file)."""
        member = self.mesh.members().get(origin)
        if member is None:
            raise crewchat.HubError("that file was shared on a machine that has left the chat")
        try:
            return self._call(member, "/peer/file", {"id": fid}, timeout=120, raw=True)
        except PeerError as e:
            raise crewchat.HubError("cannot fetch the file from %s (%s)" % (member["name"], e))

    # Talking to members ----------------------------------------------------------------------
    def _call(self, member, path, body, timeout=PULL_WAIT + 20, key=None, raw=False):
        """POST to another member; its JSON answer, or with raw its bytes."""
        body = dict(body, sender=dict(self.mesh.me, id=self.device_id))
        if self.chat is not None:
            body["chat"] = self.chat
        req = urllib.request.Request(
            member["url"] + path, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + (key or self.mesh.data["secret"]),
                     "X-Crewchat-Device": self.device_id})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as answer:
                data = answer.read()
                return data if raw else json.loads(data.decode("utf-8"))
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
                out = self._call(member, "/peer/pull", {"since": self.mesh.cursor(self._key(mid)), "v": version,
                                                        "wait": PULL_WAIT,
                                                        "decided": self.mesh.cursor(self._key(DECIDED + mid))})
                if self.chat is not None and out.get("chat") != self.chat:
                    # An older crewchat answers with its first project whatever we ask: not ours.
                    raise PeerError("no project %s there" % self.chat, 404)
            except PeerError as e:
                if self.stopped.is_set():
                    return
                if self.chat is not None and e.status == 404:
                    # That machine has no project by this name: nothing to share, nothing wrong.
                    if mid in self.shared_with:
                        self.shared_with.discard(mid)
                        self.hub.drop_remote(mid)
                    wake = self._wake(mid)
                    wake.clear()
                    if not self.stopped.is_set():
                        wake.wait(MISSING_WAIT)
                    version = ""
                    continue
                if e.status == 403:  # this machine was removed
                    self._removed()
                    return
                attempt += 1
                self.failures[mid] = self.failures.get(mid, 0) + 1
                if self.failures[mid] == OFFLINE_AFTER:
                    self.log("%s is not reachable (%s)" % (member["name"], e))
                    self.hub.set_remote(mid, member["name"], self.agents.get(mid, []), 0)
                if self.failures[mid] >= OFFLINE_AFTER and time.time() - self.relayed.get(mid, 0) >= RELAY_EVERY:
                    self.relayed[mid] = time.time()
                    threading.Thread(target=self.catch_up, args=(mid,), daemon=True).start()
                self.stopped.wait(BACKOFF[min(attempt, len(BACKOFF)) - 1])
                version = ""
                continue
            if self.stopped.is_set():
                return
            if self.failures.get(mid, 0) >= OFFLINE_AFTER:
                self.log("%s is back" % member["name"])
            self.failures[mid], attempt = 0, 0
            try:
                self._apply(mid, member, out)
            except Exception as e:  # a surprise here must not end this thread, and the link with it
                self.log("could not apply what %s sent (%s: %s); trying again" % (member["name"], type(e).__name__, e))
                self.stopped.wait(BACKOFF[-1])
                version = ""
                continue
            version = str(out.get("v", ""))

    def catch_up(self, mid):
        """A member this machine cannot reach (roadmap Y3): what it wrote that this machine has not seen, from the
        coordinator first, else from any member that can be reached, which pulled it while it was on. Paged. The
        messages keep that member's own numbers, so pulling from it again once it is back starts after them.
        Returns how many messages arrived."""
        if mid in self.relaying:
            return 0
        self.relaying.add(mid)
        got = 0
        try:
            members, coordinator = self.mesh.members(), self.coordinator()["id"]
            sources = sorted((m for m in members if m != mid and self.reachable(m)), key=lambda m: m != coordinator)
            if not sources:  # nobody else can be reached yet either: try again at the next failed pull
                self.relayed.pop(mid, None)
            for source in sources:
                while not self.stopped.is_set():
                    cursor = self._key(mid)
                    since = self.mesh.cursor(cursor)
                    try:
                        out = self._call(members[source], "/peer/relay", {"origin": mid, "since": since}, timeout=60)
                    except PeerError as e:
                        self.log("could not catch up on %s's messages through %s (%s)" % (
                            members.get(mid, {}).get("name", mid), members[source]["name"], e))
                        break
                    last = since
                    for msg in out.get("messages") or []:
                        if not isinstance(msg, dict) or msg.get("origin") != mid:
                            continue
                        number = crewchat.origin_number(msg.get("id"))
                        if number <= since:
                            continue
                        try:
                            if self.hub.ingest(msg):
                                got += 1
                        except Exception as e:  # skip it, as a pull does
                            self.log("skipped message #%s (%s: %s)" % (msg.get("id"), type(e).__name__, e))
                        last = max(last, number)
                    if last > since:
                        self.mesh.set_cursor(cursor, last)
                    if not out.get("more") or last == since:
                        break
            if got:
                self.log("caught up on %d of %s's messages through the others" % (got, members.get(mid, {}).get("name", mid)))
        finally:
            self.relaying.discard(mid)
        return got

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
        self._drop_remotes()

    def _apply(self, mid, member, out):
        removed = out.get("removed") or {}
        if self.chat is not None:
            self.shared_with.add(mid)
        else:  # membership is the machine's link's business
            if self.device_id in removed:
                self._removed()
                return
            for new in self.mesh.merge(out.get("members") or {}, removed):
                self._member_joined(new, self.mesh.members().get(new, {}).get("name", new))
            theirs, mine = out.get("preferred"), self.mesh.data.get("preferred") or {}
            if isinstance(theirs, dict) and float(theirs.get("at") or 0) > float(mine.get("at") or 0):
                with self.mesh.lock:  # the owner chose on that machine more recently: theirs holds
                    self.mesh.data["preferred"] = {"id": str(theirs.get("id") or ""), "at": float(theirs["at"])}
                    self.mesh.save()
        for gone in removed:
            self.hub.drop_remote(gone)
        self._settle(out.get("decisions"))
        if "decided" in out:  # an older crewchat sends no number: it sends all its decisions each time
            try:
                decided = int(out["decided"])
            except (TypeError, ValueError):
                decided = 0
            if decided != self.mesh.cursor(self._key(DECIDED + mid)):
                self.mesh.set_cursor(self._key(DECIDED + mid), decided)
        cursor = self._key(mid)
        start = last = self.mesh.cursor(cursor)
        for msg in out.get("messages") or []:
            if not isinstance(msg, dict) or msg.get("origin") != mid:
                continue
            try:
                seq = int(msg.get("seq") or 0)
            except (TypeError, ValueError):
                continue
            try:
                self.hub.ingest(msg)
            except Exception as e:  # skip it, so one message cannot stop all the ones after it
                self.log("skipped message #%s from %s (%s: %s)" % (msg.get("id"), member["name"], type(e).__name__, e))
            last = max(last, seq)
        if last != start:
            self.mesh.set_cursor(cursor, last)
        self.agents[mid] = out.get("agents") or []
        with self.hub.lock:  # (reentrant) detach() lets go of the hub under this lock
            if self.hub.sync is self:
                self.hub.set_remote(mid, member["name"], self.agents[mid], time.time())

    def _settle(self, decisions):
        """Claim decisions another machine made (for this link's project): adopt them, and where two machines
        gave one task to two agents, keep the earlier claim and say so (Hub.settle_claim)."""
        if not isinstance(decisions, dict):
            return
        prefix = "" if self.chat is None else self.chat + "|"
        mine = {k: v for k, v in decisions.items() if isinstance(k, str) and k.startswith(prefix) and (prefix or "|" not in k)}
        for key, winner, loser in self.mesh.merge_decisions(mine):
            task = key[len(prefix):].split("@")[0]
            try:
                self.hub.settle_claim(task, winner, loser)
            except Exception as e:  # a task this machine no longer has: nothing to tell
                self.log("could not settle the claim on #%s (%s)" % (task, e))

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
        if self.chat is not None and path not in ("/peer/pull", "/peer/file", "/peer/claim", "/peer/relay"):
            return 404, {"error": "unknown request"}
        sender = body.get("sender") if isinstance(body.get("sender"), dict) else {}
        if self.chat is None and device and sender.get("id") == device:
            for new in self.mesh.merge({device: sender}):
                self._member_joined(new, sender.get("name", new))
        if path == "/peer/pull":
            if self.chat is not None and device:
                self.shared_with.add(device)  # it asks for this project, so it has it too
                self._wake(device).set()
            return 200, self._pull(body)
        if path == "/peer/relay":  # another member's messages, for one that cannot reach it now (Y3)
            try:
                since = int(body.get("since") or 0)
            except (TypeError, ValueError):
                return 400, {"error": "since is a number"}
            origin = str(body.get("origin") or "")
            if not origin or origin == device:
                return 400, {"error": "relay is for another member's messages"}
            messages, more = self.hub.messages_after(origin, since, MAX_BATCH, by_id=True)
            return 200, {"messages": messages, "more": more}
        if path == "/peer/file":
            meta, local = self.hub.file(str(body.get("id") or ""))
            with self.hub.lock:  # only files shared here: a member never relays another's
                ours = meta is not None and any(
                    m.get("origin") == self.device_id and any(f["id"] == meta["id"] for f in m.get("files") or [])
                    for m in self.hub.messages)
            if not ours:
                return 404, {"error": "no such file here"}
            return 200, local.read_bytes()
        if path == "/peer/claim":
            task = crewchat.clean_id(body.get("id", ""))
            key = str(body.get("key") or task)
            if key != task and not re.match(r"^%s@\d{1,6}$" % re.escape(task), key):
                return 400, {"error": "bad claim key"}
            with self.hub.lock:
                ours = (self.hub.by_id.get(task) or {}).get("origin") == self.device_id or (
                    (getattr(self.hub, "sheet", {}).get(task) or {}).get("origin") == self.device_id)
            agent = str(body.get("agent") or "")
            if not crewchat.NAME_RE.match(agent):
                return 400, {"error": "bad agent name"}
            origin = str(body.get("for") or "")
            if not ours and origin and origin in self.mesh.members():
                # Asked as the coordinator, for a machine that is off: settle it here if this machine does
                # coordinate (it cannot reach that machine either); it adopts the decision when back.
                coordinator = self.coordinator(exclude={origin})
                if not coordinator["me"]:
                    return 409, {"error": "%s coordinates, not this machine" % coordinator["name"]}
                return 200, {"holder": self.mesh.claim(self._key(key), agent, origin, coordinator["term"])}
            if not ours:
                return 404, {"error": "task #%s was not posted here" % task}
            # This machine posted the task and saw every give-back of it: its count of them decides the
            # key, not the asker's, which may lag (a machine that joined later, or lost old messages).
            key = self.hub.claim_key(task) if hasattr(self.hub, "claim_key") else key
            return 200, {"holder": self.mesh.claim(self._key(key), agent)}
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
        self._member_joined(mid, info["name"])
        return 200, answer

    @staticmethod
    def _state_key(agents, preferred=None):
        """What changed in this machine's roster (or its owner's preferred coordinator), ignoring 'last seen'
        moving within a minute."""
        rows = [dict(r, seen=int(r.get("seen") or 0) // 60) for r in agents]
        return hashlib.sha256(json.dumps([rows, preferred], sort_keys=True).encode("utf-8")).hexdigest()[:16]

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
        older = self._older(since)  # the asker was away for longer than memory holds (Y3): at once, from the log
        if not older:
            with hub.lock:
                while True:
                    own = [m for m in hub.messages[hub._after(since):] if m.get("origin") == self.device_id]
                    agents = hub.local_state()
                    key = self._state_key(agents, [self.mesh.data.get("preferred"), self.mesh.data.get("clock")])
                    if own or key != known or time.time() >= deadline or self.stopped.is_set():
                        break
                    hub.lock.wait(timeout=max(0.0, deadline - time.time()))
                own = [dict(m) for m in own[:MAX_BATCH]]
            # More than memory holds was written while this pull waited: its first ones are in the log only.
            older = self._older(since) if own else []
        if older:
            own, agents, key = older, hub.local_state(), ""  # "" : the roster comes again with the next pull
        out = {"messages": own, "v": key, "agents": agents, "removed": self.mesh.data.get("removed", {}),
               "preferred": self.mesh.data.get("preferred")}
        try:
            after = int(body.get("decided"))
        except (TypeError, ValueError):  # an older crewchat asks for none: all of them, as it expects
            out["decisions"] = self.mesh.decisions()
        else:  # only those it has not had, and the number to ask after next time
            out["decisions"], out["decided"] = self.mesh.decisions_after(after)
        if self.chat is None:
            out["members"] = self.mesh.everyone()
        else:
            out["chat"] = self.chat  # so the asker knows this is the project it asked for (it ignores members)
        return out

    def _older(self, since):
        """This machine's messages after `since` when memory no longer reaches back that far (roadmap Y3): a page
        of them from the log, oldest first, or [] when memory has them all (or there are none)."""
        hub = self.hub
        with hub.lock:
            if not hub.messages or hub.messages[0]["seq"] <= since + 1 or since >= self.log_clear:
                return []
        older, _ = hub.messages_after(self.device_id, since, MAX_BATCH)
        if not older:  # nothing of this machine's after that point: no need to look again until it writes
            self.log_clear = min(self.log_clear, since)
        return older

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
        sync.detach()  # and every other project's link with it
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
        if not crewchat.server_up(config):
            # Before the code is used: the other machine would count this one in, and it could not start.
            raise PeerError("the server is not running here. Start it with `crewchat start` in your project "
                            "folder, then run this again; the code still works.")
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
        name = mesh.members()[mid]["name"]
        if not confirm(args, "This removes %s from the machines linked with this one (%s): its agents stop sharing "
                       "this chat. Remove %s?" % (name, mesh.me["name"], name)):
            print("Nothing changed.")
            return
        out = server_call("peer-remove", id=mid)
        print("Removed %s and changed the shared key." % args.target)
        if out.get("missed"):
            print("Not reachable just now, so left behind until they join again: %s" % ", ".join(out["missed"]))

    elif action == "leave":
        if not mesh.exists:
            print("This machine is not linked to other machines.")
            return
        others = ", ".join(m["name"] for m in mesh.members().values()) or "no other machines"
        if not confirm(args, "This takes THIS machine, %s, out of the chat it shares with %s. Run it on the "
                       "machine you mean to unlink. Leave?" % (mesh.me["name"], others)):
            print("Nothing changed.")
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


def confirm(args, question):
    """Ask before unlinking, when a person is at the terminal (not for scripts and agents, nor with --yes)."""
    if getattr(args, "yes", False) or not sys.stdin.isatty():
        return True
    try:
        return input(question + " [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


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
    p.add_argument("--yes", action="store_true", help="leave, remove: do not ask first")
    p.set_defaults(fn=cmd_peers)
