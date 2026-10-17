"""The benchmark (roadmap P0): a real server in a throwaway home, 5 agents and 5,000 messages, and the
numbers every speed change is measured by. Standard library only.

    python3 e2e/bench.py                       the table, medians of 9 runs
    python3 e2e/bench.py --json now.json       ... and the numbers, to compare against later
    python3 e2e/bench.py --compare base.json   exit 1 if a median got more than 20% worse

It measures, as the agents and the page see them:
- hook calls end to end, run exactly as Claude Code runs them (a process each): a tool call that tells
  the server, one the hook skips (it told the server a moment ago), a prompt, and the end of a turn;
- one /api/poll round trip of the chat page, opening the chat and catching up on one new message,
  and how big each answer is;
- server startup with that messages.jsonl, until /health answers.
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


def startup(machine, runs):
    """Seconds from starting `crewchat serve` to its /health answering, with the chat as it is."""
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
        finally:
            proc.terminate()
            proc.wait(20)
        H.eventually(lambda: not machine.up(), what="the server to stop")
    machine.restart()
    return out


def measure(agents=5, messages=5000, runs=9, say=print):
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

    numbers["server startup (s)"] = startup(m, runs)
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
            rows.append((k, "{:,}".format(v), "", ""))
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
    p.add_argument("--json", metavar="FILE", help="also write the medians to FILE")
    p.add_argument("--compare", metavar="FILE", help="exit 1 if a median is over 20%% worse than FILE's")
    args = p.parse_args(argv)
    numbers = measure(args.agents, args.messages, args.runs)
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
