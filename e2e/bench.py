"""The benchmark (roadmap P0): a real server in a throwaway home, 5 agents and 5,000 messages, and the
numbers every speed change is measured by. Standard library only.

    python3 e2e/bench.py                       the table, medians of 9 runs
    python3 e2e/bench.py --json now.json       ... and the numbers, to compare against later
    python3 e2e/bench.py --compare base.json   exit 1 if a median got more than 20% worse

It measures, as the agents and the page see them:
- hook calls end to end, run exactly as Claude Code runs them (a process each): a tool call that tells
  the server, one the hook skips (it told the server a moment ago), a prompt, and the end of a turn;
- one /api/poll round trip of the chat page, opening the chat and catching up on one new message,
  and how big each answer is; and what loading the page itself transfers, the first time and on a reload;
- what every agent loads (the tool definitions) and reads (an inbox of 50 messages), in characters;
- how often state.json is rewritten under a fixed busy load (in this process, against a Hub);
- server startup with that messages.jsonl, until /health answers, and its memory then; with --log-mb, again
  with the log grown to that size.
Timings are noisy (more so on CI runners): compare medians, on the same machine.
"""
import argparse
import hashlib
import json
import shlex
import statistics
import subprocess
import sys
import time

import harness as H

LIMIT = 0.20  # --compare fails on a median more than this much worse


def timed(fn, runs, before=None):
    """Seconds each of runs calls of fn took (before() runs first, untimed)."""
    out = []
    for _ in range(runs):
        if before:
            before()
        start = time.perf_counter()
        fn()
        out.append(time.perf_counter() - start)
    return out


def state_file(agent):
    return H.HOOK_STATE / "crewchat-hooks" / ("%s-%s" % (agent.client, hashlib.sha256(agent.session.encode()).hexdigest()[:16]))


def tool_hook_command(agent):
    settings = json.loads((agent.folder / ".claude" / "settings.local.json").read_text())
    return settings["hooks"]["PostToolUse"][0]["hooks"][0]["command"]


def run_tool_hook(agent, command):
    payload = {"session_id": agent.session, "hook_event_name": "PostToolUse", "tool_name": "Bash",
               "tool_input": {"command": "npm test"}}
    env = dict(agent.machine.env, CLAUDE_PROJECT_DIR=str(agent.folder))
    out = subprocess.run(shlex.split(command), input=json.dumps(payload), cwd=str(agent.folder), env=env,
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise AssertionError("tool hook failed (%d): %s" % (out.returncode, out.stderr))


def forget_alive(agent):
    """As if the session's last tool call was over a minute ago: the next one tells the server."""
    path = state_file(agent)
    state = json.loads(path.read_text())
    state.update(alive=0, doing="", doing_at=0)
    path.write_text(json.dumps(state))


def rss_mb(pid):
    """A process's resident memory in MB (ps: macOS and Linux), or 0 where that cannot be read."""
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
        return int(out.stdout.strip()) / 1024
    except (OSError, ValueError):
        return 0


def grow_log(machine, mb):
    """Put older messages in front of the chat's own until messages.jsonl is about mb MB: copies of its
    lines with ids of their own and numbers below the chat's (so the chat itself is unchanged)."""
    log = machine.home / "messages.jsonl"
    lines = [l for l in log.read_bytes().split(b"\n") if l.strip()]
    want, size, filler = mb * 1024 * 1024 - log.stat().st_size, 0, []
    while size < want:
        msg = json.loads(lines[len(filler) % len(lines)])
        filler.append(msg)
        size += len(lines[len(filler) % len(lines)]) + 12
    grown = log.with_name("messages.grown")
    with open(grown, "wb") as f:
        for i, msg in enumerate(filler):
            msg.update(id="f%d" % i, seq=i - len(filler))
            f.write(json.dumps(msg, ensure_ascii=False).encode() + b"\n")
        f.write(log.read_bytes())
    grown.replace(log)
    return log.stat().st_size


def startup(machine, runs, memory=None):
    """Seconds from starting `crewchat serve` to its /health answering, with the chat as it is (and, in
    memory, the server's resident MB then)."""
    machine.kill(hard=False)
    out = []
    for _ in range(runs):
        start = time.perf_counter()
        proc = subprocess.Popen(H.crewchat_command() + ["serve", "--port", str(machine.port), "--log",
                                                         str(machine.dir / "bench-serve.log")],
                                cwd=str(machine.folder), env=machine.env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        try:
            while not machine.up():
                if proc.poll() is not None:
                    raise AssertionError("the server stopped while starting; see %s" % (machine.dir / "bench-serve.log"))
                time.sleep(0.005)
            out.append(time.perf_counter() - start)
            if memory is not None:
                memory.append(rss_mb(proc.pid))
        finally:
            proc.terminate()
            proc.wait(20)
        H.eventually(lambda: not machine.up(), what="the server to stop")
    machine.restart()
    return out


def state_writes(agents=5, rounds=20):
    """state.json writes for a fixed busy load, run in this process against a Hub of a throwaway project:
    each round every agent sends a message, reads its inbox and (every fifth round) sets its status, and
    a task is added, taken and done. Counted until what may wait (SAVE_DELAY) is written too."""
    sys.path.insert(0, str(H.ROOT))
    import crewchat
    root = H.Path(H.tempfile.mkdtemp(prefix="state-writes-", dir=H.TMP))
    crewchat.save_config({"project": "writes", "port": 9}, root)
    hub = crewchat.Hub(crewchat.Roster(root))
    sids = [hub.open_session("bench", "claude-code") for _ in range(agents)]
    for sid in sids:
        crewchat.call_tool(hub, sid, "hub_agents", {})
    time.sleep(getattr(crewchat, "SAVE_DELAY", 0) + 0.2)
    count, real = [0], crewchat.write_json

    def counting(path, *a, **kw):
        count[0] += str(path).endswith("state.json")
        return real(path, *a, **kw)
    crewchat.write_json = counting
    try:
        task = "hub_task" if "hub_task" in crewchat.HUB_TOOL_NAMES else None
        for r in range(rounds):
            for i, sid in enumerate(sids):
                crewchat.call_tool(hub, sid, "hub_send", {"to": "all", "text": "round %d from %d" % (r, i)})
                crewchat.call_tool(hub, sid, "hub_inbox", {})
                if r % 5 == 0:
                    crewchat.call_tool(hub, sid, "hub_status", {"text": "round %d" % r})
            sid = sids[r % agents]
            if task:
                tid = crewchat.call_tool(hub, None, task, {"action": "add", "title": "t%d" % r}, owner=True).split("#")[1].split()[0]
                crewchat.call_tool(hub, sid, task, {"action": "take", "id": tid})
                crewchat.call_tool(hub, sid, task, {"action": "update", "id": tid, "status": "done", "note": "ok"})
            else:  # before P2: the task tools of their own
                tid = crewchat.call_tool(hub, None, "hub_task_add", {"title": "t%d" % r}, owner=True).split("#")[1].split()[0]
                crewchat.call_tool(hub, sid, "hub_take", {"id": tid})
                crewchat.call_tool(hub, sid, "hub_update", {"id": tid, "status": "done", "note": "ok"})
        time.sleep(getattr(crewchat, "SAVE_DELAY", 0) + 0.5)
    finally:
        crewchat.write_json = real
    return count[0]


def measure(agents=5, messages=5000, runs=9, say=print, log_mb=0):
    m = H.Machine("bench")
    m.start(client="claude")
    crew = [m.agent() for _ in range(agents)]
    for a in crew:
        a.link()
    say("Sending %d messages from %d agents..." % (messages, agents))
    for i in range(messages):
        a = crew[i % agents]
        a.call("hub_send", to="all" if i % 3 or agents == 1 else crew[(i + 1) % agents].name,
               text="Message %d: working on the login screen, see src/login/LoginViewModel.kt line %d" % (i, i))
    log = m.home / "messages.jsonl"
    numbers = {"messages.jsonl bytes": log.stat().st_size}

    a = crew[0]
    command = tool_hook_command(a)
    run_tool_hook(a, command)  # its state file exists from here on
    numbers["hook: tool call, tells the server (s)"] = timed(lambda: run_tool_hook(a, command), runs,
                                                             before=lambda: forget_alive(a))
    numbers["hook: tool call, skipped (s)"] = timed(lambda: run_tool_hook(a, command), runs)
    for _ in range(100):  # hand the agent what it has not seen yet, so the hooks below have nothing to deliver
        if not a.hook("stop", listen=0).strip():
            break
    numbers["hook: prompt (s)"] = timed(lambda: a.hook("prompt"), runs)
    numbers["hook: end of turn, nothing new (s)"] = timed(lambda: a.hook("stop", listen=0), runs)

    owner = m.owner
    sizes = []

    def poll(after):
        status, raw, _ = H.request(m.url + "/api/poll?after=%d&v=-1&wait=0" % after, opener=owner.opener)
        if status != 200:
            raise AssertionError("poll failed: HTTP %d" % status)
        sizes.append(len(raw))
    numbers["poll: open the chat (s)"] = timed(lambda: poll(-1), runs)
    numbers["poll: open the chat (bytes)"] = sizes[-1]
    last = owner.poll()["messages"][-1]["seq"]
    a.call("hub_send", to="all", text="one more")
    numbers["poll: one new message (s)"] = timed(lambda: poll(last), runs)
    numbers["poll: one new message (bytes)"] = sizes[-1]

    # The chat page as a browser fetches it: the first time, and a reload with what it kept (headers and body).
    status, body, headers = H.request(m.url + "/", headers={"Accept-Encoding": "gzip, deflate, br"}, opener=owner.opener)
    numbers["page: first load (bytes)"] = len(body) + len(str(headers))
    asked = {"Accept-Encoding": "gzip, deflate, br"}
    if headers.get("ETag"):
        asked["If-None-Match"] = headers["ETag"]
    status, body, headers = H.request(m.url + "/", headers=asked, opener=owner.opener)
    numbers["page: reload (bytes)"] = len(body) + len(str(headers))

    # What every agent loads, and what reading a full inbox costs it, in characters (the model's tokens).
    numbers["tools/list (chars)"] = len(json.dumps(a._rpc("tools/list", {})["tools"]))
    reader = m.agent()
    reader.link()
    for i in range(50):
        a.call("hub_send", to="all", text="Message %d: the login screen is done, tests pass, see LoginViewModel.kt" % i)
    numbers["hub_inbox, 50 unread (chars)"] = len(reader.call("hub_inbox"))

    numbers["state.json writes, a busy load (count)"] = state_writes(agents)

    memory = []
    numbers["server startup (s)"] = startup(m, runs, memory)
    if any(memory):
        numbers["server memory after startup (MB)"] = statistics.median(memory)
    if log_mb:
        say("Growing messages.jsonl to %d MB..." % log_mb)
        numbers["messages.jsonl bytes, grown"] = grow_log(m, log_mb)
        memory = []
        numbers["server startup, %d MB log (s)" % log_mb] = startup(m, runs, memory)
        if any(memory):
            numbers["server memory, %d MB log (MB)" % log_mb] = statistics.median(memory)
    m.stop()
    return numbers


def medians(numbers):
    return {k: statistics.median(v) if isinstance(v, list) else v for k, v in numbers.items()}


def table(numbers):
    rows = []
    for k, v in numbers.items():
        if isinstance(v, list):
            rows.append((k, "%.1f ms" % (statistics.median(v) * 1000),
                         "%.1f-%.1f ms" % (min(v) * 1000, max(v) * 1000), str(len(v))))
        else:
            rows.append((k, "{:,}".format(round(v, 1)) if isinstance(v, float) else "{:,}".format(v), "", ""))
    head = ("measure", "median", "range", "runs")
    width = [max(len(r[i]) for r in rows + [head]) for i in range(4)]
    line = lambda r: "  ".join(c.ljust(width[i]) if i == 0 else c.rjust(width[i]) for i, c in enumerate(r))  # noqa: E731
    return "\n".join([line(head), line(tuple("-" * w for w in width))] + [line(r) for r in rows])


def regressions(base, now, limit=LIMIT):
    """What got more than limit worse from base to now (both as medians): [(measure, before, after)]."""
    return [(k, base[k], now[k]) for k in now if k in base and base[k] and now[k] > base[k] * (1 + limit)]


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--agents", type=int, default=5)
    p.add_argument("--messages", type=int, default=5000)
    p.add_argument("--runs", type=int, default=9, help="runs of each timing; the median is what counts")
    p.add_argument("--log-mb", type=int, default=0, metavar="MB", help="also time startup with messages.jsonl grown "
                   "to about MB megabytes (older messages put in front of the chat's)")
    p.add_argument("--json", metavar="FILE", help="also write the medians to FILE")
    p.add_argument("--compare", metavar="FILE", help="exit 1 if a median is over 20%% worse than FILE's")
    args = p.parse_args(argv)
    numbers = measure(args.agents, args.messages, args.runs, log_mb=args.log_mb)
    print()
    print("Python %s, crewchat run as: %s" % (sys.version.split()[0], " ".join(H.crewchat_command())))
    print(table(numbers))
    now = medians(numbers)
    if args.json:
        with open(args.json, "w") as f:
            json.dump(now, f, indent=2)
    if args.compare:
        with open(args.compare) as f:
            worse = regressions(json.load(f), now)
        for k, before, after in worse:
            print("Worse: %s, %.4g -> %.4g" % (k, before, after))
        return 1 if worse else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
